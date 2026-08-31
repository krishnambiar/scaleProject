#ifndef MT_PHASE1_H
#define MT_PHASE1_H

#include <stddef.h>
#include <stdint.h>

#if defined(__cplusplus)
extern "C" {
#endif

#if defined(__GNUC__)
#define MT_PHASE1_API __attribute__((visibility("default")))
#else
#define MT_PHASE1_API
#endif

#define MT_PHASE1_BRIDGE_ABI_VERSION 2u

typedef struct mt_phase1_capture mt_phase1_capture_t;

/* Lifecycle/control calls for one capture must be serialized by the caller. */

/*
 * Application-owned metadata copied synchronously inside the callback.
 *
 * raw_touch_count_register and raw_frame_register intentionally describe how
 * Phase 1 captures the values. They do not assert undocumented Apple typedefs.
 * The framework-owned touch-record pointer is never stored or dereferenced.
 */
typedef struct mt_phase1_frame_metadata {
    uint64_t sequence;
    uint64_t raw_touch_count_register;
    uint64_t raw_frame_register;
    double device_timestamp;
    uint64_t host_monotonic_ns;
} mt_phase1_frame_metadata_t;

typedef struct mt_phase1_capture_stats {
    uint64_t callback_count;
    uint64_t enqueued_count;
    uint64_t queue_overwrite_count;
    uint64_t lock_contention_drop_count;
    uint64_t callback_device_mismatch_count;
    uint64_t late_callback_count;
    uint64_t in_flight_callback_count;
    uint64_t queue_depth;
} mt_phase1_capture_stats_t;

enum mt_phase1_status {
    MT_PHASE1_OK = 0,
    MT_PHASE1_ERROR_INVALID_ARGUMENT = -1,
    MT_PHASE1_ERROR_ALLOCATION = -2,
    MT_PHASE1_ERROR_FRAMEWORK_LOAD = -3,
    MT_PHASE1_ERROR_SYMBOL_RESOLUTION = -4,
    MT_PHASE1_ERROR_NO_DEVICE = -5,
    MT_PHASE1_ERROR_ALREADY_ACTIVE = -6,
    MT_PHASE1_ERROR_CALLBACK_REGISTRATION = -7,
    MT_PHASE1_ERROR_DEVICE_START = -8,
    MT_PHASE1_ERROR_DEVICE_STOP = -9,
    MT_PHASE1_ERROR_CALLBACK_UNREGISTER = -10,
    MT_PHASE1_ERROR_NOT_RUNNING = -11,
    MT_PHASE1_ERROR_INTERNAL = -12,
    MT_PHASE1_ERROR_CALLBACK_QUIESCENCE = -13,
    MT_PHASE1_ERROR_PHASE2_PROFILE = -14,
    MT_PHASE1_ERROR_INVALID_STATE = -15
};

MT_PHASE1_API uint32_t mt_phase1_bridge_abi_version(void);
MT_PHASE1_API size_t mt_phase1_frame_metadata_size(void);
MT_PHASE1_API size_t mt_phase1_capture_stats_size(void);
MT_PHASE1_API const char *mt_phase1_framework_path(void);

MT_PHASE1_API int32_t mt_phase1_capture_create(
    mt_phase1_capture_t **out_capture,
    char *error_buffer,
    size_t error_buffer_size
);

/*
 * start_options is explicit because its private semantics are not documented.
 * The diagnostic records the exact value and native return status used.
 */
MT_PHASE1_API int32_t mt_phase1_capture_start(
    mt_phase1_capture_t *capture,
    uint32_t start_options,
    int32_t *out_native_status,
    char *error_buffer,
    size_t error_buffer_size
);

MT_PHASE1_API int32_t mt_phase1_capture_stop(
    mt_phase1_capture_t *capture,
    int32_t *out_native_status,
    char *error_buffer,
    size_t error_buffer_size
);

/*
 * On cleanup uncertainty, destroy returns an error and intentionally leaves the
 * capture allocated so an undocumented late callback cannot use freed state.
 */
MT_PHASE1_API int32_t mt_phase1_capture_destroy(
    mt_phase1_capture_t *capture,
    char *error_buffer,
    size_t error_buffer_size
);

/* Returns 1 for a frame, 0 when the queue is empty, or a negative status. */
MT_PHASE1_API int32_t mt_phase1_capture_poll(
    mt_phase1_capture_t *capture,
    mt_phase1_frame_metadata_t *out_frame
);

MT_PHASE1_API int32_t mt_phase1_capture_get_stats(
    mt_phase1_capture_t *capture,
    mt_phase1_capture_stats_t *out_stats
);

/* Returns 1/0, or -1 when the capability symbol is unavailable. */
MT_PHASE1_API int32_t mt_phase1_capture_supports_force(
    const mt_phase1_capture_t *capture
);
MT_PHASE1_API int32_t mt_phase1_capture_is_built_in(
    const mt_phase1_capture_t *capture
);
MT_PHASE1_API int32_t mt_phase1_capture_is_running(
    const mt_phase1_capture_t *capture
);

#if defined(__cplusplus)
}
#endif

#endif
