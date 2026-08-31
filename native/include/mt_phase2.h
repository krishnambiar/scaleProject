#ifndef MT_PHASE2_H
#define MT_PHASE2_H

#include "mt_phase1.h"

#include <stddef.h>
#include <stdint.h>

#if defined(__cplusplus)
extern "C" {
#endif

#define MT_PHASE2_BRIDGE_ABI_VERSION 1u
#define MT_PHASE2_LAYOUT_DESCRIPTOR_VERSION 1u
#define MT_PHASE2_VERIFIED_PROFILE_ID 1u
#define MT_PHASE2_MAX_TOUCHES 32u

enum mt_phase2_touch_copied_field {
    MT_PHASE2_TOUCH_PATH_INDEX_COPIED = 1u << 0,
    MT_PHASE2_TOUCH_STATE_COPIED = 1u << 1,
    MT_PHASE2_TOUCH_FINGER_ID_COPIED = 1u << 2,
    MT_PHASE2_TOUCH_HAND_ID_COPIED = 1u << 3,
    MT_PHASE2_TOUCH_NORMALIZED_X_COPIED = 1u << 4,
    MT_PHASE2_TOUCH_NORMALIZED_Y_COPIED = 1u << 5,
    MT_PHASE2_TOUCH_Z_TOTAL_COPIED = 1u << 6,
    MT_PHASE2_TOUCH_PRESSURE_CANDIDATE_COPIED = 1u << 7,
    MT_PHASE2_TOUCH_Z_DENSITY_COPIED = 1u << 8
};

enum mt_phase2_decode_status {
    MT_PHASE2_DECODE_OK = 0,
    MT_PHASE2_DECODE_INVALID_COUNT = 1u << 0,
    MT_PHASE2_DECODE_NULL_RECORDS = 1u << 1,
    MT_PHASE2_DECODE_DEVICE_MISMATCH = 1u << 2,
    MT_PHASE2_DECODE_RECORD_FRAME_MISMATCH = 1u << 3,
    MT_PHASE2_DECODE_RECORD_TIMESTAMP_MISMATCH = 1u << 4,
    MT_PHASE2_DECODE_NONFINITE_SCALAR = 1u << 5,
    MT_PHASE2_DECODE_INVALID_STATE = 1u << 6,
    MT_PHASE2_DECODE_PRESSURE_SENTINEL = 1u << 7
};

/*
 * Project-owned Phase 2 touch snapshot. Values remain raw sensor coordinates;
 * pressure_candidate is explicitly not grams and has no claimed physical unit.
 * The *_bits fields preserve the exact IEEE-754 binary32 source bits.
 */
typedef struct mt_phase2_touch {
    /* Presence only; semantic usability still requires decode_status == 0. */
    uint32_t copied_fields;
    uint32_t path_index;
    uint32_t state;
    uint32_t finger_id;
    int32_t hand_id;
    uint32_t reserved0;
    float normalized_x;
    float normalized_y;
    float z_total;
    float pressure_candidate;
    float z_density;
    uint32_t normalized_x_bits;
    uint32_t normalized_y_bits;
    uint32_t z_total_bits;
    uint32_t pressure_candidate_bits;
    uint32_t z_density_bits;
} mt_phase2_touch_t;

typedef struct mt_phase2_frame {
    mt_phase1_frame_metadata_t metadata;
    uint32_t layout_profile_id;
    uint32_t decode_status;
    uint32_t copied_touch_count;
    uint32_t reserved0;
    mt_phase2_touch_t touches[MT_PHASE2_MAX_TOUCHES];
} mt_phase2_frame_t;

typedef struct mt_phase2_capture_stats {
    uint64_t attempted_frame_count;
    uint64_t copied_touch_count;
    uint64_t queue_overwrite_count;
    uint64_t lock_contention_drop_count;
    uint64_t invalid_count_frame_count;
    uint64_t null_records_frame_count;
    uint64_t device_mismatch_frame_count;
    uint64_t record_frame_mismatch_touch_count;
    uint64_t record_timestamp_mismatch_touch_count;
    uint64_t invalid_state_touch_count;
    uint64_t pressure_sentinel_touch_count;
    uint64_t nonfinite_touch_count;
    uint64_t queue_depth;
} mt_phase2_capture_stats_t;

/*
 * Auditable description of the source bytes accepted by the one compiled-in
 * target profile. It is diagnostic output, never caller-supplied authority.
 */
typedef struct mt_phase2_source_layout {
    uint32_t descriptor_version;
    uint32_t profile_id;
    uint32_t record_size;
    uint32_t maximum_touch_count;
    uint32_t record_frame_offset;
    uint32_t record_frame_size;
    uint32_t record_timestamp_offset;
    uint32_t record_timestamp_size;
    uint32_t path_index_offset;
    uint32_t path_index_size;
    uint32_t state_offset;
    uint32_t state_size;
    uint32_t finger_id_offset;
    uint32_t finger_id_size;
    uint32_t hand_id_offset;
    uint32_t hand_id_size;
    uint32_t normalized_x_offset;
    uint32_t normalized_x_size;
    uint32_t normalized_y_offset;
    uint32_t normalized_y_size;
    uint32_t z_total_offset;
    uint32_t z_total_size;
    uint32_t pressure_candidate_offset;
    uint32_t pressure_candidate_size;
    uint32_t z_density_offset;
    uint32_t z_density_size;
    uint32_t reserved[6];
} mt_phase2_source_layout_t;

MT_PHASE1_API uint32_t mt_phase2_bridge_abi_version(void);
MT_PHASE1_API size_t mt_phase2_touch_size(void);
MT_PHASE1_API size_t mt_phase2_frame_size(void);
MT_PHASE1_API size_t mt_phase2_capture_stats_size(void);
MT_PHASE1_API size_t mt_phase2_source_layout_size(void);
MT_PHASE1_API uint64_t mt_phase2_output_layout_fingerprint(void);
MT_PHASE1_API const char *mt_phase2_verified_profile_name(void);
MT_PHASE1_API int32_t mt_phase2_get_source_layout(
    mt_phase2_source_layout_t *out_layout
);

/*
 * Enables the only immutable, compiled-in layout profile. The native bridge
 * independently checks the exact Mac model, OS build, framework bundle/image,
 * built-in device capability, and Force Touch support. Must be called before
 * start. Arbitrary offsets are never accepted from Python.
 */
MT_PHASE1_API int32_t mt_phase2_capture_enable_profile(
    mt_phase1_capture_t *capture,
    uint32_t profile_id,
    char *error_buffer,
    size_t error_buffer_size
);

/* Pops the next rich frame from the separate Phase 2 queue. */
MT_PHASE1_API int32_t mt_phase2_capture_poll(
    mt_phase1_capture_t *capture,
    mt_phase2_frame_t *out_frame
);

MT_PHASE1_API int32_t mt_phase2_capture_get_stats(
    mt_phase1_capture_t *capture,
    mt_phase2_capture_stats_t *out_stats
);

#if defined(__cplusplus)
}
#endif

#endif
