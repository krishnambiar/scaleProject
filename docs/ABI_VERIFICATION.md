# Phase 1 ABI verification record

## Clean-room evidence boundary

This record was produced from the attached behavioral specification plus local inspection of Apple's framework on the target Mac. No TrackWeight or OpenMultitouchSupport source code was searched, inspected, copied, translated, or reproduced.

Target fingerprint at inspection time:

- hardware model: `Mac16,8`
- architecture: `arm64`
- macOS: `26.3.1 (a)`, build `25D771280a`
- kernel/base-system build: `25D2128` (Darwin `25.3.0`)
- MultitouchSupport bundle version: `9430.5`
- dyld image architecture: `arm64e`
- dyld image UUID: `40D691BB-9166-31E0-959E-351863FF09A0`

The product and kernel build values differ because the active Rapid Security
Response cryptex reports `25D771280a` while `kern.osversion` reports the base
`25D2128`. Both are recorded and checked. The framework executable is resident
in the dyld shared cache even though its filesystem executable symlink has no
standalone target. `dlopen` by the framework path succeeded on this Mac.

## What local arm64 disassembly established

`MTRegisterContactFrameCallbackWithRefcon` consumes a device in `x0`, callback pointer in `x1`, and refcon in `x2`; it returns a 0/1 result in `w0`. `MTUnregisterContactFrameCallback` consumes device/callback in `x0`/`x1` and likewise returns 0/1 in `w0`.

The framework's callback dispatch performs the following register setup immediately before its indirect branch to the registered callback:

| Register | Locally observed meaning |
| --- | --- |
| `x0` | device pointer |
| `x1` | touch-record array pointer, kept opaque |
| `x2` | raw touch-count value |
| `d0` | double-precision device timestamp |
| `x3` | raw frame-number value |
| `x4` | registration refcon |

`MTDeviceStart` consumes a device pointer in `x0` and a 32-bit option value in `w1`, returning a 32-bit native status in `w0`. `MTDeviceStop` consumes the device pointer in `x0` and returns a 32-bit native status in `w0`. The start implementation propagates IOKit-style nonzero error values and reaches zero on its success path, which is why the bridge treats zero as success.

`MTDeviceCreateDefault` takes no arguments and returns a retained device pointer. `MTDeviceSupportsForce` and `MTDeviceIsBuiltIn` consume that pointer and normalize their results to one bit in `w0`. `MTDeviceRelease` consumes the device pointer and performs cleanup.

These are machine-ABI observations for this exact dyld image, not a public compatibility contract.

The target image also gives a version-scoped shutdown guarantee for this bridge's verified `MTDeviceStart(device, 0)` call path. `MTDeviceStart` runs the device source on a framework-created `CFRunLoop`. When `MTDeviceStop` is called from a different run loop—as it is here, because the bridge never calls stop from inside its callback—the function schedules a block on the device run loop, wakes it, and waits for that block's completion byte. The block calls `CFRunLoopStop` before setting the byte. Because contact callbacks execute synchronously on that same run-loop path, the stop block cannot signal completion until an already-running callback has returned. The bridge then unregisters before releasing the device. This finding is tied to the fingerprint above and option value zero; it is not assumed for another framework image or option value.

The dispatch dataflow also shows that active contact counts are bounded at 32 and that idle frames are suppressed: active callbacks carry a positive count, and the transition out of contact emits one terminal zero-count callback. The live preflight observed that terminal zero after a sustained one-contact stream. A quiet hands-off stage may therefore contain no callbacks; it should not be judged by expecting a continuous stream of zeros.

## Runtime verification completed

- `dlopen` succeeded through the framework path even though the executable lives only in the dyld shared cache.
- The acquired default device reported built-in and Force Touch capability.
- Callback registration and `MTDeviceStart(device, 0)` succeeded; `MTDeviceStop` and unregistration completed cleanly.
- The application-thread stop path was locally verified to serialize through the callback run loop before unregister and release.
- Twenty-five Phase 1 and ten opt-in Phase 2 consecutive start/stop cycles
  returned native status zero and passed
  AddressSanitizer/UndefinedBehaviorSanitizer. Deterministic sanitizer tests
  also paused callbacks before and after admission while destroying captures
  with Phase 2 state allocated; both lifetime races completed without invalid
  access.
- A 3-second preflight delivered 352 callbacks with no bridge drops, device mismatches, duplicate/regressing frame steps, or timestamp regressions.
- A longer guided run delivered 3,372 callbacks with zero native queue loss and observed count values 0, 1, and 2. Every one of the 2,056 stage-recorded frames had forward-only frame IDs and timestamps; the earlier diagnostic version intentionally drained transition frames without retaining them. The labelled two-finger interval was physically mixed, so its exact histogram remains evidence rather than a perfect-contact claim. The current diagnostic retains and analyzes transition frames too.

## What remains unverified

- Apple's source typedef for the count argument.
- Apple's source typedef and promised width for the frame argument.
- Callback rate and frame-ID continuity under 0/1/multiple physical contacts.
- Every unselected touch-record byte and all Apple private source typedef names.
- The physical behavior, unit, drift, and geometry sensitivity of the Phase 2
  pressure candidate. The selected exact-target byte layout is documented in
  [PHASE2_ABI_VERIFICATION.md](PHASE2_ABI_VERIFICATION.md).
- Every `MTDeviceStart` option bit other than zero.

The Phase 1 diagnostic continues to verify metadata without touching record
bytes. Phase 2 is an additive, explicitly enabled path with a separate target
profile and queue; it does not change this Phase 1 contract.
