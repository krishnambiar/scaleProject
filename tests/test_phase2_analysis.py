import json
import struct
import unittest

from trackpad_scale.models import (
    PHASE2_REQUIRED_COPIED_FIELDS,
    FrameMetadata,
    Phase2DecodeStatus,
    RawTouch,
    RawTouchFrame,
    phase2_decode_status_names,
)
from trackpad_scale.phase2_analysis import (
    analyze_phase2_stage,
    summarize_pressure_sequence,
)


def float_bits(value: float) -> int:
    return struct.unpack("=I", struct.pack("=f", value))[0]


def touch(
    pressure: float,
    *,
    path: int = 3,
    finger: int = 7,
    state: int = 4,
    copied_fields: int = PHASE2_REQUIRED_COPIED_FIELDS,
) -> RawTouch:
    return RawTouch(
        copied_fields=copied_fields,
        path_index=path,
        state=state,
        finger_id=finger,
        hand_id=-1,
        normalized_x=0.5,
        normalized_y=0.6,
        z_total=10.0 + pressure,
        pressure_candidate=pressure,
        z_density=2.0,
        normalized_x_bits=float_bits(0.5),
        normalized_y_bits=float_bits(0.6),
        z_total_bits=float_bits(10.0 + pressure),
        pressure_candidate_bits=float_bits(pressure),
        z_density_bits=float_bits(2.0),
    )


def frame(
    sequence: int,
    pressure: float = 1.0,
    *,
    touches=None,
    raw_count=None,
    decode_status: int = 0,
    profile_id: int = 1,
) -> RawTouchFrame:
    materialized = tuple(touches if touches is not None else (touch(pressure),))
    count = len(materialized) if raw_count is None else raw_count
    return RawTouchFrame(
        metadata=FrameMetadata(
            sequence=sequence,
            raw_touch_count_register=count,
            raw_frame_register=100 + sequence,
            device_timestamp=1.0 + sequence / 100.0,
            host_monotonic_ns=sequence * 10_000_000,
        ),
        layout_profile_id=profile_id,
        decode_status=decode_status,
        copied_touch_count=len(materialized),
        touches=materialized,
    )


class Phase2AnalysisTests(unittest.TestCase):
    def test_describes_raw_candidate_without_units_or_threshold(self) -> None:
        report = analyze_phase2_stage(
            "light", [frame(1, 10.0), frame(2, 12.0), frame(3, 14.0)]
        )

        self.assertEqual(report.primary_sample_count, 3)
        self.assertEqual(report.pressure_candidate.minimum, 10.0)
        self.assertEqual(report.pressure_candidate.maximum, 14.0)
        self.assertEqual(report.pressure_candidate.median, 12.0)
        self.assertEqual(report.pressure_candidate.q1, 11.0)
        self.assertEqual(report.pressure_candidate.q3, 13.0)
        self.assertEqual(report.pressure_candidate.mad, 2.0)
        self.assertGreater(report.pressure_candidate.slope_per_second or 0, 0)
        self.assertIsNone(report.to_dict()["analysis_policy"]["automatic_numeric_pass_threshold"])

    def test_excludes_transitions_extra_contacts_and_identity_replacement(self) -> None:
        replacement = touch(22.0, path=9)
        report = analyze_phase2_stage(
            "medium",
            [
                frame(1, 20.0),
                frame(2, touches=(touch(21.0, state=5),)),
                frame(3, touches=(touch(21.0), touch(30.0, path=4))),
                frame(4, touches=(replacement,)),
                frame(5, 23.0),
            ],
        )

        self.assertEqual(report.primary_sample_count, 2)
        self.assertEqual(report.excluded_frame_counts["nonsteady_touch_state"], 1)
        self.assertEqual(report.excluded_frame_counts["multiple_contacts"], 1)
        self.assertEqual(report.excluded_frame_counts["identity_change"], 1)

    def test_fails_closed_on_profile_status_and_field_masks(self) -> None:
        report = analyze_phase2_stage(
            "guarded",
            [
                frame(1, profile_id=2),
                frame(2, decode_status=int(Phase2DecodeStatus.INVALID_STATE)),
                frame(
                    3,
                    touches=(
                        touch(
                            1.0,
                            copied_fields=PHASE2_REQUIRED_COPIED_FIELDS | (1 << 31),
                        ),
                    ),
                ),
            ],
        )

        self.assertEqual(report.primary_sample_count, 0)
        self.assertEqual(report.excluded_frame_counts["layout_profile_mismatch"], 1)
        self.assertEqual(report.excluded_frame_counts["decode_status_nonzero"], 1)
        self.assertEqual(
            report.excluded_frame_counts["unexpected_copied_fields_mask"], 1
        )

    def test_reports_adjacent_directions_without_tolerance(self) -> None:
        rest = analyze_phase2_stage("rest", [frame(1, 1.0)])
        light = analyze_phase2_stage("light", [frame(2, 2.0)])
        medium = analyze_phase2_stage("medium", [frame(3, 2.0)])
        harder = analyze_phase2_stage("harder", [frame(4, 1.5)])
        result = summarize_pressure_sequence(
            [
                ("rest", rest),
                ("light", light),
                ("medium", medium),
                ("harder", harder),
            ]
        )

        self.assertEqual(
            [item["direction"] for item in result["adjacent_median_comparisons"]],
            ["increase", "tie", "decrease"],
        )
        self.assertIn("light_to_medium:median_tie", result["categorical_disqualifiers"])
        self.assertIn(
            "median_direction_changes_within_cycle",
            result["categorical_disqualifiers"],
        )

    def test_json_models_preserve_nonfinite_evidence_as_strings(self) -> None:
        raw = frame(
            1,
            touches=(touch(float("nan")),),
            decode_status=int(Phase2DecodeStatus.NONFINITE_SCALAR),
        )
        encoded = json.dumps(raw.to_dict(), allow_nan=False)
        self.assertIn('"nan"', encoded)
        self.assertIn("NONFINITE_SCALAR", encoded)

    def test_unknown_decode_bits_are_named(self) -> None:
        self.assertEqual(
            phase2_decode_status_names(1 << 20),
            ("UNKNOWN_BITS_0x00100000",),
        )


if __name__ == "__main__":
    unittest.main()
