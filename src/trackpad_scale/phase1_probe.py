"""Command-line diagnostic for the clean-room Phase 1 milestone."""

import argparse
import json
import signal
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .models import FrameMetadata
from .native_bridge import Phase1BridgeError
from .reliability import StageReport, analyze_stage
from .sensor import TrackpadSensor
from .target_profile import (
    compare_target_to_profile,
    current_target_fingerprint,
    load_verified_profile,
)


@dataclass(frozen=True)
class Stage:
    label: str
    seconds: float
    expected_touch_count: Optional[int]
    instruction: str


GUIDED_STAGES = (
    Stage(
        "clear_before",
        4.0,
        None,
        (
            "Remove every finger from the trackpad. Idle may be silent; "
            "release may emit one terminal count-zero frame."
        ),
    ),
    Stage("one_finger", 6.0, 1, "Rest exactly one finger on the trackpad."),
    Stage("two_fingers", 6.0, 2, "Rest exactly two fingers on the trackpad."),
    Stage(
        "clear_after",
        4.0,
        None,
        (
            "Remove every finger from the trackpad again. Idle may be silent; "
            "release may emit one terminal count-zero frame."
        ),
    ),
)


def parse_integer(value: str) -> int:
    return int(value, 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture only frame numbers, touch counts, and timestamps. "
            "Touch-record memory remains opaque."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--guided",
        action="store_true",
        help="run the timed 0/1/2/0 physical-contact protocol",
    )
    mode.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="seconds for a single unlabelled observation (default: 10)",
    )
    parser.add_argument(
        "--lead-in",
        type=float,
        default=3.0,
        help="guided-mode preparation seconds before each stage (default: 3)",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="number of complete guided 0/1/2/0 trials (default: 1)",
    )
    parser.add_argument(
        "--confirm-stages",
        action="store_true",
        help="wait for Return before each guided-stage countdown",
    )
    parser.add_argument(
        "--start-options",
        type=parse_integer,
        default=0,
        help=(
            "explicit MTDeviceStart option register value (default: verified value "
            "0; nonzero requires --allow-unverified-target)"
        ),
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="write the complete evidence report to this JSON file",
    )
    parser.add_argument(
        "--allow-unverified-target",
        action="store_true",
        help=(
            "allow diagnostic experimentation when OS/hardware/framework no longer "
            "matches the checked-in ABI profile"
        ),
    )
    return parser


def _record_frame(
    frame: FrameMetadata,
    primary: List[FrameMetadata],
    overall: List[FrameMetadata],
) -> None:
    primary.append(frame)
    overall.append(frame)


def _drain_available(
    sensor: TrackpadSensor,
    frames: List[FrameMetadata],
    overall: List[FrameMetadata],
) -> None:
    while True:
        frame = sensor.read_frame(timeout=0)
        if frame is None:
            return
        _record_frame(frame, frames, overall)


def _observe_stage(
    sensor: TrackpadSensor,
    stage: Stage,
    overall: List[FrameMetadata],
) -> StageReport:
    frames: List[FrameMetadata] = []
    deadline = time.monotonic() + stage.seconds
    next_status = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        frame = sensor.read_frame(timeout=0.02)
        if frame is not None:
            _record_frame(frame, frames, overall)
            _drain_available(sensor, frames, overall)
        now = time.monotonic()
        if now >= next_status:
            latest = frames[-1] if frames else None
            if latest is None:
                print(f"  {len(frames):5d} frames; no callback yet", flush=True)
            else:
                print(
                    f"  {len(frames):5d} frames; "
                    f"latest frame={latest.raw_frame_register} "
                    f"touch_count={latest.raw_touch_count_register}",
                    flush=True,
                )
            next_status = now + 1.0
    _drain_available(sensor, frames, overall)
    return analyze_stage(stage.label, frames, stage.expected_touch_count)


def _print_stage_report(report: StageReport) -> None:
    print(f"Stage {report.label!r}:")
    print(f"  frames: {report.frame_count}")
    print(f"  touch-count histogram: {report.touch_count_histogram}")
    if report.expected_touch_count is not None:
        print(
            "  labelled-count agreement: "
            f"{report.expected_count_matches}/{report.frame_count} "
            f"(expected {report.expected_touch_count})"
        )
    print(
        "  frame steps: "
        f"forward={report.forward_frame_steps}, "
        f"duplicate={report.duplicate_frame_steps}, "
        f"regressing={report.regressing_frame_steps}, "
        f"non-unit-forward={report.non_unit_forward_steps}"
    )
    print(
        "  project callback-sequence gaps: "
        f"steps={report.callback_sequence_gap_steps}, "
        f"skipped-values={report.skipped_callback_sequence_values}"
    )
    print(
        "  timestamps: "
        f"non-finite={report.non_finite_timestamps}, "
        f"regressing={report.regressing_timestamp_steps}"
    )
    if report.observed_frame_rate_hz is not None:
        print(f"  observed callback rate: {report.observed_frame_rate_hz:.2f} Hz")


def _countdown(
    sensor: TrackpadSensor,
    seconds: float,
    overall: List[FrameMetadata],
) -> int:
    transition_frames: List[FrameMetadata] = []
    deadline = time.monotonic() + max(seconds, 0.0)
    last_displayed_second: Optional[int] = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _drain_available(sensor, transition_frames, overall)
            return len(transition_frames)
        displayed_second = max(1, int(remaining + 0.999))
        if displayed_second != last_displayed_second:
            print(f"  starts in {displayed_second}...", flush=True)
            last_displayed_second = displayed_second
        frame = sensor.read_frame(timeout=min(0.02, remaining))
        if frame is not None:
            _record_frame(frame, transition_frames, overall)
            _drain_available(sensor, transition_frames, overall)


def run(arguments: argparse.Namespace) -> Dict[str, object]:
    if arguments.duration <= 0:
        raise ValueError("duration must be positive")
    if arguments.lead_in < 0:
        raise ValueError("lead-in must be non-negative")
    if arguments.trials <= 0:
        raise ValueError("trials must be positive")
    if not arguments.guided and arguments.trials != 1:
        raise ValueError("multiple trials require --guided")
    if arguments.confirm_stages and not arguments.guided:
        raise ValueError("stage confirmation requires --guided")
    if not 0 <= arguments.start_options <= 0xFFFFFFFF:
        raise ValueError("start-options must fit in an unsigned 32-bit register")
    if arguments.start_options != 0 and not arguments.allow_unverified_target:
        raise ValueError(
            "only start-options 0 is verified; nonzero values require "
            "--allow-unverified-target for diagnostic experimentation"
        )

    profile = load_verified_profile()
    fingerprint = current_target_fingerprint()
    target_matches, mismatches = compare_target_to_profile(fingerprint, profile)
    print(f"Target ABI profile match: {'yes' if target_matches else 'NO'}")
    if mismatches:
        for mismatch in mismatches:
            print(f"  {mismatch}")
    if not target_matches and not arguments.allow_unverified_target:
        raise RuntimeError(
            "private ABI target is unverified; rerun only as an explicit diagnostic "
            "with --allow-unverified-target"
        )

    stages: Sequence[Stage]
    if arguments.guided:
        stages = tuple(
            replace(stage, label=f"trial_{trial}_{stage.label}")
            for trial in range(1, arguments.trials + 1)
            for stage in GUIDED_STAGES
        )
    else:
        stages = (
            Stage("observation", arguments.duration, None, "Use the trackpad normally."),
        )

    report: Dict[str, object] = {
        "schema_version": 1,
        "phase": 1,
        "target_profile_match": target_matches,
        "target_profile_mismatches": mismatches,
        "target": fingerprint.to_dict(),
        "start_options": arguments.start_options,
        "guided_trials": arguments.trials if arguments.guided else 0,
        "operator_confirmed_stages": bool(arguments.confirm_stages),
        "touch_records_dereferenced": False,
        "idle_zero_callback_note": (
            "This target suppresses continuous idle frames and may emit one terminal "
            "count-zero frame when active contact ends."
        ),
        "stages": [],
    }

    sensor = TrackpadSensor(
        allow_unverified_target=arguments.allow_unverified_target
    )
    stop_error: Optional[str] = None
    overall_frames: List[FrameMetadata] = []
    transition_frame_count = 0
    shutdown_frames: List[FrameMetadata] = []
    try:
        report["framework_path"] = sensor.framework_path
        report["framework_loaded"] = True
        report["device_acquired"] = True
        report["supports_force"] = sensor.supports_force()
        report["is_built_in"] = sensor.is_built_in()
        print(f"Framework loaded: {sensor.framework_path}")
        print("Default device acquired: yes")
        print(f"Device is built in: {sensor.is_built_in()}")
        print(f"Device supports force: {sensor.supports_force()}")
        print(f"Starting with explicit option register: 0x{arguments.start_options:08x}")
        sensor.start(start_options=arguments.start_options)
        report["callback_registered"] = True
        report["device_started"] = True
        report["start_native_status"] = sensor.last_start_native_status
        print(f"Callback registered and device started (native status {sensor.last_start_native_status}).")

        stage_reports: List[StageReport] = []
        for stage in stages:
            print(f"\n{stage.instruction}", flush=True)
            if arguments.guided:
                if arguments.confirm_stages:
                    input("Press Return when ready to begin this stage's countdown: ")
                transition_frame_count += _countdown(
                    sensor,
                    arguments.lead_in,
                    overall_frames,
                )
            print(f"Recording {stage.label!r} for {stage.seconds:.1f} seconds...", flush=True)
            stage_report = _observe_stage(sensor, stage, overall_frames)
            stage_reports.append(stage_report)
            _print_stage_report(stage_report)
        report["stages"] = [item.to_dict() for item in stage_reports]
    finally:
        if sensor.is_running():
            try:
                sensor.stop()
            except Exception as error:  # preserve evidence even if private stop fails
                stop_error = str(error)
        _drain_available(sensor, shutdown_frames, overall_frames)
        report["stop_native_status"] = sensor.last_stop_native_status
        report["stop_error"] = stop_error
        report["overall"] = analyze_stage(
            "whole_capture",
            overall_frames,
            expected_touch_count=None,
        ).to_dict()
        report["transition_frame_count"] = transition_frame_count
        report["shutdown_frame_count"] = len(shutdown_frames)
        stats = sensor.stats()
        report["native_stats"] = stats.to_dict()
        report["captured_metadata_count"] = len(overall_frames)
        report["unmaterialized_callback_count"] = (
            stats.callback_count - len(overall_frames)
        )
        sensor.close()

    stats = report["native_stats"]
    print("\nNative callback accounting:")
    print(json.dumps(stats, indent=2, sort_keys=True))
    print(f"Stop native status: {report['stop_native_status']}")
    if stop_error:
        print(f"Stop error: {stop_error}")
    print("Whole-capture ordering:")
    _print_stage_report(
        analyze_stage("whole_capture", overall_frames, expected_touch_count=None)
    )
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        report = run(arguments)
        report["completed"] = True
        if arguments.json_out is not None:
            arguments.json_out.parent.mkdir(parents=True, exist_ok=True)
            arguments.json_out.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"Evidence written to {arguments.json_out.resolve()}")
        return 0
    except (
        Phase1BridgeError,
        RuntimeError,
        ValueError,
        OSError,
        EOFError,
        KeyboardInterrupt,
    ) as error:
        failure_report = {
            "schema_version": 1,
            "phase": 1,
            "completed": False,
            "error_type": type(error).__name__,
            "error": str(error),
            "requested_guided": bool(arguments.guided),
            "requested_trials": arguments.trials,
            "start_options": arguments.start_options,
            "touch_records_dereferenced": False,
        }
        if arguments.json_out is not None:
            arguments.json_out.parent.mkdir(parents=True, exist_ok=True)
            arguments.json_out.write_text(
                json.dumps(failure_report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                f"Failure evidence written to {arguments.json_out.resolve()}",
                file=sys.stderr,
            )
        print(f"Phase 1 diagnostic failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
