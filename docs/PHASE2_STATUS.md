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

## Required before Phase 2 can be called behaviorally complete

The user must perform the physical pressure experiment. From the repository
root:

```bash
make
PYTHONPATH=src python3 -m trackpad_scale.phase2_probe \
  --cycles 3 \
  --json-out artifacts/phase2-pressure.json
```

For each prompt:

1. use exactly one fingertip;
2. keep the same fingertip and location from REST through HARDER;
3. press Return to begin changing the pose;
4. press Return again only after the requested pose is steady; and
5. release fully during RELEASE.

The callback collector runs while prompts are visible. Post-capture stage
membership uses the callback's native monotonic timestamp and recorded operator
boundaries, not delayed Python poll time.

## Evidence the report provides

For each settled plateau, the tool reports raw sample/bit-pattern counts,
minimum, maximum, median, quartiles, IQR, MAD, duration, slope, state and
identity histograms, X/Y, `zTotal`, `zDensity`, and candidate correlations. It
also reports adjacent median direction and raw-range overlap, same-label drift
across cycles, release confirmation, every transition/errored frame, and full
queue accounting.

There is no invented numeric pass threshold. Hard reason codes reject an ABI
mismatch, sentinel, non-finite, absent, zero-only, or constant field. Missing
stages or release evidence, extra contacts, contact interruption, identity/state
changes, ties, direction changes, or inconsistent cycle direction produce an
inconclusive result. Adjacent raw-range overlap is reported descriptively rather
than treated as an invented zero-overlap threshold. A clean ordinal result
still requires human review of X/Y and available geometry indicators.

## Explicit stopping point

This phase does not tare, stabilize, calibrate, convert to grams, infer bottle
weight, or generate hydration events. All values remain raw sensor coordinates,
not grams. Phase 3 or later work must wait until the physical report supports
the pressure candidate and the remaining confounds are documented.
