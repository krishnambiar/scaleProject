# Phase 4 status

## Status and stopping point

The Phase 4 pressure-domain core is algorithmically implemented. It can take
the immutable `RawFrame` produced by Phase 3, freeze an explicit reference
baseline, reject unsupported or discontinuous input, filter the baseline-
subtracted candidate, measure rolling dispersion and slope, and publish only
after repeated stable windows agree.

Phase 4 is **not experimentally accepted**. The checked-in parameters are an
exact-target exploratory profile, not validated measurement thresholds. A live
diagnostic is implemented, but no completed `artifacts/phase4-live.json` exists
yet. Acceptance requires repeated operator-guided trials on this Mac and review
of their saved raw evidence.

Every result remains an arbitrary raw pressure-domain coordinate. Phase 4 does
not calibrate, convert to grams, infer force or mass, weigh a bottle, or create
hydration events. Phase 5 has not started.

## Boundary and data flow

```text
Phase 3 RawFrameSensor
  immutable RawFrame + RawContact values
                    |
                    v
PressureStabilizer.tare(finite reference frames)
  validate continuity and freeze median baseline
                    |
                    v
PressureStabilizer.process(frame)
  subtract baseline -> reject transient -> median -> moving average
                    |
                    v
rolling MAD + OLS slope -> repeated agreeing windows
                    |
                    v
StablePressureMeasurement.pressure_delta_raw
  arbitrary raw sensor coordinate; never grams
```

Phase 4 is pure Python and depends only on the application-owned Phase 3 model.
It neither imports the native bridge nor knows the private callback ABI. This
keeps pressure policy independently testable and prevents application logic
from running on the native callback thread.

## Explicit frozen reference-contact tare

`PressureStabilizer.tare()` accepts a finite `Sequence` and examines only its
configured trailing window. Requiring a finite, indexable sequence prevents a
live or unbounded iterator from becoming an accidental unbounded collection.

A tare window is admitted only when all of the following hold:

- every frame has exactly one contact in the verified `TOUCHING` lifecycle
  state;
- sequence numbers are consecutive and host-monotonic timestamps strictly
  increase without exceeding the configured gap;
- `path_index` stays constant while finger and hand classification codes remain
  descriptive only;
- the contact remains within the configured normalized-position radius;
- the window contains enough samples and elapsed time; and
- raw pressure MAD, maximum absolute deviation, and absolute OLS slope stay
  within the explicit experimental limits.

The baseline is the window median and is frozen in `TareResult`. An empty frame
is never synthesized as pressure zero: the validated candidate exists on a
contact, so this phase tares an operator-selected **reference-contact
condition**, not a proven unloaded physical zero. Starting any new tare first
invalidates the previous baseline; a failed retare cannot silently leave an old
reference active.

The frozen baseline is never continuously adapted. Baseline-subtracted values
may be negative because REST drift is real evidence, not an error to clamp
away. The exploratory profile also expires its tare after a finite interval so
that a stale reference is not presented as current indefinitely.

## Continuity and fail-closed behavior

The implemented experiment covers one continuous contact path near the tare
position. During measurement, any zero/multiple-contact frame, non-`TOUCHING`
state, path change, sequence gap or reversal, excessive timestamp gap, position
departure, or expired tare invalidates the tare and requires an explicit new
one. This is deliberately stricter than the Phase 3 transport boundary, where
zero and multiple contacts are valid raw frames.

This restriction is important: Phase 4 does not yet support lifting and
replacing a contact, combining multiple contacts, or transferring a reference
between contact paths. Those behaviors require new evidence and policy rather
than an assumed aggregation rule. The update returned for an invalidating frame
retains the baseline that was rejected so the decision remains auditable, while
subsequent frames report `TARE_REQUIRED`.

An isolated pressure-domain departure is handled differently. Once the
outlier-history window is full, the current raw delta is compared with its
recent median using the larger of an absolute floor and a multiple of MAD. A
departure above that limit is reported as `TRANSIENT_REJECTED`; it clears the
filter and stability search but preserves the frozen tare. The rejected sample
cannot be published. If the new level persists, later samples build a fresh
filter run instead of being locked out forever.

## Filtering and stability

All filter parameters are required explicitly by `StabilizerConfig`; the class
has no production defaults. Its constructor rejects wrong types, non-finite or
negative limits, even median/outlier windows, impossible nanosecond durations,
an infeasible tare duration, a stability window too short for its sample count,
overlapping confirmation timing, and a tare lifetime too short for the minimum
warmup and confirmation sequence.

The accepted raw delta follows this bounded pipeline:

1. Subtract the frozen raw baseline without clamping.
2. Apply an odd-sized rolling median to suppress isolated excursions.
3. Apply a fixed-sized moving average to smooth the median output.
4. Keep only the configured time lookback plus at most one boundary sample.
5. Calculate unscaled median absolute deviation (MAD) and ordinary least-
   squares slope in raw units per second.
6. Treat a window as stable only when both metrics are within their limits.
7. Require the configured number of time-separated stable windows and require
   the full span of their centers to fit within the agreement limit.

The sample immediately at or before the rolling cutoff has a specific role. It
is retained in MAD and slope calculations so the metrics cover the entire time
horizon and cannot hide a just-pruned transition. If it lies before the cutoff,
it does not count toward the minimum in-window sample density. Sample-count
median/outlier/smoothing queues have fixed maximum lengths; the stability
history is time-bounded to this lookback plus the single boundary sample.

After publication, every later evaluated stable center is also compared with
the published center. This anchor prevents small pairwise changes from chaining
into unbounded drift while an old publication remains implicitly valid. Once
the configured agreement distance is exceeded, the prior publication is
retired and a complete new repeated-window confirmation is required.

`restart_stability_search()` is an explicit transition boundary. It clears the
outlier, median, smoothing, stability, confirmation, publication, and last-
update state while preserving the frozen tare and the last observed sequence
and timestamp. The next frame must therefore remain continuous. The live probe
uses this only after it has processed and recorded the operator's transition;
transition frames are not hidden, but they cannot seed the labelled settled
measurement interval.

## Exact-target exploratory profile

The bundled `exact_target_experimental_v1` profile is immutable and fail-
closed. Its loader rejects missing or unknown fields, broadened scope, a changed
status or provenance digest, physical-unit claims, and any indication that
calibration was performed.

It is restricted to:

| Target property | Required value |
| --- | --- |
| Hardware model | `Mac16,8` |
| Architecture | `arm64` |
| macOS product build | `25D771280a` |
| Kernel build | `25D2128` |
| MultitouchSupport bundle | `9430.5` |
| Framework image UUID | `40D691BB-9166-31E0-959E-351863FF09A0` |
| Phase 2 source SHA-256 | `370c03f642b71b2e4d99274f2f56ba84ce26de13aa5143b8a7c687220a7bf3d0` |
| Profile status | `experimental_unvalidated` |
| Profile scope | `exact_target_only` |

The live command compares the current target with this identity before opening
the sensor. The native and Phase 3 admission gates still apply afterward.

The starting parameters are intentionally explicit:

| Function | Exploratory setting |
| --- | ---: |
| Tare | 123 samples, at least 0.95 s |
| Tare limits | MAD 3, maximum deviation 3, absolute slope 3 raw/s |
| Median and smoothing | 31-sample median, 5-sample moving average |
| Transient gate | 31 samples, 3 x MAD, absolute floor 12 raw |
| Stability | 0.75 s, at least 90 in-window samples |
| Stability limits | MAD 3, absolute slope 3 raw/s |
| Confirmation | 2 windows, 0.75 s apart, center span at most 3 raw |
| Continuity | maximum sample gap 0.02 s |
| Position | maximum normalized displacement 0.01 |
| Tare lifetime | 15 s |

These numbers were selected to create a falsifiable next experiment from the
preserved Phase 2 data. They are not target acceptance criteria, accuracy
claims, recommended production settings, or transferable defaults.

## Offline evidence and replay

The profile records an offline check against the immutable, git-ignored
`artifacts/phase2-pressure.json`. The selected Cycle 1 REST tail covers sequence
1351 through 1473 (123 samples) and produced a raw baseline of 10, MAD 0,
maximum absolute deviation 0, and slope 0. The continuous replay then covers
sequence 1474 through 2892. With the bundled profile, the selected Cycle 1
LIGHT evidence first publishes at sequence 2459 / frame 2575 with a raw delta
of 45.5.

The checked-in replay diagnostic runs this verification without loading the
private framework:

```bash
PYTHONPATH=src python3 -m trackpad_scale.phase4_replay \
  --source artifacts/phase2-pressure.json \
  --json-out artifacts/phase4-replay.json
```

It first verifies the exact source SHA-256, Phase 2 schema and provenance,
recorded target, and transport profile. It parses all saved transport records
through `RawTouchFrame` and the Phase 3 integrity gate, even records outside the
selected replay range. It then requires consecutive profile-declared tare and
continuation ranges, processes every continuation frame, and compares the tare
and publication with the profile's exact expected checkpoints. A report is
written only to a new sidecar; the command refuses to overwrite either an
existing output or the source artifact.

The full local replay passed on 2026-09-10. It verified the source digest,
parsed all 16,334 saved frames through the Phase 3 gate, processed all 1,419
continuation frames, and produced exactly the one expected publication. This
is recorded in the git-ignored `artifacts/phase4-replay.json` report with SHA-256
`c0b11aae1a4933fbfb322d52a7937ac254b4c83153c25bae734c951d1c38a309`.
This demonstrates deterministic execution on one preserved capture; it does not
independently validate the thresholds or the meaning of the field. The live
diagnostic's JSON likewise retains raw frames and decisions, but a generalized
reassessment command for arbitrary future live artifacts is still pending.

## Live diagnostic

Run the operator-guided experiment on the exact target with:

```bash
make phase4-probe
```

or choose a unique evidence filename:

```bash
PYTHONPATH=src python3 -m trackpad_scale.phase4_probe \
  --transition-seconds 3 \
  --measurement-seconds 6 \
  --tare-timeout-seconds 12 \
  --json-out artifacts/phase4-live-TRIAL.json
```

The probe requires Force Touch support, discards queued pre-tare frames, obtains
one continuous reference-contact tare, processes and records the pressure
transition, explicitly restarts the stability search, and then records the
settled interval. Its JSON contains the complete profile and target identity,
raw application frames, tare evidence, status counts, decision changes,
publications, and Phase 1/2 transport statistics. It labels the result
`stable_raw_pressure_observed` or `no_stable_publication`; neither label means
the profile is accepted.

As of 2026-09-10, the command and deterministic fake-sensor tests exist, but no
completed live Phase 4 artifact is present. One successful live publication
would still be insufficient. Experimental acceptance should require repeated
trials across independent contact/tare sessions, preservation of every success
and failure, clean transport accounting, and human review of timing, position,
REST drift, rejected transients, unstable windows, and repeatability before the
profile status changes.

## Verification level

On 2026-09-10, `make test` passed both native ASan/UBSan test binaries and all
75 Python tests. Thirty-five of those Python tests directly cover the Phase 4
stabilizer, profile, live-probe orchestration, and offline replay.

The pure-Python tests cover:

- strict configuration and profile-schema validation, including refusal of
  broadened scope, changed provenance, calibration, or gram claims;
- explicit median tare, failed-retare invalidation, transient/dispersion/slope/
  duration/position/continuity rejection, and refusal to synthesize zero from
  no contact;
- median then moving-average behavior, negative unclamped deltas, outlier
  rejection and sustained-level recovery;
- rolling boundary-sample and in-window-density behavior, MAD and irregular-
  time OLS slope;
- repeated time-separated confirmation, disagreement restart, instability,
  published-center drift anchoring, and explicit transition restart;
- live-probe orchestration, raw-only output, capability/contact failure, and
  deterministic sensor teardown using a fake Phase 3 source; and
- offline replay provenance, whole-artifact Phase 3 validation, exact range and
  checkpoint enforcement, JSON-safe reporting, and non-overwriting output.

These tests establish algorithmic behavior, fail-closed state transitions, and
determinism. Synthetic constant streams and fake sensor frames are not physical
validation and do not justify the chosen numeric thresholds. The existing
native ASan/UBSan and Phase 3 tests continue to cover the transport below this
module; they do not validate Phase 4 measurement quality.

## Preserved caveats

- The candidate was advanced only for continued pressure-domain experiments on
  this exact Mac. It is not an independently verified force signal.
- Cycle 3 REST drifted from a first-half-second median of 49 to a last-half-
  second median of 14. A frozen tare exposes rather than corrects this drift.
- Cycle 2 HARDER contains a sustained 50-frame, approximately 0.399-second
  excursion below its MEDIUM range. The outlier policy must not relabel that
  sustained overlap as a harmless isolated spike.
- The evidence represents one operator, one device, one capture, and manually
  confirmed stage boundaries. Geometry, hysteresis, session-to-session drift,
  and long-term behavior remain unresolved.
- The offline Cycle 1 publication includes transition history and occurs only
  about 0.472 seconds into the labelled settled window. Its 45.5 raw delta is a
  local algorithm result, not the full-stage delta or a physical quantity.
- The current one-contact/same-path/same-position policy is a deliberately
  narrow experiment and is not yet a usable bottle-weighing interaction.
- A stable algorithmic output means only that configured raw-domain dispersion,
  slope, and agreement checks passed. It does not imply accuracy, separability,
  or calibration.
- The MacBook trackpad is not a certified scale. No supported mass range or
  safety-critical use is claimed.

Phase 4 stops here until repeated live evidence supports accepting or revising
the exploratory profile. Calibration and all Phase 5 work remain out of scope.
