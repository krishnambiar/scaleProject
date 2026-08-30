import unittest

from trackpad_scale.models import FrameMetadata
from trackpad_scale.reliability import analyze_stage


def frame(sequence: int, frame_id: int, count: int, timestamp: float) -> FrameMetadata:
    return FrameMetadata(
        sequence=sequence,
        raw_touch_count_register=count,
        raw_frame_register=frame_id,
        device_timestamp=timestamp,
        host_monotonic_ns=sequence * 10_000_000,
    )


class AnalyzeStageTests(unittest.TestCase):
    def test_reports_counts_and_monotonic_sequence_without_thresholds(self) -> None:
        report = analyze_stage(
            "one_finger",
            [
                frame(1, 100, 1, 1.00),
                frame(2, 101, 1, 1.01),
                frame(3, 103, 2, 1.02),
            ],
            expected_touch_count=1,
        )

        self.assertEqual(report.touch_count_histogram, {"1": 2, "2": 1})
        self.assertEqual(report.expected_count_matches, 2)
        self.assertAlmostEqual(report.expected_count_match_fraction or 0, 2 / 3)
        self.assertEqual(report.forward_frame_steps, 2)
        self.assertEqual(report.non_unit_forward_steps, 1)
        self.assertEqual(report.skipped_frame_id_values, 1)
        self.assertEqual(report.callback_sequence_gap_steps, 0)
        self.assertEqual(report.skipped_callback_sequence_values, 0)
        self.assertEqual(report.regressing_frame_steps, 0)

    def test_reports_duplicate_regression_and_bad_timestamp(self) -> None:
        report = analyze_stage(
            "diagnostic",
            [
                frame(1, 10, 0, 2.0),
                frame(2, 10, 0, 1.5),
                frame(3, 9, 0, float("nan")),
            ],
        )

        self.assertEqual(report.duplicate_frame_steps, 1)
        self.assertEqual(report.regressing_frame_steps, 1)
        self.assertEqual(report.regressing_timestamp_steps, 1)
        self.assertEqual(report.non_finite_timestamps, 1)

    def test_reports_project_queue_sequence_gaps_separately(self) -> None:
        samples = [frame(10, 200, 1, 1.0), frame(13, 201, 1, 1.1)]
        report = analyze_stage("queue", samples)

        self.assertEqual(report.non_unit_forward_steps, 0)
        self.assertEqual(report.callback_sequence_gap_steps, 1)
        self.assertEqual(report.skipped_callback_sequence_values, 2)

    def test_empty_stage_is_reported_without_claiming_failure(self) -> None:
        report = analyze_stage("clear", [], expected_touch_count=0)

        self.assertEqual(report.frame_count, 0)
        self.assertEqual(report.touch_count_histogram, {})
        self.assertIsNone(report.expected_count_match_fraction)
        self.assertIsNone(report.observed_frame_rate_hz)


if __name__ == "__main__":
    unittest.main()
