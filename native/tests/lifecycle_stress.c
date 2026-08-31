#include "mt_phase2.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static void short_pause(void) {
    const struct timespec pause = {
        .tv_sec = 0,
        .tv_nsec = 10000000L,
    };
    while (nanosleep(&pause, NULL) != 0 && errno == EINTR) {
    }
}

int main(int argc, char **argv) {
    unsigned long cycles = 25;
    if (argc == 2) {
        char *end = NULL;
        cycles = strtoul(argv[1], &end, 10);
        if (end == argv[1] || *end != '\0' || cycles == 0 || cycles > 10000) {
            fprintf(stderr, "cycle count must be in 1..10000\n");
            return 2;
        }
    }

    char error[1024] = {0};
    mt_phase1_capture_t *capture = NULL;
    int32_t status = mt_phase1_capture_create(&capture, error, sizeof(error));
    if (status != MT_PHASE1_OK) {
        fprintf(stderr, "create failed (%d): %s\n", status, error);
        return 1;
    }

    for (unsigned long cycle = 1; cycle <= cycles; ++cycle) {
        int32_t native_start = -1;
        status = mt_phase1_capture_start(
            capture,
            0,
            &native_start,
            error,
            sizeof(error)
        );
        if (status != MT_PHASE1_OK || native_start != 0) {
            fprintf(
                stderr,
                "cycle %lu start failed (bridge=%d native=%d): %s\n",
                cycle,
                status,
                native_start,
                error
            );
            return 1;
        }

        short_pause();

        int32_t native_stop = -1;
        status = mt_phase1_capture_stop(
            capture,
            &native_stop,
            error,
            sizeof(error)
        );
        if (status != MT_PHASE1_OK || native_stop != 0) {
            fprintf(
                stderr,
                "cycle %lu stop failed (bridge=%d native=%d): %s\n",
                cycle,
                status,
                native_stop,
                error
            );
            return 1;
        }

        mt_phase1_capture_stats_t stats = {0};
        status = mt_phase1_capture_get_stats(capture, &stats);
        if (status != MT_PHASE1_OK || stats.in_flight_callback_count != 0) {
            fprintf(
                stderr,
                "cycle %lu did not quiesce (bridge=%d in_flight=%llu)\n",
                cycle,
                status,
                (unsigned long long)stats.in_flight_callback_count
            );
            return 1;
        }
    }

    status = mt_phase2_capture_enable_profile(
        capture,
        UINT32_MAX,
        error,
        sizeof(error)
    );
    if (status != MT_PHASE1_ERROR_PHASE2_PROFILE) {
        fprintf(stderr, "unknown Phase 2 profile was not rejected\n");
        return 1;
    }
    status = mt_phase2_capture_enable_profile(
        capture,
        MT_PHASE2_VERIFIED_PROFILE_ID,
        error,
        sizeof(error)
    );
    if (status != MT_PHASE1_OK) {
        fprintf(stderr, "enable Phase 2 failed (%d): %s\n", status, error);
        return 1;
    }
    status = mt_phase2_capture_enable_profile(
        capture,
        MT_PHASE2_VERIFIED_PROFILE_ID,
        error,
        sizeof(error)
    );
    if (status != MT_PHASE1_OK) {
        fprintf(stderr, "idempotent Phase 2 enable failed (%d): %s\n", status, error);
        return 1;
    }

    int32_t native_start = -1;
    status = mt_phase1_capture_start(
        capture,
        1,
        &native_start,
        error,
        sizeof(error)
    );
    if (status != MT_PHASE1_ERROR_PHASE2_PROFILE) {
        fprintf(stderr, "Phase 2 accepted unverified start option one\n");
        return 1;
    }

    const unsigned long phase2_cycles = cycles < 10 ? cycles : 10;
    for (unsigned long cycle = 1; cycle <= phase2_cycles; ++cycle) {
        native_start = -1;
        status = mt_phase1_capture_start(
            capture,
            0,
            &native_start,
            error,
            sizeof(error)
        );
        if (status != MT_PHASE1_OK || native_start != 0) {
            fprintf(stderr, "Phase 2 cycle %lu start failed: %s\n", cycle, error);
            return 1;
        }
        status = mt_phase2_capture_enable_profile(
            capture,
            MT_PHASE2_VERIFIED_PROFILE_ID,
            error,
            sizeof(error)
        );
        if (status != MT_PHASE1_ERROR_INVALID_STATE) {
            fprintf(stderr, "Phase 2 reconfiguration while running was accepted\n");
            return 1;
        }
        short_pause();
        int32_t native_stop = -1;
        status = mt_phase1_capture_stop(
            capture,
            &native_stop,
            error,
            sizeof(error)
        );
        if (status != MT_PHASE1_OK || native_stop != 0) {
            fprintf(stderr, "Phase 2 cycle %lu stop failed: %s\n", cycle, error);
            return 1;
        }
        mt_phase2_capture_stats_t phase2_stats = {0};
        status = mt_phase2_capture_get_stats(capture, &phase2_stats);
        if (status != MT_PHASE1_OK) {
            fprintf(stderr, "Phase 2 stats failed on cycle %lu\n", cycle);
            return 1;
        }
    }

    status = mt_phase1_capture_destroy(capture, error, sizeof(error));
    if (status != MT_PHASE1_OK) {
        fprintf(stderr, "destroy failed (%d): %s\n", status, error);
        return 1;
    }

    printf(
        "completed %lu Phase 1 and %lu Phase 2 lifecycle cycles safely\n",
        cycles,
        phase2_cycles
    );
    return 0;
}
