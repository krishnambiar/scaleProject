"""Operator-guided, clean-room Phase 2 pressure-candidate diagnostic."""

import argparse
import hashlib
import json
import math
import signal
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import PHASE2_REQUIRED_COPIED_FIELDS, RawTouchFrame
from .native_bridge import Phase1BridgeError
from .phase2_analysis import (
    Phase2StageReport,
    analyze_phase2_stage,
    summarize_pressure_sequence,
)
from .phase2_sensor import TouchDiagnosticSensor
from .target_profile import (
    compare_target_to_profile,
    current_target_fingerprint,
    load_verified_profile,
)


@dataclass(frozen=True)
class PressureStage:
    label: str
    instruction: str
    pressure_plateau: bool


PRESSURE_STAGES = (
    PressureStage(
        "no_contact",
        "Remove every finger from the trackpad.",
        False,
    ),
    PressureStage(
        "rest",
        (
            "Place one fingertip near the center and merely rest it there, with "
            "no deliberate downward press. Keep this same finger and location "
            "through HARDER."
        ),
        True,
    ),
    PressureStage(
        "light",
        "At the same location, apply a clearly light downward press.",
        True,
    ),
    PressureStage(
        "medium",
        "At the same location, increase to a clearly medium downward press.",
        True,
    ),
    PressureStage(
        "harder",
        (
            "At the same location, press harder than MEDIUM while remaining "
            "comfortable; do not use an object or risk damaging the trackpad."
        ),
        True,
    ),
    PressureStage(
        "release",
        "Lift the finger completely and leave the trackpad clear.",
        False,
    ),
)


class Phase2DiagnosticError(RuntimeError):
    """Carries the partial evidence packet when a run cannot complete."""

    def __init__(self, message: str, report: Dict[str, object]) -> None:
        super().__init__(message)
        self.report = report


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _callback_clock_ns() -> int:
    """Use the exact POSIX clock selected by native monotonic_time_ns()."""

    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


class _FrameCollector:
    """Continuously drains both native queues while the operator reads prompts."""

    def __init__(self, sensor: TouchDiagnosticSensor) -> None:
        self._sensor = sensor
        self._records: List[Tuple[Dict[str, object], RawTouchFrame]] = []
        self._records_lock = threading.Lock()
        self._tag_lock = threading.Lock()
        self._tag: Dict[str, object] = {
            "cycle": 0,
            "stage": "startup",
            "period": "startup",
        }
        self._stop_event = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._run,
            name="phase2-frame-collector",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def mark(self, cycle: int, stage: str, period: str) -> None:
        with self._tag_lock:
            self._tag = {"cycle": cycle, "stage": stage, "period": period}

    def _run(self) -> None:
        try:
            while True:
                frame = self._sensor.read_frame(timeout=0.02)
                if frame is None:
                    if self._stop_event.is_set() and not self._sensor.is_running():
                        break
                    continue
                with self._tag_lock:
                    tag = dict(self._tag)
                tag["collected_utc"] = _utc_now()
                with self._records_lock:
                    self._records.append((tag, frame))
        except BaseException as error:
            self._error = error
            self._stop_event.set()

    def latest(self) -> Optional[RawTouchFrame]:
        with self._records_lock:
            return self._records[-1][1] if self._records else None

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise RuntimeError("Phase 2 collector thread did not stop")
        if self._error is not None:
            raise RuntimeError(
                f"Phase 2 collector failed: {self._error}"
            ) from self._error

    def records(self) -> List[Tuple[Dict[str, object], RawTouchFrame]]:
        with self._records_lock:
            return [(dict(tag), frame) for tag, frame in self._records]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record the exact-target raw pressure candidate during controlled "
            "rest/light/medium/harder/release cycles. No grams or calibration."
        )
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=3,
        help="complete pressure cycles to record (default: 3)",
    )
    parser.add_argument(
        "--stage-seconds",
        type=float,
        default=4.0,
        help="settled recording duration per stage (default: 4)",
    )
    parser.add_argument(
        "--lead-in",
        type=float,
        default=2.0,
        help="transition time after each operator mark (default: 2)",
    )
    parser.add_argument(
        "--confirm-stages",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="wait for Return before every transition (default: enabled)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="write raw frames and descriptive evidence to this JSON file",
    )
    parser.add_argument(
        "--reanalyze-json",
        type=Path,
        help=(
            "reassess a saved Phase 2 evidence file without loading the private "
            "framework or recapturing data"
        ),
    )
    return parser


def _event(
    events: List[Dict[str, object]],
    event_type: str,
    cycle: int = 0,
    stage: str = "",
    detail: str = "",
) -> Dict[str, object]:
    record = {
        "utc": _utc_now(),
        "host_monotonic_ns": _callback_clock_ns(),
        "event": event_type,
        "cycle": cycle,
        "stage": stage,
        "detail": detail,
    }
    events.append(record)
    return record


def _wait_with_status(
    collector: _FrameCollector, seconds: float, prefix: str
) -> None:
    deadline = time.monotonic() + seconds
    next_status = time.monotonic()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        now = time.monotonic()
        if now >= next_status:
            latest = collector.latest()
            if latest is None:
                status = "no callback yet"
            elif (
                latest.decode_status == 0
                and latest.copied_touch_count == 1
                and latest.touches
                and latest.touches[0].copied_fields
                == PHASE2_REQUIRED_COPIED_FIELDS
                and math.isfinite(latest.touches[0].pressure_candidate)
                and latest.touches[0].pressure_candidate != 43690.0
            ):
                touch = latest.touches[0]
                status = (
                    f"frame={latest.metadata.raw_frame_register} count=1 "
                    f"state={touch.state} candidate={touch.pressure_candidate:.6g} "
                    f"x={touch.normalized_x:.4f} y={touch.normalized_y:.4f} "
                    f"zTotal={touch.z_total:.6g} zDensity={touch.z_density:.6g}"
                )
            else:
                status = (
                    f"frame={latest.metadata.raw_frame_register} "
                    f"count={latest.copied_touch_count} "
                    f"decode=0x{latest.decode_status:08x}"
                )
            print(f"  {prefix}: {remaining:4.1f}s; {status}", flush=True)
            next_status = now + 1.0
        time.sleep(min(0.05, remaining))


def _frames_for_window(
    records: Iterable[Tuple[Dict[str, object], RawTouchFrame]],
    window: Dict[str, object],
    include_transition: bool = False,
) -> List[RawTouchFrame]:
    start_key = "transition_start_ns" if include_transition else "settled_start_ns"
    start = int(window[start_key])
    end = int(window["settled_end_ns"])
    return [
        frame
        for _, frame in records
        if start <= frame.metadata.host_monotonic_ns < end
    ]


def _classify_callback_window(
    frame: RawTouchFrame, windows: Sequence[Dict[str, object]]
) -> Dict[str, object]:
    timestamp = frame.metadata.host_monotonic_ns
    for window in windows:
        transition_start = int(window["transition_start_ns"])
        settled_start = int(window["settled_start_ns"])
        settled_end = int(window["settled_end_ns"])
        if transition_start <= timestamp < settled_start:
            return {
                "cycle": window["cycle"],
                "stage": window["stage"],
                "period": "transition",
            }
        if settled_start <= timestamp < settled_end:
            return {
                "cycle": window["cycle"],
                "stage": window["stage"],
                "period": "settled",
            }
    return {"cycle": None, "stage": None, "period": "outside_marked_windows"}


def _median_and_mad(values: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    if not values:
        return None, None
    center = median(values)
    return center, median(abs(value - center) for value in values)


def _summarize_across_cycles(
    cycle_reports: Sequence[Dict[str, object]],
    stage_reports_by_cycle: Dict[int, Dict[str, Phase2StageReport]],
) -> Dict[str, object]:
    per_label: Dict[str, object] = {}
    for label in ("rest", "light", "medium", "harder"):
        values = [
            stage_reports_by_cycle[cycle][label].pressure_candidate.median
            for cycle in sorted(stage_reports_by_cycle)
        ]
        finite_values = [
            float(value)
            for value in values
            if value is not None and math.isfinite(value)
        ]
        center, mad = _median_and_mad(finite_values)
        per_label[label] = {
            "cycle_medians": values,
            "minimum_cycle_median": min(finite_values) if finite_values else None,
            "maximum_cycle_median": max(finite_values) if finite_values else None,
            "median_of_cycle_medians": center,
            "mad_of_cycle_medians": mad,
            "first_to_last_drift": (
                finite_values[-1] - finite_values[0]
                if len(finite_values) >= 2
                else None
            ),
        }

    return_to_rest = []
    ordered_cycles = sorted(stage_reports_by_cycle)
    for previous_cycle, next_cycle in zip(ordered_cycles, ordered_cycles[1:]):
        previous = stage_reports_by_cycle[previous_cycle]["rest"].pressure_candidate.median
        following = stage_reports_by_cycle[next_cycle]["rest"].pressure_candidate.median
        return_to_rest.append(
            {
                "from_cycle": previous_cycle,
                "to_cycle": next_cycle,
                "previous_rest_median": previous,
                "next_rest_median": following,
                "delta": (
                    following - previous
                    if previous is not None and following is not None
                    else None
                ),
            }
        )

    direction_signatures = [
        tuple(
            item["direction"]
            for item in report["pressure_sequence"][
                "adjacent_median_comparisons"
            ]
        )
        for report in cycle_reports
        if report["complete_pressure_plateaus"]
    ]
    return {
        "complete_cycle_count": sum(
            bool(report["complete_pressure_plateaus"]) for report in cycle_reports
        ),
        "incomplete_cycle_count": sum(
            not bool(report["complete_pressure_plateaus"]) for report in cycle_reports
        ),
        "confounded_cycle_count": sum(
            bool(report["contact_confound"]) for report in cycle_reports
        ),
        "release_confirmed_cycle_count": sum(
            bool(report["release_count_zero_observed"]) for report in cycle_reports
        ),
        "complete_cycles_share_one_direction_signature": (
            len(set(direction_signatures)) == 1 if direction_signatures else None
        ),
        "direction_signatures": [list(item) for item in direction_signatures],
        "per_label": per_label,
        "return_to_rest": return_to_rest,
        "automatic_numeric_pass_threshold": None,
    }


def _contact_confound_reasons(
    ordered_stages: Sequence[Tuple[str, Phase2StageReport]],
) -> List[str]:
    """Return only contact facts established by count and lifecycle state.

    `path_index` is the observed contact-track continuity key.  The other copied
    identity fields remain raw classification codes, so their variation while
    the verified touch count stays at one is descriptive rather than proof of a
    second contact.
    """

    confounds = {
        reason
        for _, stage_report in ordered_stages
        for reason in stage_report.excluded_frame_counts
        if reason
        in {
            "multiple_contacts",
            "zero_contact",
            "nonsteady_touch_state",
            "path_change",
        }
        and stage_report.excluded_frame_counts[reason] > 0
    }
    return sorted(confounds)


def _classify_outcome(
    whole_report: Phase2StageReport,
    phase2_stats: Dict[str, int],
    phase1_stats: Dict[str, int],
    cycle_reports: Sequence[Dict[str, object]],
    stage_reports_by_cycle: Dict[int, Dict[str, Phase2StageReport]],
    materialized_frame_count: int,
) -> Dict[str, object]:
    abi_reasons = []
    for reason in (
        "layout_profile_mismatch",
        "count_exceeds_profile_maximum",
        "count_or_materialization_mismatch",
        "unexpected_copied_fields_mask",
    ):
        if whole_report.excluded_frame_counts.get(reason, 0):
            abi_reasons.append(reason)
    for key in (
        "invalid_count_frame_count",
        "null_records_frame_count",
        "device_mismatch_frame_count",
        "record_frame_mismatch_touch_count",
        "record_timestamp_mismatch_touch_count",
    ):
        if phase2_stats.get(key, 0):
            abi_reasons.append(key)
    if phase1_stats.get("callback_device_mismatch_count", 0):
        abi_reasons.append("phase1_callback_device_mismatch_count")
    accounted_materialized = (
        phase2_stats.get("attempted_frame_count", 0)
        - phase2_stats.get("lock_contention_drop_count", 0)
        - phase2_stats.get("queue_overwrite_count", 0)
        - phase2_stats.get("queue_depth", 0)
    )
    if accounted_materialized != materialized_frame_count:
        abi_reasons.append("phase2_materialization_accounting_mismatch")
    if phase2_stats.get("invalid_state_touch_count", 0):
        abi_reasons.append("invalid_state_touch_count")
    if any(
        key.startswith("decode_flag_UNKNOWN_BITS_") and count > 0
        for key, count in whole_report.excluded_frame_counts.items()
    ):
        abi_reasons.append("unknown_decode_status_bits")
    if whole_report.transport.get("non_finite_timestamps", 0):
        abi_reasons.append("nonfinite_device_timestamp")
    if whole_report.transport.get("regressing_timestamp_steps", 0):
        abi_reasons.append("regressing_device_timestamp")
    if whole_report.transport.get("regressing_frame_steps", 0):
        abi_reasons.append("regressing_frame_register")
    if whole_report.callback_sequence_nonincreasing_steps:
        abi_reasons.append("nonincreasing_callback_sequence")
    if whole_report.host_monotonic_regressing_steps:
        abi_reasons.append("regressing_host_monotonic_timestamp")

    all_pressure_reports = [
        reports[label].pressure_candidate
        for reports in stage_reports_by_cycle.values()
        for label in ("rest", "light", "medium", "harder")
    ]
    nonempty = [report for report in all_pressure_reports if report.count]
    reason_codes: List[str] = []
    if abi_reasons:
        outcome = "rejected_abi_integrity"
        reason_codes.extend(abi_reasons)
    elif phase2_stats.get("pressure_sentinel_touch_count", 0):
        outcome = "rejected_sentinel"
        reason_codes.append("pressure_sentinel_observed")
    elif phase2_stats.get("nonfinite_touch_count", 0):
        outcome = "rejected_nonfinite"
        reason_codes.append("nonfinite_touch_observed")
    elif any(
        not report["release_count_zero_observed"]
        or not report["no_contact_baseline_clear"]
        or not report["operator_confirmed_all_settled_windows"]
        for report in cycle_reports
    ):
        outcome = "inconclusive_incomplete"
        for report in cycle_reports:
            cycle = report["cycle"]
            if not report["release_count_zero_observed"]:
                reason_codes.append(f"cycle_{cycle}:release_zero_not_observed")
            if not report["no_contact_baseline_clear"]:
                reason_codes.append(f"cycle_{cycle}:no_contact_baseline_not_clear")
            if not report["operator_confirmed_all_settled_windows"]:
                reason_codes.append(f"cycle_{cycle}:settled_windows_not_confirmed")
    elif any(report["contact_confound"] for report in cycle_reports):
        outcome = "inconclusive_contact_confound"
        reason_codes.append("contact_count_state_or_path_confound")
    elif any(not report["complete_pressure_plateaus"] for report in cycle_reports):
        outcome = "inconclusive_incomplete"
        for report in cycle_reports:
            if not report["complete_pressure_plateaus"]:
                reason_codes.append(
                    f"cycle_{report['cycle']}:missing_pressure_plateau"
                )
    elif not nonempty:
        outcome = "rejected_absent"
        reason_codes.append("no_valid_settled_candidate_samples")
    elif all(report.zero_count == report.count for report in nonempty):
        outcome = "rejected_zero_only"
        reason_codes.append("all_valid_settled_candidates_are_zero")
    elif all(
        report.minimum == report.maximum == nonempty[0].minimum
        for report in nonempty
    ):
        outcome = "rejected_constant"
        reason_codes.append("candidate_constant_across_all_settled_levels")
    else:
        sequence_disqualifiers = [
            item
            for report in cycle_reports
            for item in report["pressure_sequence"]["categorical_disqualifiers"]
        ]
        inconsistent = len(
            {
                tuple(
                    item["direction"]
                    for item in report["pressure_sequence"][
                        "adjacent_median_comparisons"
                    ]
                )
                for report in cycle_reports
            }
        ) > 1
        if sequence_disqualifiers or inconsistent:
            outcome = "inconclusive_monotonicity"
            reason_codes.extend(sequence_disqualifiers)
            if inconsistent:
                reason_codes.append("cycle_direction_signatures_differ")
        else:
            outcome = "human_review_required"
            reason_codes.append(
                "review_position_and_geometry_summaries_before_marking_"
                "supported_for_further_validation"
            )

    return {
        "outcome": outcome,
        "reason_codes": sorted(set(reason_codes)),
        "abi_integrity_reasons": sorted(set(abi_reasons)),
        "queue_loss_is_reported_without_an_invented_threshold": True,
        "human_review_may_assign_supported_for_further_validation": (
            outcome == "human_review_required"
        ),
        "calibration_authorized": False,
        "grams_claimed": False,
    }


def _derive_capture_analysis(
    report: Dict[str, object],
    records: Sequence[Tuple[Dict[str, object], RawTouchFrame]],
) -> Dict[str, object]:
    """Derive the Phase 2 assessment from preserved raw capture evidence."""

    expected_layout = report.get("expected_phase2_source_layout")
    stage_windows = report.get("stage_windows")
    phase2_stats = report.get("phase2_capture_stats")
    phase1_stats = report.get("phase1_capture_stats")
    if not isinstance(expected_layout, dict):
        raise ValueError("saved report is missing the Phase 2 source layout")
    if not isinstance(stage_windows, list):
        raise ValueError("saved report is missing stage windows")
    if not isinstance(phase2_stats, dict) or not isinstance(phase1_stats, dict):
        raise ValueError("saved report is missing capture statistics")

    requested_cycles = int(report.get("requested_cycles", 0))
    if requested_cycles <= 0:
        raise ValueError("saved report has an invalid requested cycle count")
    expected_profile_id = int(expected_layout["profile_id"])
    maximum_touch_count = int(expected_layout["maximum_touch_count"])

    def window_for(cycle: int, stage: str) -> Dict[str, object]:
        matches = [
            item
            for item in stage_windows
            if isinstance(item, dict)
            and item.get("cycle") == cycle
            and item.get("stage") == stage
        ]
        if len(matches) != 1:
            raise ValueError(
                f"saved report must contain exactly one window for "
                f"cycle {cycle} stage {stage}"
            )
        return matches[0]

    stage_reports_by_cycle: Dict[int, Dict[str, Phase2StageReport]] = defaultdict(dict)
    cycle_reports: List[Dict[str, object]] = []
    for cycle in range(1, requested_cycles + 1):
        cycle_windows = {
            stage.label: window_for(cycle, stage.label)
            for stage in PRESSURE_STAGES
        }
        for stage in PRESSURE_STAGES:
            frames = _frames_for_window(records, cycle_windows[stage.label])
            stage_reports_by_cycle[cycle][stage.label] = analyze_phase2_stage(
                f"cycle_{cycle}_{stage.label}",
                frames,
                expected_profile_id=expected_profile_id,
                maximum_touch_count=maximum_touch_count,
            )
        ordered = [
            (label, stage_reports_by_cycle[cycle][label])
            for label in ("rest", "light", "medium", "harder")
        ]
        sequence = summarize_pressure_sequence(ordered)
        release_frames = _frames_for_window(
            records,
            cycle_windows["release"],
            include_transition=True,
        )
        no_contact_frames = _frames_for_window(
            records,
            cycle_windows["no_contact"],
        )
        contact_reasons = _contact_confound_reasons(ordered)
        cycle_reports.append(
            {
                "cycle": cycle,
                "stages": {
                    label: report_item.to_dict()
                    for label, report_item in stage_reports_by_cycle[cycle].items()
                },
                "pressure_sequence": sequence,
                "complete_pressure_plateaus": all(
                    report_item.primary_sample_count > 0
                    for _, report_item in ordered
                ),
                "contact_confound": bool(contact_reasons),
                "contact_confound_reasons": contact_reasons,
                "release_count_zero_observed": any(
                    frame.metadata.raw_touch_count_register == 0
                    for frame in release_frames
                ),
                "no_contact_baseline_clear": not any(
                    frame.metadata.raw_touch_count_register > 0
                    for frame in no_contact_frames
                ),
                "no_contact_callback_count": len(no_contact_frames),
                "operator_confirmed_all_settled_windows": all(
                    bool(window.get("operator_confirmed_settled"))
                    for window in cycle_windows.values()
                ),
            }
        )

    whole_report = analyze_phase2_stage(
        "whole_capture",
        [frame for _, frame in records],
        expected_profile_id=expected_profile_id,
        maximum_touch_count=maximum_touch_count,
    )
    expected_materialized = (
        int(phase2_stats.get("attempted_frame_count", 0))
        - int(phase2_stats.get("lock_contention_drop_count", 0))
        - int(phase2_stats.get("queue_overwrite_count", 0))
        - int(phase2_stats.get("queue_depth", 0))
    )
    materialization = {
        "attempted_frame_count": phase2_stats["attempted_frame_count"],
        "lock_contention_drop_count": phase2_stats[
            "lock_contention_drop_count"
        ],
        "queue_overwrite_count": phase2_stats["queue_overwrite_count"],
        "final_queue_depth": phase2_stats["queue_depth"],
        "expected_materialized_frame_count": expected_materialized,
        "observed_materialized_frame_count": len(records),
        "balances": expected_materialized == len(records),
    }
    return {
        "analysis_rule_revision": "single-contact-path-continuity-v2",
        "cycle_reports": cycle_reports,
        "whole_capture_analysis": whole_report.to_dict(),
        "across_cycles": _summarize_across_cycles(
            cycle_reports, stage_reports_by_cycle
        ),
        "phase2_materialization_accounting": materialization,
        "candidate_outcome": _classify_outcome(
            whole_report,
            phase2_stats,
            phase1_stats,
            cycle_reports,
            stage_reports_by_cycle,
            len(records),
        ),
        "multi_contact_policy": (
            "Multi-contact frames are retained per path_index and excluded from "
            "primary evidence; candidate values are never summed or aggregated."
        ),
        "identity_code_policy": (
            "path_index is used for contact-track continuity. finger_id and "
            "hand_id remain descriptive raw codes and do not independently "
            "establish an extra contact."
        ),
        "known_geometry_limit": (
            "No contact-area/shape field has yet been verified. X/Y, zTotal, and "
            "zDensity are reported as available confound indicators, but cannot "
            "fully rule out fingertip-geometry effects."
        ),
    }


def run(arguments: argparse.Namespace) -> Dict[str, object]:
    if arguments.cycles <= 0:
        raise ValueError("cycles must be positive")
    if not math.isfinite(arguments.stage_seconds) or arguments.stage_seconds <= 0:
        raise ValueError("stage-seconds must be positive")
    if not math.isfinite(arguments.lead_in) or arguments.lead_in < 0:
        raise ValueError("lead-in must be non-negative")

    profile = load_verified_profile()
    scope = str(profile.get("scope", ""))
    if "Phase 2" not in scope:
        raise RuntimeError("packaged target profile does not authorize Phase 2")
    fingerprint = current_target_fingerprint()
    target_matches, mismatches = compare_target_to_profile(fingerprint, profile)
    if not target_matches:
        raise RuntimeError(
            "exact Phase 2 target mismatch: " + "; ".join(mismatches)
        )

    report: Dict[str, object] = {
        "schema_version": 1,
        "phase": 2,
        "experiment": "raw pressure candidate ordinal response",
        "started_utc": _utc_now(),
        "target_profile_match": target_matches,
        "target_profile_mismatches": mismatches,
        "actual_target": fingerprint.to_dict(),
        "expected_target": profile["target"],
        "profile_scope": scope,
        "expected_phase2_source_layout": profile["phase2_source_layout"],
        "start_options": 0,
        "requested_cycles": arguments.cycles,
        "stage_seconds": arguments.stage_seconds,
        "lead_in_seconds": arguments.lead_in,
        "operator_confirmed_stages": bool(arguments.confirm_stages),
        "units": "raw sensor coordinates; not grams",
        "calibration_performed": False,
        "automatic_numeric_pass_threshold": None,
        "operator_events": [],
        "stage_windows": [],
        "raw_frames": [],
    }
    events = report["operator_events"]
    assert isinstance(events, list)
    stage_windows = report["stage_windows"]
    assert isinstance(stage_windows, list)

    sensor: Optional[TouchDiagnosticSensor] = None
    collector: Optional[_FrameCollector] = None
    stop_error: Optional[str] = None
    collector_error: Optional[str] = None
    stats_error: Optional[str] = None
    close_error: Optional[str] = None
    run_error: Optional[BaseException] = None
    try:
        sensor = TouchDiagnosticSensor()
        if sensor.is_built_in() is not True or sensor.supports_force() is not True:
            raise RuntimeError(
                "Phase 2 requires the exact built-in Force Touch device"
            )
        report["preflight_status"] = "accepted"
        report["framework_path"] = sensor.framework_path
        report["device_is_built_in"] = sensor.is_built_in()
        report["device_supports_force"] = sensor.supports_force()
        report["validated_bridge_abi"] = sensor.abi_evidence
        print("Phase 2 preflight accepted for the exact checked-in target.")
        print(f"Profile: {sensor.profile_name}")
        print("Candidate values are raw sensor coordinates, not grams.")
        print(
            "Protocol: NO_CONTACT -> REST -> LIGHT -> MEDIUM -> HARDER -> "
            "RELEASE, repeated without changing finger or location."
        )
        if arguments.confirm_stages:
            input("Press Return when you are ready to start the capture: ")
        _event(events, "capture_start_requested")
        sensor.start()
        report["start_native_status"] = sensor.last_start_native_status
        collector = _FrameCollector(sensor)
        collector.start()
        _event(events, "capture_started")

        for cycle in range(1, arguments.cycles + 1):
            print(f"\n=== Cycle {cycle}/{arguments.cycles} ===", flush=True)
            for stage in PRESSURE_STAGES:
                collector.mark(cycle, stage.label, "operator_wait")
                _event(events, "instruction_presented", cycle, stage.label, stage.instruction)
                print(f"\n{stage.label.upper()}: {stage.instruction}", flush=True)
                if arguments.confirm_stages:
                    input("Press Return when you are ready to make this change: ")
                _event(events, "operator_began_transition", cycle, stage.label)
                collector.mark(cycle, stage.label, "transition")
                transition_event = _event(
                    events, "transition_window_started", cycle, stage.label
                )
                _wait_with_status(collector, arguments.lead_in, "transition")
                if arguments.confirm_stages:
                    input(
                        "Press Return only when this pose/pressure is steady; "
                        "recording begins immediately: "
                    )
                    _event(events, "operator_confirmed_settled", cycle, stage.label)
                settled_start_event = _event(
                    events, "settled_window_started", cycle, stage.label
                )
                collector.mark(cycle, stage.label, "settled")
                _wait_with_status(collector, arguments.stage_seconds, "recording")
                settled_end_event = _event(
                    events, "settled_window_ended", cycle, stage.label
                )
                stage_windows.append(
                    {
                        "cycle": cycle,
                        "stage": stage.label,
                        "operator_confirmed_settled": bool(
                            arguments.confirm_stages
                        ),
                        "transition_start_ns": transition_event[
                            "host_monotonic_ns"
                        ],
                        "settled_start_ns": settled_start_event[
                            "host_monotonic_ns"
                        ],
                        "settled_end_ns": settled_end_event[
                            "host_monotonic_ns"
                        ],
                    }
                )

        collector.mark(arguments.cycles, "shutdown", "shutdown")
    except (Exception, KeyboardInterrupt) as error:
        run_error = error
    finally:
        if sensor is not None and sensor.is_running():
            try:
                sensor.stop()
            except Exception as error:
                stop_error = str(error)
        if collector is not None:
            try:
                collector.stop()
            except Exception as error:
                collector_error = str(error)
        if sensor is not None:
            try:
                sensor.drain_phase1_metadata()
                report["phase1_capture_stats"] = sensor.capture_stats().to_dict()
                report["phase2_capture_stats"] = sensor.phase2_stats().to_dict()
            except Exception as error:
                stats_error = str(error)
            try:
                sensor.close()
            except Exception as error:
                close_error = str(error)
            report["stop_native_status"] = sensor.last_stop_native_status
        report["stop_error"] = stop_error
        report["collector_error"] = collector_error
        report["stats_error"] = stats_error
        report["close_error"] = close_error

    records = collector.records() if collector is not None else []
    report["raw_frames"] = [
        {
            "collection_tag": tag,
            "callback_window": _classify_callback_window(frame, stage_windows),
            "frame": frame.to_dict(),
        }
        for tag, frame in records
    ]
    cleanup_errors = [
        value
        for value in (stop_error, collector_error, stats_error, close_error)
        if value
    ]
    if run_error is not None or cleanup_errors or collector is None:
        error = run_error or RuntimeError(
            "; ".join(cleanup_errors)
            if cleanup_errors
            else "capture collector was not created"
        )
        report["completed"] = False
        report["completed_utc"] = _utc_now()
        report["error_type"] = type(error).__name__
        report["error"] = str(error)
        report["preflight_status"] = report.get(
            "preflight_status", "rejected_or_interrupted"
        )
        raise Phase2DiagnosticError(str(error), report) from error

    report.update(_derive_capture_analysis(report, records))
    report["completed_utc"] = _utc_now()
    report["completed"] = True

    print("\nPhase 2 descriptive outcome:")
    print(json.dumps(report["candidate_outcome"], indent=2, sort_keys=True))
    print("No calibration or gram conversion was performed.")
    return report


def _write_report(path: Path, report: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _validate_reassessment_source(source: Dict[str, object]) -> None:
    """Reject saved evidence that lacks the original capture provenance."""

    reasons: List[str] = []
    if source.get("schema_version") != 1:
        reasons.append("unsupported_schema_version")
    if source.get("phase") != 2:
        reasons.append("not_a_phase2_report")
    if source.get("experiment") != "raw pressure candidate ordinal response":
        reasons.append("unexpected_experiment_type")
    if source.get("completed") is not True:
        reasons.append("capture_not_completed")
    if source.get("preflight_status") != "accepted":
        reasons.append("preflight_not_accepted")
    if source.get("target_profile_match") is not True:
        reasons.append("target_profile_not_matched")
    if source.get("target_profile_mismatches") != []:
        reasons.append("target_profile_mismatches_present")
    if source.get("start_options") != 0:
        reasons.append("unverified_start_options")
    if source.get("start_native_status") != 0:
        reasons.append("capture_start_not_successful")
    if source.get("stop_native_status") != 0:
        reasons.append("capture_stop_not_successful")
    if not isinstance(source.get("completed_utc"), str):
        reasons.append("completion_timestamp_missing")
    for key in ("stop_error", "collector_error", "stats_error", "close_error"):
        if source.get(key) is not None:
            reasons.append(f"{key}_present")

    profile = load_verified_profile()
    expected_target = profile.get("target")
    if source.get("expected_target") != expected_target:
        reasons.append("expected_target_differs_from_packaged_profile")
    if source.get("actual_target") != expected_target:
        reasons.append("captured_target_differs_from_packaged_profile")

    saved_layout = source.get("expected_phase2_source_layout")
    packaged_layout = profile.get("phase2_source_layout")
    layout_identity_keys = (
        "profile_id",
        "profile_name",
        "descriptor_version",
        "record_size",
        "maximum_touch_count",
        "fields",
        "decoder_guards",
    )
    if not isinstance(saved_layout, dict) or not isinstance(packaged_layout, dict):
        reasons.append("phase2_layout_missing")
    elif any(
        saved_layout.get(key) != packaged_layout.get(key)
        for key in layout_identity_keys
    ):
        reasons.append("phase2_layout_differs_from_packaged_profile")

    bridge = source.get("validated_bridge_abi")
    if not isinstance(bridge, dict) or not isinstance(packaged_layout, dict):
        reasons.append("validated_bridge_abi_missing")
    else:
        if bridge.get("bridge_abi_version") != 1:
            reasons.append("unexpected_bridge_abi_version")
        if bridge.get("profile_id") != packaged_layout.get("profile_id"):
            reasons.append("bridge_profile_id_mismatch")
        if bridge.get("profile_name") != packaged_layout.get("profile_name"):
            reasons.append("bridge_profile_name_mismatch")
        expected_sizes = [64, 2104, 104, 128]
        if bridge.get("native_project_owned_struct_sizes") != expected_sizes:
            reasons.append("native_project_owned_struct_sizes_mismatch")
        if bridge.get("python_project_owned_struct_sizes") != expected_sizes:
            reasons.append("python_project_owned_struct_sizes_mismatch")
        if bridge.get("output_layout_fingerprint") != "0xcc805c390dc2e7c1":
            reasons.append("output_layout_fingerprint_mismatch")

        bridge_layout = bridge.get("source_layout")
        if not isinstance(bridge_layout, dict):
            reasons.append("bridge_source_layout_missing")
        else:
            expected_bridge_layout = {
                "descriptor_version": packaged_layout.get("descriptor_version"),
                "profile_id": packaged_layout.get("profile_id"),
                "record_size": packaged_layout.get("record_size"),
                "maximum_touch_count": packaged_layout.get(
                    "maximum_touch_count"
                ),
            }
            fields = packaged_layout.get("fields")
            if isinstance(fields, dict):
                for name, field in fields.items():
                    if isinstance(field, dict):
                        expected_bridge_layout[f"{name}_offset"] = field.get(
                            "offset"
                        )
                        expected_bridge_layout[f"{name}_size"] = field.get("size")
            if any(
                bridge_layout.get(key) != value
                for key, value in expected_bridge_layout.items()
            ):
                reasons.append("bridge_source_layout_mismatch")

    if reasons:
        raise ValueError(
            "saved evidence is not eligible for reassessment: "
            + ", ".join(sorted(set(reasons)))
        )


def reassess_saved_report(path: Path) -> Dict[str, object]:
    """Recompute derived findings from immutable raw frames and stage windows."""

    source_path = path.resolve()
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    if not isinstance(source, dict):
        raise ValueError("saved Phase 2 evidence must be a JSON object")
    _validate_reassessment_source(source)
    raw_frames = source.get("raw_frames")
    if not isinstance(raw_frames, list):
        raise ValueError("saved Phase 2 evidence is missing raw_frames")

    records: List[Tuple[Dict[str, object], RawTouchFrame]] = []
    for index, item in enumerate(raw_frames):
        if not isinstance(item, dict):
            raise ValueError(f"raw_frames[{index}] must be an object")
        collection_tag = item.get("collection_tag", {})
        frame_value = item.get("frame")
        if not isinstance(collection_tag, dict):
            raise ValueError(f"raw_frames[{index}].collection_tag must be an object")
        if not isinstance(frame_value, dict):
            raise ValueError(f"raw_frames[{index}].frame must be an object")
        records.append(
            (dict(collection_tag), RawTouchFrame.from_dict(frame_value))
        )

    derived = _derive_capture_analysis(source, records)
    return {
        "schema_version": 1,
        "phase": 2,
        "experiment": "saved raw pressure candidate reassessment",
        "reassessment_of": str(source_path),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "reassessed_utc": _utc_now(),
        "reassessment_basis": (
            "original raw frames and original callback-time stage windows; no "
            "private framework call and no recapture"
        ),
        "source_provenance_status": "accepted",
        "source_completed": source.get("completed"),
        "source_candidate_outcome": source.get("candidate_outcome"),
        "target_profile_match": source.get("target_profile_match"),
        "expected_phase2_source_layout": source.get(
            "expected_phase2_source_layout"
        ),
        "requested_cycles": source.get("requested_cycles"),
        "raw_frame_count": len(records),
        "phase1_capture_stats": source.get("phase1_capture_stats"),
        "phase2_capture_stats": source.get("phase2_capture_stats"),
        "units": "raw sensor coordinates; not grams",
        "calibration_performed": False,
        "automatic_numeric_pass_threshold": None,
        **derived,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.reanalyze_json is not None:
        output = arguments.json_out or arguments.reanalyze_json.with_name(
            f"{arguments.reanalyze_json.stem}-reassessed.json"
        )
        if output.resolve() == arguments.reanalyze_json.resolve():
            parser.error("reassessment output must not overwrite its source evidence")
        try:
            report = reassess_saved_report(arguments.reanalyze_json)
            _write_report(output, report)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            parser.error(str(error))
        print("\nPhase 2 reassessed outcome:")
        print(json.dumps(report["candidate_outcome"], indent=2, sort_keys=True))
        print(f"Reassessment written to {output.resolve()}")
        print("No private framework call, recapture, calibration, or grams conversion.")
        return 0
    try:
        report = run(arguments)
        if arguments.json_out is not None:
            _write_report(arguments.json_out, report)
            print(f"Evidence written to {arguments.json_out.resolve()}")
        return 0
    except Phase2DiagnosticError as error:
        if arguments.json_out is not None:
            _write_report(arguments.json_out, error.report)
            print(
                f"Partial failure evidence written to "
                f"{arguments.json_out.resolve()}",
                file=sys.stderr,
            )
        print(f"Phase 2 diagnostic failed: {error}", file=sys.stderr)
        return 1
    except (
        Phase1BridgeError,
        RuntimeError,
        ValueError,
        OSError,
        EOFError,
        KeyboardInterrupt,
    ) as error:
        failure = {
            "schema_version": 1,
            "phase": 2,
            "completed": False,
            "preflight_status": "rejected_or_interrupted",
            "error_type": type(error).__name__,
            "error": str(error),
            "units": "raw sensor coordinates; not grams",
            "calibration_performed": False,
        }
        if arguments.json_out is not None:
            _write_report(arguments.json_out, failure)
            print(
                f"Failure evidence written to {arguments.json_out.resolve()}",
                file=sys.stderr,
            )
        print(f"Phase 2 diagnostic failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
