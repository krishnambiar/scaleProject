# Clean-room MacBook trackpad scale diagnostics

This repository has a verified Phase 1 transport, an exact-target Phase 2
pressure diagnostic, a Phase 3 application boundary for immutable raw frames,
and an experimental Phase 4 tare/filter/stability engine. The pressure
candidate remains an uncalibrated sensor coordinate. Phase 4 is implemented
for replay and live tuning, but its thresholds are not yet accepted for
measurement use; the project does not implement grams, calibration, bottle
logic, or hydration behavior.

No TrackWeight or OpenMultitouchSupport source was searched, inspected, copied,
translated, or reproduced. The ABI evidence comes from the project
specification, local inspection of Apple's framework on this Mac, compiler
checks, guarded synthetic tests, and local runtime experiments.

## Current status

- Phase 1 framework/device/callback transport is verified on the checked-in
  target.
- Phase 2 establishes a 96-byte callback-record stride and copies only the
  evidence-backed identity, lifecycle, X/Y, `zTotal`, pressure-candidate, and
  `zDensity` scalars.
- Native and Python project-owned layouts agree exactly and are protected by a
  layout fingerprint.
- The guarded decoder, callback lifetime races, and 25 Phase 1 plus 10 Phase 2
  real start/stop cycles pass AddressSanitizer and UndefinedBehaviorSanitizer.
- A three-cycle operator-guided experiment produced strictly increasing
  REST/LIGHT/MEDIUM/HARDER medians in every cycle. That observation advances
  the raw field as a candidate for further validation on this Mac; it is not an
  independent pressure reference and remains unsuitable for gram claims.
- Phase 3 validates each copied transport record and returns a private-ABI-free
  `RawFrame` to application code. Invalid decode status, profile/count/mask
  disagreement, invalid state, sentinel/non-finite data, or changed binary32
  bits fail closed.
- Deterministic sanitizer tests prove both native queues are bounded, preserve
  FIFO order for sequentially injected callbacks, drop the oldest records at
  capacity, never block on queue-lock contention, and safely copy a 32-contact
  frame.
- An exact-target live check returned a two-contact application `RawFrame`, then
  stopped and closed with balanced accounting and zero drops or ABI findings;
  the immutable frame remained readable after native teardown.
- Phase 4 now performs an explicit reference-contact tare, freezes its robust
  median baseline, rejects unsupported continuity and transient states, applies
  median plus moving-average filtering, and requires repeated time-separated
  low-MAD/low-slope windows before publishing a raw pressure delta.
- Phase 4 has no built-in production defaults. Its packaged exact-target
  profile is immutable and explicitly `experimental_unvalidated`; every value
  is tied to the preserved Phase 2 capture and must be challenged by repeated
  live trials before acceptance.

All candidate values are raw sensor coordinates, not grams.

## Architecture

```text
MultitouchSupport.framework (private; runtime-loaded)
                    |
                    v
native/src/mt_phase1.c
  device ownership, callback admission, bounded queues, deterministic teardown
                    |
                    +---- Phase 1 metadata queue (ABI remains version 2)
                    |
                    v
native/src/mt_phase2_decode.c
  exact-target gate + byte-wise guarded decoder
  no Apple pointer escapes and no caller-provided offsets
                    |
                    v
native/include/mt_phase2.h (project-owned additive ABI)
                    |
                    v
NativePhase2Capture -> TouchDiagnosticSensor
                    |
                    v
Phase 3 integrity gate -> RawFrameSensor -> immutable RawFrame
                    |
                    v
PressureStabilizer (pure Python, private-ABI-free)
  explicit frozen tare -> transient gate -> median -> moving average
  -> rolling MAD/slope -> repeated stable-window confirmation
                    |
                    v
phase4_probe / phase4_replay (experimental diagnostics)

Phase 5 calibration and future hydration policy: intentionally not implemented
```

The native callback copies selected values before returning. Python sees only
immutable project-owned snapshots. Phase 2 has its own bounded queue, while the
legacy Phase 1 ABI and queue remain unchanged. The metadata queue holds 4,096
frames and the rich-frame queue holds 1,024. A full queue discards its oldest
record. If the shared queue mutex is momentarily unavailable, the callback
returns immediately and counts the incoming record as a contention drop; it
never waits for Python.

Application code should depend on `RawFrameSensor`, not the private framework
or the diagnostic transport model:

```python
from trackpad_scale import RawFrameSensor

with RawFrameSensor() as sensor:
    sensor.start()
    frame = sensor.read_frame(timeout=2.0)
    sensor.stop()

if frame is not None:
    for contact in frame.contacts:
        print(contact.path_index, contact.pressure_candidate_raw)
```

`pressure_candidate_raw`, `z_total_raw`, and `z_density_raw` have arbitrary raw
sensor units. `RawFrameSensor` performs no baseline subtraction, selection,
aggregation, filtering, stabilization, calibration, or unit conversion. The
separate `PressureStabilizer` consumes only these application-owned models, so
none of the private framework or diagnostic transport types leak into the
signal-processing layer.

The stabilizer requires every threshold explicitly. For the one exact-target
experiment profile:

```python
from trackpad_scale import PressureStabilizer
from trackpad_scale.phase4_profile import load_experimental_phase4_profile

profile = load_experimental_phase4_profile()
stabilizer = PressureStabilizer(profile.config)
```

Call `tare()` with a finite continuous window containing exactly one
`TOUCHING` path. A release, extra contact, path replacement, excessive position
change, or frame/time gap invalidates that tare and requires an explicit new
one. `restart_stability_search()` clears transition/filter history while
preserving the frozen baseline and stream-continuity checks. `process()` returns
an auditable status for every frame; `ingest()` returns a
`StablePressureMeasurement` only after the configured confirmation windows
agree. Negative deltas remain negative, and the baseline never adapts to hide
drift.

## Exact-target source layout

The compiled profile applies only to Mac model `Mac16,8`, arm64, macOS product
build `25D771280a`, kernel build `25D2128`, MultitouchSupport bundle `9430.5`,
and image UUID `40D691BB-9166-31E0-959E-351863FF09A0`.

| Byte offset | Copied encoding | Diagnostic meaning |
| ---: | --- | --- |
| `0x00` | 64-bit raw value | record frame token |
| `0x08` | IEEE-754 binary64 | record timestamp |
| `0x10` | 32-bit raw integer | path/contact identity |
| `0x14` | 32-bit raw integer | lifecycle state |
| `0x18` | 32-bit raw integer | finger identity/classification |
| `0x1c` | signed 32-bit raw integer | hand identity |
| `0x20`, `0x24` | IEEE-754 binary32 | normalized X/Y |
| `0x30` | IEEE-754 binary32 | `zTotal` candidate metric |
| `0x34` | IEEE-754 binary32 | pressure candidate, uncalibrated |
| `0x5c` | IEEE-754 binary32 | `zDensity` candidate metric |

Every other byte remains opaque. The bridge uses byte pointers plus `memcpy`;
it does not cast callback memory to an assumed Apple struct. A `copied_fields`
mask says only that evidence-backed bytes were copied. A value is usable only
when the frame's decode status is clean.

## Build and verify

Requirements: this exact target Mac, Apple Command Line Tools, and arm64 Python.

```bash
make
make test
make stress
```

`make test` runs the Python suite plus guarded native decoder and callback-race
tests under ASan/UBSan. `make stress` additionally performs real framework
lifecycle cycles with Phase 2 enabled.

## Repeat the Phase 2 experiment

From the repository root:

```bash
make
PYTHONPATH=src python3 -m trackpad_scale.phase2_probe \
  --cycles 3 \
  --json-out artifacts/phase2-pressure.json
```

For each cycle, the diagnostic asks the operator to mark and then confirm a
settled `NO_CONTACT -> REST -> LIGHT -> MEDIUM -> HARDER -> RELEASE` sequence.
Use one fingertip at one location from REST through HARDER. The collector keeps
draining while prompts are displayed, and classifies samples afterward using
the native callback's monotonic timestamp rather than Python poll time.

The JSON packet retains transitions, errored frames, exact float bit patterns,
target/profile/layout evidence, queue accounting, operator event times, and raw
frames. Plateau summaries use only clean, steady-state, single-contact samples
from one stable `path_index`. `finger_id` and `hand_id` remain descriptive raw
classification codes: changing one does not invent a second contact when the
verified touch count remains one. The report includes medians, quartiles, IQR,
MAD, slopes, X/Y and contact-metric correlations, adjacent direction, overlap,
and repeatability. It intentionally defines no numeric pass threshold.

Saved evidence can be reassessed after an analysis-rule correction without
loading the private framework or repeating the physical experiment:

```bash
PYTHONPATH=src python3 -m trackpad_scale.phase2_probe \
  --reanalyze-json artifacts/phase2-pressure.json
```

The command writes a separate `*-reassessed.json` sidecar and records the source
file's SHA-256; it refuses to overwrite the original raw evidence.

Sentinel, non-finite, absent, zero-only, constant, ABI-integrity, incomplete,
contact-confounded, and non-monotonic results stop before calibration. Even a
clean ordinal result remains subject to human review for movement/geometry
confounds.

## Exercise Phase 4

Replay the exact preserved Phase 2 evidence without loading the private
framework:

```bash
PYTHONPATH=src python3 -m trackpad_scale.phase4_replay \
  --source artifacts/phase2-pressure.json \
  --json-out artifacts/phase4-replay.json
```

Run one operator-guided live raw-domain trial on the checked-in target:

```bash
make phase4-probe
```

The live probe first freezes a same-contact resting-pressure tare, processes a
light-pressure transition without hiding any frames, explicitly restarts only
the stability/filter search, and then records a separate steady interval. It
writes the raw frames and decisions needed for reassessment. A single
publication is not Phase 4 acceptance: repeated baseline, steady-hold,
transition, jostle, movement, contact-loss, and cross-session trials still need
to establish or reject the exploratory thresholds.

## Why the design is defensible

- **Exact target, not folklore:** hardware, both OS build identities, framework
  bundle version, and loaded image UUID are checked before decoding.
- **Immutable native profile:** Python cannot supply offsets or widen the source
  record. A changed OS/framework requires a new evidence profile.
- **Additive compatibility:** Phase 1's ABI remains version 2; Phase 2 is a
  separate opt-in ABI and queue.
- **Fail-closed memory access:** count is bounded at 32, a positive count
  requires a non-null pointer, device mismatch prevents all dereferencing, and
  only individually selected scalars are copied.
- **Bit-preserving diagnostics:** each binary32 value keeps its exact source
  bits, allowing constant/sentinel evidence to be distinguished from display
  formatting.
- **Narrow application contract:** the Phase 3 conversion gate strips target
  profile/layout details from application frames while rechecking all transport
  integrity invariants. Diagnostic failures remain explicit exceptions.
- **No premature physical meaning:** `zTotal`, `zDensity`, and the pressure
  candidate are raw coordinates. Phase 4 cannot output grams or a calibration
  model.

See [docs/ABI_VERIFICATION.md](docs/ABI_VERIFICATION.md),
[docs/PHASE2_ABI_VERIFICATION.md](docs/PHASE2_ABI_VERIFICATION.md), and
[docs/PHASE2_STATUS.md](docs/PHASE2_STATUS.md), and
[docs/PHASE3_STATUS.md](docs/PHASE3_STATUS.md), and
[docs/PHASE4_STATUS.md](docs/PHASE4_STATUS.md).
