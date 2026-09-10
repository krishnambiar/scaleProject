"""Live, operator-guided Phase 4 pressure-domain diagnostic.

This command exercises the Phase 3 application boundary and the pure-Python
Phase 4 stabilizer.  It never calibrates the candidate or assigns physical
units.  Its bundled parameters remain an exact-target, unvalidated starting
profile derived from the preserved Phase 2 capture.
"""

import argparse
import json
import math
import sys
import time
from collections import Counter, deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .models import RawFrame, TargetTouchState
from .phase3_sensor import RawFrameSensor
from .phase4_profile import (
    ExperimentalPhase4Profile,
    load_experimental_phase4_profile,
)
from .phase4_stabilizer import (
    PressureStabilizer,
    StabilizationUpdate,
    TareValidationError,
)
from .target_profile import compare_target_to_profile, current_target_fingerprint


class Phase4DiagnosticError(RuntimeError):
    """The live experiment could not produce trustworthy Phase 4 evidence."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one exact-target Phase 4 tare/stability trial in arbitrary raw "
            "sensor coordinates. No masses, grams, or calibration."
        )
    )
    parser.add_argument(
        "--transition-seconds",
        type=_positive_seconds,
        default=3.0,
        help="time to process the pressure transition before restarting filters",
    )
    parser.add_argument(
        "--measurement-seconds",
        type=_positive_seconds,
        default=6.0,
        help="settled pressure-domain observation time (default: 6)",
    )
    parser.add_argument(
        "--tare-timeout-seconds",
        type=_positive_seconds,
        default=12.0,
        help="maximum wait for one valid continuous tare segment (default: 12)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="write the replayable raw-domain evidence packet to this path",
    )
    return parser


def _frame_to_dict(frame: RawFrame) -> Dict[str, object]:
    return {
        "sequence": frame.sequence,
        "frame_number": frame.frame_number,
        "device_timestamp": frame.device_timestamp,
        "host_monotonic_ns": frame.host_monotonic_ns,
        "contacts": [asdict(contact) for contact in frame.contacts],
    }


def _update_to_dict(update: StabilizationUpdate) -> Dict[str, object]:
    value = asdict(update)
    value["status"] = update.status.value
    return value


def _is_tare_contact(frame: RawFrame) -> bool:
    return len(frame.contacts) == 1 and int(frame.contacts[0].state) == int(
        TargetTouchState.TOUCHING
    )


def _read_frame_before(
    sensor: RawFrameSensor,
    deadline: float,
    *,
    reason: str,
) -> RawFrame:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Phase4DiagnosticError(reason, "timed out waiting for a raw frame")
        frame = sensor.read_frame(timeout=min(0.25, remaining))
        if frame is not None:
            return frame
        if not sensor.is_running():
            raise Phase4DiagnosticError(reason, "sensor stopped before a frame arrived")


def _acquire_tare_segment(
    sensor: RawFrameSensor,
    profile: ExperimentalPhase4Profile,
    timeout_seconds: float,
) -> Tuple[Tuple[RawFrame, ...], Dict[str, int]]:
    required = profile.config.tare_window_samples
    candidate: Deque[RawFrame] = deque(maxlen=required)
    rejection_counts: Counter[str] = Counter()
    deadline = time.monotonic() + timeout_seconds

    while len(candidate) < required:
        frame = _read_frame_before(sensor, deadline, reason="tare_acquisition")
        if not _is_tare_contact(frame):
            rejection_counts["unsupported_contact"] += 1
            candidate.clear()
            continue
        if candidate:
            previous = candidate[-1]
            gap_seconds = (
                frame.host_monotonic_ns - previous.host_monotonic_ns
            ) / 1_000_000_000
            same_path = frame.contacts[0].path_index == previous.contacts[0].path_index
            if frame.sequence != previous.sequence + 1:
                rejection_counts["sequence_discontinuity"] += 1
                candidate.clear()
            elif (
                gap_seconds <= 0 or gap_seconds > profile.config.max_sample_gap_seconds
            ):
                rejection_counts["timestamp_discontinuity"] += 1
                candidate.clear()
            elif not same_path:
                rejection_counts["path_changed"] += 1
                candidate.clear()
        candidate.append(frame)

    return tuple(candidate), dict(sorted(rejection_counts.items()))


def _process_interval(
    sensor: RawFrameSensor,
    stabilizer: PressureStabilizer,
    duration_seconds: float,
    label: str,
    raw_frames: List[Dict[str, object]],
    event_updates: List[Dict[str, object]],
    status_counts: Counter[str],
) -> None:
    first_timestamp_ns: Optional[int] = None
    last_status: Optional[str] = None
    inactivity_deadline = time.monotonic() + max(2.0, duration_seconds + 2.0)

    while True:
        frame = _read_frame_before(
            sensor,
            inactivity_deadline,
            reason=f"{label}_capture",
        )
        inactivity_deadline = time.monotonic() + 2.0
        if first_timestamp_ns is None:
            first_timestamp_ns = frame.host_monotonic_ns
        raw_frames.append({"interval": label, "frame": _frame_to_dict(frame)})
        update = stabilizer.process(frame)
        status = update.status.value
        status_counts[status] += 1
        if status != last_status or update.published_measurement is not None:
            event_updates.append({"interval": label, "update": _update_to_dict(update)})
            last_status = status
        if stabilizer.tare_result is None:
            raise Phase4DiagnosticError(status, update.reason)
        elapsed = (frame.host_monotonic_ns - first_timestamp_ns) / 1_000_000_000
        if elapsed >= duration_seconds:
            return


def run_live_trial(
    sensor: RawFrameSensor,
    profile: ExperimentalPhase4Profile,
    *,
    transition_seconds: float,
    measurement_seconds: float,
    tare_timeout_seconds: float,
    prompt: Callable[[str], str] = input,
    announce: Callable[[str], None] = print,
) -> Dict[str, object]:
    """Run one live trial; the caller owns closing ``sensor``."""

    stabilizer = PressureStabilizer(profile.config)
    raw_frames: List[Dict[str, object]] = []
    event_updates: List[Dict[str, object]] = []
    status_counts: Counter[str] = Counter()
    started_utc = _utc_now()
    sensor.start()
    try:
        if sensor.supports_force() is not True:
            raise Phase4DiagnosticError(
                "force_capability",
                "built-in device did not report the required force capability",
            )
        prompt(
            "Place one fingertip near the center with only resting pressure. "
            "Keep the same contact and location, then press Return: "
        )

        pre_tare_discard_count = 0
        while True:
            queued = sensor.read_frame(timeout=0.0)
            if queued is None:
                break
            pre_tare_discard_count += 1

        tare_frames, tare_rejections = _acquire_tare_segment(
            sensor,
            profile,
            tare_timeout_seconds,
        )
        raw_frames.extend(
            {"interval": "tare", "frame": _frame_to_dict(frame)}
            for frame in tare_frames
        )
        try:
            tare_result = stabilizer.tare(tare_frames)
        except TareValidationError as error:
            raise Phase4DiagnosticError(error.reason, error.detail) from error

        announce(
            "Tare frozen. Increase to a clearly light downward pressure now; "
            "keep the same fingertip and location."
        )
        _process_interval(
            sensor,
            stabilizer,
            transition_seconds,
            "transition",
            raw_frames,
            event_updates,
            status_counts,
        )
        stabilizer.restart_stability_search()
        announce(
            "Measurement interval started. Hold that pressure and position steady."
        )
        _process_interval(
            sensor,
            stabilizer,
            measurement_seconds,
            "measurement",
            raw_frames,
            event_updates,
            status_counts,
        )
    finally:
        if sensor.is_running():
            sensor.stop()

    transition_publications = [
        event["update"]["published_measurement"]
        for event in event_updates
        if event["interval"] == "transition"
        and event["update"]["published_measurement"] is not None
    ]
    publications = [
        event["update"]["published_measurement"]
        for event in event_updates
        if event["interval"] == "measurement"
        and event["update"]["published_measurement"] is not None
    ]
    outcome = (
        "stable_raw_pressure_observed" if publications else "no_stable_publication"
    )
    return {
        "schema_version": 1,
        "phase": 4,
        "status": "experimental_unvalidated",
        "scope": "single_live_trial",
        "started_utc": started_utc,
        "completed_utc": _utc_now(),
        "units": "arbitrary raw sensor coordinate; not grams",
        "calibration_performed": False,
        "grams_claimed": False,
        "profile": asdict(profile),
        "protocol": {
            "transition_seconds": transition_seconds,
            "measurement_seconds": measurement_seconds,
            "tare_timeout_seconds": tare_timeout_seconds,
            "pre_tare_discard_count": pre_tare_discard_count,
            "tare_segment_rejection_counts": tare_rejections,
            "stability_search_restarted_after_transition": True,
        },
        "tare": asdict(tare_result),
        "status_counts": dict(sorted(status_counts.items())),
        "event_updates": event_updates,
        "publication_count": len(publications),
        "publications": publications,
        "transition_publication_count": len(transition_publications),
        "transition_publications": transition_publications,
        "outcome": outcome,
        "raw_frames": raw_frames,
        "phase1_capture_stats": sensor.capture_stats().to_dict(),
        "phase2_transport_stats": sensor.transport_stats().to_dict(),
    }


def _write_json(path: Path, report: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report_text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(report_text)
            stream.write("\n")
    except FileExistsError as error:
        raise Phase4DiagnosticError(
            "output_exists", f"refusing to overwrite {path}"
        ) from error


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    profile = load_experimental_phase4_profile()
    actual = current_target_fingerprint()
    expected = {"target": asdict(profile.source_phase2_artifact.target)}
    matches, mismatches = compare_target_to_profile(actual, expected)
    if not matches:
        print(
            "Phase 4 profile target mismatch: " + "; ".join(mismatches),
            file=sys.stderr,
        )
        return 2

    print(
        "EXPERIMENTAL PHASE 4 ONLY — arbitrary raw sensor coordinates; "
        "no grams or calibration."
    )
    try:
        with RawFrameSensor() as sensor:
            report = run_live_trial(
                sensor,
                profile,
                transition_seconds=args.transition_seconds,
                measurement_seconds=args.measurement_seconds,
                tare_timeout_seconds=args.tare_timeout_seconds,
            )
            report["actual_target"] = actual.to_dict()
    except KeyboardInterrupt:
        print(
            "Phase 4 diagnostic cancelled; no evidence file was written.",
            file=sys.stderr,
        )
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Phase 4 diagnostic failed: {error}", file=sys.stderr)
        return 1

    if args.json_out is not None:
        _write_json(args.json_out, report)
        print(f"Evidence written to {args.json_out}")
    print(
        json.dumps(
            {
                "outcome": report["outcome"],
                "publication_count": report["publication_count"],
                "tare": report["tare"],
                "status_counts": report["status_counts"],
                "units": report["units"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["publication_count"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
