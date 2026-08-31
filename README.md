# Clean-room MacBook trackpad scale diagnostics

This repository has a verified Phase 1 transport and an exact-target Phase 2
pressure diagnostic. Phase 2 deliberately stops at an uncalibrated raw
candidate: it does not implement tare, smoothing, grams, bottle logic, or
hydration behavior.

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
phase2_probe + phase2_analysis (diagnostic only)

Future hydration application: intentionally not implemented
```

The native callback copies selected values before returning. Python sees only
immutable project-owned snapshots. Phase 2 has its own bounded queue, while the
legacy Phase 1 ABI and queue remain unchanged.

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
- **No premature physical meaning:** `zTotal`, `zDensity`, and the pressure
  candidate are raw coordinates. Phase 2 cannot output grams.

See [docs/ABI_VERIFICATION.md](docs/ABI_VERIFICATION.md),
[docs/PHASE2_ABI_VERIFICATION.md](docs/PHASE2_ABI_VERIFICATION.md), and
[docs/PHASE2_STATUS.md](docs/PHASE2_STATUS.md).
