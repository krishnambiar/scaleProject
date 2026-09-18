"""Evidence-gated, pure-Python Phase 5 calibration mathematics.

The inputs are *already stable* Phase 4 pressure deltas paired with independently
known reference masses.  A passing fit is only a mathematical candidate: this
module cannot establish sensor provenance, reference-mass accuracy, repeatable
placement, independent session/trial identity, or real-world weighing validity.
Those are evidence-layer obligations.  It never publishes a public weight.
"""

import bisect
import math
import statistics
from dataclasses import InitVar, dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple

from .phase4_stabilizer import StablePressureMeasurement


_VALIDATED_MODEL_TOKEN = object()


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _median(values: Sequence[float]) -> float:
    result = statistics.median(values)
    if not _finite_number(result):
        raise ValueError("calibration median is not finite")
    return float(result)


@dataclass(frozen=True)
class CalibrationSeries:
    """Repeated, stable raw-domain observations for one independently known mass.

    A zero-mass series is permitted but not required: Phase 4's reference-contact
    tare has not established an unloaded physical zero.
    """

    known_mass_grams: float
    stable_pressure_deltas_raw: Tuple[float, ...]

    def __post_init__(self) -> None:
        if not _finite_number(self.known_mass_grams) or self.known_mass_grams < 0:
            raise ValueError("known_mass_grams must be finite and >= 0")
        readings = self.stable_pressure_deltas_raw
        if not isinstance(readings, tuple) or len(readings) < 2:
            raise ValueError("stable_pressure_deltas_raw must be a tuple of repeats")
        if any(not _finite_number(reading) for reading in readings):
            raise ValueError("all stable pressure deltas must be finite numbers")


@dataclass(frozen=True)
class CalibrationConfig:
    """All acceptance tolerances must come from a documented experiment."""

    min_repeats_per_mass: int
    max_repeated_mad_raw: float
    max_repeated_absolute_deviation_raw: float
    max_training_absolute_residual_grams: float
    nonlinear_residual_trigger_grams: float
    max_piecewise_training_cv_absolute_error_grams: float
    minimum_piecewise_training_cv_improvement_grams: float
    max_holdout_absolute_error_grams: float
    allowed_mass_min_grams: float
    allowed_mass_max_grams: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.min_repeats_per_mass, bool)
            or not isinstance(self.min_repeats_per_mass, int)
            or self.min_repeats_per_mass < 2
        ):
            raise ValueError("min_repeats_per_mass must be an integer >= 2")
        nonnegative = (
            "max_repeated_mad_raw",
            "max_repeated_absolute_deviation_raw",
            "max_training_absolute_residual_grams",
            "max_piecewise_training_cv_absolute_error_grams",
            "max_holdout_absolute_error_grams",
        )
        positive = (
            "nonlinear_residual_trigger_grams",
            "minimum_piecewise_training_cv_improvement_grams",
        )
        for name in nonnegative:
            value = getattr(self, name)
            if not _finite_number(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        for name in positive:
            value = getattr(self, name)
            if not _finite_number(value) or value <= 0:
                raise ValueError(f"{name} must be finite and > 0")
        if (
            not _finite_number(self.allowed_mass_min_grams)
            or not _finite_number(self.allowed_mass_max_grams)
            or self.allowed_mass_min_grams < 0
            or self.allowed_mass_max_grams <= self.allowed_mass_min_grams
        ):
            raise ValueError(
                "allowed mass range must be finite, nonnegative, and increasing"
            )


@dataclass(frozen=True)
class CalibrationPoint:
    known_mass_grams: float
    median_pressure_delta_raw: float
    repeat_count: int
    repeat_mad_raw: float
    repeat_max_absolute_deviation_raw: float
    repeat_min_pressure_delta_raw: float
    repeat_max_pressure_delta_raw: float


@dataclass(frozen=True)
class PredictionError:
    known_mass_grams: float
    median_pressure_delta_raw: float
    predicted_mass_grams: float
    error_grams: float


@dataclass(frozen=True)
class TrialPredictionError:
    known_mass_grams: float
    trial_index: int
    pressure_delta_raw: float
    predicted_mass_grams: float
    error_grams: float


class CalibrationKind(str, Enum):
    LINEAR = "linear"
    PIECEWISE_LINEAR = "piecewise_linear"


@dataclass(frozen=True)
class CandidateMassEstimate:
    """Mathematical estimate only, not a validated public weight or hydration event."""

    candidate_grams: float
    pressure_delta_raw: float
    sequence: int
    host_monotonic_ns: int
    model_kind: CalibrationKind
    validation_max_holdout_absolute_error_grams: float


@dataclass(frozen=True)
class CalibrationModel:
    """A mathematically accepted candidate bounded by measured training knots."""

    kind: CalibrationKind
    training_points: Tuple[CalibrationPoint, ...]
    linear_slope_grams_per_raw: Optional[float]
    linear_intercept_grams: Optional[float]
    validation_max_holdout_absolute_error_grams: float
    allowed_mass_min_grams: float
    allowed_mass_max_grams: float
    _fit_validation_token: InitVar[object]

    def __post_init__(self, _fit_validation_token: object) -> None:
        if _fit_validation_token is not _VALIDATED_MODEL_TOKEN:
            raise ValueError(
                "CalibrationModel must come from fit_and_validate_calibration"
            )

    @property
    def pressure_range_raw(self) -> Tuple[float, float]:
        return (
            self.training_points[0].median_pressure_delta_raw,
            self.training_points[-1].median_pressure_delta_raw,
        )

    @property
    def mass_range_grams(self) -> Tuple[float, float]:
        return (
            self.training_points[0].known_mass_grams,
            self.training_points[-1].known_mass_grams,
        )

    def estimate_stable_candidate(
        self, measurement: StablePressureMeasurement
    ) -> Optional[CandidateMassEstimate]:
        """Return None outside the *measured* pressure/mass range; never extrapolate."""

        if not isinstance(measurement, StablePressureMeasurement):
            raise TypeError("a Phase 4 StablePressureMeasurement is required")
        raw = measurement.pressure_delta_raw
        if not _finite_number(raw):
            raise ValueError("stable pressure delta must be finite")
        lower_raw, upper_raw = self.pressure_range_raw
        if raw < lower_raw or raw > upper_raw:
            return None
        if self.kind is CalibrationKind.LINEAR:
            if (
                self.linear_slope_grams_per_raw is None
                or self.linear_intercept_grams is None
            ):
                raise ValueError("linear model coefficients are unavailable")
            candidate = (
                self.linear_slope_grams_per_raw * raw + self.linear_intercept_grams
            )
        elif self.kind is CalibrationKind.PIECEWISE_LINEAR:
            candidate = _piecewise_predict(self.training_points, raw)
        else:
            raise ValueError("unsupported calibration kind")
        lower_mass, upper_mass = self.mass_range_grams
        if (
            not _finite_number(candidate)
            or candidate < lower_mass
            or candidate > upper_mass
            or candidate < self.allowed_mass_min_grams
            or candidate > self.allowed_mass_max_grams
        ):
            return None
        return CandidateMassEstimate(
            candidate_grams=float(candidate),
            pressure_delta_raw=float(raw),
            sequence=measurement.sequence,
            host_monotonic_ns=measurement.host_monotonic_ns,
            model_kind=self.kind,
            validation_max_holdout_absolute_error_grams=(
                self.validation_max_holdout_absolute_error_grams
            ),
        )


@dataclass(frozen=True)
class CalibrationValidation:
    """Transparent fit diagnostics; `model` is absent on any validation failure."""

    accepted: bool
    reason: str
    model: Optional[CalibrationModel]
    training_points: Tuple[CalibrationPoint, ...]
    heldout_points: Tuple[CalibrationPoint, ...]
    preselected_kind: Optional[CalibrationKind] = None
    linear_training_residuals: Tuple[PredictionError, ...] = ()
    linear_training_cv_errors: Tuple[PredictionError, ...] = ()
    piecewise_training_cv_errors: Tuple[PredictionError, ...] = ()
    linear_heldout_errors: Tuple[PredictionError, ...] = ()
    piecewise_heldout_errors: Tuple[PredictionError, ...] = ()
    linear_heldout_trial_errors: Tuple[TrialPredictionError, ...] = ()
    piecewise_heldout_trial_errors: Tuple[TrialPredictionError, ...] = ()
    nonlinear_triggered: bool = False
    piecewise_cv_max_error_improvement_grams: Optional[float] = None


def _points(
    series: Sequence[CalibrationSeries],
    config: CalibrationConfig,
    label: str,
) -> Tuple[CalibrationPoint, ...]:
    if not isinstance(series, (tuple, list)):
        raise TypeError(f"{label} must be a tuple or list of CalibrationSeries")
    if any(not isinstance(item, CalibrationSeries) for item in series):
        raise TypeError(f"{label} must contain only CalibrationSeries")
    masses = [item.known_mass_grams for item in series]
    if len(masses) != len(set(masses)):
        raise ValueError(f"{label} masses must be distinct")
    points = []
    for item in series:
        readings = item.stable_pressure_deltas_raw
        if len(readings) < config.min_repeats_per_mass:
            raise ValueError(f"{label} has fewer than the required stable repeats")
        center = _median(readings)
        deviations = tuple(abs(value - center) for value in readings)
        mad = _median(deviations)
        largest = max(deviations)
        if not _finite_number(largest):
            raise ValueError("calibration repeat dispersion is not finite")
        points.append(
            CalibrationPoint(
                known_mass_grams=float(item.known_mass_grams),
                median_pressure_delta_raw=center,
                repeat_count=len(readings),
                repeat_mad_raw=mad,
                repeat_max_absolute_deviation_raw=float(largest),
                repeat_min_pressure_delta_raw=float(min(readings)),
                repeat_max_pressure_delta_raw=float(max(readings)),
            )
        )
    return tuple(sorted(points, key=lambda item: item.known_mass_grams))


def _linear_fit(points: Tuple[CalibrationPoint, ...]) -> Tuple[float, float]:
    x_mean = math.fsum(point.median_pressure_delta_raw for point in points) / len(
        points
    )
    y_mean = math.fsum(point.known_mass_grams for point in points) / len(points)
    numerator = math.fsum(
        (point.median_pressure_delta_raw - x_mean) * (point.known_mass_grams - y_mean)
        for point in points
    )
    denominator = math.fsum(
        (point.median_pressure_delta_raw - x_mean) ** 2 for point in points
    )
    if not _finite_number(denominator) or denominator <= 0:
        raise ValueError("linear fit has no finite positive pressure variance")
    slope = numerator / denominator
    intercept = y_mean - slope * x_mean
    if not _finite_number(slope) or slope <= 0 or not _finite_number(intercept):
        raise ValueError("linear fit requires finite, positive mass-per-pressure slope")
    return float(slope), float(intercept)


def _piecewise_predict(points: Tuple[CalibrationPoint, ...], raw: float) -> float:
    xs = tuple(point.median_pressure_delta_raw for point in points)
    if raw < xs[0] or raw > xs[-1]:
        raise ValueError("piecewise prediction cannot extrapolate")
    index = min(bisect.bisect_right(xs, raw) - 1, len(points) - 2)
    left = points[index]
    right = points[index + 1]
    fraction = (raw - left.median_pressure_delta_raw) / (
        right.median_pressure_delta_raw - left.median_pressure_delta_raw
    )
    return left.known_mass_grams + fraction * (
        right.known_mass_grams - left.known_mass_grams
    )


def _errors(
    points: Tuple[CalibrationPoint, ...], predict
) -> Tuple[PredictionError, ...]:
    errors = []
    for point in points:
        predicted = predict(point.median_pressure_delta_raw)
        if not _finite_number(predicted):
            raise ValueError("calibration prediction is not finite")
        errors.append(
            PredictionError(
                known_mass_grams=point.known_mass_grams,
                median_pressure_delta_raw=point.median_pressure_delta_raw,
                predicted_mass_grams=float(predicted),
                error_grams=float(predicted - point.known_mass_grams),
            )
        )
    return tuple(errors)


def _max_absolute_error(errors: Tuple[PredictionError, ...]) -> float:
    return max(abs(item.error_grams) for item in errors)


def _interior_training_cv(
    points: Tuple[CalibrationPoint, ...],
) -> Tuple[Tuple[PredictionError, ...], Tuple[PredictionError, ...]]:
    """Compare candidate methods without ever consulting final held-out masses."""

    linear_errors = []
    piecewise_errors = []
    for index in range(1, len(points) - 1):
        excluded = points[index]
        remaining = points[:index] + points[index + 1 :]
        slope, intercept = _linear_fit(remaining)
        linear_predicted = slope * excluded.median_pressure_delta_raw + intercept
        piecewise_predicted = _piecewise_predict(
            remaining, excluded.median_pressure_delta_raw
        )
        if not _finite_number(linear_predicted) or not _finite_number(
            piecewise_predicted
        ):
            raise ValueError("training cross-validation prediction is not finite")
        linear_errors.append(
            PredictionError(
                known_mass_grams=excluded.known_mass_grams,
                median_pressure_delta_raw=excluded.median_pressure_delta_raw,
                predicted_mass_grams=float(linear_predicted),
                error_grams=float(linear_predicted - excluded.known_mass_grams),
            )
        )
        piecewise_errors.append(
            PredictionError(
                known_mass_grams=excluded.known_mass_grams,
                median_pressure_delta_raw=excluded.median_pressure_delta_raw,
                predicted_mass_grams=float(piecewise_predicted),
                error_grams=float(piecewise_predicted - excluded.known_mass_grams),
            )
        )
    return tuple(linear_errors), tuple(piecewise_errors)


def _trial_errors(
    series: Sequence[CalibrationSeries], predict
) -> Tuple[TrialPredictionError, ...]:
    errors = []
    for group in series:
        for index, raw in enumerate(group.stable_pressure_deltas_raw):
            predicted = predict(raw)
            if not _finite_number(predicted):
                raise ValueError("held-out trial prediction is not finite")
            errors.append(
                TrialPredictionError(
                    known_mass_grams=float(group.known_mass_grams),
                    trial_index=index,
                    pressure_delta_raw=float(raw),
                    predicted_mass_grams=float(predicted),
                    error_grams=float(predicted - group.known_mass_grams),
                )
            )
    return tuple(errors)


def fit_and_validate_calibration(
    training: Sequence[CalibrationSeries],
    heldout: Sequence[CalibrationSeries],
    config: CalibrationConfig,
) -> CalibrationValidation:
    """Freeze method from training-only evidence, then open independent holdout.

    Structural invalidity raises.  Insufficient repeat quality, non-monotonic
    pressure, or accuracy failure returns a rejected report without a model.
    The caller must separately validate device/protocol evidence before using
    even an accepted candidate model for actual weight claims.
    """

    if not isinstance(config, CalibrationConfig):
        raise TypeError("config must be CalibrationConfig")
    training_points = _points(training, config, "training")
    if len(training_points) < 3:
        raise ValueError("at least three distinct training masses are required")
    lower_mass = training_points[0].known_mass_grams
    upper_mass = training_points[-1].known_mass_grams
    if (
        config.allowed_mass_min_grams < lower_mass
        or config.allowed_mass_max_grams > upper_mass
    ):
        raise ValueError("allowed mass range must be inside measured training masses")

    heldout_points: Tuple[CalibrationPoint, ...] = ()
    linear_training: Tuple[PredictionError, ...] = ()
    linear_cv: Tuple[PredictionError, ...] = ()
    piecewise_cv: Tuple[PredictionError, ...] = ()
    linear_heldout: Tuple[PredictionError, ...] = ()
    piecewise_heldout: Tuple[PredictionError, ...] = ()
    linear_heldout_trials: Tuple[TrialPredictionError, ...] = ()
    piecewise_heldout_trials: Tuple[TrialPredictionError, ...] = ()
    kind: Optional[CalibrationKind] = None
    nonlinear = False
    improvement: Optional[float] = None

    def reject(reason: str) -> CalibrationValidation:
        return CalibrationValidation(
            accepted=False,
            reason=reason,
            model=None,
            training_points=training_points,
            heldout_points=heldout_points,
            preselected_kind=kind,
            linear_training_residuals=linear_training,
            linear_training_cv_errors=linear_cv,
            piecewise_training_cv_errors=piecewise_cv,
            linear_heldout_errors=linear_heldout,
            piecewise_heldout_errors=piecewise_heldout,
            linear_heldout_trial_errors=linear_heldout_trials,
            piecewise_heldout_trial_errors=piecewise_heldout_trials,
            nonlinear_triggered=nonlinear,
            piecewise_cv_max_error_improvement_grams=improvement,
        )

    for point in training_points:
        if (
            point.repeat_mad_raw > config.max_repeated_mad_raw
            or point.repeat_max_absolute_deviation_raw
            > config.max_repeated_absolute_deviation_raw
        ):
            return reject("repeat_dispersion_exceeded")

    if any(
        right.median_pressure_delta_raw <= left.median_pressure_delta_raw
        for left, right in zip(training_points, training_points[1:])
    ):
        return reject("non_monotonic_pressure_medians")
    if any(
        left.repeat_max_pressure_delta_raw >= right.repeat_min_pressure_delta_raw
        for left, right in zip(training_points, training_points[1:])
    ):
        return reject("overlapping_mass_pressure_ranges")

    try:
        slope, intercept = _linear_fit(training_points)
        linear_training = _errors(training_points, lambda raw: slope * raw + intercept)
        for left, right in zip(training_points, training_points[1:]):
            segment_slope = (right.known_mass_grams - left.known_mass_grams) / (
                right.median_pressure_delta_raw - left.median_pressure_delta_raw
            )
            if not _finite_number(segment_slope) or segment_slope <= 0:
                return reject("invalid_piecewise_slope")
        if len(training_points) >= 4:
            linear_cv, piecewise_cv = _interior_training_cv(training_points)
    except (OverflowError, ValueError, ZeroDivisionError):
        return reject("non_finite_or_degenerate_fit")

    linear_train_max = _max_absolute_error(linear_training)
    nonlinear = linear_train_max > config.nonlinear_residual_trigger_grams
    if linear_cv and piecewise_cv:
        improvement = _max_absolute_error(linear_cv) - _max_absolute_error(piecewise_cv)
    if (
        nonlinear
        and improvement is not None
        and improvement > 0
        and improvement >= config.minimum_piecewise_training_cv_improvement_grams
        and _max_absolute_error(piecewise_cv)
        <= config.max_piecewise_training_cv_absolute_error_grams
    ):
        kind = CalibrationKind.PIECEWISE_LINEAR
    else:
        kind = CalibrationKind.LINEAR
        if linear_train_max > config.max_training_absolute_residual_grams:
            return reject("linear_training_residual_exceeded")

    # The method is now frozen. Held-out observations must not influence its choice.
    heldout_points = _points(heldout, config, "heldout")
    if len(heldout_points) < 2:
        raise ValueError("at least two distinct held-out masses are required")
    training_masses = {item.known_mass_grams for item in training_points}
    for point in heldout_points:
        if point.known_mass_grams in training_masses:
            raise ValueError("held-out masses must not appear in training")
        if not lower_mass < point.known_mass_grams < upper_mass:
            raise ValueError("held-out masses must be strictly interior to training")
        if (
            point.repeat_mad_raw > config.max_repeated_mad_raw
            or point.repeat_max_absolute_deviation_raw
            > config.max_repeated_absolute_deviation_raw
        ):
            return reject("repeat_dispersion_exceeded")

    if (
        heldout_points[0].known_mass_grams > config.allowed_mass_min_grams
        or heldout_points[-1].known_mass_grams < config.allowed_mass_max_grams
    ):
        return reject("allowed_range_not_covered_by_heldout_masses")
    if kind is CalibrationKind.PIECEWISE_LINEAR:
        for left, right in zip(training_points, training_points[1:]):
            overlaps_allowed = max(
                left.known_mass_grams, config.allowed_mass_min_grams
            ) < min(right.known_mass_grams, config.allowed_mass_max_grams)
            if overlaps_allowed and not any(
                left.known_mass_grams < point.known_mass_grams < right.known_mass_grams
                for point in heldout_points
            ):
                return reject("piecewise_segment_missing_heldout_mass")

    combined = sorted(
        training_points + heldout_points, key=lambda item: item.known_mass_grams
    )
    if any(
        right.median_pressure_delta_raw <= left.median_pressure_delta_raw
        for left, right in zip(combined, combined[1:])
    ):
        return reject("non_monotonic_pressure_medians")
    if any(
        left.repeat_max_pressure_delta_raw >= right.repeat_min_pressure_delta_raw
        for left, right in zip(combined, combined[1:])
    ):
        return reject("overlapping_mass_pressure_ranges")
    lower_raw = training_points[0].median_pressure_delta_raw
    upper_raw = training_points[-1].median_pressure_delta_raw
    if any(
        point.repeat_min_pressure_delta_raw <= lower_raw
        or point.repeat_max_pressure_delta_raw >= upper_raw
        for point in heldout_points
    ):
        return reject("heldout_trials_outside_training_range")

    def predict(raw: float) -> float:
        if kind is CalibrationKind.LINEAR:
            return slope * raw + intercept
        return _piecewise_predict(training_points, raw)

    try:
        if kind is CalibrationKind.LINEAR:
            linear_heldout = _errors(heldout_points, predict)
            linear_heldout_trials = _trial_errors(heldout, predict)
            selected_median_errors = linear_heldout
            selected_trial_errors = linear_heldout_trials
        else:
            piecewise_heldout = _errors(heldout_points, predict)
            piecewise_heldout_trials = _trial_errors(heldout, predict)
            selected_median_errors = piecewise_heldout
            selected_trial_errors = piecewise_heldout_trials
    except (OverflowError, ValueError, ZeroDivisionError):
        return reject("non_finite_or_degenerate_heldout_prediction")
    accepted_holdout_max = max(
        _max_absolute_error(selected_median_errors),
        max(abs(item.error_grams) for item in selected_trial_errors),
    )
    if accepted_holdout_max > config.max_holdout_absolute_error_grams:
        return reject("independent_heldout_error_exceeded")

    model = CalibrationModel(
        kind=kind,
        training_points=training_points,
        linear_slope_grams_per_raw=(slope if kind is CalibrationKind.LINEAR else None),
        linear_intercept_grams=(intercept if kind is CalibrationKind.LINEAR else None),
        validation_max_holdout_absolute_error_grams=accepted_holdout_max,
        allowed_mass_min_grams=float(config.allowed_mass_min_grams),
        allowed_mass_max_grams=float(config.allowed_mass_max_grams),
        _fit_validation_token=_VALIDATED_MODEL_TOKEN,
    )
    return CalibrationValidation(
        accepted=True,
        reason=f"{kind.value}_passed_independent_holdout",
        model=model,
        training_points=training_points,
        heldout_points=heldout_points,
        preselected_kind=kind,
        linear_training_residuals=linear_training,
        linear_training_cv_errors=linear_cv,
        piecewise_training_cv_errors=piecewise_cv,
        linear_heldout_errors=linear_heldout,
        piecewise_heldout_errors=piecewise_heldout,
        linear_heldout_trial_errors=linear_heldout_trials,
        piecewise_heldout_trial_errors=piecewise_heldout_trials,
        nonlinear_triggered=nonlinear,
        piecewise_cv_max_error_improvement_grams=improvement,
    )
