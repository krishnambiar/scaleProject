# Phase 5 status: calibration software, not a validated scale

Phase 5's mathematical workflow is implemented independently from the private
macOS interface. A real bottle-weight calibration is **not complete**.
The repository has no accepted Phase 4 live trials, no stable readings from
known reference masses, and no held-out mass trials. The existing Phase 2
REST/LIGHT/MEDIUM/HARDER capture records operator pressure instructions, not
mass labels. Its Phase 4 replay publishes one uncalibrated raw delta; it cannot
be assigned grams.

## Required evidence before a weight claim

1. Establish a safe, repeatable physical setup in which the bottle or reference
   mass produces a valid trackpad contact and tare without changing contact
   path, position, or contact count. Record failures as well as successes. The
   current Phase 4 protocol instead uses one continuous fingertip contact;
   replacing it with a bottle invalidates the tare, while leaving the finger
   in place introduces uncontrolled finger force.
2. Validate or revise Phase 4 stability parameters on repeated live trials.
   The packaged profile is still `experimental_unvalidated`.
3. Independently verify several reference masses and their provenance. Capture
   repeated independent placement/tare trials at each training mass, plus
   distinct interior held-out masses and sessions. Preserve raw evidence and
   hashes for each attempt, including non-publications.
4. Predeclare tolerances for within-mass spread, linear-fit residuals,
   meaningful nonlinearity, held-out per-trial error, and the allowed mass
   range. These values cannot be inferred from the old finger-pressure replay.
5. Fit on training masses only, choose linear versus piecewise from training
   evidence, freeze the method, and evaluate the untouched held-out masses
   once. Reject extrapolation and any failed quality or provenance gate.
6. Have the complete physical protocol and evidence independently reviewed
   before making an accuracy, supported-range, or grams claim.

## Implemented software boundary

```text
future physical known-mass capture files + manifest
  -> phase5_evidence: schema, SHA-256, target/profile, trial/session,
     stable-publication and transport-accounting consistency checks
  -> phase5_calibration: per-mass medians and repeat spread
     -> linear fit and training residuals
     -> training-only piecewise decision, if justified
     -> freeze method
     -> untouched held-out masses and every repeat
     -> bounded mathematical candidate or rejection
  -> phase5_assessment: versioned, non-overwriting offline report
```

The calibration layer imports only Phase 4's application-owned stable
measurement type, never private Apple types. `CalibrationConfig` requires every
tolerance and allowed mass bound explicitly; none is guessed from the old
pressure experiment. Training requires at least three mass levels with repeated
readings. The model-selection decision uses training-only residuals and
interior-mass cross-validation. The chosen method is then frozen: failed
held-out error does not trigger a fallback to the other method. Held-out mass
levels are distinct from training and inside its range. Their per-mass medians
**and every repeat** must pass; raw pressure ranges must not overlap. The
allowed operating interval must be covered by held-out masses, and each
included piecewise segment needs an interior held-out mass. Predictions outside
measured pressure or the narrower allowed mass interval return unavailable.

The evidence loader requires a separate Phase 5 physical-known-mass artifact
for each independent trial, not a relabelled Phase 4 probe or replay. Each file
is referenced by SHA-256 from a manifest, and the manifest must separate
training and held-out sessions. At least two sessions per mass/role are
required. It admits an explicitly measured zero-additional-mass trial, but
never infers 0 g from the Phase 4 fingertip tare. Exact JSON schemas are
documented in `src/trackpad_scale/phase5_evidence.py`.

The offline entry point is:

```bash
PYTHONPATH=src python3 -m trackpad_scale.phase5_assessment \
  --manifest PATH_TO_FUTURE_KNOWN_MASS_MANIFEST.json \
  --criteria PATH_TO_PREDECLARED_CRITERIA.json \
  --phase4-profile-id CALLER_CLAIMED_REVIEWED_PROFILE_ID \
  --json-out PATH_TO_NEW_CANDIDATE_REPORT.json
```

The criteria file must contain exactly the fields of `CalibrationConfig`:
`min_repeats_per_mass`, `max_repeated_mad_raw`,
`max_repeated_absolute_deviation_raw`,
`max_training_absolute_residual_grams`,
`nonlinear_residual_trigger_grams`,
`max_piecewise_training_cv_absolute_error_grams`,
`minimum_piecewise_training_cv_improvement_grams`,
`max_holdout_absolute_error_grams`, `allowed_mass_min_grams`, and
`allowed_mass_max_grams`. There are no bundled criteria values, accepted
Phase 4 profile, known-mass manifest, or acquisition writer. The current
`experimental_unvalidated` profile is refused before fitting.

The report records SHA-256 digests for the manifest, criteria file, and every
selected source artifact so the precise inputs can be reproduced. A digest
does **not** prove the criteria were chosen before held-out evidence was seen.

The software's mathematical acceptance result, when available, will mean only
that supplied observations satisfy the configured calculations. A study must
predeclare those criteria, but the software cannot verify when they were set.
It also cannot verify that the claimed masses were physically applied, that
all failed trials were
included, or that a bottle is actually being weighed. No production
`WeightMeasurement`, hydration event, or Phase 6 integration is authorized by
this status.

The evidence gate checks self-attested source JSON against hashes and recorded
fields, not raw-frame replay or physical truth. Its `validated_for_phase5`
status and contact-geometry identifier are *claims*, not an independently
reviewed trust anchor. A later acquisition implementation must preserve all
attempts, replay raw frames through the verified gates, bind a reviewed Phase 4
profile and physical protocol to exact hashes, and verify reference masses.
Until then even a passing offline report is labelled
`mathematical_candidate_only` with `public_weight_available: false`.

## Current local evidence

`artifacts/phase4-replay.json` is an offline check of the existing Phase 2
capture. It has one stable publication of 45.5 arbitrary raw pressure units,
but neither an independently known mass nor a bottle-contact protocol. No
`artifacts/phase4-live.json` or Phase 5 known-mass dataset was present when
Phase 5 work began on 2026-09-17.

The exact hardware, OS, and framework identity still matches the Phase 4
profile on 2026-09-17. That identity check does not validate pressure behavior
or the calibration protocol.
