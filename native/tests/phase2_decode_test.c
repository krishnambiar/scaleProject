#include "mt_phase2.h"
#include "../src/mt_phase2_internal.h"

#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static void write_u32(unsigned char *record, size_t offset, uint32_t value) {
    memcpy(record + offset, &value, sizeof(value));
}

static void write_i32(unsigned char *record, size_t offset, int32_t value) {
    memcpy(record + offset, &value, sizeof(value));
}

static void write_u64(unsigned char *record, size_t offset, uint64_t value) {
    memcpy(record + offset, &value, sizeof(value));
}

static void write_double(unsigned char *record, size_t offset, double value) {
    memcpy(record + offset, &value, sizeof(value));
}

static void write_float(unsigned char *record, size_t offset, float value) {
    memcpy(record + offset, &value, sizeof(value));
}

static void populate_record(
    unsigned char *record,
    uint64_t frame,
    double timestamp,
    uint32_t path,
    float pressure
) {
    memset(record, 0, 96);
    write_u64(record, 0x00, frame);
    write_double(record, 0x08, timestamp);
    write_u32(record, 0x10, path);
    write_u32(record, 0x14, 4);
    write_u32(record, 0x18, 7);
    write_i32(record, 0x1c, -1);
    write_float(record, 0x20, 0.25f);
    write_float(record, 0x24, 0.75f);
    write_float(record, 0x30, 12.5f);
    write_float(record, 0x34, pressure);
    write_float(record, 0x5c, 3.5f);
}

static void test_layout_validator(void) {
    mt_phase2_source_layout_t layout = *mt_phase2_compiled_source_layout();
    assert(mt_phase2_validate_source_layout(&layout));

    layout.record_size = 0;
    assert(!mt_phase2_validate_source_layout(&layout));
    layout = *mt_phase2_compiled_source_layout();
    layout.record_size = UINT32_MAX;
    assert(!mt_phase2_validate_source_layout(&layout));
    layout = *mt_phase2_compiled_source_layout();
    layout.record_size = 97;
    assert(!mt_phase2_validate_source_layout(&layout));
    layout = *mt_phase2_compiled_source_layout();
    layout.maximum_touch_count = 31;
    assert(!mt_phase2_validate_source_layout(&layout));
    layout = *mt_phase2_compiled_source_layout();
    layout.maximum_touch_count = 33;
    assert(!mt_phase2_validate_source_layout(&layout));
    layout = *mt_phase2_compiled_source_layout();
    layout.pressure_candidate_offset = layout.record_size;
    assert(!mt_phase2_validate_source_layout(&layout));
    layout = *mt_phase2_compiled_source_layout();
    layout.pressure_candidate_size = UINT32_MAX;
    assert(!mt_phase2_validate_source_layout(&layout));
    layout = *mt_phase2_compiled_source_layout();
    layout.pressure_candidate_size = sizeof(double);
    assert(!mt_phase2_validate_source_layout(&layout));
}

static void test_fail_closed_counts_and_null(void) {
    mt_phase2_frame_t frame = {0};
    mt_phase2_decode_result_t result = {0};
    mt_phase2_decode_contacts(NULL, 0, 1, 1.0, &frame, &result);
    assert(frame.decode_status == MT_PHASE2_DECODE_OK);
    assert(frame.copied_touch_count == 0);

    memset(&frame, 0, sizeof(frame));
    mt_phase2_decode_contacts(NULL, 1, 1, 1.0, &frame, &result);
    assert((frame.decode_status & MT_PHASE2_DECODE_NULL_RECORDS) != 0);
    assert(frame.copied_touch_count == 0);

    memset(&frame, 0, sizeof(frame));
    mt_phase2_decode_contacts((const void *)(uintptr_t)1, 33, 1, 1.0, &frame, &result);
    assert((frame.decode_status & MT_PHASE2_DECODE_INVALID_COUNT) != 0);
    assert(frame.copied_touch_count == 0);

    memset(&frame, 0, sizeof(frame));
    mt_phase2_decode_contacts(
        (const void *)(uintptr_t)1,
        UINTPTR_MAX,
        1,
        1.0,
        &frame,
        &result
    );
    assert((frame.decode_status & MT_PHASE2_DECODE_INVALID_COUNT) != 0);
}

static void test_verified_fields_and_owned_copy(void) {
    unsigned char records[2 * 96];
    populate_record(records, 42, 10.5, 3, 50.25f);
    populate_record(records + 96, 42, 10.5, 8, 75.5f);

    mt_phase2_frame_t frame = {0};
    mt_phase2_decode_result_t result = {0};
    mt_phase2_decode_contacts(records, 2, 42, 10.5, &frame, &result);
    assert(frame.decode_status == MT_PHASE2_DECODE_OK);
    assert(frame.copied_touch_count == 2);
    assert(result.copied_touches == 2);
    assert(frame.touches[0].path_index == 3);
    assert(frame.touches[0].state == 4);
    assert(frame.touches[0].finger_id == 7);
    assert(frame.touches[0].hand_id == -1);
    assert(frame.touches[0].normalized_x == 0.25f);
    assert(frame.touches[0].normalized_y == 0.75f);
    assert(frame.touches[0].z_total == 12.5f);
    assert(frame.touches[0].pressure_candidate == 50.25f);
    assert(frame.touches[0].z_density == 3.5f);
    assert(frame.touches[1].path_index == 8);
    assert(frame.touches[1].pressure_candidate == 75.5f);

    uint32_t pressure_bits = 0;
    memcpy(&pressure_bits, &(float){50.25f}, sizeof(pressure_bits));
    assert(frame.touches[0].pressure_candidate_bits == pressure_bits);

    memset(records, 0xa5, sizeof(records));
    assert(frame.touches[0].pressure_candidate == 50.25f);
    assert(frame.touches[1].pressure_candidate == 75.5f);
}

static void test_mismatch_and_nonfinite_flags(void) {
    unsigned char record[96];
    populate_record(record, 6, 2.0, 1, NAN);

    mt_phase2_frame_t frame = {0};
    mt_phase2_decode_result_t result = {0};
    mt_phase2_decode_contacts(record, 1, 7, 3.0, &frame, &result);
    assert((frame.decode_status & MT_PHASE2_DECODE_RECORD_FRAME_MISMATCH) != 0);
    assert((frame.decode_status & MT_PHASE2_DECODE_RECORD_TIMESTAMP_MISMATCH) != 0);
    assert((frame.decode_status & MT_PHASE2_DECODE_NONFINITE_SCALAR) != 0);
    assert(result.record_frame_mismatches == 1);
    assert(result.record_timestamp_mismatches == 1);
    assert(result.nonfinite_touches == 1);

    populate_record(record, 7, 3.0, 1, 43690.0f);
    write_u32(record, 0x14, 8);
    memset(&frame, 0, sizeof(frame));
    mt_phase2_decode_contacts(record, 1, 7, 3.0, &frame, &result);
    assert((frame.decode_status & MT_PHASE2_DECODE_INVALID_STATE) != 0);
    assert((frame.decode_status & MT_PHASE2_DECODE_PRESSURE_SENTINEL) != 0);
    assert(result.invalid_states == 1);
    assert(result.pressure_sentinels == 1);
}

static void test_decoder_resets_reused_output_status(void) {
    unsigned char record[96];
    populate_record(record, 20, 8.0, 1, 10.0f);
    mt_phase2_frame_t frame;
    memset(&frame, 0xff, sizeof(frame));
    mt_phase2_decode_result_t result = {0};
    mt_phase2_decode_contacts(record, 1, 20, 8.0, &frame, &result);
    assert(frame.decode_status == MT_PHASE2_DECODE_OK);
    assert(frame.copied_touch_count == 1);
}

static void test_guard_page_no_overread(void) {
    long page_size = sysconf(_SC_PAGESIZE);
    assert(page_size > 96);
    size_t mapping_size = (size_t)page_size * 2u;
    unsigned char *mapping = mmap(
        NULL,
        mapping_size,
        PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANONYMOUS,
        -1,
        0
    );
    assert(mapping != MAP_FAILED);
    assert(mprotect(mapping + page_size, (size_t)page_size, PROT_NONE) == 0);

    unsigned char *record = mapping + page_size - 96;
    populate_record(record, 9, 4.0, 2, 100.0f);
    mt_phase2_frame_t frame = {0};
    mt_phase2_decode_result_t result = {0};
    mt_phase2_decode_contacts(record, 1, 9, 4.0, &frame, &result);
    assert(frame.decode_status == MT_PHASE2_DECODE_OK);
    assert(frame.touches[0].pressure_candidate == 100.0f);
    assert(munmap(mapping, mapping_size) == 0);
}

static void test_exact_maximum_count(void) {
    unsigned char records[MT_PHASE2_MAX_TOUCHES * 96];
    for (uint32_t index = 0; index < MT_PHASE2_MAX_TOUCHES; ++index) {
        populate_record(records + (index * 96), 12, 5.0, index, (float)index);
    }
    mt_phase2_frame_t frame = {0};
    mt_phase2_decode_result_t result = {0};
    mt_phase2_decode_contacts(
        records,
        MT_PHASE2_MAX_TOUCHES,
        12,
        5.0,
        &frame,
        &result
    );
    assert(frame.decode_status == MT_PHASE2_DECODE_OK);
    assert(frame.copied_touch_count == MT_PHASE2_MAX_TOUCHES);
    assert(frame.touches[31].path_index == 31);
}

int main(void) {
    test_layout_validator();
    test_fail_closed_counts_and_null();
    test_verified_fields_and_owned_copy();
    test_mismatch_and_nonfinite_flags();
    test_decoder_resets_reused_output_status();
    test_guard_page_no_overread();
    test_exact_maximum_count();
    puts("Phase 2 guarded decoder tests passed");
    return 0;
}
