"""Descriptive Phase 1 diagnostics with no invented pass/fail thresholds."""

import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional

from .models import FrameMetadata


@dataclass(frozen=True)
class StageReport:
    label: str
    expected_touch_count: Optional[int]
    frame_count: int
    touch_count_histogram: Dict[str, int]
    expected_count_matches: Optional[int]
    expected_count_match_fraction: Optional[float]
    first_frame_register: Optional[int]
    last_frame_register: Optional[int]
    duplicate_frame_steps: int
    regressing_frame_steps: int
    forward_frame_steps: int
    non_unit_forward_steps: int
    skipped_frame_id_values: int
    callback_sequence_gap_steps: int
    skipped_callback_sequence_values: int
    non_finite_timestamps: int
    regressing_timestamp_steps: int
    host_observation_seconds: float
    observed_frame_rate_hz: Optional[float]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def analyze_stage(
    label: str,
    frames: Iterable[FrameMetadata],
    expected_touch_count: Optional[int] = None,
) -> StageReport:
    samples: List[FrameMetadata] = list(frames)
    histogram = Counter(sample.raw_touch_count_register for sample in samples)

    duplicate_steps = 0
    regressing_steps = 0
    forward_steps = 0
    non_unit_steps = 0
    skipped_frame_ids = 0
    sequence_gap_steps = 0
    skipped_sequences = 0
    regressing_timestamps = 0
    for previous, current in zip(samples, samples[1:]):
        delta = current.raw_frame_register - previous.raw_frame_register
        if delta == 0:
            duplicate_steps += 1
        elif delta < 0:
            regressing_steps += 1
        else:
            forward_steps += 1
            if delta != 1:
                non_unit_steps += 1
                skipped_frame_ids += delta - 1
        sequence_delta = current.sequence - previous.sequence
        if sequence_delta > 1:
            sequence_gap_steps += 1
            skipped_sequences += sequence_delta - 1
        if current.device_timestamp < previous.device_timestamp:
            regressing_timestamps += 1

    non_finite_timestamps = sum(
        not math.isfinite(sample.device_timestamp) for sample in samples
    )
    if len(samples) >= 2:
        host_seconds = (
            samples[-1].host_monotonic_ns - samples[0].host_monotonic_ns
        ) / 1_000_000_000.0
    else:
        host_seconds = 0.0
    rate = None
    if host_seconds > 0 and len(samples) >= 2:
        rate = (len(samples) - 1) / host_seconds

    matches: Optional[int] = None
    fraction: Optional[float] = None
    if expected_touch_count is not None:
        matches = histogram.get(expected_touch_count, 0)
        fraction = matches / len(samples) if samples else None

    return StageReport(
        label=label,
        expected_touch_count=expected_touch_count,
        frame_count=len(samples),
        touch_count_histogram={str(key): histogram[key] for key in sorted(histogram)},
        expected_count_matches=matches,
        expected_count_match_fraction=fraction,
        first_frame_register=(samples[0].raw_frame_register if samples else None),
        last_frame_register=(samples[-1].raw_frame_register if samples else None),
        duplicate_frame_steps=duplicate_steps,
        regressing_frame_steps=regressing_steps,
        forward_frame_steps=forward_steps,
        non_unit_forward_steps=non_unit_steps,
        skipped_frame_id_values=skipped_frame_ids,
        callback_sequence_gap_steps=sequence_gap_steps,
        skipped_callback_sequence_values=skipped_sequences,
        non_finite_timestamps=non_finite_timestamps,
        regressing_timestamp_steps=regressing_timestamps,
        host_observation_seconds=host_seconds,
        observed_frame_rate_hz=rate,
    )
