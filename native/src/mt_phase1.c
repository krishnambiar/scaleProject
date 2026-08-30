#include "mt_phase1.h"

#include <dlfcn.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#if !defined(__APPLE__) || !defined(__aarch64__)
#error "This Phase 1 ABI profile has only been verified for arm64 macOS."
#endif

_Static_assert(sizeof(void *) == 8, "The verified ABI profile requires 64-bit pointers.");
_Static_assert(sizeof(uintptr_t) == 8, "The raw register container must be 64-bit.");

#define MT_FRAMEWORK_PATH \
    "/System/Library/PrivateFrameworks/MultitouchSupport.framework/MultitouchSupport"
#define MT_PHASE1_QUEUE_CAPACITY 4096u
#define MT_PHASE1_CONTEXT_CAPACITY 4096u
#define MT_PHASE1_QUIESCENCE_POLLS 5000u
#define MT_PHASE1_QUIESCENCE_POLL_NS 1000000L

typedef void *mt_device_ref_t;

/*
 * Verified from this target's arm64 disassembly at the machine-ABI level:
 * x0=device, x1=opaque records, x2=count register, d0=timestamp,
 * x3=frame register, x4=refcon. uintptr_t is deliberately a raw register
 * container; it is not a claim about Apple's undocumented source typedef.
 */
typedef void (*mt_raw_frame_callback_t)(
    mt_device_ref_t device,
    const void *touch_records_opaque,
    uintptr_t raw_touch_count_register,
    double device_timestamp,
    uintptr_t raw_frame_register,
    void *refcon
);

typedef mt_device_ref_t (*mt_device_create_default_fn)(void);
typedef void (*mt_device_release_fn)(mt_device_ref_t);
typedef int32_t (*mt_register_callback_with_refcon_fn)(
    mt_device_ref_t,
    mt_raw_frame_callback_t,
    void *
);
typedef int32_t (*mt_unregister_callback_fn)(
    mt_device_ref_t,
    mt_raw_frame_callback_t
);
typedef int32_t (*mt_device_start_fn)(mt_device_ref_t, uint32_t);
typedef int32_t (*mt_device_stop_fn)(mt_device_ref_t);
typedef uint32_t (*mt_device_capability_fn)(mt_device_ref_t);

typedef struct mt_private_api {
    mt_device_create_default_fn create_default;
    mt_device_release_fn release;
    mt_register_callback_with_refcon_fn register_with_refcon;
    mt_unregister_callback_fn unregister_callback;
    mt_device_start_fn start;
    mt_device_stop_fn stop;
    mt_device_capability_fn supports_force;
    mt_device_capability_fn is_built_in;
} mt_private_api_t;

struct mt_phase1_capture {
    void *framework_handle;
    mt_private_api_t api;
    mt_device_ref_t device;
    pthread_mutex_t queue_mutex;
    mt_phase1_frame_metadata_t queue[MT_PHASE1_QUEUE_CAPACITY];
    size_t queue_head;
    size_t queue_tail;
    size_t queue_depth;
    atomic_bool accepting_callbacks;
    atomic_uint_fast64_t callback_count;
    atomic_uint_fast64_t enqueued_count;
    atomic_uint_fast64_t queue_overwrite_count;
    atomic_uint_fast64_t lock_contention_drop_count;
    atomic_uint_fast64_t callback_device_mismatch_count;
    atomic_uint_fast64_t late_callback_count;
    atomic_uint_fast64_t in_flight_callback_count;
    uint64_t rejected_callback_baseline;
    const void *callback_context;
    bool registered;
    bool running;
    int32_t supports_force;
    int32_t is_built_in;
};

static pthread_mutex_t g_active_capture_mutex = PTHREAD_MUTEX_INITIALIZER;
static mt_phase1_capture_t *g_active_capture = NULL;
/*
 * This admission gate and refcon live for the process lifetime. A callback
 * never dereferences its refcon or a capture until it has been admitted under
 * this gate, so capture destruction cannot race the first object access.
 */
static pthread_mutex_t g_callback_gate_mutex = PTHREAD_MUTEX_INITIALIZER;
static mt_phase1_capture_t *g_callback_capture = NULL;
static const void *g_callback_context = NULL;
static unsigned char g_callback_context_pool[MT_PHASE1_CONTEXT_CAPACITY];
static size_t g_next_callback_context = 0;
static atomic_uint_fast64_t g_rejected_callback_count = ATOMIC_VAR_INIT(0);

#if defined(MT_PHASE1_TESTING)
static void (*g_test_before_callback_admission)(void) = NULL;
static void (*g_test_after_callback_admission)(void) = NULL;
#endif

static void write_error(char *buffer, size_t buffer_size, const char *format, ...) {
    if (buffer == NULL || buffer_size == 0) {
        return;
    }

    va_list arguments;
    va_start(arguments, format);
    (void)vsnprintf(buffer, buffer_size, format, arguments);
    va_end(arguments);
}

static void clear_error(char *buffer, size_t buffer_size) {
    if (buffer != NULL && buffer_size > 0) {
        buffer[0] = '\0';
    }
}

static bool resolve_required_symbol(
    void *handle,
    const char *name,
    void *out_function,
    size_t function_size,
    char *error_buffer,
    size_t error_buffer_size
) {
    (void)dlerror();
    void *symbol = dlsym(handle, name);
    const char *error = dlerror();
    if (error != NULL || symbol == NULL) {
        write_error(
            error_buffer,
            error_buffer_size,
            "required private symbol %s was not resolved: %s",
            name,
            error != NULL ? error : "unknown dlsym failure"
        );
        return false;
    }
    if (function_size != sizeof(symbol)) {
        write_error(
            error_buffer,
            error_buffer_size,
            "function-pointer size mismatch while resolving %s",
            name
        );
        return false;
    }
    memcpy(out_function, &symbol, sizeof(symbol));
    return true;
}

static bool resolve_optional_symbol(
    void *handle,
    const char *name,
    void *out_function,
    size_t function_size
) {
    (void)dlerror();
    void *symbol = dlsym(handle, name);
    if (dlerror() != NULL || symbol == NULL || function_size != sizeof(symbol)) {
        memset(out_function, 0, function_size);
        return false;
    }
    memcpy(out_function, &symbol, sizeof(symbol));
    return true;
}

static uint64_t monotonic_time_ns(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return ((uint64_t)now.tv_sec * UINT64_C(1000000000)) + (uint64_t)now.tv_nsec;
}

static void finish_callback(mt_phase1_capture_t *capture) {
    (void)atomic_fetch_sub_explicit(
        &capture->in_flight_callback_count,
        1,
        memory_order_release
    );
}

static bool wait_for_callbacks_to_quiesce(mt_phase1_capture_t *capture) {
    const struct timespec pause = {
        .tv_sec = 0,
        .tv_nsec = MT_PHASE1_QUIESCENCE_POLL_NS,
    };
    for (uint32_t attempt = 0; attempt < MT_PHASE1_QUIESCENCE_POLLS; ++attempt) {
        if (atomic_load_explicit(
                &capture->in_flight_callback_count,
                memory_order_acquire
            ) == 0) {
            return true;
        }
        (void)nanosleep(&pause, NULL);
    }
    return false;
}

static void reject_callback_without_capture(void) {
    (void)atomic_fetch_add_explicit(
        &g_rejected_callback_count,
        1,
        memory_order_relaxed
    );
}

static bool attach_callback_capture(mt_phase1_capture_t *capture) {
    (void)pthread_mutex_lock(&g_callback_gate_mutex);
    if (g_callback_capture != NULL || g_callback_context != NULL ||
        capture->callback_context == NULL) {
        (void)pthread_mutex_unlock(&g_callback_gate_mutex);
        return false;
    }
    atomic_store_explicit(&capture->accepting_callbacks, true, memory_order_release);
    g_callback_context = capture->callback_context;
    g_callback_capture = capture;
    (void)pthread_mutex_unlock(&g_callback_gate_mutex);
    return true;
}

static void detach_callback_capture(mt_phase1_capture_t *capture) {
    (void)pthread_mutex_lock(&g_callback_gate_mutex);
    atomic_store_explicit(&capture->accepting_callbacks, false, memory_order_release);
    if (g_callback_capture == capture) {
        g_callback_capture = NULL;
        g_callback_context = NULL;
    }
    (void)pthread_mutex_unlock(&g_callback_gate_mutex);
}

static void mt_phase1_raw_callback(
    mt_device_ref_t device,
    const void *touch_records_opaque,
    uintptr_t raw_touch_count_register,
    double device_timestamp,
    uintptr_t raw_frame_register,
    void *refcon
) {
    (void)touch_records_opaque;

#if defined(MT_PHASE1_TESTING)
    if (g_test_before_callback_admission != NULL) {
        g_test_before_callback_admission();
    }
#endif

    if (pthread_mutex_trylock(&g_callback_gate_mutex) != 0) {
        reject_callback_without_capture();
        return;
    }

    mt_phase1_capture_t *capture = g_callback_capture;
    if (capture == NULL || refcon != g_callback_context) {
        (void)pthread_mutex_unlock(&g_callback_gate_mutex);
        reject_callback_without_capture();
        return;
    }
    (void)atomic_fetch_add_explicit(
        &capture->in_flight_callback_count,
        1,
        memory_order_acquire
    );
    (void)pthread_mutex_unlock(&g_callback_gate_mutex);
#if defined(MT_PHASE1_TESTING)
    if (g_test_after_callback_admission != NULL) {
        g_test_after_callback_admission();
    }
#endif
    if (!atomic_load_explicit(
            &capture->accepting_callbacks,
            memory_order_acquire
        )) {
        (void)atomic_fetch_add_explicit(
            &capture->late_callback_count,
            1,
            memory_order_relaxed
        );
        finish_callback(capture);
        return;
    }

    uint64_t sequence = atomic_fetch_add_explicit(
        &capture->callback_count,
        1,
        memory_order_relaxed
    ) + 1;

    if (device != capture->device) {
        (void)atomic_fetch_add_explicit(
            &capture->callback_device_mismatch_count,
            1,
            memory_order_relaxed
        );
    }

    mt_phase1_frame_metadata_t frame = {
        .sequence = sequence,
        .raw_touch_count_register = (uint64_t)raw_touch_count_register,
        .raw_frame_register = (uint64_t)raw_frame_register,
        .device_timestamp = device_timestamp,
        .host_monotonic_ns = monotonic_time_ns(),
    };

    if (pthread_mutex_trylock(&capture->queue_mutex) != 0) {
        (void)atomic_fetch_add_explicit(
            &capture->lock_contention_drop_count,
            1,
            memory_order_relaxed
        );
        finish_callback(capture);
        return;
    }

    if (capture->queue_depth == MT_PHASE1_QUEUE_CAPACITY) {
        capture->queue_tail = (capture->queue_tail + 1u) % MT_PHASE1_QUEUE_CAPACITY;
        capture->queue_depth -= 1u;
        (void)atomic_fetch_add_explicit(
            &capture->queue_overwrite_count,
            1,
            memory_order_relaxed
        );
    }

    capture->queue[capture->queue_head] = frame;
    capture->queue_head = (capture->queue_head + 1u) % MT_PHASE1_QUEUE_CAPACITY;
    capture->queue_depth += 1u;
    (void)atomic_fetch_add_explicit(
        &capture->enqueued_count,
        1,
        memory_order_relaxed
    );

    (void)pthread_mutex_unlock(&capture->queue_mutex);
    finish_callback(capture);
}

static void release_capture_resources(mt_phase1_capture_t *capture) {
    if (capture == NULL) {
        return;
    }
    if (capture->device != NULL && capture->api.release != NULL) {
        capture->api.release(capture->device);
        capture->device = NULL;
    }
    if (capture->framework_handle != NULL) {
        (void)dlclose(capture->framework_handle);
        capture->framework_handle = NULL;
    }
}

uint32_t mt_phase1_bridge_abi_version(void) {
    return MT_PHASE1_BRIDGE_ABI_VERSION;
}

size_t mt_phase1_frame_metadata_size(void) {
    return sizeof(mt_phase1_frame_metadata_t);
}

size_t mt_phase1_capture_stats_size(void) {
    return sizeof(mt_phase1_capture_stats_t);
}

const char *mt_phase1_framework_path(void) {
    return MT_FRAMEWORK_PATH;
}

int32_t mt_phase1_capture_create(
    mt_phase1_capture_t **out_capture,
    char *error_buffer,
    size_t error_buffer_size
) {
    clear_error(error_buffer, error_buffer_size);
    if (out_capture == NULL) {
        write_error(error_buffer, error_buffer_size, "out_capture is NULL");
        return MT_PHASE1_ERROR_INVALID_ARGUMENT;
    }
    *out_capture = NULL;

    mt_phase1_capture_t *capture = calloc(1, sizeof(*capture));
    if (capture == NULL) {
        write_error(error_buffer, error_buffer_size, "capture allocation failed");
        return MT_PHASE1_ERROR_ALLOCATION;
    }
    capture->supports_force = -1;
    capture->is_built_in = -1;
    atomic_init(&capture->accepting_callbacks, false);
    atomic_init(&capture->callback_count, 0);
    atomic_init(&capture->enqueued_count, 0);
    atomic_init(&capture->queue_overwrite_count, 0);
    atomic_init(&capture->lock_contention_drop_count, 0);
    atomic_init(&capture->callback_device_mismatch_count, 0);
    atomic_init(&capture->late_callback_count, 0);
    atomic_init(&capture->in_flight_callback_count, 0);

    if (!atomic_is_lock_free(&capture->accepting_callbacks) ||
        !atomic_is_lock_free(&capture->callback_count) ||
        !atomic_is_lock_free(&capture->enqueued_count) ||
        !atomic_is_lock_free(&capture->queue_overwrite_count) ||
        !atomic_is_lock_free(&capture->lock_contention_drop_count) ||
        !atomic_is_lock_free(&capture->callback_device_mismatch_count) ||
        !atomic_is_lock_free(&capture->late_callback_count) ||
        !atomic_is_lock_free(&capture->in_flight_callback_count) ||
        !atomic_is_lock_free(&g_rejected_callback_count)) {
        write_error(
            error_buffer,
            error_buffer_size,
            "callback-path atomics are not lock-free on this target"
        );
        free(capture);
        return MT_PHASE1_ERROR_INTERNAL;
    }

    if (pthread_mutex_init(&capture->queue_mutex, NULL) != 0) {
        write_error(error_buffer, error_buffer_size, "queue mutex initialization failed");
        free(capture);
        return MT_PHASE1_ERROR_INTERNAL;
    }

    capture->framework_handle = dlopen(MT_FRAMEWORK_PATH, RTLD_NOW | RTLD_LOCAL);
    if (capture->framework_handle == NULL) {
        const char *error = dlerror();
        write_error(
            error_buffer,
            error_buffer_size,
            "framework load failed: %s",
            error != NULL ? error : "unknown dlopen failure"
        );
        (void)pthread_mutex_destroy(&capture->queue_mutex);
        free(capture);
        return MT_PHASE1_ERROR_FRAMEWORK_LOAD;
    }

#define RESOLVE_REQUIRED(field, symbol_name) \
    do { \
        if (!resolve_required_symbol( \
                capture->framework_handle, \
                symbol_name, \
                &capture->api.field, \
                sizeof(capture->api.field), \
                error_buffer, \
                error_buffer_size \
            )) { \
            release_capture_resources(capture); \
            (void)pthread_mutex_destroy(&capture->queue_mutex); \
            free(capture); \
            return MT_PHASE1_ERROR_SYMBOL_RESOLUTION; \
        } \
    } while (0)

    RESOLVE_REQUIRED(create_default, "MTDeviceCreateDefault");
    RESOLVE_REQUIRED(release, "MTDeviceRelease");
    RESOLVE_REQUIRED(register_with_refcon, "MTRegisterContactFrameCallbackWithRefcon");
    RESOLVE_REQUIRED(unregister_callback, "MTUnregisterContactFrameCallback");
    RESOLVE_REQUIRED(start, "MTDeviceStart");
    RESOLVE_REQUIRED(stop, "MTDeviceStop");

#undef RESOLVE_REQUIRED

    (void)resolve_optional_symbol(
        capture->framework_handle,
        "MTDeviceSupportsForce",
        &capture->api.supports_force,
        sizeof(capture->api.supports_force)
    );
    (void)resolve_optional_symbol(
        capture->framework_handle,
        "MTDeviceIsBuiltIn",
        &capture->api.is_built_in,
        sizeof(capture->api.is_built_in)
    );

    capture->device = capture->api.create_default();
    if (capture->device == NULL) {
        write_error(error_buffer, error_buffer_size, "MTDeviceCreateDefault returned NULL");
        release_capture_resources(capture);
        (void)pthread_mutex_destroy(&capture->queue_mutex);
        free(capture);
        return MT_PHASE1_ERROR_NO_DEVICE;
    }

    if (capture->api.supports_force != NULL) {
        capture->supports_force = capture->api.supports_force(capture->device) != 0 ? 1 : 0;
    }
    if (capture->api.is_built_in != NULL) {
        capture->is_built_in = capture->api.is_built_in(capture->device) != 0 ? 1 : 0;
    }

    *out_capture = capture;
    return MT_PHASE1_OK;
}

int32_t mt_phase1_capture_start(
    mt_phase1_capture_t *capture,
    uint32_t start_options,
    int32_t *out_native_status,
    char *error_buffer,
    size_t error_buffer_size
) {
    clear_error(error_buffer, error_buffer_size);
    if (out_native_status != NULL) {
        *out_native_status = 0;
    }
    if (capture == NULL) {
        write_error(error_buffer, error_buffer_size, "capture is NULL");
        return MT_PHASE1_ERROR_INVALID_ARGUMENT;
    }
    if (capture->running || capture->registered) {
        write_error(error_buffer, error_buffer_size, "capture is already running");
        return MT_PHASE1_ERROR_ALREADY_ACTIVE;
    }

    (void)pthread_mutex_lock(&g_active_capture_mutex);
    if (g_active_capture != NULL) {
        (void)pthread_mutex_unlock(&g_active_capture_mutex);
        write_error(
            error_buffer,
            error_buffer_size,
            "a Phase 1 capture is active or has unresolved callback cleanup"
        );
        return MT_PHASE1_ERROR_ALREADY_ACTIVE;
    }

    (void)pthread_mutex_lock(&capture->queue_mutex);
    capture->queue_head = 0;
    capture->queue_tail = 0;
    capture->queue_depth = 0;
    (void)pthread_mutex_unlock(&capture->queue_mutex);
    atomic_store_explicit(&capture->callback_count, 0, memory_order_relaxed);
    atomic_store_explicit(&capture->enqueued_count, 0, memory_order_relaxed);
    atomic_store_explicit(&capture->queue_overwrite_count, 0, memory_order_relaxed);
    atomic_store_explicit(&capture->lock_contention_drop_count, 0, memory_order_relaxed);
    atomic_store_explicit(
        &capture->callback_device_mismatch_count,
        0,
        memory_order_relaxed
    );
    atomic_store_explicit(&capture->late_callback_count, 0, memory_order_relaxed);
    atomic_store_explicit(
        &capture->in_flight_callback_count,
        0,
        memory_order_relaxed
    );

    if (g_next_callback_context == MT_PHASE1_CONTEXT_CAPACITY) {
        (void)pthread_mutex_unlock(&g_active_capture_mutex);
        write_error(
            error_buffer,
            error_buffer_size,
            "callback context pool exhausted; restart the diagnostic process"
        );
        return MT_PHASE1_ERROR_INTERNAL;
    }
    capture->callback_context =
        (const void *)&g_callback_context_pool[g_next_callback_context++];
    capture->rejected_callback_baseline = atomic_load_explicit(
        &g_rejected_callback_count,
        memory_order_relaxed
    );

    int32_t registered = capture->api.register_with_refcon(
        capture->device,
        mt_phase1_raw_callback,
        (void *)capture->callback_context
    );
    if (registered == 0) {
        (void)pthread_mutex_unlock(&g_active_capture_mutex);
        write_error(
            error_buffer,
            error_buffer_size,
            "MTRegisterContactFrameCallbackWithRefcon rejected the callback"
        );
        return MT_PHASE1_ERROR_CALLBACK_REGISTRATION;
    }

    capture->registered = true;
    g_active_capture = capture;
    if (!attach_callback_capture(capture)) {
        int32_t unregistered = capture->api.unregister_callback(
            capture->device,
            mt_phase1_raw_callback
        );
        if (unregistered != 0) {
            capture->registered = false;
            g_active_capture = NULL;
        }
        (void)pthread_mutex_unlock(&g_active_capture_mutex);
        write_error(
            error_buffer,
            error_buffer_size,
            "the immortal callback-admission gate was unexpectedly occupied"
        );
        return MT_PHASE1_ERROR_INTERNAL;
    }

    int32_t native_status = capture->api.start(capture->device, start_options);
    if (out_native_status != NULL) {
        *out_native_status = native_status;
    }
    if (native_status != 0) {
        detach_callback_capture(capture);
        int32_t unregistered = capture->api.unregister_callback(
            capture->device,
            mt_phase1_raw_callback
        );
        if (unregistered != 0) {
            capture->registered = false;
        }
        bool callbacks_quiesced = wait_for_callbacks_to_quiesce(capture);
        if (!capture->registered && callbacks_quiesced) {
            g_active_capture = NULL;
        }
        (void)pthread_mutex_unlock(&g_active_capture_mutex);
        write_error(
            error_buffer,
            error_buffer_size,
            "MTDeviceStart returned native status 0x%08x for options 0x%08x; "
            "callback unregister=%d, quiesced=%s",
            (uint32_t)native_status,
            start_options,
            unregistered,
            callbacks_quiesced ? "yes" : "no"
        );
        return MT_PHASE1_ERROR_DEVICE_START;
    }

    capture->running = true;
    (void)pthread_mutex_unlock(&g_active_capture_mutex);
    return MT_PHASE1_OK;
}

int32_t mt_phase1_capture_stop(
    mt_phase1_capture_t *capture,
    int32_t *out_native_status,
    char *error_buffer,
    size_t error_buffer_size
) {
    clear_error(error_buffer, error_buffer_size);
    if (out_native_status != NULL) {
        *out_native_status = 0;
    }
    if (capture == NULL) {
        write_error(error_buffer, error_buffer_size, "capture is NULL");
        return MT_PHASE1_ERROR_INVALID_ARGUMENT;
    }
    (void)pthread_mutex_lock(&g_active_capture_mutex);
    detach_callback_capture(capture);

    if (!capture->running && !capture->registered) {
        bool callbacks_quiesced = wait_for_callbacks_to_quiesce(capture);
        if (g_active_capture == capture && callbacks_quiesced) {
            g_active_capture = NULL;
        }
        (void)pthread_mutex_unlock(&g_active_capture_mutex);
        if (!callbacks_quiesced) {
            write_error(
                error_buffer,
                error_buffer_size,
                "callbacks did not quiesce within the bounded shutdown wait"
            );
            return MT_PHASE1_ERROR_CALLBACK_QUIESCENCE;
        }
        return MT_PHASE1_OK;
    }

    int32_t native_status = 0;
    if (capture->running) {
        native_status = capture->api.stop(capture->device);
        if (native_status == 0) {
            capture->running = false;
        }
    }
    if (out_native_status != NULL) {
        *out_native_status = native_status;
    }

    int32_t unregistered = 1;
    if (capture->registered) {
        unregistered = capture->api.unregister_callback(
            capture->device,
            mt_phase1_raw_callback
        );
        if (unregistered != 0) {
            capture->registered = false;
        }
    }

    bool callbacks_quiesced = wait_for_callbacks_to_quiesce(capture);
    if (g_active_capture == capture &&
        !capture->running &&
        !capture->registered &&
        callbacks_quiesced) {
        g_active_capture = NULL;
    }
    (void)pthread_mutex_unlock(&g_active_capture_mutex);

    if (native_status != 0) {
        write_error(
            error_buffer,
            error_buffer_size,
            "MTDeviceStop returned native status 0x%08x",
            (uint32_t)native_status
        );
        return MT_PHASE1_ERROR_DEVICE_STOP;
    }
    if (unregistered == 0) {
        write_error(
            error_buffer,
            error_buffer_size,
            "MTUnregisterContactFrameCallback did not find the registered callback"
        );
        return MT_PHASE1_ERROR_CALLBACK_UNREGISTER;
    }
    if (!callbacks_quiesced) {
        write_error(
            error_buffer,
            error_buffer_size,
            "callbacks did not quiesce within the bounded shutdown wait"
        );
        return MT_PHASE1_ERROR_CALLBACK_QUIESCENCE;
    }
    return MT_PHASE1_OK;
}

int32_t mt_phase1_capture_destroy(
    mt_phase1_capture_t *capture,
    char *error_buffer,
    size_t error_buffer_size
) {
    clear_error(error_buffer, error_buffer_size);
    if (capture == NULL) {
        return MT_PHASE1_OK;
    }
    int32_t status = mt_phase1_capture_stop(
        capture,
        NULL,
        error_buffer,
        error_buffer_size
    );
    if (status != MT_PHASE1_OK || capture->running || capture->registered ||
        atomic_load_explicit(
            &capture->in_flight_callback_count,
            memory_order_acquire
        ) != 0) {
        if (status == MT_PHASE1_OK) {
            write_error(
                error_buffer,
                error_buffer_size,
                "refusing to free capture while callback cleanup remains uncertain"
            );
            status = MT_PHASE1_ERROR_CALLBACK_QUIESCENCE;
        }
        return status;
    }
    release_capture_resources(capture);
    (void)pthread_mutex_destroy(&capture->queue_mutex);
    free(capture);
    return MT_PHASE1_OK;
}

int32_t mt_phase1_capture_poll(
    mt_phase1_capture_t *capture,
    mt_phase1_frame_metadata_t *out_frame
) {
    if (capture == NULL || out_frame == NULL) {
        return MT_PHASE1_ERROR_INVALID_ARGUMENT;
    }

    (void)pthread_mutex_lock(&capture->queue_mutex);
    if (capture->queue_depth == 0) {
        (void)pthread_mutex_unlock(&capture->queue_mutex);
        return 0;
    }

    *out_frame = capture->queue[capture->queue_tail];
    capture->queue_tail = (capture->queue_tail + 1u) % MT_PHASE1_QUEUE_CAPACITY;
    capture->queue_depth -= 1u;
    (void)pthread_mutex_unlock(&capture->queue_mutex);
    return 1;
}

int32_t mt_phase1_capture_get_stats(
    mt_phase1_capture_t *capture,
    mt_phase1_capture_stats_t *out_stats
) {
    if (capture == NULL || out_stats == NULL) {
        return MT_PHASE1_ERROR_INVALID_ARGUMENT;
    }

    out_stats->callback_count = atomic_load_explicit(
        &capture->callback_count,
        memory_order_relaxed
    );
    out_stats->enqueued_count = atomic_load_explicit(
        &capture->enqueued_count,
        memory_order_relaxed
    );
    out_stats->queue_overwrite_count = atomic_load_explicit(
        &capture->queue_overwrite_count,
        memory_order_relaxed
    );
    out_stats->lock_contention_drop_count = atomic_load_explicit(
        &capture->lock_contention_drop_count,
        memory_order_relaxed
    );
    out_stats->callback_device_mismatch_count = atomic_load_explicit(
        &capture->callback_device_mismatch_count,
        memory_order_relaxed
    );
    uint64_t admitted_late_callbacks = atomic_load_explicit(
        &capture->late_callback_count,
        memory_order_relaxed
    );
    uint64_t rejected_callbacks = atomic_load_explicit(
        &g_rejected_callback_count,
        memory_order_relaxed
    );
    out_stats->late_callback_count = admitted_late_callbacks +
        (rejected_callbacks - capture->rejected_callback_baseline);
    out_stats->in_flight_callback_count = atomic_load_explicit(
        &capture->in_flight_callback_count,
        memory_order_acquire
    );

    (void)pthread_mutex_lock(&capture->queue_mutex);
    out_stats->queue_depth = (uint64_t)capture->queue_depth;
    (void)pthread_mutex_unlock(&capture->queue_mutex);
    return MT_PHASE1_OK;
}

int32_t mt_phase1_capture_supports_force(const mt_phase1_capture_t *capture) {
    return capture != NULL ? capture->supports_force : -1;
}

int32_t mt_phase1_capture_is_built_in(const mt_phase1_capture_t *capture) {
    return capture != NULL ? capture->is_built_in : -1;
}

int32_t mt_phase1_capture_is_running(const mt_phase1_capture_t *capture) {
    return capture != NULL && capture->running ? 1 : 0;
}
