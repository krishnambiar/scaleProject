# Phase 3 status

## Scope

Phase 3 isolates the private macOS callback ABI behind the native library and
gives Python application code an immutable, project-owned `RawFrame`. It is a
transport and integrity boundary only. It does not implement contact selection,
tare, baseline correction, smoothing, stability detection, calibration, grams,
bottle logic, or hydration events.

The Phase 2 candidate is admitted for continued pressure-domain experiments on
the exact checked-in target. Its values remain arbitrary raw sensor coordinates.
In particular, Phase 3 does not compensate for the observed REST drift or the
Cycle 2 HARDER/lower-stage raw-range overlap. Those remain experimental caveats,
not inputs to a correction rule.

## Boundary and ownership

```text
Apple callback-local memory
          |
          | synchronous byte-wise copy; source pointer is never retained
          v
native project-owned frame + fixed-capacity queue
          |
          | mt_phase2_capture_poll
          v
diagnostic RawTouchFrame (may carry decoder findings)
          |
          | fail-closed Phase 3 integrity gate
          v
application RawFrame + tuple[RawContact, ...]
```

The registered callback is a static C function. The dynamic library neither
links to Python nor calls Python, and the callback performs no allocation or
I/O. It copies callback metadata and selected record scalars into stack-owned
project structures, updates lock-free counters, and attempts one queue lock.
Python polling and all application work run after the callback returns.

`RawFrame` and `RawContact` are frozen Python dataclasses containing Python
integers/floats and an immutable tuple. They contain no Apple device, record,
callback, or `ctypes` pointer and remain valid after the next poll and after the
native capture is stopped or closed.

The application contact retains `path_index` as the observed continuity key.
`finger_code` and `hand_code` remain descriptive classifications. Phase 3 does
not merge paths or infer that a classification-code change creates a new
contact.

## Queue and thread behavior

- The legacy metadata queue is fixed at 4,096 frames.
- The rich-frame queue is fixed at 1,024 frames.
- When either queue is at capacity, enqueue advances its tail first, so the
  oldest record is discarded and the newest record is retained.
- The callback uses `pthread_mutex_trylock`; it never waits for a polling thread.
  If the mutex is already held, the incoming record cannot safely modify either
  queue and is dropped. This distinct fail-safe path increments
  `lock_contention_drop_count`; capacity eviction increments
  `queue_overwrite_count`.
- Start resets queue indices and counters. Stop detaches callback admission,
  unregisters, waits for admitted callbacks to quiesce, and only then permits
  native storage to be released.

After capture has stopped, callbacks have quiesced, and the queue has been fully
drained, the expected rich-frame accounting is:

```text
attempted_frame_count
  = materialized_frames
  + queue_overwrite_count
  + lock_contention_drop_count
```

If polling stops before the queue is drained, add `queue_depth` to the right
side, but take the stats only after stop/quiescence. Live counter reads are
individually safe but are not promised as one transactional snapshot. Both loss
counters are surfaced to Python; no loss is silently presented as a complete
stream.

The deterministic FIFO result covers sequential callback injection. Sequence
is assigned before the queue try-lock, so hypothetical overlapping callbacks
could successfully enqueue in a different order than callback admission. Live
target captures have shown forward-only sequence and frame IDs, but Apple's
callback serialization is not treated as a contract; a future observation of
overlap requires a new diagnostic or an explicit serialization policy.

## Application integrity gate

`raw_frame_from_transport()` accepts zero-, one-, and multi-contact frames in
their original order. It rejects a frame instead of partially using it when:

- native decode status is nonzero, including unknown status bits;
- the frame profile differs from the profile enabled by the native source;
- callback, copied, and materialized touch counts disagree or exceed 32;
- a contact's copied-field mask is missing a selected field or has an unknown
  extra field;
- the device timestamp or a copied scalar is non-finite;
- a lifecycle state is outside the verified target table;
- the pressure slot contains the verified missing-value sentinel; or
- a Python float no longer encodes to its preserved binary32 bit pattern.

Rejection raises `RawFrameValidationError` with a stable reason and detail. The
reader does not silently skip the bad record, because doing so would hide an ABI
or transport fault from the caller.

Production code constructs `RawFrameSensor()` without arguments so the guarded
native source performs device and target admission. Its optional `source=`
parameter is a trusted dependency-injection seam for deterministic tests, not a
way to bypass target validation in production.

## Verification

Deterministic native tests run under AddressSanitizer and
UndefinedBehaviorSanitizer and verify:

- capacity-plus-three sequential callbacks retain exactly the newest 4,096
  metadata frames and newest 1,024 rich frames in FIFO order;
- overwrite, depth, enqueue, attempt, copy, and contention counters balance;
- a callback returns while the queue mutex remains deliberately held and counts
  one contention drop; and
- the maximum admitted 32-contact source array is copied without an invalid
  access.

Pure Python tests use a fake source and never load the private framework. They
cover immutable/lifetime-independent copies, zero/one/multiple contacts,
ordering, classification-code changes on a stable path, every integrity
rejection class, lifecycle/capability/stats forwarding, empty/error reads, and
finite timeout validation.

## Exact-target runtime result

On 2026-08-31, `RawFrameSensor` passed the exact-target and Force Touch gates,
started with option zero, and returned an operator-produced two-contact
`RawFrame`. After stop and close, its sequence, frame number, two path indices,
and preserved candidate bit patterns remained readable from the immutable
Python object. Accounting was exact: one callback and one rich-frame attempt,
one materialized frame, zero queue overwrite/contention drops, zero device,
record-frame, or record-timestamp mismatches, zero invalid/sentinel/non-finite
findings, zero late or in-flight callbacks, and zero final queue depth.

This check must be repeated before treating Phase 3 as accepted on a changed
target. The existing exact-target admission gates still apply: a changed
hardware model, OS or kernel build, framework bundle or image UUID,
architecture, device capability, profile, or start option fails closed.

## Stopping point

Phase 3 stops at validated transport of raw frames. REST drift, range overlap,
hysteresis, geometry sensitivity, physical units, calibration, and production
measurement quality remain unresolved. Phase 4 has not been implemented.
