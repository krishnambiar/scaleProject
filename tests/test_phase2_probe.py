import argparse
import struct
import time
import unittest

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
    _frames_for_window,
    run,
)


def _bits(value: float) -> int:
    return struct.unpack("=I", struct.pack("=f", value))[0]


def _frame(sequence: int, host_ns: int, pressure: float = 1.0) -> RawTouchFrame:
    touch = RawTouch(
        copied_fields=PHASE2_REQUIRED_COPIED_FIELDS,
        path_index=1,
        state=4,
        finger_id=2,
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


if __name__ == "__main__":
    unittest.main()
