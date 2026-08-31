#include "mt_phase2_internal.h"

#include <CoreFoundation/CoreFoundation.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <mach-o/loader.h>
#include <math.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <unistd.h>

#if !defined(__APPLE__) || !defined(__aarch64__)
#error "The compiled Phase 2 source layout is verified only for arm64 macOS."
#endif

#define MT_PHASE2_SOURCE_RECORD_SIZE 0x60u

#define MT_PHASE2_RECORD_FRAME_OFFSET 0x00u
#define MT_PHASE2_RECORD_TIMESTAMP_OFFSET 0x08u
#define MT_PHASE2_PATH_INDEX_OFFSET 0x10u
#define MT_PHASE2_STATE_OFFSET 0x14u
#define MT_PHASE2_FINGER_ID_OFFSET 0x18u
#define MT_PHASE2_HAND_ID_OFFSET 0x1cu
#define MT_PHASE2_NORMALIZED_X_OFFSET 0x20u
#define MT_PHASE2_NORMALIZED_Y_OFFSET 0x24u
#define MT_PHASE2_Z_TOTAL_OFFSET 0x30u
#define MT_PHASE2_PRESSURE_CANDIDATE_OFFSET 0x34u
#define MT_PHASE2_Z_DENSITY_OFFSET 0x5cu

#define MT_PHASE2_VERIFIED_HARDWARE_MODEL "Mac16,8"
#define MT_PHASE2_VERIFIED_PRODUCT_BUILD "25D771280a"
#define MT_PHASE2_VERIFIED_KERNEL_BUILD "25D2128"
#define MT_PHASE2_VERIFIED_FRAMEWORK_VERSION "9430.5"
#define MT_PHASE2_SYSTEM_VERSION_PATH \
    "/System/Volumes/Preboot/Cryptexes/OS/System/Library/CoreServices/" \
    "SystemVersion.plist"
#define MT_PHASE2_FRAMEWORK_BUNDLE_PATH \
    "/System/Library/PrivateFrameworks/MultitouchSupport.framework"

/*
 * This target view exists only to make the compiler prove size/alignment and
 * the offsets established from this exact framework image's dataflow. Runtime
 * decoding still uses byte pointers plus memcpy; it never casts Apple memory
 * to this type.
 */
typedef struct mt_phase2_target_contact_view {
    uint64_t record_frame;
    double record_timestamp;
    uint32_t path_index;
    uint32_t state;
    uint32_t finger_id;
    int32_t hand_id;
    float normalized_x;
    float normalized_y;
    unsigned char unverified_28_to_2f[8];
    float z_total;
    float pressure_candidate;
    unsigned char unverified_38_to_5b[36];
    float z_density;
} mt_phase2_target_contact_view_t;

_Static_assert(
    sizeof(mt_phase2_target_contact_view_t) == MT_PHASE2_SOURCE_RECORD_SIZE,
    "verified target contact view must be exactly 96 bytes"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, record_frame) ==
        MT_PHASE2_RECORD_FRAME_OFFSET,
    "record frame offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, record_timestamp) ==
        MT_PHASE2_RECORD_TIMESTAMP_OFFSET,
    "record timestamp offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, path_index) ==
        MT_PHASE2_PATH_INDEX_OFFSET,
    "path index offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, state) == MT_PHASE2_STATE_OFFSET,
    "state offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, finger_id) ==
        MT_PHASE2_FINGER_ID_OFFSET,
    "finger ID offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, hand_id) ==
        MT_PHASE2_HAND_ID_OFFSET,
    "hand ID offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, normalized_x) ==
        MT_PHASE2_NORMALIZED_X_OFFSET,
    "normalized X offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, normalized_y) ==
        MT_PHASE2_NORMALIZED_Y_OFFSET,
    "normalized Y offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, z_total) ==
        MT_PHASE2_Z_TOTAL_OFFSET,
    "zTotal offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, pressure_candidate) ==
        MT_PHASE2_PRESSURE_CANDIDATE_OFFSET,
    "pressure candidate offset mismatch"
);
_Static_assert(
    offsetof(mt_phase2_target_contact_view_t, z_density) ==
        MT_PHASE2_Z_DENSITY_OFFSET,
    "zDensity offset mismatch"
);

static const mt_phase2_source_layout_t g_source_layout = {
    .descriptor_version = MT_PHASE2_LAYOUT_DESCRIPTOR_VERSION,
    .profile_id = MT_PHASE2_VERIFIED_PROFILE_ID,
    .record_size = MT_PHASE2_SOURCE_RECORD_SIZE,
    .maximum_touch_count = MT_PHASE2_MAX_TOUCHES,
    .record_frame_offset = MT_PHASE2_RECORD_FRAME_OFFSET,
    .record_frame_size = sizeof(uint64_t),
    .record_timestamp_offset = MT_PHASE2_RECORD_TIMESTAMP_OFFSET,
    .record_timestamp_size = sizeof(double),
    .path_index_offset = MT_PHASE2_PATH_INDEX_OFFSET,
    .path_index_size = sizeof(uint32_t),
    .state_offset = MT_PHASE2_STATE_OFFSET,
    .state_size = sizeof(uint32_t),
    .finger_id_offset = MT_PHASE2_FINGER_ID_OFFSET,
    .finger_id_size = sizeof(uint32_t),
    .hand_id_offset = MT_PHASE2_HAND_ID_OFFSET,
    .hand_id_size = sizeof(int32_t),
    .normalized_x_offset = MT_PHASE2_NORMALIZED_X_OFFSET,
    .normalized_x_size = sizeof(float),
    .normalized_y_offset = MT_PHASE2_NORMALIZED_Y_OFFSET,
    .normalized_y_size = sizeof(float),
    .z_total_offset = MT_PHASE2_Z_TOTAL_OFFSET,
    .z_total_size = sizeof(float),
    .pressure_candidate_offset = MT_PHASE2_PRESSURE_CANDIDATE_OFFSET,
    .pressure_candidate_size = sizeof(float),
    .z_density_offset = MT_PHASE2_Z_DENSITY_OFFSET,
    .z_density_size = sizeof(float),
    .reserved = {0},
};

static const unsigned char g_verified_framework_uuid[16] = {
    0x40, 0xd6, 0x91, 0xbb,
    0x91, 0x66,
    0x31, 0xe0,
    0x95, 0x9e,
    0x35, 0x18, 0x63, 0xff, 0x09, 0xa0,
};

static void write_profile_error(
    char *buffer,
    size_t buffer_size,
    const char *format,
    ...
) {
    if (buffer == NULL || buffer_size == 0) {
        return;
    }
    va_list arguments;
    va_start(arguments, format);
    (void)vsnprintf(buffer, buffer_size, format, arguments);
    va_end(arguments);
}

static bool field_fits(uint32_t record_size, uint32_t offset, uint32_t width) {
    return width > 0 && offset <= record_size && width <= record_size - offset;
}

const mt_phase2_source_layout_t *mt_phase2_compiled_source_layout(void) {
    return &g_source_layout;
}

bool mt_phase2_validate_source_layout(const mt_phase2_source_layout_t *layout) {
    if (layout == NULL ||
        layout->descriptor_version != MT_PHASE2_LAYOUT_DESCRIPTOR_VERSION ||
        layout->profile_id != MT_PHASE2_VERIFIED_PROFILE_ID ||
        layout->record_size == 0 || layout->record_size > 4096 ||
        layout->maximum_touch_count == 0 ||
        layout->maximum_touch_count > MT_PHASE2_MAX_TOUCHES ||
        layout->record_size > SIZE_MAX / layout->maximum_touch_count) {
        return false;
    }

    const uint32_t offsets[] = {
        layout->record_frame_offset,
        layout->record_timestamp_offset,
        layout->path_index_offset,
        layout->state_offset,
        layout->finger_id_offset,
        layout->hand_id_offset,
        layout->normalized_x_offset,
        layout->normalized_y_offset,
        layout->z_total_offset,
        layout->pressure_candidate_offset,
        layout->z_density_offset,
    };
    const uint32_t widths[] = {
        layout->record_frame_size,
        layout->record_timestamp_size,
        layout->path_index_size,
        layout->state_size,
        layout->finger_id_size,
        layout->hand_id_size,
        layout->normalized_x_size,
        layout->normalized_y_size,
        layout->z_total_size,
        layout->pressure_candidate_size,
        layout->z_density_size,
    };
    const uint32_t expected_widths[] = {
        sizeof(uint64_t),
        sizeof(double),
        sizeof(uint32_t),
        sizeof(uint32_t),
        sizeof(uint32_t),
        sizeof(int32_t),
        sizeof(float),
        sizeof(float),
        sizeof(float),
        sizeof(float),
        sizeof(float),
    };

    for (size_t index = 0; index < sizeof(offsets) / sizeof(offsets[0]); ++index) {
        if (widths[index] != expected_widths[index] ||
            !field_fits(layout->record_size, offsets[index], widths[index])) {
            return false;
        }
    }
    /* There is no caller-selectable layout family in Phase 2 ABI v1. */
    return memcmp(layout, &g_source_layout, sizeof(*layout)) == 0;
}

static void copy_u32(
    const unsigned char *record,
    uint32_t offset,
    uint32_t *out_value
) {
    memcpy(out_value, record + offset, sizeof(*out_value));
}

static void copy_i32(
    const unsigned char *record,
    uint32_t offset,
    int32_t *out_value
) {
    memcpy(out_value, record + offset, sizeof(*out_value));
}

static void copy_float_and_bits(
    const unsigned char *record,
    uint32_t offset,
    float *out_value,
    uint32_t *out_bits
) {
    memcpy(out_bits, record + offset, sizeof(*out_bits));
    memcpy(out_value, out_bits, sizeof(*out_value));
}

void mt_phase2_decode_contacts(
    const void *records,
    uintptr_t raw_touch_count_register,
    uintptr_t raw_frame_register,
    double device_timestamp,
    mt_phase2_frame_t *out_frame,
    mt_phase2_decode_result_t *out_result
) {
    if (out_frame == NULL || out_result == NULL) {
        return;
    }
    memset(out_result, 0, sizeof(*out_result));
    out_frame->decode_status = MT_PHASE2_DECODE_OK;
    out_frame->layout_profile_id = MT_PHASE2_VERIFIED_PROFILE_ID;
    out_frame->copied_touch_count = 0;

    const mt_phase2_source_layout_t *layout = &g_source_layout;
    if (!mt_phase2_validate_source_layout(layout) ||
        raw_touch_count_register > layout->maximum_touch_count ||
        raw_touch_count_register > SIZE_MAX / layout->record_size) {
        out_frame->decode_status |= MT_PHASE2_DECODE_INVALID_COUNT;
        return;
    }
    if (raw_touch_count_register > 0 && records == NULL) {
        out_frame->decode_status |= MT_PHASE2_DECODE_NULL_RECORDS;
        return;
    }

    const unsigned char *source = records;
    const uint32_t all_copied =
        MT_PHASE2_TOUCH_PATH_INDEX_COPIED |
        MT_PHASE2_TOUCH_STATE_COPIED |
        MT_PHASE2_TOUCH_FINGER_ID_COPIED |
        MT_PHASE2_TOUCH_HAND_ID_COPIED |
        MT_PHASE2_TOUCH_NORMALIZED_X_COPIED |
        MT_PHASE2_TOUCH_NORMALIZED_Y_COPIED |
        MT_PHASE2_TOUCH_Z_TOTAL_COPIED |
        MT_PHASE2_TOUCH_PRESSURE_CANDIDATE_COPIED |
        MT_PHASE2_TOUCH_Z_DENSITY_COPIED;

    uint64_t callback_timestamp_bits = 0;
    memcpy(
        &callback_timestamp_bits,
        &device_timestamp,
        sizeof(callback_timestamp_bits)
    );

    for (uintptr_t index = 0; index < raw_touch_count_register; ++index) {
        const unsigned char *record = source + (index * layout->record_size);
        mt_phase2_touch_t *touch = &out_frame->touches[index];
        touch->copied_fields = all_copied;
        copy_u32(record, layout->path_index_offset, &touch->path_index);
        copy_u32(record, layout->state_offset, &touch->state);
        copy_u32(record, layout->finger_id_offset, &touch->finger_id);
        copy_i32(record, layout->hand_id_offset, &touch->hand_id);
        copy_float_and_bits(
            record,
            layout->normalized_x_offset,
            &touch->normalized_x,
            &touch->normalized_x_bits
        );
        copy_float_and_bits(
            record,
            layout->normalized_y_offset,
            &touch->normalized_y,
            &touch->normalized_y_bits
        );
        copy_float_and_bits(
            record,
            layout->z_total_offset,
            &touch->z_total,
            &touch->z_total_bits
        );
        copy_float_and_bits(
            record,
            layout->pressure_candidate_offset,
            &touch->pressure_candidate,
            &touch->pressure_candidate_bits
        );
        copy_float_and_bits(
            record,
            layout->z_density_offset,
            &touch->z_density,
            &touch->z_density_bits
        );

        uint64_t record_frame = 0;
        uint64_t record_timestamp_bits = 0;
        memcpy(
            &record_frame,
            record + layout->record_frame_offset,
            sizeof(record_frame)
        );
        memcpy(
            &record_timestamp_bits,
            record + layout->record_timestamp_offset,
            sizeof(record_timestamp_bits)
        );
        if (record_frame != (uint64_t)raw_frame_register) {
            out_result->record_frame_mismatches += 1;
        }
        if (record_timestamp_bits != callback_timestamp_bits) {
            out_result->record_timestamp_mismatches += 1;
        }
        if (touch->state > 7) {
            out_result->invalid_states += 1;
        }
        if (touch->pressure_candidate == 43690.0f) {
            out_result->pressure_sentinels += 1;
        }
        if (!isfinite(touch->normalized_x) ||
            !isfinite(touch->normalized_y) ||
            !isfinite(touch->z_total) ||
            !isfinite(touch->pressure_candidate) ||
            !isfinite(touch->z_density)) {
            out_result->nonfinite_touches += 1;
        }
    }

    out_frame->copied_touch_count = (uint32_t)raw_touch_count_register;
    out_result->copied_touches = (uint64_t)raw_touch_count_register;
    if (out_result->record_frame_mismatches != 0) {
        out_frame->decode_status |= MT_PHASE2_DECODE_RECORD_FRAME_MISMATCH;
    }
    if (out_result->record_timestamp_mismatches != 0) {
        out_frame->decode_status |= MT_PHASE2_DECODE_RECORD_TIMESTAMP_MISMATCH;
    }
    if (out_result->invalid_states != 0) {
        out_frame->decode_status |= MT_PHASE2_DECODE_INVALID_STATE;
    }
    if (out_result->pressure_sentinels != 0) {
        out_frame->decode_status |= MT_PHASE2_DECODE_PRESSURE_SENTINEL;
    }
    if (out_result->nonfinite_touches != 0) {
        out_frame->decode_status |= MT_PHASE2_DECODE_NONFINITE_SCALAR;
    }
}

static bool read_sysctl_string(
    const char *name,
    char *buffer,
    size_t buffer_size
) {
    if (buffer == NULL || buffer_size == 0) {
        return false;
    }
    size_t size = buffer_size;
    if (sysctlbyname(name, buffer, &size, NULL, 0) != 0 ||
        size == 0 || size > buffer_size) {
        return false;
    }
    buffer[buffer_size - 1] = '\0';
    return true;
}

static bool framework_bundle_version_matches(void) {
    const UInt8 *path = (const UInt8 *)MT_PHASE2_FRAMEWORK_BUNDLE_PATH;
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(
        kCFAllocatorDefault,
        path,
        (CFIndex)strlen((const char *)path),
        true
    );
    if (url == NULL) {
        return false;
    }
    CFBundleRef bundle = CFBundleCreate(kCFAllocatorDefault, url);
    CFRelease(url);
    if (bundle == NULL) {
        return false;
    }

    CFTypeRef value = CFBundleGetValueForInfoDictionaryKey(
        bundle,
        kCFBundleVersionKey
    );
    char version[64] = {0};
    bool copied = value != NULL && CFGetTypeID(value) == CFStringGetTypeID() &&
        CFStringGetCString(
            (CFStringRef)value,
            version,
            sizeof(version),
            kCFStringEncodingUTF8
        );
    CFRelease(bundle);
    return copied && strcmp(version, MT_PHASE2_VERIFIED_FRAMEWORK_VERSION) == 0;
}

static CFDataRef copy_file_data(const char *path) {
    int descriptor = open(path, O_RDONLY | O_CLOEXEC);
    if (descriptor < 0) {
        return NULL;
    }
    struct stat status = {0};
    if (fstat(descriptor, &status) != 0 || status.st_size <= 0 ||
        status.st_size > 1024 * 1024) {
        (void)close(descriptor);
        return NULL;
    }
    size_t size = (size_t)status.st_size;
    unsigned char *bytes = malloc(size);
    if (bytes == NULL) {
        (void)close(descriptor);
        return NULL;
    }
    size_t copied = 0;
    while (copied < size) {
        ssize_t amount = read(descriptor, bytes + copied, size - copied);
        if (amount < 0 && errno == EINTR) {
            continue;
        }
        if (amount <= 0) {
            free(bytes);
            (void)close(descriptor);
            return NULL;
        }
        copied += (size_t)amount;
    }
    (void)close(descriptor);
    CFDataRef data = CFDataCreate(
        kCFAllocatorDefault,
        bytes,
        (CFIndex)size
    );
    free(bytes);
    return data;
}

static bool product_build_matches(void) {
    CFDataRef data = copy_file_data(MT_PHASE2_SYSTEM_VERSION_PATH);
    if (data == NULL) {
        return false;
    }
    CFErrorRef error = NULL;
    CFPropertyListRef property_list = CFPropertyListCreateWithData(
        kCFAllocatorDefault,
        data,
        kCFPropertyListImmutable,
        NULL,
        &error
    );
    CFRelease(data);
    if (error != NULL) {
        CFRelease(error);
    }
    if (property_list == NULL ||
        CFGetTypeID(property_list) != CFDictionaryGetTypeID()) {
        if (property_list != NULL) {
            CFRelease(property_list);
        }
        return false;
    }
    CFTypeRef value = CFDictionaryGetValue(
        (CFDictionaryRef)property_list,
        CFSTR("ProductBuildVersion")
    );
    char build[64] = {0};
    bool copied = value != NULL && CFGetTypeID(value) == CFStringGetTypeID() &&
        CFStringGetCString(
            (CFStringRef)value,
            build,
            sizeof(build),
            kCFStringEncodingUTF8
        );
    CFRelease(property_list);
    return copied && strcmp(build, MT_PHASE2_VERIFIED_PRODUCT_BUILD) == 0;
}

static bool framework_image_uuid_matches(void *framework_handle) {
    if (framework_handle == NULL) {
        return false;
    }
    (void)dlerror();
    void *symbol = dlsym(framework_handle, "MTGetPathFrame");
    if (dlerror() != NULL || symbol == NULL) {
        return false;
    }

    Dl_info info = {0};
    if (dladdr(symbol, &info) == 0 || info.dli_fbase == NULL) {
        return false;
    }
    const struct mach_header_64 *header = info.dli_fbase;
    if (header->magic != MH_MAGIC_64 || header->sizeofcmds > 1024u * 1024u) {
        return false;
    }

    const unsigned char *cursor = (const unsigned char *)(header + 1);
    size_t remaining = header->sizeofcmds;
    for (uint32_t index = 0; index < header->ncmds; ++index) {
        if (remaining < sizeof(struct load_command)) {
            return false;
        }
        const struct load_command *command = (const struct load_command *)cursor;
        if (command->cmdsize < sizeof(*command) || command->cmdsize > remaining) {
            return false;
        }
        if (command->cmd == LC_UUID) {
            if (command->cmdsize < sizeof(struct uuid_command)) {
                return false;
            }
            const struct uuid_command *uuid =
                (const struct uuid_command *)command;
            return memcmp(
                uuid->uuid,
                g_verified_framework_uuid,
                sizeof(g_verified_framework_uuid)
            ) == 0;
        }
        cursor += command->cmdsize;
        remaining -= command->cmdsize;
    }
    return false;
}

bool mt_phase2_native_target_matches(
    void *framework_handle,
    char *error_buffer,
    size_t error_buffer_size
) {
    char hardware_model[128] = {0};
    if (!read_sysctl_string("hw.model", hardware_model, sizeof(hardware_model))) {
        write_profile_error(error_buffer, error_buffer_size, "could not read hw.model");
        return false;
    }
    if (strcmp(hardware_model, MT_PHASE2_VERIFIED_HARDWARE_MODEL) != 0) {
        write_profile_error(
            error_buffer,
            error_buffer_size,
            "Phase 2 hardware mismatch: expected %s, observed %s",
            MT_PHASE2_VERIFIED_HARDWARE_MODEL,
            hardware_model
        );
        return false;
    }

    char os_build[128] = {0};
    if (!read_sysctl_string("kern.osversion", os_build, sizeof(os_build))) {
        write_profile_error(
            error_buffer,
            error_buffer_size,
            "could not read kern.osversion"
        );
        return false;
    }
    if (strcmp(os_build, MT_PHASE2_VERIFIED_KERNEL_BUILD) != 0) {
        write_profile_error(
            error_buffer,
            error_buffer_size,
            "Phase 2 kernel build mismatch: expected %s, observed %s",
            MT_PHASE2_VERIFIED_KERNEL_BUILD,
            os_build
        );
        return false;
    }

    if (!product_build_matches()) {
        write_profile_error(
            error_buffer,
            error_buffer_size,
            "Phase 2 product build is not %s",
            MT_PHASE2_VERIFIED_PRODUCT_BUILD
        );
        return false;
    }

    if (!framework_bundle_version_matches()) {
        write_profile_error(
            error_buffer,
            error_buffer_size,
            "Phase 2 framework bundle version is not %s",
            MT_PHASE2_VERIFIED_FRAMEWORK_VERSION
        );
        return false;
    }
    if (!framework_image_uuid_matches(framework_handle)) {
        write_profile_error(
            error_buffer,
            error_buffer_size,
            "Phase 2 framework image UUID does not match the verified profile"
        );
        return false;
    }
    if (!mt_phase2_validate_source_layout(&g_source_layout)) {
        write_profile_error(
            error_buffer,
            error_buffer_size,
            "compiled Phase 2 source layout failed internal validation"
        );
        return false;
    }
    return true;
}
