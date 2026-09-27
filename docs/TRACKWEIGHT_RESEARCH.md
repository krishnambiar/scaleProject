# TrackWeight reference review

The user explicitly authorized reading TrackWeight and applying its research to
this project on 2026-09-21, superseding the previous source-isolation constraint.
The reference repositories were inspected in a temporary directory. No upstream
code, binary, dependency, asset, or Swift application was added to this project.
The native bridge, exact-target ABI checks, RawFrame boundary, and Phase 4/5
experiments retain their existing implementations.

## Sources reviewed

- [TrackWeight README, commit e322cae](https://github.com/KrishKrosh/TrackWeight/blob/e322cae241d29afbee2860a6b585e9fe3974bd0c/README.md):
  conductive contact is required to receive force data; the author reports
  comparing readings against a conventional scale and finding a gram-valued
  pressure field. Metal objects may need an insulating layer.
- [ScaleViewModel.swift](https://github.com/KrishKrosh/TrackWeight/blob/e322cae241d29afbee2860a6b585e9fe3974bd0c/TrackWeight/ScaleViewModel.swift):
  the ordinary scale uses the first contact's pressure directly with an explicit
  zero offset, accepts multiple contacts, and resets that offset on an empty
  contact list. It does not establish a multi-contact summation algorithm.
- [WeighingViewModel.swift](https://github.com/KrishKrosh/TrackWeight/blob/e322cae241d29afbee2860a6b585e9fe3974bd0c/TrackWeight/WeighingViewModel.swift):
  the separate guided experiment uses ten-sample pressure averaging and timed
  state transitions. Those timers are unnecessary for an immediate display.
- [ContentView.swift](https://github.com/KrishKrosh/TrackWeight/blob/e322cae241d29afbee2860a6b585e9fe3974bd0c/TrackWeight/ContentView.swift)
  and [ScaleView.swift](https://github.com/KrishKrosh/TrackWeight/blob/e322cae241d29afbee2860a6b585e9fe3974bd0c/TrackWeight/ScaleView.swift):
  normal scale and experimental guided mode are distinct; Space zeroes the scale.
- TrackWeight's [resolved dependency](https://github.com/KrishKrosh/TrackWeight/blob/e322cae241d29afbee2860a6b585e9fe3974bd0c/TrackWeight.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved)
  pins OpenMultitouchSupport to `fb6991fa1ece8c5faa8eef49190beb2baf071694`.
  Its [contact layout](https://github.com/KrishKrosh/OpenMultitouchSupport/blob/fb6991fa1ece8c5faa8eef49190beb2baf071694/Framework/OpenMultitouchSupportXCF/OpenMTInternal.h),
  [touch wrapper](https://github.com/KrishKrosh/OpenMultitouchSupport/blob/fb6991fa1ece8c5faa8eef49190beb2baf071694/Framework/OpenMultitouchSupportXCF/OpenMTTouch.m),
  and [Swift manager](https://github.com/KrishKrosh/OpenMultitouchSupport/blob/fb6991fa1ece8c5faa8eef49190beb2baf071694/Sources/OpenMultitouchSupport/OMSManager.swift)
  route `pressure` through unchanged. The arm64 layout puts that field at `0x34`,
  matching our independently verified decoder. No conversion factor is applied
  in that data path. `total` and `density` describe capacitance, not object mass.

## Findings applied here

The readout was treating a usable force channel only as an abstract diagnostic
and telling the user to lift their finger as soon as any reading appeared.
That instruction defeats continuous object weighing. The GUI now describes
maintaining light contact, zeroing before adding an object, and using paper when
a conductive object introduces another touch.

`live_readout.py` is a new, independently written presentation layer over our
immutable RawFrames. It uses current START_IN_RANGE, HOVER_IN_RANGE, MAKE_TOUCH,
and TOUCHING records, averages up to ten recent frame totals, and optionally
subtracts the latest total captured by Zero. The experimental assumption is:

```
frame pressure = sum(current in-range contact pressures)
estimated grams = mean(recent frame pressures) - explicit display zero
```

Zero clears filter history and immediately gives zero. The offset remains fixed
while the initial reference path remains in range. That path prefers an active
touch when a session starts, persists through record reordering and lifecycle
reclassification, and is independent of added/removed object contacts. A
departed/replaced reference, missing/reordered frame, or more than 300 ms without
fresh data clears zero and filtering. A queued zero is tied to this epoch, so it
cannot zero a replacement finger or reused path after release. Departing records
are excluded because they can retain old pressure. Nonfinite/sentinel pressure
or duplicate current path IDs make the estimate unavailable and invalidate Zero.
Negative deltas remain visible instead of hiding finger pressure or removed load.

Summation is our independently implemented experimental interpretation of the
local capture below, not an algorithm copied from or validated by TrackWeight.
The diagnostic `estimate_basis` is `in_range_pressure_sum_1_to_1_unvalidated`;
each live reading includes its `contributing_paths` and aggregate `pressure_raw`.
All per-contact pressure remains available in the controller snapshot.

The last live estimate can remain visible as history after sensing ends. It is
never presented as a finalized or stable weight. The immediate display zero is
separate from Phase 4's experimental tare, and neither a display value nor a
successful calibration playground fit is admitted as Phase 5 physical evidence.

## Evidence limits

The 1:1 gram interpretation is an upstream empirical report, not a verified
Apple unit contract or a calibration of this particular Mac. The UI therefore
says `g · estimate`; demo says `g · simulated`. Raw models retain their original
field names and unit claims. No gain, supported mass range, or accuracy tolerance
was invented. Physical testing with known masses is still required to establish
accuracy, finger-pressure sensitivity, and repeatability on this hardware.
The GUI diagnostic records the estimate basis and `hardware_validated: false`;
its existing `public_weight_available: false` continues to mean no validated
weight publication, while `estimated_weight_available` indicates live display
availability.

The native ABI is deliberately unchanged. In particular, the reference header
uses a 32-bit frame member followed by alignment padding, whereas our local
investigation copied the 64-bit frame token at offset zero. Agreement on the
pressure offset is not evidence to change unrelated source widths, callback
signatures, lifecycle guarantees, or target compatibility checks.

## Verification for this change

- `make test`: 131 Python tests passed, plus the native callback/queue/lifetime
  and guarded-decoder tests under AddressSanitizer and UndefinedBehaviorSanitizer.
- `node --test tests/gui_view.test.cjs`: eight frontend behavior tests passed.
- Browser checks exercised Start, immediate readings, Space and button zeroing,
  historical labels after Stop, and switching from Demo to the real trackpad.
  The browser reported no warning or error logs.
- A live capture produced pressure readings through the new display layer with
  zero reported queue drops or decoder-integrity findings in the saved snapshot,
  `artifacts/gui-reference-update-live-check.json`. Capture was stopped afterward.
  This checks the data path; it is not a known-mass accuracy experiment.

## Frozen-reading investigation, 2026-09-21

The stopped GUI session was preserved as
`artifacts/gui-frozen-reading-before.json` (3,000 frames). Frames continued to
arrive with no drops or integrity findings in the reported transport snapshot.
There was one active contact in 2,993 frames. However, many of those frames also
contained pressure-bearing state-2 contacts, which the display ignored.
For example, frame 1,001 contains a TOUCHING contact at pressure 0 plus four
HOVER_IN_RANGE contacts at 23, 28, 28, and 25. At frame 1,401 the same active
contact remains at 0 while the others report 124, 65, 72, and 89.

This explains a concrete way the display could appear unresponsive to object
pressure while still responding to the fingertip. The recording does not prove
which physical object produced each contact or that pressure values can be
summed into a valid mass. At that point, the readout flagged these frames as contact
interference, clears zero and smoothing history, and hides the number while
showing removal, insulation, and re-zeroing instructions. Per-contact pressure
is included in the GUI state for diagnosis. Native acquisition and the Phase 4/5
evidence paths are unchanged.

Replay results are saved in `artifacts/gui-interference-replay.json`. Regression
tests cover the captured contact pattern, changing pressure at fixed finger
coordinates, recovery, zero invalidation, and the visible interference message.
A physical repeat with the object on dry paper was still needed to establish
whether insulation resolved that setup's interference. This veto was superseded
by the correction below after the user reported that paper did not resolve it.

## Multi-contact display correction, 2026-09-26

The prior interference veto was itself preventing testing: it suppressed 1,700
of the 3,000 saved frames. None of those frames has multiple active fingertips.
In 1,492 frames, the active finger reports zero while other in-range contacts
carry positive pressure. Merely choosing the first or only active contact loses
that changing signal.

The original recording also provides evidence of pressure redistribution.
Sequences 1835–1837 split force as finger/hover `7+0`, `6+1`, `5+2`, keeping a
total of 7. Sequences 868–871 shift the finger from 8 to zero while totals rise
smoothly through 62, 63, 64, 65. Combining current pressure avoids both the
fingertip-only freeze and the later interference veto. The frontend now accepts
these live readings, and object contact changes preserve Zero while the original
reference remains. Native acquisition and Phase 4/5 evidence rules are unchanged.

Rechecking the pinned upstream sources confirmed that TrackWeight accepts
nonempty multi-contact lists but uses their first record, without aggregation
or active-state filtering. The maintainer also
[describes objects being rejected as non-fingers](https://github.com/KrishKrosh/TrackWeight/issues/35#issuecomment-3121391706),
which supports retaining a real fingertip during testing; it is not an Apple
hardware specification or proof that insulation always works.

These observations justify a responsive experimental total. They cannot prove
mass accuracy, distinguish every force-sharing/duplication case, or compensate
for changing finger pressure. A known-mass comparison on this Mac is still
required. The app continues to label grams as estimates and reports
`hardware_validated: false`.

Verification: `make test` passed all 140 Python tests and the native ASan/UBSan
callback/queue/lifetime and decoder checks. All 10 frontend behavior tests passed.
Replaying the original 3,000 frames produced 2,998 live readings, recovering all
1,700 previously vetoed frames; terminal-only/empty frames remain unavailable.
The replay summary and sample decisions are in
`artifacts/gui-multicontact-replay.json`. A new local GUI session also connected
to the real sensor successfully; this checks transport, not object accuracy.
