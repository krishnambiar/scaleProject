"""Responsive scale display over validated RawFrames, independent of Phase 4.

TrackWeight reports that the framework pressure field is already gram-valued.
We use that 1:1 interpretation only for an explicitly unvalidated estimate;
raw frames and the physical-calibration evidence path keep their original units.
See docs/TRACKWEIGHT_RESEARCH.md for pinned sources and the limits of this model.
"""

import math
from collections import deque
from dataclasses import dataclass, replace
from typing import Optional

from .models import RawFrame, TargetTouchState


DISPLAY_WINDOW_SAMPLES = 10
STALE_FRAME_SECONDS = 0.3
ESTIMATE_BASIS = "in_range_pressure_sum_1_to_1_unvalidated"
CURRENT_STATES = (
    TargetTouchState.START_IN_RANGE, TargetTouchState.HOVER_IN_RANGE,
    TargetTouchState.MAKE_TOUCH, TargetTouchState.TOUCHING,
)


@dataclass(frozen=True)
class LiveReading:
    pressure_raw: float
    smoothed_pressure_raw: float
    zero_offset_raw: float
    estimated_grams: float
    zeroed: bool
    path_index: int
    contributing_paths: tuple[int, ...]
    contact_epoch: int
    sequence: int


class LiveReadout:
    """Combined current pressure, short smoothing, explicit reference zero.

    No stable-weight or calibration claim is made. In particular, a display
    zero cannot create a Phase 4 tare or Phase 5 known-mass observation.
    """

    def __init__(self):
        self._samples = deque(maxlen=DISPLAY_WINDOW_SAMPLES)
        self._previous = None
        self._epoch = 0
        self.reading: Optional[LiveReading] = None
        self.issue: Optional[str] = None

    def reset(self):
        self._samples.clear()
        self._previous = None
        self.reading = None
        self.issue = None
        self._epoch += 1

    def process(self, frame: RawFrame) -> Optional[LiveReading]:
        self.issue = None
        # Force can move from the fingertip into other in-range records, even
        # ones classified as hovering. Combine them instead of rejecting the
        # frame or following whichever record happens to be first. Departing
        # records can retain old pressure and must not contribute a second time.
        # This aggregation is a display hypothesis, not calibrated mass evidence.
        contacts = tuple(c for c in frame.contacts if c.state in CURRENT_STATES)
        if not contacts:
            self.reset()
            return None
        if any(not math.isfinite(c.pressure_candidate_raw)
               or c.pressure_candidate_raw == 43690.0 for c in contacts):
            self.reset()
            self.issue = "invalid_pressure"
            return None
        paths = tuple(sorted(c.path_index for c in contacts))
        if len(set(paths)) != len(paths):
            self.reset()
            self.issue = "invalid_pressure"
            return None
        raw = math.fsum(c.pressure_candidate_raw for c in contacts)
        previous = self._previous
        if previous is not None and (
            self.reading.path_index not in paths
            or frame.sequence != previous.sequence + 1
            or frame.frame_number != previous.frame_number + 1
            or not 0 < frame.device_timestamp - previous.device_timestamp <= STALE_FRAME_SECONDS
            or not 0 < frame.host_monotonic_ns - previous.host_monotonic_ns <= STALE_FRAME_SECONDS * 1e9
        ):
            self.reset()
        # Keep the initial reference identity through added/removed object
        # contacts and lifecycle reclassification. Reordering cannot move it.
        # If the reference leaves, start a fresh epoch and require a new Zero.
        reference_path = self.reading.path_index if self.reading else min(
            contacts,
            key=lambda c: (c.state not in (
                TargetTouchState.MAKE_TOUCH, TargetTouchState.TOUCHING
            ), c.path_index),
        ).path_index
        offset = self.reading.zero_offset_raw if self.reading else 0.0
        zeroed = self.reading.zeroed if self.reading else False
        self._samples.append(raw)
        smoothed = math.fsum(self._samples) / len(self._samples)
        self.reading = LiveReading(
            pressure_raw=raw,
            smoothed_pressure_raw=smoothed,
            zero_offset_raw=offset,
            estimated_grams=smoothed - offset,
            zeroed=zeroed,
            path_index=reference_path,
            contributing_paths=paths,
            contact_epoch=self._epoch,
            sequence=frame.sequence,
        )
        self._previous = frame
        return self.reading

    def zero(self, expected_epoch: int) -> LiveReading:
        if self.reading is None or self.reading.contact_epoch != expected_epoch:
            raise ValueError("Contact changed. Rest one finger lightly and press Zero again.")
        # Use the latest sample and clear old filter history, so Zero is exactly
        # zero immediately and previous loads cannot leak into the next display.
        raw = self.reading.pressure_raw
        self._samples.clear()
        self._samples.append(raw)
        self.reading = replace(
            self.reading, smoothed_pressure_raw=raw, zero_offset_raw=raw,
            estimated_grams=0.0, zeroed=True,
        )
        return self.reading
