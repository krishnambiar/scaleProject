# Phase 2 status

## Implemented and verified

- The callback-record container is exactly 96 bytes on the checked-in target,
  with a maximum count of 32.
- The smallest selected layout copies identity, state, normalized X/Y,
  `zTotal`, the `0x34` pressure candidate, and `zDensity`.
- Native admission fails closed on hardware, product build, kernel build,
  framework bundle, loaded image UUID, device type, capability, profile, and
  start option.
- Source memory is decoded only inside the native callback and selected values
  are copied into a separate bounded Phase 2 queue.
- The Phase 1 ABI remains version 2 and is usable without enabling Phase 2.
- Native/Python ABI sizes and the output-layout fingerprint match.
- Guard-page, malformed-input, poison-pointer, lifecycle, and callback-race
  tests pass with ASan/UBSan.
- A local hands-off smoke test started and stopped Phase 2 cleanly. It observed
  zero frames, consistent with the already verified idle-frame suppression.
- A separate unconfirmed smoke run observed live one-contact records with state
  `4` and finite X/Y, `zTotal`, pressure-candidate, and `zDensity` values. Its
  record-local frame/timestamp checks and queue accounting were clean. Because
  the operator did not follow labelled pressure stages, the diagnostic correctly
  classified it as incomplete; those values are not monotonicity evidence.

## Controlled physical result

The three-cycle physical experiment was completed on the checked-in target on
2026-08-31. The original evidence is the local, git-ignored
`artifacts/phase2-pressure.json` (SHA-256
`370c03f642b71b2e4d99274f2f56ba84ce26de13aa5143b8a7c687220a7bf3d0`).

An initial analysis incorrectly treated the full
`(path_index, finger_id, hand_id)` tuple as a stable contact identity. In the
raw data, `finger_id` briefly changed from `2` to `7` while `path_index`, touch
count, state, X/Y, and the candidate stream remained continuous. The corrected
`single-contact-path-continuity-v2` rule therefore:

- uses `path_index` as the contact-track continuity key;
- keeps `finger_id` and `hand_id` as descriptive raw classification codes;
- still rejects actual multiple contacts, zero-contact interruptions,
  nonsteady states, and path changes; and
- includes same-path classification changes in the primary sample.

Reassessment from the preserved raw frames produced these stage medians:

| Cycle | REST | LIGHT | MEDIUM | HARDER | Direction |
|---:|---:|---:|---:|---:|---|
| 1 | 9 | 51 | 82 | 102 | strictly increasing |
| 2 | 14 | 57 | 90 | 131 | strictly increasing |
| 3 | 33 | 63 | 82 | 96 | strictly increasing |

All 16,334 attempted frames materialized with no queue loss, ABI-integrity
finding, non-finite scalar, or sentinel. All releases and no-contact baselines
were confirmed, and all pressure plateaus retained one stable path. The
automatic result is `human_review_required`, as designed; it never declares an
automatic numeric pass.

Human review advances the raw field as a candidate for further validation on
this exact Mac because its medians followed every operator-labelled stage. The
evidence is not an independent pressure reference or calibration: Cycle 2's
HARDER range overlaps lower levels, REST drifts between cycles, and the current
layout has no verified contact-area/shape field. Phase 2 therefore documents an
ordered association in this trial, not pressure semantics, grams, accuracy, or
production measurement quality.

The original capture can be reassessed reproducibly without touching the
private framework:

```bash
PYTHONPATH=src python3 -m trackpad_scale.phase2_probe \
  --reanalyze-json artifacts/phase2-pressure.json
```

## Evidence the report provides

For each settled plateau, the tool reports raw sample/bit-pattern counts,
minimum, maximum, median, quartiles, IQR, MAD, duration, slope, state and
identity histograms, X/Y, `zTotal`, `zDensity`, and candidate correlations. It
also reports adjacent median direction and raw-range overlap, same-label drift
across cycles, release confirmation, every transition/errored frame, and full
queue accounting.

There is no invented numeric pass threshold. Hard reason codes reject an ABI
mismatch, sentinel, non-finite, absent, zero-only, or constant field. Missing
stages or release evidence, extra contacts, contact interruption, path/state
changes, ties, direction changes, or inconsistent cycle direction produce an
inconclusive result. `finger_id` and `hand_id` variation is reported but does not
override a verified single touch on a continuous path. Adjacent raw-range
overlap is reported descriptively rather than treated as an invented
zero-overlap threshold. A clean ordinal result still requires human review of
X/Y and available geometry indicators.

## Explicit stopping point

This phase does not tare, stabilize, calibrate, convert to grams, infer bottle
weight, or generate hydration events. All values remain raw sensor coordinates,
not grams. Phase 2 stops with the raw field advanced for further validation and
the remaining drift/geometry confounds documented; no Phase 3 work is included
here.
