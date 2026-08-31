# Phase 2 ABI verification record

## Evidence boundary

This record is based on the project specification and independent inspection of
Apple's locally installed `MultitouchSupport.framework`. No TrackWeight or
OpenMultitouchSupport source was searched, inspected, copied, translated, or
reproduced.

Every statement below is scoped to this fingerprint:

- hardware: `Mac16,8`
- architecture: `arm64` (framework image is arm64e)
- macOS product build: `25D771280a`
- kernel/base-system build: `25D2128`
- MultitouchSupport bundle version: `9430.5`
- loaded image UUID: `40D691BB-9166-31E0-959E-351863FF09A0`

The two build identifiers are intentional. This Mac has a Rapid Security
Response cryptex: `sw_vers` and the active cryptex report `25D771280a`, while
`kern.osversion` reports the underlying `25D2128`. Python and native admission
checks cover both identities.

These findings are machine-ABI and dataflow observations, not an Apple source
or compatibility contract.

## How the 96-byte record was established

Local disassembly provides two independent size proofs:

1. `MTAlg_IssueContactFrameCallbacks` reserves `0xc00` bytes for a maximum of
   32 callback records. `0xc00 / 32 == 0x60`.
2. `MTGetPathFrame` advances its output by `0x60` bytes per accepted contact and
   copies six consecutive 16-byte blocks.

The callback therefore receives a contiguous, callback-owned array of at most
32 records with a 96-byte stride on this image. The bridge copies selected
scalars synchronously and never stores the source pointer.

## Smallest selected layout

| Offset | Width | Machine encoding | Evidence-backed interpretation |
| ---: | ---: | --- | --- |
| `0x00` | 8 | raw 64-bit | record frame token |
| `0x08` | 8 | binary64 | record timestamp |
| `0x10` | 4 | raw 32-bit | path/contact identity |
| `0x14` | 4 | raw 32-bit | lifecycle state |
| `0x18` | 4 | raw 32-bit | finger identity/classification |
| `0x1c` | 4 | signed raw 32-bit | hand identity |
| `0x20` | 4 | binary32 | normalized X |
| `0x24` | 4 | binary32 | normalized Y |
| `0x30` | 4 | binary32 | `zTotal` candidate metric |
| `0x34` | 4 | binary32 | pressure/force candidate |
| `0x5c` | 4 | binary32 | `zDensity` candidate metric |

Dataflow in both the binary and precise contact-fill paths writes the same
encodings at these offsets. `MTContact_isActive` consumes state at `0x14` and
`zTotal` at `0x30`. The framework's own diagnostic-print path consumes X/Y,
`zTotal`, the `0x34` candidate, `zDensity`, state, and identities at the listed
offsets.

The framework state-name table has eight indexed values (`0` through `7`), with
steady physical contact at value `4` (`Touching`). A parsing path can put the
`0xAAAA` missing/invalid marker into the candidate slot, which appears as
binary32 value `43690.0`; the guarded decoder flags it as a sentinel.

Names such as “pressure candidate” describe this project's evidence. Apple's
private source typedef and physical unit remain unknown. The candidate is not
grams.

## Guarded native implementation

`native/src/mt_phase2_decode.c` contains one immutable compiled profile. It
does not accept record sizes or offsets from Python. Before enabling it, the
native bridge independently verifies:

- arm64 compilation;
- hardware model;
- active cryptex product build;
- kernel build;
- framework bundle version;
- UUID of the loaded image containing `MTGetPathFrame`;
- built-in device status and Force Touch capability; and
- exact source descriptor equality.

The decoder does not cast Apple memory to a struct. It validates count before
pointer arithmetic and then uses byte pointers plus `memcpy` for every selected
value. A positive count with a null record pointer, a count above 32, or a
callback from a different device prevents all record access. Exact binary32
bits are copied alongside each float.

The C target-view struct exists only for compile-time `_Static_assert` checks of
size and selected offsets. Its opaque gaps make no claims about unselected
bytes.

## Runtime self-consistency checks

For every physical callback, Phase 2 compares the record-local frame and
timestamp with the independently verified callback arguments. Any mismatch is
reported and blocks ABI acceptance. The Python experiment additionally checks:

- native/Python output sizes and a full output-layout fingerprint;
- expected profile ID on every returned frame;
- raw count, copied count, and materialized tuple agreement;
- copied-field mask equality;
- finite timestamp/scalar values;
- known state range and sentinel absence;
- callback sequence, frame, device timestamp, and host-time ordering; and
- decoded/queued/dropped/materialized accounting.

`copied_fields` means only that bytes came from evidence-backed offsets. It is
not a semantic-validity promise; a sample enters plateau analysis only when the
entire frame decode status is zero.

## Tests completed

- Malformed descriptors, null/oversized counts, maximum count 32, source
  mutation, sentinel/non-finite values, frame/timestamp mismatches, and a guard
  page immediately after the source record pass ASan/UBSan tests.
- Poison-pointer tests prove Phase 1 never reads records while Phase 2 is
  disabled and that Phase 2 never reads a record from a device-mismatch
  callback.
- Callback destruction races pass with Phase 2 state allocated, both before and
  after admission.
- The target completed 25 Phase 1 and 10 Phase 2 real start/stop cycles under
  ASan/UBSan with option value zero.
- Native and Python layouts currently agree at 64-byte touch, 2,104-byte frame,
  104-byte stats, and 128-byte descriptor sizes, fingerprint
  `0xcc805c390dc2e7c1`.

## Still unverified

- Apple's source typedef names for callback and record fields.
- The meaning of every unselected byte in the 96-byte record.
- A verified contact-area/shape field.
- The physical unit, direction, ordinal reliability, hysteresis, drift, and
  geometry sensitivity of the `0x34` candidate.
- Compatibility with any other hardware, OS build, framework version, image
  UUID, architecture, or nonzero `MTDeviceStart` option.

The operator-guided Phase 2 experiment exists to resolve the pressure
candidate's behavioral questions. Calibration must not begin before that
evidence is reviewed.
