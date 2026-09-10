import math
import struct
import unittest
from dataclasses import asdict
from typing import Dict, Iterable, Optional

from trackpad_scale.models import RawContact, RawFrame
from trackpad_scale.phase4_stabilizer import (
    PressureStabilizer,
    StabilizationStatus,
    StabilizerConfig,
    TareValidationError,
    _mad,
    _slope_raw_per_second,
)


def _bits(value: float) -> int:
    return struct.unpack(">I", struct.pack(">f", value))[0]


def _contact(
    pressure: float,
    *,
    path: int = 7,
    state: int = 4,
    finger: int = 2,
    x: float = 0.5,
    y: float = 0.5,
) -> RawContact:
    return RawContact(
        path_index=path,
        state=state,
        finger_code=finger,
        hand_code=1,
        normalized_x=x,
        normalized_y=y,
        z_total_raw=1.0,
        pressure_candidate_raw=pressure,
        z_density_raw=1.0,
        normalized_x_bits=_bits(x),
        normalized_y_bits=_bits(y),
        z_total_bits=_bits(1.0),
        pressure_candidate_bits=_bits(pressure),
        z_density_bits=_bits(1.0),
    )


def _frame(
    sequence: int,
    pressure: float = 10.0,
    *,
    timestamp_seconds: Optional[float] = None,
    path: int = 7,
    state: int = 4,
    finger: int = 2,
    x: float = 0.5,
    contacts: Optional[tuple[RawContact, ...]] = None,
) -> RawFrame:
    timestamp = (sequence - 1) / 10 if timestamp_seconds is None else timestamp_seconds
    materialized = (
        (_contact(pressure, path=path, state=state, finger=finger, x=x),)
        if contacts is None
        else contacts
    )
    return RawFrame(
        sequence=sequence,
        frame_number=sequence + 100,
        device_timestamp=timestamp,
        host_monotonic_ns=int(timestamp * 1_000_000_000),
        contacts=materialized,
    )


def _config(**overrides: object) -> StabilizerConfig:
    values: Dict[str, object] = {
        "tare_window_samples": 5,
        "tare_min_duration_seconds": 0.4,
        "tare_max_mad_raw": 1.0,
        "tare_max_absolute_deviation_raw": 10.0,
        "tare_max_abs_slope_raw_per_second": 1.0,
        "median_window_samples": 3,
        "smoothing_window_samples": 2,
        "outlier_window_samples": 3,
        "outlier_mad_multiplier": 3.0,
        "outlier_floor_raw": 50.0,
        "stability_window_seconds": 0.2,
        "stability_min_samples": 3,
        "stability_max_mad_raw": 0.01,
        "stability_max_abs_slope_raw_per_second": 0.01,
        "stable_window_interval_seconds": 0.2,
        "stable_windows_required": 2,
        "stable_window_agreement_raw": 0.01,
        "max_sample_gap_seconds": 0.11,
        "maximum_tare_age_seconds": 20.0,
        "max_position_deviation_normalized": 0.05,
    }
    values.update(overrides)
    return StabilizerConfig(**values)  # type: ignore[arg-type]


def _tare_frames(
    pressures: Iterable[float] = (10, 10, 10, 10, 10),
) -> tuple[RawFrame, ...]:
    return tuple(
        _frame(index, pressure, timestamp_seconds=(index - 1) / 10)
        for index, pressure in enumerate(pressures, start=1)
    )


class StabilizerConfigTests(unittest.TestCase):
    def test_invalid_structural_and_numeric_parameters_fail_fast(self) -> None:
        invalid = (
            {"median_window_samples": 4},
            {"outlier_window_samples": 2},
            {"smoothing_window_samples": 1},
            {"stable_windows_required": 1},
            {"stability_max_mad_raw": -1},
            {"tare_max_absolute_deviation_raw": -1},
            {"outlier_mad_multiplier": True},
            {"tare_max_mad_raw": "1"},
            {"outlier_floor_raw": math.nan},
            {"stable_window_interval_seconds": 0.1},
            {"max_position_deviation_normalized": math.inf},
            {"max_sample_gap_seconds": 1e-10},
            {"maximum_tare_age_seconds": 0.3},
            {"tare_min_duration_seconds": 0.5},
            {"outlier_floor_raw": 10**1000},
            {
                "tare_window_samples": 3,
                "max_sample_gap_seconds": 1.9e-9,
                "tare_min_duration_seconds": 3e-9,
            },
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    _config(**overrides)


class TareTests(unittest.TestCase):
    def test_tare_uses_robust_median_and_freezes_reference(self) -> None:
        stabilizer = PressureStabilizer(
            _config(
                tare_max_mad_raw=2.0,
                tare_max_absolute_deviation_raw=100.0,
                tare_max_abs_slope_raw_per_second=10.0,
            )
        )
        result = stabilizer.tare(_tare_frames((9, 10, 100, 10, 11)))

        self.assertEqual(result.baseline_pressure_raw, 10.0)
        self.assertEqual(result.dispersion_mad_raw, 1.0)
        self.assertEqual(result.sample_count, 5)
        update = stabilizer.process(_frame(6, 5.0, timestamp_seconds=0.5))
        self.assertEqual(update.pressure_delta_raw, -5.0)
        self.assertEqual(stabilizer.tare_result, result)

    def test_failed_retare_invalidates_the_previous_baseline(self) -> None:
        stabilizer = PressureStabilizer(_config())
        stabilizer.tare(_tare_frames())

        with self.assertRaises(TareValidationError) as raised:
            stabilizer.tare(_tare_frames()[:2])

        self.assertEqual(raised.exception.reason, "insufficient_samples")
        self.assertIsNone(stabilizer.tare_result)

    def test_tare_rejects_unstable_or_discontinuous_windows(self) -> None:
        cases = {
            "tare_dispersion": _tare_frames((10, 12, 14, 12, 10)),
            "tare_transient": _tare_frames((10, 10, 100, 10, 10)),
            "tare_slope": _tare_frames((10, 10, 10, 11, 12)),
            "insufficient_duration": tuple(
                _frame(index, timestamp_seconds=(index - 1) / 20)
                for index in range(1, 6)
            ),
            "tare_position_changed": (
                *_tare_frames()[:4],
                _frame(5, x=0.7, timestamp_seconds=0.4),
            ),
            "sequence_discontinuity": (
                *_tare_frames()[:4],
                _frame(7, timestamp_seconds=0.4),
            ),
            "path_changed": (
                *_tare_frames()[:4],
                _frame(5, path=8, timestamp_seconds=0.4),
            ),
            "unsupported_contact": (
                *_tare_frames()[:4],
                _frame(5, contacts=(), timestamp_seconds=0.4),
            ),
        }
        for reason, frames in cases.items():
            with self.subTest(reason=reason):
                stabilizer = PressureStabilizer(_config())
                with self.assertRaises(TareValidationError) as raised:
                    stabilizer.tare(frames)
                self.assertEqual(raised.exception.reason, reason)
                self.assertIsNone(stabilizer.tare_result)

    def test_tare_requires_a_finite_sequence_not_an_unbounded_iterator(self) -> None:
        stabilizer = PressureStabilizer(_config())
        with self.assertRaisesRegex(TypeError, "finite Sequence"):
            stabilizer.tare(iter(_tare_frames()))  # type: ignore[arg-type]

    def test_zero_contact_is_not_synthesized_as_numeric_tare(self) -> None:
        stabilizer = PressureStabilizer(_config())
        frames = tuple(
            _frame(index, contacts=(), timestamp_seconds=(index - 1) / 10)
            for index in range(1, 6)
        )
        with self.assertRaises(TareValidationError) as raised:
            stabilizer.tare(frames)
        self.assertEqual(raised.exception.reason, "unsupported_contact")


class ProcessingTests(unittest.TestCase):
    def _tared(self, **config: object) -> PressureStabilizer:
        stabilizer = PressureStabilizer(_config(**config))
        stabilizer.tare(_tare_frames())
        return stabilizer

    def test_no_measurement_is_available_before_explicit_tare(self) -> None:
        stabilizer = PressureStabilizer(_config())
        update = stabilizer.process(_frame(1))
        self.assertEqual(update.status, StabilizationStatus.TARE_REQUIRED)
        self.assertIsNone(update.published_measurement)

    def test_repeated_time_separated_stable_windows_publish_once(self) -> None:
        stabilizer = self._tared()
        updates = [
            stabilizer.process(_frame(sequence, 20.0)) for sequence in range(6, 14)
        ]
        published = [
            update.published_measurement
            for update in updates
            if update.published_measurement is not None
        ]

        self.assertEqual(len(published), 1)
        measurement = published[0]
        self.assertEqual(measurement.pressure_delta_raw, 10.0)
        self.assertEqual(measurement.confidence.agreeing_window_count, 2)
        self.assertEqual(measurement.confidence.agreement_span_raw, 0.0)
        self.assertNotIn("gram", repr(asdict(measurement)).lower())
        self.assertNotIn("weight", repr(asdict(measurement)).lower())

    def test_classification_changes_do_not_break_a_continuous_path(self) -> None:
        stabilizer = self._tared()
        published = None
        for sequence in range(6, 14):
            update = stabilizer.process(
                _frame(sequence, 20.0, finger=2 if sequence % 2 else 7)
            )
            published = update.published_measurement or published
        self.assertIsNotNone(published)
        self.assertIsNotNone(stabilizer.tare_result)

    def test_contact_path_position_and_stream_breaks_require_retare(self) -> None:
        scenarios = (
            (_frame(6, contacts=()), StabilizationStatus.CONTACT_UNSUPPORTED),
            (
                _frame(6, contacts=(_contact(10), _contact(11, path=8))),
                StabilizationStatus.CONTACT_UNSUPPORTED,
            ),
            (_frame(6, path=8), StabilizationStatus.STREAM_DISCONTINUITY),
            (_frame(7), StabilizationStatus.STREAM_DISCONTINUITY),
            (
                _frame(6, timestamp_seconds=0.7),
                StabilizationStatus.STREAM_DISCONTINUITY,
            ),
            (_frame(6, x=0.7), StabilizationStatus.POSITION_CHANGED),
            (_frame(6, state=5), StabilizationStatus.CONTACT_UNSUPPORTED),
        )
        for frame, status in scenarios:
            with self.subTest(status=status, sequence=frame.sequence):
                stabilizer = self._tared()
                update = stabilizer.process(frame)
                self.assertEqual(update.status, status)
                self.assertEqual(update.baseline_pressure_raw, 10.0)
                self.assertIsNone(stabilizer.tare_result)
                followup = stabilizer.process(_frame(frame.sequence + 1))
                self.assertEqual(followup.status, StabilizationStatus.TARE_REQUIRED)

    def test_tare_expires_instead_of_adapting_to_drift(self) -> None:
        stabilizer = self._tared(
            stability_window_seconds=0.05,
            stable_window_interval_seconds=0.05,
            maximum_tare_age_seconds=0.15,
        )
        update = stabilizer.process(_frame(6, 11.0, timestamp_seconds=0.6))
        self.assertEqual(update.status, StabilizationStatus.TARE_EXPIRED)
        self.assertIsNone(stabilizer.tare_result)

    def test_median_then_moving_average_are_exact_and_unclamped(self) -> None:
        stabilizer = self._tared(
            outlier_floor_raw=1_000.0,
            stability_window_seconds=5.0,
            stable_window_interval_seconds=5.0,
            maximum_tare_age_seconds=20.0,
        )
        values = (10.0, 110.0, 10.0, 20.0)
        updates = [
            stabilizer.process(_frame(sequence, value))
            for sequence, value in enumerate(values, start=6)
        ]

        self.assertEqual(updates[2].median_filtered_delta_raw, 0.0)
        self.assertEqual(updates[3].median_filtered_delta_raw, 10.0)
        self.assertEqual(updates[3].smoothed_delta_raw, 5.0)

    def test_transient_is_rejected_and_sustained_new_level_recovers(self) -> None:
        stabilizer = self._tared(outlier_floor_raw=2.0)
        first_publication = None
        for sequence in range(6, 14):
            update = stabilizer.process(_frame(sequence, 12.0))
            first_publication = update.published_measurement or first_publication
        self.assertIsNotNone(first_publication)

        transient = stabilizer.process(_frame(14, 30.0))
        self.assertEqual(transient.status, StabilizationStatus.TRANSIENT_REJECTED)
        self.assertIsNone(transient.published_measurement)
        self.assertIsNotNone(stabilizer.tare_result)

        second_publication = None
        for sequence in range(15, 23):
            update = stabilizer.process(_frame(sequence, 30.0))
            second_publication = update.published_measurement or second_publication
        self.assertIsNotNone(second_publication)
        self.assertEqual(second_publication.pressure_delta_raw, 20.0)

    def test_drift_remains_visible_and_never_moves_the_frozen_tare(self) -> None:
        stabilizer = self._tared(
            outlier_floor_raw=1_000.0,
            stability_max_mad_raw=100.0,
            stability_max_abs_slope_raw_per_second=100.0,
            stable_window_agreement_raw=100.0,
        )
        observed_slope = None
        for sequence in range(6, 12):
            update = stabilizer.process(_frame(sequence, float(sequence + 5)))
            if update.slope_raw_per_second is not None:
                observed_slope = update.slope_raw_per_second
        self.assertIsNotNone(observed_slope)
        self.assertGreater(observed_slope, 0.0)
        self.assertEqual(stabilizer.tare_result.baseline_pressure_raw, 10.0)

    def test_irregular_timing_cannot_borrow_a_pre_cutoff_sample(self) -> None:
        stabilizer = self._tared(
            smoothing_window_samples=2,
            max_sample_gap_seconds=0.12,
        )
        timestamps = (0.51, 0.62, 0.73, 0.84, 0.95)
        updates = [
            stabilizer.process(_frame(sequence, 20.0, timestamp_seconds=timestamp))
            for sequence, timestamp in zip(range(6, 11), timestamps)
        ]
        self.assertEqual(updates[-1].status, StabilizationStatus.FILTER_WARMUP)
        self.assertEqual(updates[-1].window_duration_seconds, 0.11)

    def test_stable_window_disagreement_restarts_confirmation(self) -> None:
        stabilizer = self._tared(
            smoothing_window_samples=2,
            outlier_floor_raw=1_000.0,
            stability_max_mad_raw=100.0,
            stability_max_abs_slope_raw_per_second=1_000.0,
            stable_window_agreement_raw=1.0,
        )
        first = None
        for sequence in range(6, 10):
            first = stabilizer.process(_frame(sequence, 20.0))
        stabilizer.process(_frame(10, 100.0))
        first = stabilizer.process(_frame(11, 100.0))
        self.assertEqual(first.status, StabilizationStatus.STABLE_PENDING)
        self.assertEqual(first.agreeing_window_count, 1)

        stabilizer.process(_frame(12, 100.0))
        disagreement = stabilizer.process(_frame(13, 100.0))
        self.assertEqual(disagreement.status, StabilizationStatus.STABLE_PENDING)
        self.assertEqual(disagreement.agreeing_window_count, 1)
        self.assertIn("disagreed", disagreement.reason)
        self.assertIsNone(disagreement.published_measurement)

    def test_dispersion_and_slope_each_reject_a_window(self) -> None:
        cases = (
            {
                "stability_max_mad_raw": 1.0,
                "stability_max_abs_slope_raw_per_second": 1_000.0,
            },
            {
                "stability_max_mad_raw": 1_000.0,
                "stability_max_abs_slope_raw_per_second": 1.0,
            },
        )
        for limits in cases:
            with self.subTest(limits=limits):
                stabilizer = self._tared(
                    smoothing_window_samples=2,
                    outlier_floor_raw=1_000.0,
                    **limits,
                )
                update = None
                for sequence, pressure in zip(
                    range(6, 12), (10.0, 15.0, 20.0, 25.0, 30.0, 35.0)
                ):
                    update = stabilizer.process(_frame(sequence, pressure))
                self.assertEqual(update.status, StabilizationStatus.UNSTABLE)

    def test_cumulative_drift_cannot_leave_an_unbounded_stale_publication(self) -> None:
        stabilizer = self._tared(
            smoothing_window_samples=2,
            outlier_floor_raw=1_000.0,
            stability_max_mad_raw=100.0,
            stability_max_abs_slope_raw_per_second=10.0,
            stable_window_agreement_raw=0.3,
        )
        publications = []
        restart_observed = False
        for sequence in range(6, 56):
            pressure = 10.0 + (sequence - 5) * 0.05
            update = stabilizer.process(_frame(sequence, pressure))
            if update.published_measurement is not None:
                publications.append(update.published_measurement.pressure_delta_raw)
            restart_observed = restart_observed or "published" in update.reason

        self.assertTrue(restart_observed)
        self.assertGreaterEqual(len(publications), 2)
        self.assertGreater(publications[-1], publications[0])

    def test_reset_clears_tare_and_any_stable_publication(self) -> None:
        stabilizer = self._tared()
        for sequence in range(6, 14):
            stabilizer.process(_frame(sequence, 20.0))
        stabilizer.reset()
        self.assertIsNone(stabilizer.tare_result)
        self.assertIsNone(stabilizer.last_update)
        self.assertEqual(
            stabilizer.process(_frame(14)).status,
            StabilizationStatus.TARE_REQUIRED,
        )

    def test_restart_stability_search_preserves_tare_and_continuity(self) -> None:
        stabilizer = self._tared()
        for sequence in range(6, 10):
            stabilizer.process(_frame(sequence, 20.0))

        stabilizer.restart_stability_search()

        self.assertEqual(stabilizer.tare_result.baseline_pressure_raw, 10.0)
        self.assertIsNone(stabilizer.last_update)
        update = stabilizer.process(_frame(10, 30.0))
        self.assertEqual(update.status, StabilizationStatus.FILTER_WARMUP)
        self.assertEqual(update.pressure_delta_raw, 20.0)

    def test_restart_stability_search_requires_tare(self) -> None:
        stabilizer = PressureStabilizer(_config())
        with self.assertRaisesRegex(RuntimeError, "before tare"):
            stabilizer.restart_stability_search()


class MetricTests(unittest.TestCase):
    def test_mad_and_ols_slope_use_raw_units_and_irregular_time(self) -> None:
        timestamps = (0, 1_000_000_000, 3_000_000_000)
        values = (1.0, 3.0, 7.0)
        self.assertEqual(_mad(values), 2.0)
        self.assertAlmostEqual(_slope_raw_per_second(timestamps, values), 2.0)


if __name__ == "__main__":
    unittest.main()
