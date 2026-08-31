"""Operator-guided, clean-room Phase 2 pressure-candidate diagnostic."""

import argparse
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
        reason_codes.append("extra_contact_identity_or_state_confound")
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

    expected_layout = profile["phase2_source_layout"]
    expected_profile_id = int(expected_layout["profile_id"])
    maximum_touch_count = int(expected_layout["maximum_touch_count"])

    stage_reports_by_cycle: Dict[int, Dict[str, Phase2StageReport]] = defaultdict(dict)
    cycle_reports: List[Dict[str, object]] = []
    for cycle in range(1, arguments.cycles + 1):
        for stage in PRESSURE_STAGES:
            window = next(
                item
                for item in stage_windows
                if item["cycle"] == cycle and item["stage"] == stage.label
            )
            frames = _frames_for_window(records, window)
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
        release_window = next(
            item
            for item in stage_windows
            if item["cycle"] == cycle and item["stage"] == "release"
        )
        release_frames = _frames_for_window(
            records, release_window, include_transition=True
        )
        no_contact_window = next(
            item
            for item in stage_windows
            if item["cycle"] == cycle and item["stage"] == "no_contact"
        )
        no_contact_frames = _frames_for_window(records, no_contact_window)
        contact_reasons = {
            reason
            for _, stage_report in ordered
            for reason in stage_report.excluded_frame_counts
            if reason
            in {
                "multiple_contacts",
                "zero_contact",
                "identity_change",
                "nonsteady_touch_state",
            }
            and stage_report.excluded_frame_counts[reason] > 0
        }
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
                "contact_confound_reasons": sorted(contact_reasons),
                "release_count_zero_observed": any(
                    frame.metadata.raw_touch_count_register == 0
                    for frame in release_frames
                ),
                "no_contact_baseline_clear": not any(
                    frame.metadata.raw_touch_count_register > 0
                    for frame in no_contact_frames
                ),
                "no_contact_callback_count": len(no_contact_frames),
                "operator_confirmed_all_settled_windows": bool(
                    arguments.confirm_stages
                ),
            }
        )

    whole_report = analyze_phase2_stage(
        "whole_capture",
        [frame for _, frame in records],
        expected_profile_id=expected_profile_id,
        maximum_touch_count=maximum_touch_count,
    )
    report["cycle_reports"] = cycle_reports
    report["whole_capture_analysis"] = whole_report.to_dict()
    report["across_cycles"] = _summarize_across_cycles(
        cycle_reports, stage_reports_by_cycle
    )
    phase2_stats = report["phase2_capture_stats"]
    phase1_stats = report["phase1_capture_stats"]
    assert isinstance(phase2_stats, dict)
    assert isinstance(phase1_stats, dict)
    expected_materialized = (
        phase2_stats["attempted_frame_count"]
        - phase2_stats["lock_contention_drop_count"]
        - phase2_stats["queue_overwrite_count"]
        - phase2_stats["queue_depth"]
    )
    report["phase2_materialization_accounting"] = {
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
    report["candidate_outcome"] = _classify_outcome(
        whole_report,
        phase2_stats,
        phase1_stats,
        cycle_reports,
        stage_reports_by_cycle,
        len(records),
    )
    report["multi_contact_policy"] = (
        "Multi-contact frames are retained per path_index and excluded from "
        "primary evidence; candidate values are never summed or aggregated."
    )
    report["known_geometry_limit"] = (
        "No contact-area/shape field has yet been verified. X/Y, zTotal, and "
        "zDensity are reported as available confound indicators, but cannot "
        "fully rule out fingertip-geometry effects."
    )
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
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
