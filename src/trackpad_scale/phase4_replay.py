"""Offline, fail-closed replay of the bundled Phase 4 experiment profile.

The replay consumes preserved Phase 2 JSON only.  It reconstructs every saved
transport frame through the Phase 2 model and Phase 3 integrity gate before it
selects the profile-declared tare and continuation ranges.  All reported
values remain arbitrary raw sensor coordinates.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from .models import RawFrame, RawTouchFrame
from .phase3_sensor import RawFrameValidationError, raw_frame_from_transport
from .phase4_profile import (
    PHASE4_EXPERIMENTAL_STATUS,
    PHASE4_RAW_UNITS,
    ExperimentalPhase4Profile,
    Phase4ProfileError,
    load_experimental_phase4_profile,
)
from .phase4_stabilizer import PressureStabilizer, StabilizationUpdate


PHASE4_REPLAY_SCHEMA_VERSION = 1
PHASE4_REPLAY_EXPERIMENT = "offline exact-target Phase 4 pressure replay"
_SOURCE_EXPERIMENT = "raw pressure candidate ordinal response"
_SOURCE_UNITS = "raw sensor coordinates; not grams"
_FRAME_ENTRY_KEYS = frozenset({"callback_window", "collection_tag", "frame"})
_WINDOW_KEYS = frozenset({"cycle", "period", "stage"})


class Phase4ReplayError(ValueError):
    """A stable, machine-readable reason that replay could not be trusted."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class _SavedFrame:
    index: int
    cycle: Optional[int]
    stage: Optional[str]
    period: str
    frame: RawFrame


def _fail(reason: str, detail: str) -> None:
    raise Phase4ReplayError(reason, detail)


def _mapping(value: object, reason: str, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(reason, f"{context} must be an object")
    if not all(type(key) is str for key in value):
        _fail(reason, f"{context} keys must all be strings")
    return value


def _exact_keys(
    value: Mapping[str, object], expected: frozenset, reason: str, context: str
) -> None:
    observed = frozenset(value)
    missing = sorted(expected - observed)
    unknown = sorted(observed - expected)
    if missing or unknown:
        _fail(reason, f"{context} missing keys={missing}; unknown keys={unknown}")


def _require_literal(source: Mapping[str, object], key: str, expected: object) -> None:
    if key not in source:
        _fail("source_schema_invalid", f"missing top-level field {key!r}")
    observed = source[key]
    if type(observed) is not type(expected) or observed != expected:
        _fail(
            "source_provenance_mismatch",
            f"{key} expected {expected!r}, observed {observed!r}",
        )


def _read_verified_source(
    source_path: Path, profile: ExperimentalPhase4Profile
) -> Tuple[Mapping[str, object], str, int]:
    try:
        payload_bytes = source_path.read_bytes()
    except OSError as error:
        _fail("source_unreadable", str(error))

    observed_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    expected_sha256 = profile.source_phase2_artifact.sha256
    if observed_sha256 != expected_sha256:
        _fail(
            "source_sha256_mismatch",
            f"expected {expected_sha256}, observed {observed_sha256}",
        )

    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("source_json_invalid", str(error))
    return (
        _mapping(payload, "source_schema_invalid", "source"),
        observed_sha256,
        len(payload_bytes),
    )


def _target_dict(profile: ExperimentalPhase4Profile) -> Dict[str, str]:
    return {
        key: str(value)
        for key, value in asdict(profile.source_phase2_artifact.target).items()
    }


def _verify_target(
    source: Mapping[str, object], profile: ExperimentalPhase4Profile
) -> Dict[str, str]:
    expected = _target_dict(profile)
    expected_keys = frozenset(expected)
    for field_name in ("actual_target", "expected_target"):
        if field_name not in source:
            _fail("target_mismatch", f"missing recorded {field_name}")
        observed = _mapping(source[field_name], "target_mismatch", field_name)
        _exact_keys(observed, expected_keys, "target_mismatch", field_name)
        mismatches = []
        for key in sorted(expected):
            value = observed[key]
            if type(value) is not str or value != expected[key]:
                mismatches.append(
                    f"{key}: expected {expected[key]!r}, observed {value!r}"
                )
        if mismatches:
            _fail("target_mismatch", f"{field_name}: " + "; ".join(mismatches))
    return expected


def _source_layout_profile_id(source: Mapping[str, object]) -> int:
    if "expected_phase2_source_layout" not in source:
        _fail("source_layout_invalid", "missing expected_phase2_source_layout")
    layout = _mapping(
        source["expected_phase2_source_layout"],
        "source_layout_invalid",
        "expected_phase2_source_layout",
    )
    profile_id = layout.get("profile_id")
    if type(profile_id) is not int or profile_id <= 0:
        _fail(
            "source_layout_invalid",
            "expected_phase2_source_layout.profile_id must be a positive integer",
        )

    if "validated_bridge_abi" not in source:
        _fail("source_layout_invalid", "missing validated_bridge_abi")
    bridge = _mapping(
        source["validated_bridge_abi"],
        "source_layout_invalid",
        "validated_bridge_abi",
    )
    bridge_profile_id = bridge.get("profile_id")
    bridge_layout = _mapping(
        bridge.get("source_layout"),
        "source_layout_invalid",
        "validated_bridge_abi.source_layout",
    )
    if (
        type(bridge_profile_id) is not int
        or bridge_profile_id != profile_id
        or type(bridge_layout.get("profile_id")) is not int
        or bridge_layout.get("profile_id") != profile_id
    ):
        _fail(
            "source_layout_invalid",
            "recorded profile identifiers do not agree",
        )
    return profile_id


def _parse_window(
    value: object, index: int
) -> Tuple[Optional[int], Optional[str], str]:
    context = f"raw_frames[{index}].callback_window"
    window = _mapping(value, "frame_entry_invalid", context)
    _exact_keys(window, _WINDOW_KEYS, "frame_entry_invalid", context)

    cycle = window["cycle"]
    if cycle is not None and (type(cycle) is not int or cycle <= 0):
        _fail("frame_entry_invalid", f"{context}.cycle must be positive or null")
    stage = window["stage"]
    if stage is not None and (type(stage) is not str or not stage):
        _fail("frame_entry_invalid", f"{context}.stage must be non-empty or null")
    period = window["period"]
    if type(period) is not str or not period:
        _fail("frame_entry_invalid", f"{context}.period must be a non-empty string")
    return cycle, stage, period


def _parse_all_frames(
    source: Mapping[str, object], expected_profile_id: int
) -> List[_SavedFrame]:
    raw_entries = source.get("raw_frames")
    if type(raw_entries) is not list or not raw_entries:
        _fail("source_frames_invalid", "raw_frames must be a non-empty array")

    parsed: List[_SavedFrame] = []
    previous_sequence: Optional[int] = None
    for index, value in enumerate(raw_entries):
        context = f"raw_frames[{index}]"
        entry = _mapping(value, "frame_entry_invalid", context)
        _exact_keys(entry, _FRAME_ENTRY_KEYS, "frame_entry_invalid", context)
        cycle, stage, period = _parse_window(entry["callback_window"], index)
        frame_payload = _mapping(
            entry["frame"], "frame_entry_invalid", f"{context}.frame"
        )
        try:
            transport = RawTouchFrame.from_dict(frame_payload)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            _fail("transport_frame_invalid", f"{context}: {error}")
        try:
            frame = raw_frame_from_transport(
                transport, expected_profile_id=expected_profile_id
            )
        except RawFrameValidationError as error:
            _fail(
                "transport_frame_invalid",
                f"{context}: {error.reason}: {error.detail}",
            )

        if frame.sequence <= 0:
            _fail("saved_frame_order_invalid", f"{context} has non-positive sequence")
        if previous_sequence is not None and frame.sequence != previous_sequence + 1:
            _fail(
                "saved_frame_order_invalid",
                (
                    f"{context} sequence {frame.sequence} did not follow "
                    f"{previous_sequence}"
                ),
            )
        previous_sequence = frame.sequence
        parsed.append(
            _SavedFrame(
                index=index,
                cycle=cycle,
                stage=stage,
                period=period,
                frame=frame,
            )
        )
    return parsed


def _matching_frames(
    frames: Sequence[_SavedFrame], cycle: int, stage: str, period: str
) -> List[_SavedFrame]:
    return [
        item
        for item in frames
        if item.cycle == cycle and item.stage == stage and item.period == period
    ]


def _expect_equal(name: str, observed: object, expected: object) -> None:
    if type(observed) is not type(expected) or observed != expected:
        _fail(
            "replay_expectation_mismatch",
            f"{name} expected {expected!r}, observed {observed!r}",
        )


def _verify_consecutive(
    frames: Sequence[_SavedFrame], first: int, last: int, context: str
) -> None:
    expected_count = last - first + 1
    if last < first or len(frames) != expected_count:
        _fail(
            "replay_range_invalid",
            f"{context} expected {expected_count} frames, observed {len(frames)}",
        )
    for offset, item in enumerate(frames):
        expected_sequence = first + offset
        if item.frame.sequence != expected_sequence:
            _fail(
                "replay_range_invalid",
                (
                    f"{context}[{offset}] expected sequence {expected_sequence}, "
                    f"observed {item.frame.sequence}"
                ),
            )


def _measurement_dict(
    update: StabilizationUpdate, saved: _SavedFrame
) -> Dict[str, object]:
    measurement = update.published_measurement
    if measurement is None:
        raise AssertionError("publication serialization requires a measurement")
    result = asdict(measurement)
    result["frame_number"] = saved.frame.frame_number
    result["callback_window"] = {
        "cycle": saved.cycle,
        "stage": saved.stage,
        "period": saved.period,
    }
    result["units"] = PHASE4_RAW_UNITS
    return result


def _audit_with_profile(
    source_path: Path, profile: ExperimentalPhase4Profile
) -> Dict[str, object]:
    if not isinstance(profile, ExperimentalPhase4Profile):
        _fail("profile_invalid", "loader did not return ExperimentalPhase4Profile")
    if (
        profile.status != PHASE4_EXPERIMENTAL_STATUS
        or profile.units != PHASE4_RAW_UNITS
        or profile.calibration_performed
    ):
        _fail(
            "profile_invalid",
            "profile must remain experimental, raw-domain, and uncalibrated",
        )

    source, source_sha256, source_size_bytes = _read_verified_source(
        source_path, profile
    )
    _require_literal(source, "schema_version", 1)
    _require_literal(source, "phase", 2)
    _require_literal(source, "experiment", _SOURCE_EXPERIMENT)
    _require_literal(source, "completed", True)
    _require_literal(source, "preflight_status", "accepted")
    _require_literal(source, "target_profile_match", True)
    _require_literal(source, "units", _SOURCE_UNITS)
    _require_literal(source, "calibration_performed", False)
    exact_target = _verify_target(source, profile)
    expected_profile_id = _source_layout_profile_id(source)
    frames = _parse_all_frames(source, expected_profile_id)

    replay = profile.source_phase2_artifact.replay
    _expect_equal(
        "tare_trailing_sample_count",
        replay.tare_trailing_sample_count,
        profile.config.tare_window_samples,
    )
    tare_stage_frames = _matching_frames(
        frames, replay.tare_cycle, replay.tare_stage, replay.tare_period
    )
    if len(tare_stage_frames) < replay.tare_trailing_sample_count:
        _fail(
            "tare_selection_invalid",
            (
                f"need {replay.tare_trailing_sample_count} matching tare frames, "
                f"observed {len(tare_stage_frames)}"
            ),
        )
    tare_frames = tare_stage_frames[-replay.tare_trailing_sample_count :]
    _verify_consecutive(
        tare_frames,
        replay.tare_first_sequence,
        replay.tare_last_sequence,
        "tare",
    )
    _expect_equal(
        "tare_first_frame_number",
        tare_frames[0].frame.frame_number,
        replay.tare_first_frame_number,
    )
    _expect_equal(
        "tare_last_frame_number",
        tare_frames[-1].frame.frame_number,
        replay.tare_last_frame_number,
    )

    pressure_stage_frames = _matching_frames(
        frames,
        replay.pressure_cycle,
        replay.pressure_stage,
        replay.pressure_period,
    )
    if not pressure_stage_frames:
        _fail("pressure_selection_invalid", "profile-declared pressure stage is empty")
    _expect_equal(
        "pressure_first_sequence",
        pressure_stage_frames[0].frame.sequence,
        replay.pressure_first_sequence,
    )
    _expect_equal(
        "pressure_last_sequence",
        pressure_stage_frames[-1].frame.sequence,
        replay.pressure_last_sequence,
    )

    continuation = [
        item
        for item in frames
        if replay.continuation_first_sequence
        <= item.frame.sequence
        <= replay.continuation_last_sequence
    ]
    _verify_consecutive(
        continuation,
        replay.continuation_first_sequence,
        replay.continuation_last_sequence,
        "continuation",
    )
    if tare_frames[-1].frame.sequence + 1 != continuation[0].frame.sequence:
        _fail(
            "replay_range_invalid",
            "continuation does not immediately follow the trailing tare window",
        )

    stabilizer = PressureStabilizer(profile.config)
    try:
        tare_result = stabilizer.tare(tuple(item.frame for item in tare_frames))
    except (TypeError, ValueError, RuntimeError) as error:
        reason = getattr(error, "reason", "tare_failed")
        _fail("tare_failed", f"{reason}: {error}")

    _expect_equal(
        "expected_tare_baseline_raw",
        tare_result.baseline_pressure_raw,
        replay.expected_tare_baseline_raw,
    )
    _expect_equal(
        "expected_tare_mad_raw",
        tare_result.dispersion_mad_raw,
        replay.expected_tare_mad_raw,
    )
    _expect_equal(
        "expected_tare_max_absolute_deviation_raw",
        tare_result.maximum_absolute_deviation_raw,
        replay.expected_tare_max_absolute_deviation_raw,
    )
    _expect_equal(
        "expected_tare_slope_raw_per_second",
        tare_result.slope_raw_per_second,
        replay.expected_tare_slope_raw_per_second,
    )

    status_counts: Counter = Counter()
    publications: List[Dict[str, object]] = []
    for saved in continuation:
        try:
            update = stabilizer.process(saved.frame)
        except (TypeError, ValueError, RuntimeError, ArithmeticError) as error:
            _fail(
                "stabilizer_processing_failed",
                f"sequence {saved.frame.sequence}: {error}",
            )
        status_counts[update.status.value] += 1
        if update.published_measurement is not None:
            publications.append(_measurement_dict(update, saved))

    if len(publications) != 1:
        _fail(
            "publication_expectation_mismatch",
            f"expected exactly one publication, observed {len(publications)}",
        )
    publication = publications[0]
    _expect_equal(
        "expected_publication_sequence",
        publication["sequence"],
        replay.expected_publication_sequence,
    )
    _expect_equal(
        "expected_publication_frame_number",
        publication["frame_number"],
        replay.expected_publication_frame_number,
    )
    _expect_equal(
        "expected_published_delta_raw",
        publication["pressure_delta_raw"],
        replay.expected_published_delta_raw,
    )
    callback_window = publication["callback_window"]
    if callback_window != {
        "cycle": replay.pressure_cycle,
        "stage": replay.pressure_stage,
        "period": replay.pressure_period,
    }:
        _fail(
            "publication_expectation_mismatch",
            "publication did not occur inside the declared pressure window",
        )

    report: Dict[str, object] = {
        "schema_version": PHASE4_REPLAY_SCHEMA_VERSION,
        "phase": 4,
        "experiment": PHASE4_REPLAY_EXPERIMENT,
        "outcome": "profile_expectations_verified",
        "units": profile.units,
        "calibration_performed": False,
        "profile": {
            "profile_id": profile.profile_id,
            "status": profile.status,
            "scope": profile.scope,
            "config": asdict(profile.config),
            "source_phase2_artifact": asdict(profile.source_phase2_artifact),
            "rationale": list(profile.rationale),
            "caveats": list(profile.caveats),
        },
        "provenance": {
            "source_path": str(source_path.resolve()),
            "source_sha256": source_sha256,
            "source_sha256_verified": True,
            "source_size_bytes": source_size_bytes,
            "source_phase": 2,
            "source_experiment": _SOURCE_EXPERIMENT,
            "exact_recorded_target": exact_target,
            "exact_recorded_target_verified": True,
            "transport_profile_id": expected_profile_id,
            "saved_frame_count": len(frames),
            "all_saved_frames_parsed_through_phase3_gate": True,
        },
        "selection": {
            "tare": {
                "cycle": replay.tare_cycle,
                "stage": replay.tare_stage,
                "period": replay.tare_period,
                "matching_stage_frame_count": len(tare_stage_frames),
                "trailing_sample_count": len(tare_frames),
                "first_sequence": tare_frames[0].frame.sequence,
                "last_sequence": tare_frames[-1].frame.sequence,
                "first_frame_number": tare_frames[0].frame.frame_number,
                "last_frame_number": tare_frames[-1].frame.frame_number,
            },
            "continuation": {
                "frame_count": len(continuation),
                "first_sequence": continuation[0].frame.sequence,
                "last_sequence": continuation[-1].frame.sequence,
            },
            "pressure_window": {
                "cycle": replay.pressure_cycle,
                "stage": replay.pressure_stage,
                "period": replay.pressure_period,
                "frame_count": len(pressure_stage_frames),
                "first_sequence": pressure_stage_frames[0].frame.sequence,
                "last_sequence": pressure_stage_frames[-1].frame.sequence,
            },
        },
        "tare": asdict(tare_result),
        "status_counts": {key: status_counts[key] for key in sorted(status_counts)},
        "publications": publications,
        "expectations": {
            "verified": True,
            "publication_count": 1,
            "processed_every_continuation_frame": (
                sum(status_counts.values()) == len(continuation)
            ),
        },
    }
    try:
        json.dumps(report, allow_nan=False)
    except (TypeError, ValueError) as error:
        _fail("report_not_json_serializable", str(error))
    return report


def audit_phase4_replay(source: Union[str, Path]) -> Dict[str, object]:
    """Verify and replay the exact artifact selected by the bundled profile."""

    try:
        profile = load_experimental_phase4_profile()
    except Phase4ProfileError as error:
        _fail("profile_invalid", str(error))
    return _audit_with_profile(Path(source), profile)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay the immutable exact-target Phase 2 artifact through the "
            "experimental Phase 4 raw-pressure stabilizer."
        )
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    return parser


def _write_report(path: Path, report_text: str) -> None:
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(report_text)
            stream.write("\n")
    except FileExistsError:
        _fail("output_exists", f"refusing to overwrite {path}")
    except OSError as error:
        _fail("output_unwritable", str(error))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    source_path = args.source
    output_path = args.json_out
    try:
        if output_path is not None and (output_path.resolve() == source_path.resolve()):
            _fail(
                "output_conflicts_source",
                "--json-out must not identify the source artifact",
            )
        report = audit_phase4_replay(source_path)
        report_text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
        if output_path is not None:
            _write_report(output_path, report_text)
    except Phase4ReplayError as error:
        print(
            f"Phase 4 replay failed [{error.reason}]: {error.detail}",
            file=sys.stderr,
        )
        return 2
    print(report_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
