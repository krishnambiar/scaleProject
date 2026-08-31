#ifndef MT_PHASE2_INTERNAL_H
#define MT_PHASE2_INTERNAL_H

#include "mt_phase2.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct mt_phase2_decode_result {
    uint64_t copied_touches;
    uint64_t record_frame_mismatches;
    uint64_t record_timestamp_mismatches;
    uint64_t invalid_states;
    uint64_t pressure_sentinels;
    uint64_t nonfinite_touches;
} mt_phase2_decode_result_t;

const mt_phase2_source_layout_t *mt_phase2_compiled_source_layout(void);
bool mt_phase2_validate_source_layout(const mt_phase2_source_layout_t *layout);

void mt_phase2_decode_contacts(
    const void *records,
    uintptr_t raw_touch_count_register,
    uintptr_t raw_frame_register,
    double device_timestamp,
    mt_phase2_frame_t *out_frame,
    mt_phase2_decode_result_t *out_result
);

bool mt_phase2_native_target_matches(
    void *framework_handle,
    char *error_buffer,
    size_t error_buffer_size
);

#endif
