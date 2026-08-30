#define MT_PHASE1_TESTING 1
#include "../src/mt_phase1.c"

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

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

static mt_phase1_capture_t *synthetic_capture(const void *context) {
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
    return capture;
}

static void install_synthetic_capture(mt_phase1_capture_t *capture) {
    (void)pthread_mutex_lock(&g_callback_gate_mutex);
    g_callback_capture = capture;
    g_callback_context = capture->callback_context;
    (void)pthread_mutex_unlock(&g_callback_gate_mutex);
}

static void *invoke_callback(void *argument) {
    const void *context = argument;
    mt_phase1_raw_callback(
        (void *)(uintptr_t)0x1000,
        NULL,
        1,
        1.0,
        1,
        (void *)context
    );
    return NULL;
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
    mt_phase1_capture_t *capture = synthetic_capture(context);
    install_synthetic_capture(capture);
    barrier_initialize();
    g_test_before_callback_admission = barrier_hook;

    pthread_t callback_thread;
    if (pthread_create(&callback_thread, NULL, invoke_callback, (void *)context) != 0) {
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
    mt_phase1_capture_t *capture = synthetic_capture(context);
    install_synthetic_capture(capture);
    barrier_initialize();
    g_test_after_callback_admission = barrier_hook;

    pthread_t callback_thread;
    if (pthread_create(&callback_thread, NULL, invoke_callback, (void *)context) != 0) {
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

int main(void) {
    test_pause_before_admission();
    test_pause_after_admission();
    puts("callback admission/destruction race tests passed");
    return 0;
}

