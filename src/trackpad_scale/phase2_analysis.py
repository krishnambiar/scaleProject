"""Descriptive analysis for the controlled Phase 2 pressure experiment.

This module deliberately does not calibrate the candidate, invent pressure
units, or choose an application threshold.  It reports the evidence needed to
decide whether offset 0x34 behaves like a usable monotonic pressure signal on
this exact Mac.
"""

import math
from collections import Counter
from dataclasses import asdict, dataclass
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import (
    PHASE2_REQUIRED_COPIED_FIELDS,
    Phase2DecodeStatus,
    RawTouch,
    RawTouchFrame,
    TargetTouchState,
    phase2_decode_status_names,
)
from .reliability import analyze_stage


@dataclass(frozen=True)
class ScalarSummary:
    count: int
    nonfinite_count: int
    zero_count: int
    first: Optional[float]
    last: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]
    median: Optional[float]
    q1: Optional[float]
    q3: Optional[float]
    iqr: Optional[float]
    mad: Optional[float]
    slope_per_second: Optional[float]
    distinct_bit_pattern_count: int
    observation_seconds: float

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Phase2StageReport:
    label: str
    frame_count: int
    decode_status_histogram: Dict[str, int]
    raw_touch_count_histogram: Dict[str, int]
    copied_touch_count_histogram: Dict[str, int]
    transport: Dict[str, object]
    callback_sequence_nonincreasing_steps: int
    host_monotonic_regressing_steps: int
    primary_sample_count: int
    excluded_frame_counts: Dict[str, int]
    selected_identity: Optional[Dict[str, int]]
    identity_histogram: Dict[str, int]
    state_histogram: Dict[str, int]
    pressure_candidate: ScalarSummary
    z_total: ScalarSummary
    z_density: ScalarSummary
    normalized_x: ScalarSummary
    normalized_y: ScalarSummary
    pressure_correlations: Dict[str, Optional[float]]

    def to_dict(self) -> Dict[str, object]:
        result = asdict(self)
        result["analysis_policy"] = {
            "primary_sample": (
                "decode_status zero; required fields present; raw, copied, and "
                "materialized counts all equal one; finite nonsentinel scalars; "
                "modal contact identity for this plateau"
            ),
            "pressure_units": "arbitrary raw sensor coordinate; not grams",
            "quartile_method": "linear interpolation at index (n - 1) * p",
            "automatic_numeric_pass_threshold": None,
        }
        return result


def _quantile(sorted_values: Sequence[float], fraction: float) -> Optional[float]:
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return (
        sorted_values[lower] * (1.0 - weight)
        + sorted_values[upper] * weight
    )


def _least_squares_slope(
    times_ns: Sequence[int], values: Sequence[float]
) -> Optional[float]:
    if len(values) < 2 or len(times_ns) != len(values):
        return None
    origin = times_ns[0]
    times = [(item - origin) / 1_000_000_000.0 for item in times_ns]
    mean_time = sum(times) / len(times)
    mean_value = sum(values) / len(values)
    denominator = sum((item - mean_time) ** 2 for item in times)
    if denominator == 0:
        return None
    numerator = sum(
        (time_value - mean_time) * (sample - mean_value)
        for time_value, sample in zip(times, values)
    )
    return numerator / denominator


def _scalar_summary(
    times_ns: Sequence[int], values: Sequence[float], bits: Sequence[int]
) -> ScalarSummary:
    finite = [value for value in values if math.isfinite(value)]
    ordered = sorted(finite)
    middle = median(ordered) if ordered else None
    deviations = (
        sorted(abs(value - middle) for value in ordered)
        if middle is not None
        else []
    )
    q1 = _quantile(ordered, 0.25)
    q3 = _quantile(ordered, 0.75)
    finite_times = [
        timestamp
        for timestamp, value in zip(times_ns, values)
        if math.isfinite(value)
    ]
    observation_seconds = 0.0
    if len(finite_times) >= 2:
        observation_seconds = (
            finite_times[-1] - finite_times[0]
        ) / 1_000_000_000.0
    return ScalarSummary(
        count=len(finite),
        nonfinite_count=len(values) - len(finite),
        zero_count=sum(value == 0 for value in finite),
        first=(finite[0] if finite else None),
        last=(finite[-1] if finite else None),
        minimum=ordered[0] if ordered else None,
        maximum=ordered[-1] if ordered else None,
        median=middle,
        q1=q1,
        q3=q3,
        iqr=(q3 - q1 if q1 is not None and q3 is not None else None),
        mad=(median(deviations) if deviations else None),
        slope_per_second=_least_squares_slope(finite_times, finite),
        distinct_bit_pattern_count=len(set(bits)),
        observation_seconds=observation_seconds,
    )


def _correlation(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    pairs = [
        (a, b)
        for a, b in zip(left, right)
        if math.isfinite(a) and math.isfinite(b)
    ]
    if len(pairs) < 2:
        return None
    mean_left = sum(item[0] for item in pairs) / len(pairs)
    mean_right = sum(item[1] for item in pairs) / len(pairs)
    left_energy = sum((item[0] - mean_left) ** 2 for item in pairs)
    right_energy = sum((item[1] - mean_right) ** 2 for item in pairs)
    if left_energy == 0 or right_energy == 0:
        return None
    covariance = sum(
        (a - mean_left) * (b - mean_right) for a, b in pairs
    )
    return covariance / math.sqrt(left_energy * right_energy)


def _counter_dict(counter: Counter) -> Dict[str, int]:
    return {str(key): counter[key] for key in sorted(counter, key=str)}


def _state_label(state: int) -> str:
    try:
        return f"{state}:{TargetTouchState(state).name}"
    except ValueError:
        return f"{state}:UNRECOGNIZED"


def _identity(touch: RawTouch) -> Tuple[int, int, int]:
    return touch.path_index, touch.finger_id, touch.hand_id


def analyze_phase2_stage(
    label: str,
    frames: Iterable[RawTouchFrame],
    expected_profile_id: int = 1,
    maximum_touch_count: int = 32,
) -> Phase2StageReport:
    samples = list(frames)
    decode_histogram = Counter(
        "|".join(phase2_decode_status_names(frame.decode_status))
        for frame in samples
    )
    raw_count_histogram = Counter(
        frame.metadata.raw_touch_count_register for frame in samples
    )
    copied_count_histogram = Counter(frame.copied_touch_count for frame in samples)
    exclusions = Counter()
    eligible: List[Tuple[RawTouchFrame, RawTouch]] = []
    observed_states = Counter()

    for frame in samples:
        if frame.layout_profile_id != expected_profile_id:
            exclusions["layout_profile_mismatch"] += 1
            continue
        if frame.decode_status != int(Phase2DecodeStatus.OK):
            exclusions["decode_status_nonzero"] += 1
            for name in phase2_decode_status_names(frame.decode_status):
                exclusions[f"decode_flag_{name}"] += 1
            continue
        raw_count = frame.metadata.raw_touch_count_register
        if raw_count > maximum_touch_count or frame.copied_touch_count > maximum_touch_count:
            exclusions["count_exceeds_profile_maximum"] += 1
            continue
        if (
            raw_count != frame.copied_touch_count
            or frame.copied_touch_count != len(frame.touches)
        ):
            exclusions["count_or_materialization_mismatch"] += 1
            continue
        if frame.copied_touch_count == 0:
            exclusions["zero_contact"] += 1
            continue
        if frame.copied_touch_count != 1:
            exclusions["multiple_contacts"] += 1
            continue
        touch = frame.touches[0]
        if touch.copied_fields != PHASE2_REQUIRED_COPIED_FIELDS:
            exclusions["unexpected_copied_fields_mask"] += 1
            continue
        observed_states[_state_label(touch.state)] += 1
        scalars = (
            touch.normalized_x,
            touch.normalized_y,
            touch.z_total,
            touch.pressure_candidate,
            touch.z_density,
        )
        if not all(math.isfinite(value) for value in scalars):
            exclusions["nonfinite_scalar"] += 1
            continue
        if touch.pressure_candidate == 43690.0:
            exclusions["pressure_sentinel"] += 1
            continue
        if touch.state != int(TargetTouchState.TOUCHING):
            exclusions["nonsteady_touch_state"] += 1
            continue
        eligible.append((frame, touch))

    identity_counts = Counter(_identity(touch) for _, touch in eligible)
    selected_identity: Optional[Tuple[int, int, int]] = None
    if identity_counts:
        selected_identity = min(
            identity_counts,
            key=lambda item: (-identity_counts[item], item),
        )
    primary: List[Tuple[RawTouchFrame, RawTouch]] = []
    for pair in eligible:
        if _identity(pair[1]) != selected_identity:
            exclusions["identity_change"] += 1
        else:
            primary.append(pair)

    times = [frame.metadata.host_monotonic_ns for frame, _ in primary]
    touches = [touch for _, touch in primary]
    pressure = [touch.pressure_candidate for touch in touches]
    z_total = [touch.z_total for touch in touches]
    z_density = [touch.z_density for touch in touches]
    x_values = [touch.normalized_x for touch in touches]
    y_values = [touch.normalized_y for touch in touches]

    selected_identity_dict = None
    if selected_identity is not None:
        selected_identity_dict = {
            "path_index": selected_identity[0],
            "finger_id": selected_identity[1],
            "hand_id": selected_identity[2],
        }

    identity_histogram = {
        f"path={key[0]},finger={key[1]},hand={key[2]}": count
        for key, count in sorted(identity_counts.items())
    }
    return Phase2StageReport(
        label=label,
        frame_count=len(samples),
        decode_status_histogram=_counter_dict(decode_histogram),
        raw_touch_count_histogram=_counter_dict(raw_count_histogram),
        copied_touch_count_histogram=_counter_dict(copied_count_histogram),
        transport=analyze_stage(
            label,
            [frame.metadata for frame in samples],
            expected_touch_count=None,
        ).to_dict(),
        callback_sequence_nonincreasing_steps=sum(
            current.metadata.sequence <= previous.metadata.sequence
            for previous, current in zip(samples, samples[1:])
        ),
        host_monotonic_regressing_steps=sum(
            current.metadata.host_monotonic_ns < previous.metadata.host_monotonic_ns
            for previous, current in zip(samples, samples[1:])
        ),
        primary_sample_count=len(primary),
        excluded_frame_counts=_counter_dict(exclusions),
        selected_identity=selected_identity_dict,
        identity_histogram=identity_histogram,
        state_histogram=_counter_dict(observed_states),
        pressure_candidate=_scalar_summary(
            times,
            pressure,
            [touch.pressure_candidate_bits for touch in touches],
        ),
        z_total=_scalar_summary(
            times, z_total, [touch.z_total_bits for touch in touches]
        ),
        z_density=_scalar_summary(
            times, z_density, [touch.z_density_bits for touch in touches]
        ),
        normalized_x=_scalar_summary(
            times, x_values, [touch.normalized_x_bits for touch in touches]
        ),
        normalized_y=_scalar_summary(
            times, y_values, [touch.normalized_y_bits for touch in touches]
        ),
        pressure_correlations={
            "normalized_x": _correlation(pressure, x_values),
            "normalized_y": _correlation(pressure, y_values),
            "z_total": _correlation(pressure, z_total),
            "z_density": _correlation(pressure, z_density),
        },
    )


def summarize_pressure_sequence(
    ordered_stages: Sequence[Tuple[str, Phase2StageReport]]
) -> Dict[str, object]:
    """Describe adjacent medians and categorical reasons not to trust a trial."""

    comparisons: List[Dict[str, object]] = []
    disqualifiers: List[str] = []
    all_stage_medians: List[float] = []
    total_distinct_patterns = set()
    structural_error_frames = 0
    identities = []

    for label, report in ordered_stages:
        if report.primary_sample_count == 0:
            disqualifiers.append(f"{label}:no_primary_single_contact_samples")
        else:
            value = report.pressure_candidate.median
            if value is not None:
                all_stage_medians.append(value)
        if report.selected_identity is not None:
            identities.append(
                (
                    report.selected_identity["path_index"],
                    report.selected_identity["finger_id"],
                    report.selected_identity["hand_id"],
                )
            )
        structural_error_frames += report.excluded_frame_counts.get(
            "decode_status_nonzero", 0
        )
        structural_error_frames += sum(
            report.excluded_frame_counts.get(reason, 0)
            for reason in (
                "layout_profile_mismatch",
                "count_exceeds_profile_maximum",
                "count_or_materialization_mismatch",
                "unexpected_copied_fields_mask",
            )
        )
        if report.excluded_frame_counts.get("pressure_sentinel", 0):
            disqualifiers.append(f"{label}:pressure_sentinel")
        if report.excluded_frame_counts.get("nonfinite_scalar", 0):
            disqualifiers.append(f"{label}:nonfinite_scalar")
        if report.excluded_frame_counts.get("multiple_contacts", 0):
            disqualifiers.append(f"{label}:multiple_contact_confound")
        if report.excluded_frame_counts.get("identity_change", 0):
            disqualifiers.append(f"{label}:identity_change")
        if report.excluded_frame_counts.get("nonsteady_touch_state", 0):
            disqualifiers.append(f"{label}:nonsteady_touch_state")
        # Stage summaries expose only a count, not the actual patterns. A
        # count greater than one is still sufficient to rule out constancy in
        # that stage; cross-stage medians handle constancy between stages.
        if report.pressure_candidate.distinct_bit_pattern_count > 1:
            total_distinct_patterns.add((label, "varies"))

    for (lower_label, lower), (upper_label, upper) in zip(
        ordered_stages, ordered_stages[1:]
    ):
        lower_median = lower.pressure_candidate.median
        upper_median = upper.pressure_candidate.median
        delta = None
        direction = "unavailable"
        if lower_median is not None and upper_median is not None:
            delta = upper_median - lower_median
            if delta > 0:
                direction = "increase"
            elif delta < 0:
                direction = "decrease"
            else:
                direction = "tie"
                disqualifiers.append(
                    f"{lower_label}_to_{upper_label}:median_tie"
                )
        ranges_overlap = None
        if (
            lower.pressure_candidate.minimum is not None
            and lower.pressure_candidate.maximum is not None
            and upper.pressure_candidate.minimum is not None
            and upper.pressure_candidate.maximum is not None
        ):
            ranges_overlap = not (
                lower.pressure_candidate.maximum
                < upper.pressure_candidate.minimum
                or upper.pressure_candidate.maximum
                < lower.pressure_candidate.minimum
            )
        comparisons.append(
            {
                "from": lower_label,
                "to": upper_label,
                "from_median": lower_median,
                "to_median": upper_median,
                "delta": delta,
                "direction": direction,
                "raw_ranges_overlap": ranges_overlap,
            }
        )

    if structural_error_frames:
        disqualifiers.append("nonzero_decode_status_observed")
    if len(set(identities)) > 1:
        disqualifiers.append("contact_identity_changed_between_plateaus")
    if all_stage_medians and all(value == 0 for value in all_stage_medians):
        disqualifiers.append("pressure_candidate_zero_only")
    if len(all_stage_medians) >= 2 and len(set(all_stage_medians)) == 1:
        disqualifiers.append("pressure_candidate_constant_across_stage_medians")
    if len(all_stage_medians) >= 2 and not total_distinct_patterns and len(
        set(all_stage_medians)
    ) == 1:
        disqualifiers.append("pressure_candidate_constant_bit_pattern_evidence")

    available_directions = [
        item["direction"]
        for item in comparisons
        if item["direction"] != "unavailable"
    ]
    signed_directions = set(available_directions) - {"tie"}
    if len(signed_directions) > 1:
        disqualifiers.append("median_direction_changes_within_cycle")
    return {
        "stage_order": [label for label, _ in ordered_stages],
        "adjacent_median_comparisons": comparisons,
        "strictly_increasing": bool(available_directions)
        and all(item == "increase" for item in available_directions),
        "weakly_increasing": bool(available_directions)
        and all(item in ("increase", "tie") for item in available_directions),
        "strictly_decreasing": bool(available_directions)
        and all(item == "decrease" for item in available_directions),
        "weakly_decreasing": bool(available_directions)
        and all(item in ("decrease", "tie") for item in available_directions),
        "categorical_disqualifiers": sorted(set(disqualifiers)),
        "automatic_numeric_pass_threshold": None,
        "interpretation": (
            "Absence of categorical disqualifiers is evidence for further "
            "repeatability review, not calibration and not a grams claim."
        ),
    }
