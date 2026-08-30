# Phase 1 status

## Verified on the target Mac

- Exact OS/hardware/framework fingerprint matched the checked-in ABI profile.
- The private framework loaded from the dyld shared cache.
- The default device was acquired and reported both built-in and Force Touch capability.
- Callback registration, `MTDeviceStart(device, 0)`, stop, unregister, and release succeeded.
- A 3-second live preflight delivered 352 callbacks: 351 count-one frames and one terminal count-zero frame, with no bridge loss, device mismatch, duplicate/regressing frame step, or timestamp regression.
- The first guided run delivered 3,372 callbacks with zero native queue loss and observed raw count values 0, 1, and 2.
- The one-finger labelled window contained 496/502 count-one frames.
- The two-finger labelled window was physically mixed: 365/672 count-two and 305/672 count-one frames. That proves the count-two path arrives, but it is not treated as a perfect-contact trial.
- Seven Python tests, Clang static analysis, 25 real lifecycle cycles under ASan/UBSan, deterministic callback-destruction race tests, and ten concurrent-reader stop cycles passed.

## Still requiring operator-controlled verification

The original guide gives no numeric reliability threshold. The existing evidence proves the transport and count paths, but the physically mixed two-finger window is not enough to claim repeatable 0/1/2 labelling without qualification.

Run the operator-confirmed diagnostic from the repository root:

```bash
make
PYTHONPATH=src python3 -m trackpad_scale.phase1_probe \
  --guided --trials 3 --confirm-stages \
  --json-out artifacts/phase1-final-guided.json
```

Only the stage histograms, ordering facts, queue accounting, and operator labels should be reported. Do not invent a pass percentage after the fact. Phase 2 remains intentionally absent.
