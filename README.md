# MacBook Trackpad Scale - Phase 1

This repository currently implements only the first clean-room milestone:

1. dynamically load `MultitouchSupport.framework`;
2. resolve the minimum device/callback lifecycle symbols;
3. acquire the default device and query optional built-in/force capabilities;
4. register one callback and start/stop the device deterministically; and
5. copy frame number, touch count, and timestamp metadata into project-owned memory.

It does **not** define or dereference an Apple touch-record structure. It contains no pressure offsets, pressure parsing, calibration, grams, bottle logic, or hydration logic. No TrackWeight or OpenMultitouchSupport source was searched, inspected, copied, translated, or reproduced.

## Architecture

```text
MultitouchSupport.framework (private, runtime-loaded)
                  |
                  v
native/src/mt_phase1.c
  - owns the private device and callback
  - never dereferences the touch-record pointer
  - copies metadata into a bounded queue
                  |
                  v
project-owned C ABI in native/include/mt_phase1.h
                  |
                  v
TrackpadSensor (Python)
  - reads immutable FrameMetadata values
                  |
                  v
phase1_probe diagnostic
```

The native callback uses `pthread_mutex_trylock`; it records and drops a frame instead of waiting if the Python poller owns the queue lock. Queue overwrites and lock-contention drops are reported explicitly.

Callback lifetime is guarded outside the disposable capture object: an immortal admission gate and never-reused process-lifetime refcon token let stop detach the capture before waiting for already-admitted callbacks. A late callback can therefore be rejected without touching released capture memory. The fixed token pool permits 4,096 start attempts per process; exhaustion fails explicitly and asks for a fresh diagnostic process.

On the checked-in target only, local disassembly also established that a stop from the application thread is serialized through the framework's callback run loop before device release. That teardown fact is part of the exact-target ABI profile and must be re-established after an OS/framework change.

## Build and test

Requirements: macOS, Apple Command Line Tools, arm64 Python.

```bash
make
make test
make stress
```

`make stress` compiles the native lifecycle test with AddressSanitizer and UndefinedBehaviorSanitizer, then repeatedly registers, starts, stops, unregisters, checks callback quiescence, and destroys the capture.

The bridge deliberately refuses to compile for an architecture other than the locally inspected arm64 ABI. The diagnostic also compares the current OS build, hardware model, framework bundle version, and dyld image UUID with the checked-in target profile. A changed target requires a new ABI investigation; `--allow-unverified-target` exists only for explicit diagnostic work. The normal path likewise rejects every `MTDeviceStart` option value except the locally verified zero.

Both `TrackpadSensor` and the low-level native binding enforce that exact-target check, so a future Python application cannot silently bypass it. Re-verification requires an explicit diagnostic-only override.

## Run the diagnostic

Unlabelled observation:

```bash
PYTHONPATH=src python3 -m trackpad_scale.phase1_probe \
  --duration 10 \
  --json-out artifacts/phase1-observation.json
```

Physical 0/1/2/0 contact trial:

```bash
PYTHONPATH=src python3 -m trackpad_scale.phase1_probe \
  --guided \
  --json-out artifacts/phase1-guided.json
```

For the final controlled verification, run three operator-confirmed trials:

```bash
PYTHONPATH=src python3 -m trackpad_scale.phase1_probe \
  --guided --trials 3 --confirm-stages \
  --json-out artifacts/phase1-final-guided.json
```

Press Return only when ready for each countdown, then hold the requested contact configuration steady for the recorded interval. The hands-off stages account for the target's transition-only zero behavior.

The report is descriptive. The guide does not define a numeric reliability threshold, so the diagnostic does not invent one. It reports exact count histograms, frame/timestamp ordering, frame-ID gaps, callback-sequence gaps, callback rate, queue loss, and device mismatches. On the inspected target, continuous idle frames are suppressed; releasing the last contact may produce one terminal count-zero callback, so a quiet hands-off stage can correctly contain no frames.

If a framework, device, registration, start, cleanup, or operator-confirmation step fails, `--json-out` receives a failure record rather than silently losing the experiment.

## Important Phase 1 decisions

- **Native callback boundary:** callbacks arrive on a framework-owned thread. A tiny C bridge returns quickly and keeps Python/GIL behavior out of the private callback.
- **Opaque touch memory:** the touch pointer is accepted only so the machine ABI matches; it is immediately ignored and never stored.
- **Raw register containers:** local disassembly establishes where count and frame values arrive, but not Apple's private source typedef names. They remain labelled as raw 64-bit register values until experiments narrow the contract.
- **Runtime loading:** no private framework symbol is linked into the Python application.
- **Exact-target gate:** private ABI evidence is tied to one OS build, hardware model, architecture, framework version, and image UUID so an update cannot silently inherit old assumptions.
- **Explicit start options:** the diagnostic prints and records the exact 32-bit option value passed to `MTDeviceStart`; no other bit semantics are claimed.

See [docs/ABI_VERIFICATION.md](docs/ABI_VERIFICATION.md) for the evidence and remaining unknowns.
