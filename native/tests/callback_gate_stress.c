#define MT_PHASE1_TESTING 1
#include "../src/mt_phase1.c"

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct test_barrier {
    pthread_mutex_t mutex;
    pthread_cond_t condition;
    bool reached;
    bool released;
} test_barrier_t;

static test_barrier_t g_test_barrier;

static void barrier_initialize(void) {
    g_test_barrier = (test_barrier_t){0};
    if (pthread_mutex_init(&g_test_barrier.mutex, NULL) != 0 ||
        pthread_cond_init(&g_test_barrier.condition, NULL) != 0) {
        abort();
    }
}

static void barrier_destroy(void) {
    (void)pthread_cond_destroy(&g_test_barrier.condition);
    (void)pthread_mutex_destroy(&g_test_barrier.mutex);
}

static void barrier_hook(void) {
    (void)pthread_mutex_lock(&g_test_barrier.mutex);
    g_test_barrier.reached = true;
    (void)pthread_cond_broadcast(&g_test_barrier.condition);
    while (!g_test_barrier.released) {
        (void)pthread_cond_wait(
            &g_test_barrier.condition,
            &g_test_barrier.mutex
        );
    }
    (void)pthread_mutex_unlock(&g_test_barrier.mutex);
}

static void barrier_wait_until_reached(void) {
    (void)pthread_mutex_lock(&g_test_barrier.mutex);
    while (!g_test_barrier.reached) {
        (void)pthread_cond_wait(
            &g_test_barrier.condition,
            &g_test_barrier.mutex
        );
    }
    (void)pthread_mutex_unlock(&g_test_barrier.mutex);
}

static void barrier_release(void) {
    (void)pthread_mutex_lock(&g_test_barrier.mutex);
    g_test_barrier.released = true;
    (void)pthread_cond_broadcast(&g_test_barrier.condition);
    (void)pthread_mutex_unlock(&g_test_barrier.mutex);
}

static mt_phase1_capture_t *synthetic_capture(
    const void *context,
    bool enable_phase2
) {
    mt_phase1_capture_t *capture = calloc(1, sizeof(*capture));
    if (capture == NULL || pthread_mutex_init(&capture->queue_mutex, NULL) != 0) {
        abort();
    }
    atomic_init(&capture->accepting_callbacks, true);
    atomic_init(&capture->callback_count, 0);
    atomic_init(&capture->enqueued_count, 0);
    atomic_init(&capture->queue_overwrite_count, 0);
    atomic_init(&capture->lock_contention_drop_count, 0);
    atomic_init(&capture->callback_device_mismatch_count, 0);
    atomic_init(&capture->late_callback_count, 0);
    atomic_init(&capture->in_flight_callback_count, 0);
    capture->device = (void *)(uintptr_t)0x1000;
    capture->callback_context = context;
    if (enable_phase2) {
        capture->phase2 = calloc(1, sizeof(*capture->phase2));
        if (capture->phase2 == NULL) {
            abort();
        }
        initialize_phase2_counters(capture->phase2);
    }
    return capture;
}

static void install_synthetic_capture(mt_phase1_capture_t *capture) {
    (void)pthread_mutex_lock(&g_callback_gate_mutex);
    g_callback_capture = capture;
    g_callback_context = capture->callback_context;
    (void)pthread_mutex_unlock(&g_callback_gate_mutex);
}

typedef struct callback_arguments {
    const void *context;
    void *device;
    const void *records;
} callback_arguments_t;

typedef struct callback_completion_arguments {
    callback_arguments_t callback;
    atomic_bool completed;
} callback_completion_arguments_t;

static void *invoke_callback(void *argument) {
    callback_arguments_t *arguments = argument;
    mt_phase1_raw_callback(
        arguments->device,
        arguments->records,
        1,
        1.0,
        1,
        (void *)arguments->context
    );
    return NULL;
}

static void *invoke_callback_and_mark_complete(void *argument) {
    callback_completion_arguments_t *arguments = argument;
    (void)invoke_callback(&arguments->callback);
    atomic_store_explicit(&arguments->completed, true, memory_order_release);
    return NULL;
}

static void make_valid_record(unsigned char *record) {
    memset(record, 0, 96);
    uint64_t frame = 1;
    double timestamp = 1.0;
    uint32_t state = 4;
    float x = 0.5f;
    float y = 0.5f;
    float z_total = 1.0f;
    float pressure = 2.0f;
    float density = 3.0f;
    memcpy(record + 0x00, &frame, sizeof(frame));
    memcpy(record + 0x08, &timestamp, sizeof(timestamp));
    memcpy(record + 0x14, &state, sizeof(state));
    memcpy(record + 0x20, &x, sizeof(x));
    memcpy(record + 0x24, &y, sizeof(y));
    memcpy(record + 0x30, &z_total, sizeof(z_total));
    memcpy(record + 0x34, &pressure, sizeof(pressure));
    memcpy(record + 0x5c, &density, sizeof(density));
}

typedef struct destroy_result {
    mt_phase1_capture_t *capture;
    int32_t status;
} destroy_result_t;

static void *destroy_capture(void *argument) {
    destroy_result_t *result = argument;
    char error[256] = {0};
    result->status = mt_phase1_capture_destroy(
        result->capture,
        error,
        sizeof(error)
    );
    if (result->status != MT_PHASE1_OK) {
        fprintf(stderr, "destroy failed in gate test: %s\n", error);
    }
    return NULL;
}

static void test_pause_before_admission(void) {
    const void *context = &g_callback_context_pool[0];
    mt_phase1_capture_t *capture = synthetic_capture(context, true);
    install_synthetic_capture(capture);
    barrier_initialize();
    g_test_before_callback_admission = barrier_hook;
    callback_arguments_t arguments = {
        .context = context,
        .device = (void *)(uintptr_t)0x1000,
        .records = (const void *)(uintptr_t)1,
    };

    pthread_t callback_thread;
    if (pthread_create(&callback_thread, NULL, invoke_callback, &arguments) != 0) {
        abort();
    }
    barrier_wait_until_reached();

    detach_callback_capture(capture);
    char error[256] = {0};
    int32_t status = mt_phase1_capture_destroy(capture, error, sizeof(error));
    if (status != MT_PHASE1_OK) {
        fprintf(stderr, "pre-admission destroy failed: %s\n", error);
        abort();
    }

    barrier_release();
    (void)pthread_join(callback_thread, NULL);
    g_test_before_callback_admission = NULL;
    barrier_destroy();
}

static void test_pause_after_admission(void) {
    const void *context = &g_callback_context_pool[1];
    mt_phase1_capture_t *capture = synthetic_capture(context, true);
    install_synthetic_capture(capture);
    barrier_initialize();
    g_test_after_callback_admission = barrier_hook;
    unsigned char record[96];
    make_valid_record(record);
    callback_arguments_t arguments = {
        .context = context,
        .device = (void *)(uintptr_t)0x1000,
        .records = record,
    };

    pthread_t callback_thread;
    if (pthread_create(&callback_thread, NULL, invoke_callback, &arguments) != 0) {
        abort();
    }
    barrier_wait_until_reached();
    detach_callback_capture(capture);

    destroy_result_t destroy = {
        .capture = capture,
        .status = MT_PHASE1_ERROR_INTERNAL,
    };
    pthread_t destroy_thread;
    if (pthread_create(&destroy_thread, NULL, destroy_capture, &destroy) != 0) {
        abort();
    }

    barrier_release();
    (void)pthread_join(callback_thread, NULL);
    (void)pthread_join(destroy_thread, NULL);
    if (destroy.status != MT_PHASE1_OK) {
        abort();
    }
    g_test_after_callback_admission = NULL;
    barrier_destroy();
}

static void test_disabled_phase2_never_reads_poison(void) {
    const void *context = &g_callback_context_pool[2];
    mt_phase1_capture_t *capture = synthetic_capture(context, false);
    install_synthetic_capture(capture);
    callback_arguments_t arguments = {
        .context = context,
        .device = (void *)(uintptr_t)0x1000,
        .records = (const void *)(uintptr_t)1,
    };
    (void)invoke_callback(&arguments);
    mt_phase1_frame_metadata_t frame = {0};
    if (mt_phase1_capture_poll(capture, &frame) != 1 ||
        frame.raw_touch_count_register != 1) {
        abort();
    }
    char error[256] = {0};
    if (mt_phase1_capture_destroy(capture, error, sizeof(error)) != MT_PHASE1_OK) {
        abort();
    }
}

static void test_device_mismatch_never_reads_poison(void) {
    const void *context = &g_callback_context_pool[3];
    mt_phase1_capture_t *capture = synthetic_capture(context, true);
    install_synthetic_capture(capture);
    callback_arguments_t arguments = {
        .context = context,
        .device = (void *)(uintptr_t)0x2000,
        .records = (const void *)(uintptr_t)1,
    };
    (void)invoke_callback(&arguments);
    mt_phase2_frame_t frame = {0};
    if (mt_phase2_capture_poll(capture, &frame) != 1 ||
        frame.decode_status != MT_PHASE2_DECODE_DEVICE_MISMATCH ||
        frame.copied_touch_count != 0) {
        abort();
    }
    char error[256] = {0};
    if (mt_phase1_capture_destroy(capture, error, sizeof(error)) != MT_PHASE1_OK) {
        abort();
    }
}

static void test_valid_callback_reaches_phase2_queue_and_stats(void) {
    const void *context = &g_callback_context_pool[5];
    mt_phase1_capture_t *capture = synthetic_capture(context, true);
    install_synthetic_capture(capture);
    unsigned char record[96];
    make_valid_record(record);
    callback_arguments_t arguments = {
        .context = context,
        .device = (void *)(uintptr_t)0x1000,
        .records = record,
    };
    (void)invoke_callback(&arguments);
    mt_phase2_frame_t frame = {0};
    if (mt_phase2_capture_poll(capture, &frame) != 1 ||
        frame.decode_status != MT_PHASE2_DECODE_OK ||
        frame.copied_touch_count != 1 ||
        frame.touches[0].pressure_candidate != 2.0f) {
        abort();
    }
    mt_phase2_capture_stats_t stats = {0};
    if (mt_phase2_capture_get_stats(capture, &stats) != MT_PHASE1_OK ||
        stats.attempted_frame_count != 1 || stats.copied_touch_count != 1 ||
        stats.queue_depth != 0) {
        abort();
    }
    char error[256] = {0};
    if (mt_phase1_capture_destroy(capture, error, sizeof(error)) != MT_PHASE1_OK) {
        abort();
    }
}

static void test_queue_overflow_drops_oldest_in_fifo_order(void) {
    const void *context = &g_callback_context_pool[6];
    mt_phase1_capture_t *capture = synthetic_capture(context, true);
    install_synthetic_capture(capture);
    unsigned char record[96];
    make_valid_record(record);

    const size_t callback_total = MT_PHASE1_QUEUE_CAPACITY + 3u;
    for (size_t index = 0; index < callback_total; ++index) {
        mt_phase1_raw_callback(
            capture->device,
            record,
            1,
            1.0,
            1,
            (void *)context
        );
    }

    mt_phase1_capture_stats_t phase1_stats = {0};
    mt_phase2_capture_stats_t phase2_stats = {0};
    if (mt_phase1_capture_get_stats(capture, &phase1_stats) != MT_PHASE1_OK ||
        mt_phase2_capture_get_stats(capture, &phase2_stats) != MT_PHASE1_OK ||
        phase1_stats.callback_count != callback_total ||
        phase1_stats.enqueued_count != callback_total ||
        phase1_stats.queue_depth != MT_PHASE1_QUEUE_CAPACITY ||
        phase1_stats.queue_overwrite_count != 3 ||
        phase1_stats.lock_contention_drop_count != 0 ||
        phase2_stats.attempted_frame_count != callback_total ||
        phase2_stats.copied_touch_count != callback_total ||
        phase2_stats.queue_depth != MT_PHASE2_QUEUE_CAPACITY ||
        phase2_stats.queue_overwrite_count !=
            callback_total - MT_PHASE2_QUEUE_CAPACITY ||
        phase2_stats.lock_contention_drop_count != 0) {
        abort();
    }

    const uint64_t phase1_first = callback_total - MT_PHASE1_QUEUE_CAPACITY + 1u;
    for (uint64_t index = 0; index < MT_PHASE1_QUEUE_CAPACITY; ++index) {
        mt_phase1_frame_metadata_t frame = {0};
        if (mt_phase1_capture_poll(capture, &frame) != 1 ||
            frame.sequence != phase1_first + index) {
            abort();
        }
    }
    mt_phase1_frame_metadata_t no_metadata = {0};
    if (mt_phase1_capture_poll(capture, &no_metadata) != 0) {
        abort();
    }

    const uint64_t phase2_first = callback_total - MT_PHASE2_QUEUE_CAPACITY + 1u;
    for (uint64_t index = 0; index < MT_PHASE2_QUEUE_CAPACITY; ++index) {
        mt_phase2_frame_t frame = {0};
        if (mt_phase2_capture_poll(capture, &frame) != 1 ||
            frame.metadata.sequence != phase2_first + index) {
            abort();
        }
    }
    mt_phase2_frame_t no_touch_frame = {0};
    if (mt_phase2_capture_poll(capture, &no_touch_frame) != 0) {
        abort();
    }

    char error[256] = {0};
    if (mt_phase1_capture_destroy(capture, error, sizeof(error)) != MT_PHASE1_OK) {
        abort();
    }
}

static void test_callback_never_waits_for_queue_mutex(void) {
    const void *context = &g_callback_context_pool[7];
    mt_phase1_capture_t *capture = synthetic_capture(context, true);
    install_synthetic_capture(capture);
    unsigned char record[96];
    make_valid_record(record);
    callback_completion_arguments_t arguments = {
        .callback = {
            .context = context,
            .device = capture->device,
            .records = record,
        },
    };
    atomic_init(&arguments.completed, false);

    (void)pthread_mutex_lock(&capture->queue_mutex);
    pthread_t callback_thread;
    if (pthread_create(
            &callback_thread,
            NULL,
            invoke_callback_and_mark_complete,
            &arguments
        ) != 0) {
        abort();
    }
    const struct timespec pause = {.tv_sec = 0, .tv_nsec = 1000000L};
    bool completed_while_locked = false;
    for (size_t attempt = 0; attempt < 1000u; ++attempt) {
        if (atomic_load_explicit(&arguments.completed, memory_order_acquire)) {
            completed_while_locked = true;
            break;
        }
        (void)nanosleep(&pause, NULL);
    }
    (void)pthread_mutex_unlock(&capture->queue_mutex);
    (void)pthread_join(callback_thread, NULL);
    if (!completed_while_locked) {
        abort();
    }

    mt_phase1_capture_stats_t phase1_stats = {0};
    mt_phase2_capture_stats_t phase2_stats = {0};
    if (mt_phase1_capture_get_stats(capture, &phase1_stats) != MT_PHASE1_OK ||
        mt_phase2_capture_get_stats(capture, &phase2_stats) != MT_PHASE1_OK ||
        phase1_stats.callback_count != 1 || phase1_stats.enqueued_count != 0 ||
        phase1_stats.lock_contention_drop_count != 1 ||
        phase1_stats.queue_depth != 0 ||
        phase2_stats.attempted_frame_count != 1 ||
        phase2_stats.lock_contention_drop_count != 1 ||
        phase2_stats.queue_depth != 0) {
        abort();
    }

    char error[256] = {0};
    if (mt_phase1_capture_destroy(capture, error, sizeof(error)) != MT_PHASE1_OK) {
        abort();
    }
}

static void test_maximum_touch_frame_is_copied(void) {
    const void *context = &g_callback_context_pool[8];
    mt_phase1_capture_t *capture = synthetic_capture(context, true);
    install_synthetic_capture(capture);
    unsigned char records[MT_PHASE2_MAX_TOUCHES][96];
    for (size_t index = 0; index < MT_PHASE2_MAX_TOUCHES; ++index) {
        make_valid_record(records[index]);
    }

    mt_phase1_raw_callback(
        capture->device,
        records,
        MT_PHASE2_MAX_TOUCHES,
        1.0,
        1,
        (void *)context
    );
    mt_phase2_frame_t frame = {0};
    if (mt_phase2_capture_poll(capture, &frame) != 1 ||
        frame.decode_status != MT_PHASE2_DECODE_OK ||
        frame.copied_touch_count != MT_PHASE2_MAX_TOUCHES) {
        abort();
    }

    char error[256] = {0};
    if (mt_phase1_capture_destroy(capture, error, sizeof(error)) != MT_PHASE1_OK) {
        abort();
    }
}

static void test_enable_refuses_unresolved_callbacks(void) {
    const void *context = &g_callback_context_pool[4];
    mt_phase1_capture_t *capture = synthetic_capture(context, false);
    capture->supports_force = 1;
    capture->is_built_in = 1;
    atomic_store_explicit(
        &capture->in_flight_callback_count,
        1,
        memory_order_release
    );
    char error[256] = {0};
    int32_t status = mt_phase2_capture_enable_profile(
        capture,
        MT_PHASE2_VERIFIED_PROFILE_ID,
        error,
        sizeof(error)
    );
    if (status != MT_PHASE1_ERROR_INVALID_STATE) {
        abort();
    }
    atomic_store_explicit(
        &capture->in_flight_callback_count,
        0,
        memory_order_release
    );
    (void)pthread_mutex_lock(&g_active_capture_mutex);
    g_active_capture = capture;
    (void)pthread_mutex_unlock(&g_active_capture_mutex);
    status = mt_phase2_capture_enable_profile(
        capture,
        MT_PHASE2_VERIFIED_PROFILE_ID,
        error,
        sizeof(error)
    );
    if (status != MT_PHASE1_ERROR_INVALID_STATE) {
        abort();
    }
    (void)pthread_mutex_lock(&g_active_capture_mutex);
    g_active_capture = NULL;
    (void)pthread_mutex_unlock(&g_active_capture_mutex);
    if (mt_phase1_capture_destroy(capture, error, sizeof(error)) != MT_PHASE1_OK) {
        abort();
    }
}

int main(void) {
    test_pause_before_admission();
    test_pause_after_admission();
    test_disabled_phase2_never_reads_poison();
    test_device_mismatch_never_reads_poison();
    test_valid_callback_reaches_phase2_queue_and_stats();
    test_queue_overflow_drops_oldest_in_fifo_order();
    test_callback_never_waits_for_queue_mutex();
    test_maximum_touch_frame_is_copied();
    test_enable_refuses_unresolved_callbacks();
    puts("callback admission, queue, and destruction tests passed");
    return 0;
}
