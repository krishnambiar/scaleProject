CC := xcrun clang
CFLAGS := -std=c11 -O2 -g -Wall -Wextra -Werror -fvisibility=hidden
CPPFLAGS := -Inative/include
LDFLAGS := -dynamiclib -pthread

BUILD_DIR := build
LIBRARY := $(BUILD_DIR)/libmt_phase1.dylib
SOURCE := native/src/mt_phase1.c
HEADER := native/include/mt_phase1.h
STRESS_SOURCE := native/tests/lifecycle_stress.c
STRESS_BINARY := $(BUILD_DIR)/phase1_lifecycle_stress
GATE_STRESS_SOURCE := native/tests/callback_gate_stress.c
GATE_STRESS_BINARY := $(BUILD_DIR)/phase1_callback_gate_stress

.PHONY: all clean test stress probe

all: $(LIBRARY)

$(LIBRARY): $(SOURCE) $(HEADER)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) \
		-install_name @rpath/libmt_phase1.dylib \
		-o $@ $(SOURCE)

test: $(LIBRARY)
	PYTHONPATH=src python3 -m unittest discover -s tests -v

$(STRESS_BINARY): $(SOURCE) $(HEADER) $(STRESS_SOURCE)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CPPFLAGS) -std=c11 -O1 -g -Wall -Wextra -Werror \
		-fno-omit-frame-pointer -fsanitize=address,undefined -pthread \
		-o $@ $(SOURCE) $(STRESS_SOURCE)

$(GATE_STRESS_BINARY): $(SOURCE) $(HEADER) $(GATE_STRESS_SOURCE)
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CPPFLAGS) -std=c11 -O1 -g -Wall -Wextra -Werror \
		-fno-omit-frame-pointer -fsanitize=address,undefined -pthread \
		-o $@ $(GATE_STRESS_SOURCE)

stress: $(STRESS_BINARY) $(GATE_STRESS_BINARY)
	ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
	UBSAN_OPTIONS=halt_on_error=1 \
		$(STRESS_BINARY) 25
	ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
	UBSAN_OPTIONS=halt_on_error=1 \
		$(GATE_STRESS_BINARY)

probe: $(LIBRARY)
	PYTHONPATH=src python3 -m trackpad_scale.phase1_probe --duration 10

clean:
	rm -f $(LIBRARY) $(STRESS_BINARY) $(GATE_STRESS_BINARY)
