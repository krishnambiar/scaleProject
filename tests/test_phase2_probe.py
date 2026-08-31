import argparse
import copy
import json
import struct
import tempfile
import time
import unittest
from pathlib import Path

from trackpad_scale.models import (
    PHASE2_REQUIRED_COPIED_FIELDS,
    FrameMetadata,
    RawTouch,
    RawTouchFrame,
)
from trackpad_scale.phase2_analysis import (
    analyze_phase2_stage,
    summarize_pressure_sequence,
)
from trackpad_scale.phase2_probe import (
    _callback_clock_ns,
    _classify_callback_window,
    _classify_outcome,
    _contact_confound_reasons,
    _frames_for_window,
    reassess_saved_report,
    run,
)
from trackpad_scale.target_profile import load_verified_profile


def _bits(value: float) -> int:
    return struct.unpack("=I", struct.pack("=f", value))[0]


def _frame(
    sequence: int,
    host_ns: int,
    pressure: float = 1.0,
    *,
    path_index: int = 1,
    finger_id: int = 2,
) -> RawTouchFrame:
    touch = RawTouch(
        copied_fields=PHASE2_REQUIRED_COPIED_FIELDS,
        path_index=path_index,
        state=4,
        finger_id=finger_id,
        hand_id=-1,
        normalized_x=0.5,
        normalized_y=0.5,
        z_total=1.0,
        pressure_candidate=pressure,
        z_density=1.0,
        normalized_x_bits=_bits(0.5),
        normalized_y_bits=_bits(0.5),
        z_total_bits=_bits(1.0),
        pressure_candidate_bits=_bits(pressure),
        z_density_bits=_bits(1.0),
    )
    return RawTouchFrame(
        metadata=FrameMetadata(sequence, 1, sequence, sequence / 100.0, host_ns),
        layout_profile_id=1,
        decode_status=0,
        copied_touch_count=1,
        touches=(touch,),
    )


def _stats(attempted: int):
    return {
        "attempted_frame_count": attempted,
        "copied_touch_count": attempted,
        "queue_overwrite_count": 0,
        "lock_contention_drop_count": 0,
        "invalid_count_frame_count": 0,
        "null_records_frame_count": 0,
        "device_mismatch_frame_count": 0,
        "record_frame_mismatch_touch_count": 0,
        "record_timestamp_mismatch_touch_count": 0,
        "invalid_state_touch_count": 0,
        "pressure_sentinel_touch_count": 0,
        "nonfinite_touch_count": 0,
        "queue_depth": 0,
    }


def _zero_frame(sequence: int, host_ns: int) -> RawTouchFrame:
    return RawTouchFrame(
        metadata=FrameMetadata(sequence, 0, sequence, sequence / 100.0, host_ns),
        layout_profile_id=1,
        decode_status=0,
        copied_touch_count=0,
        touches=(),
    )


class Phase2ProbeArgumentTests(unittest.TestCase):
    def test_event_clock_matches_native_clock_domain(self) -> None:
        before = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        observed = _callback_clock_ns()
        after = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        self.assertLessEqual(before, observed)
        self.assertLessEqual(observed, after)

    def test_callback_time_not_collection_tag_selects_stage(self) -> None:
        window = {
            "cycle": 1,
            "stage": "light",
            "transition_start_ns": 100,
            "settled_start_ns": 200,
            "settled_end_ns": 300,
        }
        settled = _frame(1, 250)
        records = [
            ({"cycle": 99, "stage": "wrong", "period": "wrong"}, settled)
        ]
        self.assertEqual(_frames_for_window(records, window), [settled])
        self.assertEqual(
            _classify_callback_window(settled, [window]),
            {"cycle": 1, "stage": "light", "period": "settled"},
        )

    def test_rejects_nonpositive_cycles_before_touching_private_api(self) -> None:
        arguments = argparse.Namespace(
            cycles=0,
            stage_seconds=1.0,
            lead_in=0.0,
            confirm_stages=False,
            json_out=None,
        )
        with self.assertRaisesRegex(ValueError, "cycles must be positive"):
            run(arguments)

    def test_rejects_nonpositive_stage_duration(self) -> None:
        arguments = argparse.Namespace(
            cycles=1,
            stage_seconds=0.0,
            lead_in=0.0,
            confirm_stages=False,
            json_out=None,
        )
        with self.assertRaisesRegex(ValueError, "stage-seconds must be positive"):
            run(arguments)

    def test_incomplete_protocol_precedes_absent_candidate(self) -> None:
        empty = analyze_phase2_stage("empty", [])
        reports = {
            label: empty for label in ("rest", "light", "medium", "harder")
        }
        sequence = summarize_pressure_sequence(list(reports.items()))
        cycle = {
            "cycle": 1,
            "pressure_sequence": sequence,
            "complete_pressure_plateaus": False,
            "release_count_zero_observed": True,
            "no_contact_baseline_clear": True,
            "operator_confirmed_all_settled_windows": True,
            "contact_confound": False,
        }
        result = _classify_outcome(
            empty,
            _stats(0),
            {"callback_device_mismatch_count": 0},
            [cycle],
            {1: reports},
            0,
        )
        self.assertEqual(result["outcome"], "inconclusive_incomplete")

    def test_rejects_nonfinite_timing_arguments(self) -> None:
        arguments = argparse.Namespace(
            cycles=1,
            stage_seconds=float("nan"),
            lead_in=0.0,
            confirm_stages=False,
            json_out=None,
        )
        with self.assertRaisesRegex(ValueError, "stage-seconds must be positive"):
            run(arguments)

    def test_contact_confound_precedes_zero_only_rejection(self) -> None:
        reports = {
            label: analyze_phase2_stage(label, [_frame(index, index, 0.0)])
            for index, label in enumerate(
                ("rest", "light", "medium", "harder"), start=1
            )
        }
        frames = [_frame(index, index, 0.0) for index in range(1, 5)]
        whole = analyze_phase2_stage("whole", frames)
        cycle = {
            "cycle": 1,
            "pressure_sequence": summarize_pressure_sequence(list(reports.items())),
            "complete_pressure_plateaus": True,
            "release_count_zero_observed": True,
            "no_contact_baseline_clear": True,
            "operator_confirmed_all_settled_windows": True,
            "contact_confound": True,
        }
        result = _classify_outcome(
            whole,
            _stats(4),
            {"callback_device_mismatch_count": 0},
            [cycle],
            {1: reports},
            4,
        )
        self.assertEqual(result["outcome"], "inconclusive_contact_confound")

    def test_finger_classification_variation_is_not_a_contact_confound(self) -> None:
        rest = analyze_phase2_stage(
            "rest",
            [
                _frame(1, 1, 8.0, path_index=9, finger_id=2),
                _frame(2, 2, 9.0, path_index=9, finger_id=7),
                _frame(3, 3, 10.0, path_index=9, finger_id=2),
            ],
        )

        self.assertEqual(_contact_confound_reasons([("rest", rest)]), [])
        self.assertEqual(rest.primary_sample_count, 3)

    def test_path_replacement_remains_a_contact_confound(self) -> None:
        rest = analyze_phase2_stage(
            "rest",
            [
                _frame(1, 1, 8.0, path_index=9),
                _frame(2, 2, 9.0, path_index=11),
                _frame(3, 3, 10.0, path_index=9),
            ],
        )

        self.assertEqual(_contact_confound_reasons([("rest", rest)]), ["path_change"])

    def test_reassesses_saved_raw_frames_without_recapture(self) -> None:
        frames = [
            _frame(1, 11, 1.0, path_index=9, finger_id=2),
            _frame(2, 12, 1.5, path_index=9, finger_id=7),
            _frame(3, 21, 2.0, path_index=9),
            _frame(4, 31, 3.0, path_index=9),
            _frame(5, 41, 4.0, path_index=9),
            _zero_frame(6, 51),
        ]
        windows = []
        for stage, start in (
            ("no_contact", 0),
            ("rest", 10),
            ("light", 20),
            ("medium", 30),
            ("harder", 40),
            ("release", 50),
        ):
            windows.append(
                {
                    "cycle": 1,
                    "stage": stage,
                    "operator_confirmed_settled": True,
                    "transition_start_ns": start,
                    "settled_start_ns": start + 1,
                    "settled_end_ns": start + 10,
                }
            )
        phase2_stats = _stats(len(frames))
        phase2_stats["copied_touch_count"] = len(frames) - 1
        profile = load_verified_profile()
        layout = profile["phase2_source_layout"]
        bridge_layout = {
            "descriptor_version": layout["descriptor_version"],
            "profile_id": layout["profile_id"],
            "record_size": layout["record_size"],
            "maximum_touch_count": layout["maximum_touch_count"],
        }
        for name, field in layout["fields"].items():
            bridge_layout[f"{name}_offset"] = field["offset"]
            bridge_layout[f"{name}_size"] = field["size"]
        source = {
            "schema_version": 1,
            "phase": 2,
            "experiment": "raw pressure candidate ordinal response",
            "completed": True,
            "completed_utc": "2026-01-01T00:00:00+00:00",
            "preflight_status": "accepted",
            "requested_cycles": 1,
            "target_profile_match": True,
            "target_profile_mismatches": [],
            "actual_target": profile["target"],
            "expected_target": profile["target"],
            "expected_phase2_source_layout": layout,
            "start_options": 0,
            "start_native_status": 0,
            "stop_native_status": 0,
            "stop_error": None,
            "collector_error": None,
            "stats_error": None,
            "close_error": None,
            "validated_bridge_abi": {
                "bridge_abi_version": 1,
                "native_project_owned_struct_sizes": [64, 2104, 104, 128],
                "python_project_owned_struct_sizes": [64, 2104, 104, 128],
                "output_layout_fingerprint": "0xcc805c390dc2e7c1",
                "profile_id": layout["profile_id"],
                "profile_name": layout["profile_name"],
                "source_layout": bridge_layout,
            },
            "stage_windows": windows,
            "phase1_capture_stats": {"callback_device_mismatch_count": 0},
            "phase2_capture_stats": phase2_stats,
            "candidate_outcome": {"outcome": "obsolete_test_result"},
            "raw_frames": [
                {"collection_tag": {}, "frame": frame.to_dict()}
                for frame in frames
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "phase2.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            result = reassess_saved_report(path)

            invalid_cases = (
                ("incomplete", ("completed", False), "capture_not_completed"),
                (
                    "target mismatch",
                    ("target_profile_match", False),
                    "target_profile_not_matched",
                ),
                (
                    "cleanup error",
                    ("collector_error", "collector failed"),
                    "collector_error_present",
                ),
            )
            for label, (key, value), expected_reason in invalid_cases:
                with self.subTest(label=label):
                    invalid = copy.deepcopy(source)
                    invalid[key] = value
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, expected_reason):
                        reassess_saved_report(path)

            invalid_layout = copy.deepcopy(source)
            invalid_layout["expected_phase2_source_layout"]["record_size"] = 95
            path.write_text(json.dumps(invalid_layout), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "phase2_layout_differs_from_packaged_profile"
            ):
                reassess_saved_report(path)

        self.assertEqual(result["candidate_outcome"]["outcome"], "human_review_required")
        self.assertEqual(result["source_provenance_status"], "accepted")
        self.assertEqual(result["source_candidate_outcome"]["outcome"], "obsolete_test_result")
        self.assertEqual(result["analysis_rule_revision"], "single-contact-path-continuity-v2")
        self.assertEqual(
            result["cycle_reports"][0]["stages"]["rest"]["primary_sample_count"],
            2,
        )
        self.assertTrue(result["phase2_materialization_accounting"]["balances"])


if __name__ == "__main__":
    unittest.main()
