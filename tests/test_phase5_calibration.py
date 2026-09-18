"""Synthetic mathematics only; these fixtures make no hardware-accuracy claim."""

import math
import unittest
from dataclasses import replace

from trackpad_scale.phase4_stabilizer import (
    StabilityConfidence,
    StablePressureMeasurement,
)
from trackpad_scale.phase5_calibration import (
    CalibrationConfig,
    CalibrationKind,
    CalibrationSeries,
    fit_and_validate_calibration,
)


def _config(**overrides: object) -> CalibrationConfig:
    values = {
        "min_repeats_per_mass": 3,
        "max_repeated_mad_raw": 0.5,
        "max_repeated_absolute_deviation_raw": 0.5,
        "max_training_absolute_residual_grams": 5.0,
        "nonlinear_residual_trigger_grams": 5.0,
        "max_piecewise_training_cv_absolute_error_grams": 15.0,
        "minimum_piecewise_training_cv_improvement_grams": 5.0,
        "max_holdout_absolute_error_grams": 3.0,
        "allowed_mass_min_grams": 50.0,
        "allowed_mass_max_grams": 150.0,
    }
    values.update(overrides)
    return CalibrationConfig(**values)  # type: ignore[arg-type]


def _series(mass: float, raw: float) -> CalibrationSeries:
    return CalibrationSeries(mass, (raw, raw, raw))


def _linear_data():
    training = (_series(20, 2), _series(100, 10), _series(200, 20))
    heldout = (_series(50, 5), _series(150, 15))
    return training, heldout


def _nonlinear_data():
    # Synthetic y=10*x^2. Interior held-out x=2.5 and x=3.5 are not fit knots.
    training = tuple(_series(10 * x * x, x) for x in (1, 2, 3, 4, 5))
    heldout = (_series(62.5, 2.5), _series(122.5, 3.5))
    return training, heldout


def _measurement(raw: float) -> StablePressureMeasurement:
    confidence = StabilityConfidence(
        agreeing_window_count=2,
        required_window_count=2,
        agreement_span_raw=0.0,
        agreement_limit_raw=0.0,
        maximum_window_mad_raw=0.0,
        mad_limit_raw=0.0,
        maximum_absolute_slope_raw_per_second=0.0,
        slope_limit_raw_per_second=0.0,
    )
    return StablePressureMeasurement(
        pressure_delta_raw=raw,
        dispersion_mad_raw=0.0,
        slope_raw_per_second=0.0,
        duration_seconds=1.0,
        sequence=123,
        host_monotonic_ns=456,
        confidence=confidence,
    )


class CalibrationInputTests(unittest.TestCase):
    def test_config_has_no_defaults_and_rejects_invalid_tolerances(self) -> None:
        with self.assertRaises(TypeError):
            CalibrationConfig()  # type: ignore[call-arg]
        invalid = (
            {"min_repeats_per_mass": True},
            {"min_repeats_per_mass": 1},
            {"max_repeated_mad_raw": math.nan},
            {"max_repeated_absolute_deviation_raw": -1},
            {"nonlinear_residual_trigger_grams": 0},
            {"minimum_piecewise_training_cv_improvement_grams": 0},
            {"max_holdout_absolute_error_grams": math.inf},
            {"allowed_mass_min_grams": -1},
            {"allowed_mass_max_grams": 20},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                _config(**overrides)

    def test_series_requires_immutable_finite_repeats_and_nonnegative_mass(
        self,
    ) -> None:
        invalid = (
            (True, (1.0, 1.0)),
            (-1.0, (1.0, 1.0)),
            (math.inf, (1.0, 1.0)),
            (20.0, [1.0, 1.0]),
            (20.0, (1.0,)),
            (20.0, (1.0, math.nan)),
            (20.0, (1.0, True)),
        )
        for mass, readings in invalid:
            with self.subTest(mass=mass, readings=readings):
                with self.assertRaises(ValueError):
                    CalibrationSeries(mass, readings)  # type: ignore[arg-type]

    def test_requires_independent_interior_holdout_and_measured_allowed_range(
        self,
    ) -> None:
        training, heldout = _linear_data()
        invalid_cases = (
            (training[:2], heldout, _config()),
            (training, heldout[:1], _config()),
            (training, (_series(100, 10), heldout[1]), _config()),
            (training, (_series(10, 1), heldout[1]), _config()),
            (training, heldout, _config(allowed_mass_min_grams=0)),
            (training, heldout, _config(allowed_mass_max_grams=201)),
            (training + (training[1],), heldout, _config()),
        )
        for train, test, config in invalid_cases:
            with self.subTest(train=train, test=test, config=config):
                with self.assertRaises(ValueError):
                    fit_and_validate_calibration(train, test, config)

    def test_repeated_readings_must_meet_configured_count(self) -> None:
        training, heldout = _linear_data()
        two_repeats = replace(training[0], stable_pressure_deltas_raw=(2, 2))
        with self.assertRaises(ValueError):
            fit_and_validate_calibration(
                (two_repeats,) + training[1:], heldout, _config()
            )


class CalibrationFitTests(unittest.TestCase):
    def test_exact_linear_fit_and_bounded_candidate_estimation(self) -> None:
        training, heldout = _linear_data()
        report = fit_and_validate_calibration(training, heldout, _config())
        self.assertTrue(report.accepted)
        self.assertEqual(report.preselected_kind, CalibrationKind.LINEAR)
        self.assertIsNotNone(report.model)
        assert report.model is not None
        self.assertEqual(report.model.kind, CalibrationKind.LINEAR)
        self.assertEqual(report.model.pressure_range_raw, (2.0, 20.0))
        self.assertEqual(report.model.mass_range_grams, (20.0, 200.0))
        self.assertTrue(
            all(abs(item.error_grams) < 1e-12 for item in report.linear_heldout_errors)
        )
        self.assertEqual(len(report.linear_heldout_trial_errors), 6)
        estimate = report.model.estimate_stable_candidate(_measurement(5))
        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertAlmostEqual(estimate.candidate_grams, 50.0)
        self.assertEqual(estimate.sequence, 123)
        self.assertIsNone(report.model.estimate_stable_candidate(_measurement(1.9)))
        self.assertIsNone(report.model.estimate_stable_candidate(_measurement(20.1)))
        with self.assertRaises(TypeError):
            report.model.estimate_stable_candidate(5.0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            report.model.estimate_stable_candidate(_measurement(math.nan))

    def test_narrowed_allowed_mass_range_is_enforced_after_fit(self) -> None:
        training, heldout = _linear_data()
        config = _config(allowed_mass_min_grams=59, allowed_mass_max_grams=141)
        report = fit_and_validate_calibration(training, heldout, config)
        self.assertTrue(report.accepted)
        assert report.model is not None
        self.assertIsNone(report.model.estimate_stable_candidate(_measurement(4)))
        self.assertIsNone(report.model.estimate_stable_candidate(_measurement(5)))
        self.assertIsNotNone(report.model.estimate_stable_candidate(_measurement(6)))
        self.assertIsNotNone(report.model.estimate_stable_candidate(_measurement(14)))
        self.assertIsNone(report.model.estimate_stable_candidate(_measurement(15)))
        self.assertIsNone(report.model.estimate_stable_candidate(_measurement(16)))

    def test_piecewise_is_preselected_by_training_cv_then_passes_holdout(self) -> None:
        training, heldout = _nonlinear_data()
        report = fit_and_validate_calibration(
            training,
            heldout,
            _config(
                max_training_absolute_residual_grams=50,
                allowed_mass_min_grams=62.5,
                allowed_mass_max_grams=122.5,
            ),
        )
        self.assertTrue(report.accepted, report.reason)
        self.assertTrue(report.nonlinear_triggered)
        self.assertEqual(report.preselected_kind, CalibrationKind.PIECEWISE_LINEAR)
        self.assertGreater(report.piecewise_cv_max_error_improvement_grams, 5)  # type: ignore[operator]
        self.assertEqual(len(report.piecewise_heldout_trial_errors), 6)
        self.assertFalse(report.linear_heldout_trial_errors)
        assert report.model is not None
        self.assertEqual(report.model.kind, CalibrationKind.PIECEWISE_LINEAR)
        self.assertTrue(
            any(item.error_grams > 0 for item in report.linear_training_residuals)
        )
        self.assertTrue(
            any(item.error_grams < 0 for item in report.linear_training_residuals)
        )
        self.assertAlmostEqual(
            report.model.estimate_stable_candidate(_measurement(2.5)).candidate_grams,  # type: ignore[union-attr]
            65.0,
        )

    def test_piecewise_is_not_selected_without_training_cv_margin(self) -> None:
        training, heldout = _nonlinear_data()
        report = fit_and_validate_calibration(
            training,
            heldout,
            _config(
                allowed_mass_min_grams=62.5,
                allowed_mass_max_grams=122.5,
                max_training_absolute_residual_grams=100,
                max_holdout_absolute_error_grams=100,
                minimum_piecewise_training_cv_improvement_grams=100,
            ),
        )
        self.assertTrue(report.accepted)
        self.assertEqual(report.preselected_kind, CalibrationKind.LINEAR)
        self.assertTrue(report.nonlinear_triggered)
        self.assertFalse(report.piecewise_heldout_errors)

    def test_allowed_range_requires_independent_heldout_envelope(self) -> None:
        training, heldout = _linear_data()
        report = fit_and_validate_calibration(
            training,
            heldout,
            _config(allowed_mass_min_grams=20, allowed_mass_max_grams=200),
        )
        self.assertFalse(report.accepted)
        self.assertEqual(report.reason, "allowed_range_not_covered_by_heldout_masses")
        self.assertEqual(report.preselected_kind, CalibrationKind.LINEAR)
        self.assertIsNone(report.model)

    def test_piecewise_requires_heldout_inside_every_allowed_segment(self) -> None:
        training, _ = _nonlinear_data()
        # 22.5 and 122.5 bracket this interval, but neither tests segment 40-90.
        heldout = (_series(22.5, 1.5), _series(122.5, 3.5))
        report = fit_and_validate_calibration(
            training,
            heldout,
            _config(allowed_mass_min_grams=22.5, allowed_mass_max_grams=122.5),
        )
        self.assertFalse(report.accepted)
        self.assertEqual(report.preselected_kind, CalibrationKind.PIECEWISE_LINEAR)
        self.assertEqual(report.reason, "piecewise_segment_missing_heldout_mass")
        self.assertIsNone(report.model)

    def test_frozen_method_does_not_fallback_after_heldout_failure(self) -> None:
        training, _ = _nonlinear_data()
        # Pressure remains increasing, but independent references expose error.
        heldout = (_series(62.5, 2.7), _series(122.5, 3.7))
        report = fit_and_validate_calibration(
            training,
            heldout,
            _config(allowed_mass_min_grams=62.5, allowed_mass_max_grams=122.5),
        )
        self.assertFalse(report.accepted)
        self.assertEqual(report.preselected_kind, CalibrationKind.PIECEWISE_LINEAR)
        self.assertEqual(report.reason, "independent_heldout_error_exceeded")
        self.assertIsNone(report.model)

    def test_holdout_trial_error_cannot_hide_behind_median(self) -> None:
        training, _ = _linear_data()
        heldout = (
            CalibrationSeries(50, (4.5, 5.0, 5.5)),
            _series(150, 15),
        )
        report = fit_and_validate_calibration(
            training,
            heldout,
            _config(
                max_repeated_mad_raw=1,
                max_repeated_absolute_deviation_raw=1,
                max_holdout_absolute_error_grams=2,
            ),
        )
        self.assertFalse(report.accepted)
        self.assertEqual(report.reason, "independent_heldout_error_exceeded")
        self.assertTrue(
            all(abs(item.error_grams) < 1e-12 for item in report.linear_heldout_errors)
        )
        self.assertAlmostEqual(
            max(abs(item.error_grams) for item in report.linear_heldout_trial_errors), 5
        )
        self.assertIsNone(report.model)

    def test_repeat_outlier_overlap_and_nonmonotonicity_reject(self) -> None:
        training, heldout = _linear_data()
        noisy = (CalibrationSeries(20, (2, 2, 9)),) + training[1:]
        rejected = fit_and_validate_calibration(noisy, heldout, _config())
        self.assertEqual(rejected.reason, "repeat_dispersion_exceeded")
        self.assertIsNone(rejected.model)

        overlap = (CalibrationSeries(20, (2, 2.5, 3)),) + training[1:]
        heldout_overlap = (CalibrationSeries(50, (3, 5, 5)), heldout[1])
        rejected = fit_and_validate_calibration(
            overlap,
            heldout_overlap,
            _config(
                max_repeated_mad_raw=2,
                max_repeated_absolute_deviation_raw=2,
                max_training_absolute_residual_grams=20,
            ),
        )
        self.assertEqual(rejected.reason, "overlapping_mass_pressure_ranges")

        nonmonotonic = (training[0], training[1], _series(200, 9))
        rejected = fit_and_validate_calibration(nonmonotonic, heldout, _config())
        self.assertEqual(rejected.reason, "non_monotonic_pressure_medians")

    def test_three_training_masses_do_not_claim_piecewise_cv_evidence(self) -> None:
        training = (_series(10, 1), _series(90, 3), _series(250, 5))
        heldout = (_series(40, 2), _series(160, 4))
        report = fit_and_validate_calibration(
            training,
            heldout,
            _config(
                allowed_mass_min_grams=40,
                allowed_mass_max_grams=160,
                max_training_absolute_residual_grams=50,
                max_holdout_absolute_error_grams=50,
            ),
        )
        self.assertEqual(report.preselected_kind, CalibrationKind.LINEAR)
        self.assertFalse(report.piecewise_training_cv_errors)


if __name__ == "__main__":
    unittest.main()
