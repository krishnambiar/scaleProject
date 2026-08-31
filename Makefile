CC := xcrun clang
CFLAGS := -std=c11 -O2 -g -Wall -Wextra -Werror -fvisibility=hidden
CPPFLAGS := -Inative/include
LDFLAGS := -dynamiclib -pthread -framework CoreFoundation

BUILD_DIR := build
LIBRARY := $(BUILD_DIR)/libmt_phase1.dylib
SOURCE := native/src/mt_phase1.c
HEADER := native/include/mt_phase1.h
PHASE2_SOURCE := native/src/mt_phase2_decode.c
PHASE2_INTERNAL_HEADER := native/src/mt_phase2_internal.h
PHASE2_HEADER := native/include/mt_phase2.h
NATIVE_SOURCES := $(SOURCE) $(PHASE2_SOURCE)
STRESS_SOURCE := native/tests/lifecycle_stress.c
STRESS_BINARY := $(BUILD_DIR)/phase1_lifecycle_stress
GATE_STRESS_SOURCE := native/tests/callback_gate_stress.c
GATE_STRESS_BINARY := $(BUILD_DIR)/phase1_callback_gate_stress
PHASE2_TEST_SOURCE := native/tests/phase2_decode_test.c
PHASE2_TEST_BINARY := $(BUILD_DIR)/phase2_decode_test

.PHONY: all clean test stress probe phase2-probe

all: $(LIBRARY)

$(LIBRARY): $(NATIVE_SOURCES) $(HEADER) $(PHASE2_HEADER) $(PHASE2_INTERNAL_HEADER)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) \
		-install_name @rpath/libmt_phase1.dylib \
		-o $@ $(NATIVE_SOURCES)

test: $(LIBRARY) $(GATE_STRESS_BINARY) $(PHASE2_TEST_BINARY)
	ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
	UBSAN_OPTIONS=halt_on_error=1 \
		$(GATE_STRESS_BINARY)
	ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
	UBSAN_OPTIONS=halt_on_error=1 \
		$(PHASE2_TEST_BINARY)
	PYTHONPATH=src python3 -m unittest discover -s tests -v

$(STRESS_BINARY): $(NATIVE_SOURCES) $(HEADER) $(PHASE2_HEADER) $(PHASE2_INTERNAL_HEADER) $(STRESS_SOURCE)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CPPFLAGS) -std=c11 -O1 -g -Wall -Wextra -Werror \
		-fno-omit-frame-pointer -fsanitize=address,undefined -pthread \
		-framework CoreFoundation \
		-o $@ $(NATIVE_SOURCES) $(STRESS_SOURCE)

$(GATE_STRESS_BINARY): $(NATIVE_SOURCES) $(HEADER) $(PHASE2_HEADER) $(PHASE2_INTERNAL_HEADER) $(GATE_STRESS_SOURCE)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CPPFLAGS) -std=c11 -O1 -g -Wall -Wextra -Werror \
		-fno-omit-frame-pointer -fsanitize=address,undefined -pthread \
		-framework CoreFoundation \
		-o $@ $(GATE_STRESS_SOURCE) $(PHASE2_SOURCE)

$(PHASE2_TEST_BINARY): $(PHASE2_SOURCE) $(PHASE2_HEADER) $(PHASE2_INTERNAL_HEADER) $(PHASE2_TEST_SOURCE)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CPPFLAGS) -std=c11 -O1 -g -Wall -Wextra -Werror \
		-fno-omit-frame-pointer -fsanitize=address,undefined -pthread \
		-framework CoreFoundation \
		-o $@ $(PHASE2_SOURCE) $(PHASE2_TEST_SOURCE)

stress: $(STRESS_BINARY) $(GATE_STRESS_BINARY) $(PHASE2_TEST_BINARY)
	ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
	UBSAN_OPTIONS=halt_on_error=1 \
		$(STRESS_BINARY) 25
	ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
	UBSAN_OPTIONS=halt_on_error=1 \
		$(GATE_STRESS_BINARY)
	ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
	UBSAN_OPTIONS=halt_on_error=1 \
		$(PHASE2_TEST_BINARY)

probe: $(LIBRARY)
	PYTHONPATH=src python3 -m trackpad_scale.phase1_probe --duration 10

phase2-probe: $(LIBRARY)
	PYTHONPATH=src python3 -m trackpad_scale.phase2_probe \
		--cycles 3 --json-out artifacts/phase2-pressure.json

clean:
	rm -f $(LIBRARY) $(STRESS_BINARY) $(GATE_STRESS_BINARY) $(PHASE2_TEST_BINARY)
