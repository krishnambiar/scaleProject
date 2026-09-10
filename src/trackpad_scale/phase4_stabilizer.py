"""Pure-Python Phase 4 tare, filtering, and pressure stabilization.

This module consumes only Phase 3 application models.  Every value remains an
arbitrary pressure-domain sensor coordinate; calibration and grams belong to a
later phase.
"""

import math
import statistics
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, List, Optional, Sequence, Tuple

from .models import RawContact, RawFrame, TargetTouchState


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class StabilizationStatus(str, Enum):
    TARE_REQUIRED = "tare_required"
    TARE_EXPIRED = "tare_expired"
    CONTACT_UNSUPPORTED = "contact_unsupported"
    POSITION_CHANGED = "position_changed"
    STREAM_DISCONTINUITY = "stream_discontinuity"
    TRANSIENT_REJECTED = "transient_rejected"
    FILTER_WARMUP = "filter_warmup"
    MONITORING = "monitoring"
    UNSTABLE = "unstable"
    STABLE_PENDING = "stable_pending"
    STABLE_PUBLISHED = "stable_published"


class TareValidationError(ValueError):
    """The explicit reference-contact window cannot establish a safe tare."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class StabilizerConfig:
    """Experiment-selected Phase 4 parameters, with no production defaults."""

    tare_window_samples: int
    tare_min_duration_seconds: float
    tare_max_mad_raw: float
    tare_max_absolute_deviation_raw: float
    tare_max_abs_slope_raw_per_second: float
    median_window_samples: int
    smoothing_window_samples: int
    outlier_window_samples: int
    outlier_mad_multiplier: float
    outlier_floor_raw: float
    stability_window_seconds: float
    stability_min_samples: int
    stability_max_mad_raw: float
    stability_max_abs_slope_raw_per_second: float
    stable_window_interval_seconds: float
    stable_windows_required: int
    stable_window_agreement_raw: float
    max_sample_gap_seconds: float
    maximum_tare_age_seconds: float
    max_position_deviation_normalized: Optional[float]

    def __post_init__(self) -> None:
        integer_minima = {
            "tare_window_samples": (self.tare_window_samples, 3),
            "median_window_samples": (self.median_window_samples, 3),
            "smoothing_window_samples": (self.smoothing_window_samples, 2),
            "outlier_window_samples": (self.outlier_window_samples, 3),
            "stability_min_samples": (self.stability_min_samples, 3),
            "stable_windows_required": (self.stable_windows_required, 2),
        }
        for name, (value, minimum) in integer_minima.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name in ("median_window_samples", "outlier_window_samples"):
            if getattr(self, name) % 2 == 0:
                raise ValueError(f"{name} must be odd")

        positive = {
            "tare_min_duration_seconds": self.tare_min_duration_seconds,
            "outlier_mad_multiplier": self.outlier_mad_multiplier,
            "stability_window_seconds": self.stability_window_seconds,
            "stable_window_interval_seconds": self.stable_window_interval_seconds,
            "max_sample_gap_seconds": self.max_sample_gap_seconds,
            "maximum_tare_age_seconds": self.maximum_tare_age_seconds,
        }
        for name, value in positive.items():
            if not _is_finite_number(value) or value <= 0:
                raise ValueError(f"{name} must be finite and > 0")

        nanosecond_durations = {
            "tare_min_duration_seconds": self.tare_min_duration_seconds,
            "stability_window_seconds": self.stability_window_seconds,
            "stable_window_interval_seconds": self.stable_window_interval_seconds,
            "max_sample_gap_seconds": self.max_sample_gap_seconds,
            "maximum_tare_age_seconds": self.maximum_tare_age_seconds,
        }
        duration_ns = {}
        for name, value in nanosecond_durations.items():
            scaled = value * 1_000_000_000
            if not _is_finite_number(scaled):
                raise ValueError(
                    f"{name} must convert to a positive signed 64-bit nanosecond value"
                )
            converted = int(scaled)
            if converted <= 0 or converted > (1 << 63) - 1:
                raise ValueError(
                    f"{name} must convert to a positive signed 64-bit nanosecond value"
                )
            duration_ns[name] = converted

        nonnegative = {
            "tare_max_mad_raw": self.tare_max_mad_raw,
            "tare_max_absolute_deviation_raw": (self.tare_max_absolute_deviation_raw),
            "tare_max_abs_slope_raw_per_second": (
                self.tare_max_abs_slope_raw_per_second
            ),
            "outlier_floor_raw": self.outlier_floor_raw,
            "stability_max_mad_raw": self.stability_max_mad_raw,
            "stability_max_abs_slope_raw_per_second": (
                self.stability_max_abs_slope_raw_per_second
            ),
            "stable_window_agreement_raw": self.stable_window_agreement_raw,
        }
        for name, value in nonnegative.items():
            if not _is_finite_number(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")

        position_limit = self.max_position_deviation_normalized
        if position_limit is not None and (
            not _is_finite_number(position_limit) or position_limit < 0
        ):
            raise ValueError(
                "max_position_deviation_normalized must be finite and >= 0, " "or None"
            )
        if self.stable_window_interval_seconds < self.stability_window_seconds:
            raise ValueError(
                "stable_window_interval_seconds must be >= "
                "stability_window_seconds so confirmation windows are time-separated"
            )
        required_tare_duration_ns = math.ceil(
            self.tare_min_duration_seconds * 1_000_000_000
        )
        maximum_tare_window_duration_ns = (self.tare_window_samples - 1) * duration_ns[
            "max_sample_gap_seconds"
        ]
        if required_tare_duration_ns > maximum_tare_window_duration_ns:
            raise ValueError(
                "tare_min_duration_seconds cannot exceed the longest continuous "
                "configured tare window"
            )
        stability_window_ns = duration_ns["stability_window_seconds"]
        if stability_window_ns < self.stability_min_samples - 1:
            raise ValueError(
                "stability_window_seconds cannot contain stability_min_samples "
                "strictly increasing nanosecond timestamps"
            )
        minimum_filter_warmup_ns = (
            self.median_window_samples + self.smoothing_window_samples - 1
        )
        minimum_confirmation_ns = (
            minimum_filter_warmup_ns
            + stability_window_ns
            + (
                (self.stable_windows_required - 1)
                * duration_ns["stable_window_interval_seconds"]
            )
        )
        if duration_ns["maximum_tare_age_seconds"] < minimum_confirmation_ns:
            raise ValueError(
                "maximum_tare_age_seconds is too short for the required stable "
                "filter warmup and confirmation windows"
            )


@dataclass(frozen=True)
class TareResult:
    baseline_pressure_raw: float
    sample_count: int
    duration_seconds: float
    dispersion_mad_raw: float
    maximum_absolute_deviation_raw: float
    slope_raw_per_second: float
    path_index: int
    median_x: float
    median_y: float
    maximum_position_deviation_normalized: float
    first_sequence: int
    last_sequence: int
    started_host_monotonic_ns: int
    completed_host_monotonic_ns: int


@dataclass(frozen=True)
class StabilityConfidence:
    """Transparent threshold evidence, not a probability or accuracy claim."""

    agreeing_window_count: int
    required_window_count: int
    agreement_span_raw: float
    agreement_limit_raw: float
    maximum_window_mad_raw: float
    mad_limit_raw: float
    maximum_absolute_slope_raw_per_second: float
    slope_limit_raw_per_second: float


@dataclass(frozen=True)
class StablePressureMeasurement:
    pressure_delta_raw: float
    dispersion_mad_raw: float
    slope_raw_per_second: float
    duration_seconds: float
    sequence: int
    host_monotonic_ns: int
    confidence: StabilityConfidence


@dataclass(frozen=True)
class StabilizationUpdate:
    status: StabilizationStatus
    reason: str
    sequence: int
    baseline_pressure_raw: Optional[float]
    pressure_raw: Optional[float] = None
    pressure_delta_raw: Optional[float] = None
    median_filtered_delta_raw: Optional[float] = None
    smoothed_delta_raw: Optional[float] = None
    dispersion_mad_raw: Optional[float] = None
    slope_raw_per_second: Optional[float] = None
    window_duration_seconds: Optional[float] = None
    agreeing_window_count: int = 0
    published_measurement: Optional[StablePressureMeasurement] = None


@dataclass(frozen=True)
class _StableWindow:
    center_raw: float
    dispersion_mad_raw: float
    slope_raw_per_second: float
    started_ns: int
    ended_ns: int
    sample_count: int


def _mad(values: Sequence[float]) -> float:
    center = float(statistics.median(values))
    return float(statistics.median(abs(value - center) for value in values))


def _slope_raw_per_second(
    timestamps_ns: Sequence[int], values: Sequence[float]
) -> float:
    if len(timestamps_ns) != len(values) or len(values) < 2:
        raise ValueError("slope requires at least two paired samples")
    origin = timestamps_ns[0]
    seconds = [(timestamp - origin) / 1_000_000_000 for timestamp in timestamps_ns]
    mean_time = statistics.fmean(seconds)
    mean_value = statistics.fmean(values)
    denominator = sum((timestamp - mean_time) ** 2 for timestamp in seconds)
    if denominator == 0:
        return 0.0
    numerator = sum(
        (timestamp - mean_time) * (value - mean_value)
        for timestamp, value in zip(seconds, values)
    )
    return float(numerator / denominator)


def _single_touching_contact(frame: RawFrame) -> Tuple[Optional[RawContact], str]:
    if len(frame.contacts) != 1:
        return None, f"expected one contact, observed {len(frame.contacts)}"
    contact = frame.contacts[0]
    if int(contact.state) != int(TargetTouchState.TOUCHING):
        return None, f"contact state {contact.state} is not TOUCHING"
    scalar_values = (
        float(contact.normalized_x),
        float(contact.normalized_y),
        float(contact.pressure_candidate_raw),
    )
    if not all(math.isfinite(value) for value in scalar_values):
        return None, "contact position or pressure candidate is non-finite"
    return contact, ""


class PressureStabilizer:
    """Phase 4 engine for one continuous, explicitly tared contact path."""

    def __init__(self, config: StabilizerConfig) -> None:
        self.config = config
        self._tare: Optional[TareResult] = None
        self._last_update: Optional[StabilizationUpdate] = None
        self._last_sequence: Optional[int] = None
        self._last_timestamp_ns: Optional[int] = None
        self._outlier_history: Deque[float] = deque(
            maxlen=config.outlier_window_samples
        )
        self._median_history: Deque[float] = deque(maxlen=config.median_window_samples)
        self._smoothing_history: Deque[float] = deque(
            maxlen=config.smoothing_window_samples
        )
        self._stability_history: Deque[Tuple[int, float]] = deque()
        self._stable_windows: List[_StableWindow] = []
        self._last_evaluation_ns: Optional[int] = None
        self._published_center_raw: Optional[float] = None

    @property
    def tare_result(self) -> Optional[TareResult]:
        return self._tare

    @property
    def last_update(self) -> Optional[StabilizationUpdate]:
        return self._last_update

    def reset(self) -> None:
        self._tare = None
        self._last_sequence = None
        self._last_timestamp_ns = None
        self._last_update = None
        self._clear_filter_state()

    def restart_stability_search(self) -> None:
        """Clear measurement history without changing the frozen tare.

        The next frame must still be continuous with the last observed frame.
        This lets a diagnostic delimit a new measurement interval after it has
        processed (rather than hidden) an operator transition.
        """

        if self._tare is None:
            raise RuntimeError("cannot restart stability search before tare")
        self._clear_filter_state()
        self._last_update = None

    def _clear_filter_state(self) -> None:
        self._outlier_history.clear()
        self._median_history.clear()
        self._smoothing_history.clear()
        self._stability_history.clear()
        self._stable_windows.clear()
        self._last_evaluation_ns = None
        self._published_center_raw = None

    def _invalidate_tare(self) -> None:
        self._tare = None
        self._last_sequence = None
        self._last_timestamp_ns = None
        self._clear_filter_state()

    def tare(self, frames: Sequence[RawFrame]) -> TareResult:
        """Freeze the median of an explicit finite reference-contact window."""

        self.reset()
        try:
            frame_count = len(frames)
        except TypeError as error:
            raise TypeError("tare frames must be a finite Sequence") from error
        if frame_count < self.config.tare_window_samples:
            raise TareValidationError(
                "insufficient_samples",
                (f"need {self.config.tare_window_samples}, " f"received {frame_count}"),
            )
        try:
            window = tuple(
                frames[index]
                for index in range(
                    frame_count - self.config.tare_window_samples,
                    frame_count,
                )
            )
        except (IndexError, KeyError, TypeError) as error:
            raise TypeError(
                "tare frames must support finite Sequence indexing"
            ) from error

        contacts: List[RawContact] = []
        sequences: List[int] = []
        timestamps: List[int] = []
        for index, frame in enumerate(window):
            contact, reason = _single_touching_contact(frame)
            if contact is None:
                raise TareValidationError(
                    "unsupported_contact", f"frame {index}: {reason}"
                )
            sequence = int(frame.sequence)
            timestamp = int(frame.host_monotonic_ns)
            if sequence <= 0 or timestamp < 0:
                raise TareValidationError(
                    "invalid_metadata",
                    f"frame {index} has non-positive sequence or negative timestamp",
                )
            if sequences and sequence != sequences[-1] + 1:
                raise TareValidationError(
                    "sequence_discontinuity",
                    f"frame {index} sequence {sequence} did not follow {sequences[-1]}",
                )
            if timestamps:
                gap_seconds = (timestamp - timestamps[-1]) / 1_000_000_000
                if gap_seconds <= 0 or gap_seconds > self.config.max_sample_gap_seconds:
                    raise TareValidationError(
                        "timestamp_discontinuity",
                        f"frame {index} gap was {gap_seconds:.9g} seconds",
                    )
            contacts.append(contact)
            sequences.append(sequence)
            timestamps.append(timestamp)

        path_index = int(contacts[0].path_index)
        if any(int(contact.path_index) != path_index for contact in contacts[1:]):
            raise TareValidationError(
                "path_changed", "reference-contact path changed inside the tare window"
            )

        pressures = [float(contact.pressure_candidate_raw) for contact in contacts]
        xs = [float(contact.normalized_x) for contact in contacts]
        ys = [float(contact.normalized_y) for contact in contacts]
        duration = (timestamps[-1] - timestamps[0]) / 1_000_000_000
        if duration < self.config.tare_min_duration_seconds:
            raise TareValidationError(
                "insufficient_duration",
                (
                    f"tare duration {duration:.9g}s is below "
                    f"{self.config.tare_min_duration_seconds:.9g}s"
                ),
            )

        baseline = float(statistics.median(pressures))
        dispersion = _mad(pressures)
        maximum_absolute_deviation = max(
            abs(pressure - baseline) for pressure in pressures
        )
        slope = _slope_raw_per_second(timestamps, pressures)
        if dispersion > self.config.tare_max_mad_raw:
            raise TareValidationError(
                "tare_dispersion",
                (
                    f"MAD {dispersion:.9g} exceeds "
                    f"{self.config.tare_max_mad_raw:.9g} raw units"
                ),
            )
        if maximum_absolute_deviation > (self.config.tare_max_absolute_deviation_raw):
            raise TareValidationError(
                "tare_transient",
                (
                    f"maximum absolute deviation {maximum_absolute_deviation:.9g} "
                    f"exceeds {self.config.tare_max_absolute_deviation_raw:.9g} "
                    "raw units"
                ),
            )
        if abs(slope) > self.config.tare_max_abs_slope_raw_per_second:
            raise TareValidationError(
                "tare_slope",
                (
                    f"|slope| {abs(slope):.9g} exceeds "
                    f"{self.config.tare_max_abs_slope_raw_per_second:.9g} raw/s"
                ),
            )

        median_x = float(statistics.median(xs))
        median_y = float(statistics.median(ys))
        maximum_position_deviation = max(
            math.hypot(x - median_x, y - median_y) for x, y in zip(xs, ys)
        )
        position_limit = self.config.max_position_deviation_normalized
        if position_limit is not None and maximum_position_deviation > position_limit:
            raise TareValidationError(
                "tare_position_changed",
                (
                    f"maximum normalized position deviation "
                    f"{maximum_position_deviation:.9g} exceeds {position_limit:.9g}"
                ),
            )

        result = TareResult(
            baseline_pressure_raw=baseline,
            sample_count=len(window),
            duration_seconds=duration,
            dispersion_mad_raw=dispersion,
            maximum_absolute_deviation_raw=maximum_absolute_deviation,
            slope_raw_per_second=slope,
            path_index=path_index,
            median_x=median_x,
            median_y=median_y,
            maximum_position_deviation_normalized=maximum_position_deviation,
            first_sequence=sequences[0],
            last_sequence=sequences[-1],
            started_host_monotonic_ns=timestamps[0],
            completed_host_monotonic_ns=timestamps[-1],
        )
        self._tare = result
        self._last_sequence = sequences[-1]
        self._last_timestamp_ns = timestamps[-1]
        self._clear_filter_state()
        return result

    def _update(
        self,
        status: StabilizationStatus,
        reason: str,
        sequence: int,
        **values: object,
    ) -> StabilizationUpdate:
        baseline = self._tare.baseline_pressure_raw if self._tare is not None else None
        update = StabilizationUpdate(
            status=status,
            reason=reason,
            sequence=sequence,
            baseline_pressure_raw=baseline,
            **values,
        )
        self._last_update = update
        return update

    def process(self, frame: RawFrame) -> StabilizationUpdate:
        """Process one frame and return an auditable Phase 4 decision."""

        sequence = int(frame.sequence)
        if self._tare is None:
            return self._update(
                StabilizationStatus.TARE_REQUIRED,
                "an explicit reference-contact tare is required",
                sequence,
            )

        contact, contact_reason = _single_touching_contact(frame)
        if contact is None:
            update = self._update(
                StabilizationStatus.CONTACT_UNSUPPORTED,
                contact_reason + "; explicit retare required",
                sequence,
            )
            self._invalidate_tare()
            return update

        timestamp = int(frame.host_monotonic_ns)
        tare_age_seconds = (
            timestamp - self._tare.completed_host_monotonic_ns
        ) / 1_000_000_000
        if tare_age_seconds > self.config.maximum_tare_age_seconds:
            update = self._update(
                StabilizationStatus.TARE_EXPIRED,
                "frozen tare exceeded its configured experimental lifetime",
                sequence,
            )
            self._invalidate_tare()
            return update

        assert self._last_sequence is not None
        assert self._last_timestamp_ns is not None
        gap_seconds = (timestamp - self._last_timestamp_ns) / 1_000_000_000
        if (
            sequence != self._last_sequence + 1
            or gap_seconds <= 0
            or (gap_seconds > self.config.max_sample_gap_seconds)
        ):
            previous_sequence = self._last_sequence
            update = self._update(
                StabilizationStatus.STREAM_DISCONTINUITY,
                (
                    f"sequence {sequence} after {previous_sequence}, "
                    f"timestamp gap {gap_seconds:.9g}s; explicit retare required"
                ),
                sequence,
            )
            self._invalidate_tare()
            return update

        if int(contact.path_index) != self._tare.path_index:
            expected_path = self._tare.path_index
            update = self._update(
                StabilizationStatus.STREAM_DISCONTINUITY,
                (
                    f"path changed from {expected_path} to {int(contact.path_index)}; "
                    "explicit retare required"
                ),
                sequence,
            )
            self._invalidate_tare()
            return update

        position_limit = self.config.max_position_deviation_normalized
        position_deviation = math.hypot(
            float(contact.normalized_x) - self._tare.median_x,
            float(contact.normalized_y) - self._tare.median_y,
        )
        if position_limit is not None and position_deviation > position_limit:
            update = self._update(
                StabilizationStatus.POSITION_CHANGED,
                (
                    f"normalized position deviation {position_deviation:.9g} "
                    f"exceeded {position_limit:.9g}; explicit retare required"
                ),
                sequence,
            )
            self._invalidate_tare()
            return update

        self._last_sequence = sequence
        self._last_timestamp_ns = timestamp
        pressure_raw = float(contact.pressure_candidate_raw)
        pressure_delta = pressure_raw - self._tare.baseline_pressure_raw

        if len(self._outlier_history) == self.config.outlier_window_samples:
            outlier_center = float(statistics.median(self._outlier_history))
            outlier_mad = _mad(tuple(self._outlier_history))
            outlier_limit = max(
                self.config.outlier_floor_raw,
                self.config.outlier_mad_multiplier * outlier_mad,
            )
            if abs(pressure_delta - outlier_center) > outlier_limit:
                self._clear_filter_state()
                return self._update(
                    StabilizationStatus.TRANSIENT_REJECTED,
                    (
                        f"raw delta departed recent median by "
                        f"{abs(pressure_delta - outlier_center):.9g}, above "
                        f"{outlier_limit:.9g}; filter run reset"
                    ),
                    sequence,
                    pressure_raw=pressure_raw,
                    pressure_delta_raw=pressure_delta,
                )

        self._outlier_history.append(pressure_delta)
        self._median_history.append(pressure_delta)
        if len(self._median_history) < self.config.median_window_samples:
            return self._update(
                StabilizationStatus.FILTER_WARMUP,
                "median filter window is not full",
                sequence,
                pressure_raw=pressure_raw,
                pressure_delta_raw=pressure_delta,
            )

        median_filtered = float(statistics.median(self._median_history))
        self._smoothing_history.append(median_filtered)
        if len(self._smoothing_history) < self.config.smoothing_window_samples:
            return self._update(
                StabilizationStatus.FILTER_WARMUP,
                "moving-average smoothing window is not full",
                sequence,
                pressure_raw=pressure_raw,
                pressure_delta_raw=pressure_delta,
                median_filtered_delta_raw=median_filtered,
            )

        smoothed = float(statistics.fmean(self._smoothing_history))
        self._stability_history.append((timestamp, smoothed))
        cutoff = timestamp - int(self.config.stability_window_seconds * 1_000_000_000)
        # Retain at most one sample at/before the cutoff so metric coverage
        # spans the full lookback.  That bracketing sample is deliberately not
        # counted toward the configured in-window sample density below.
        while (
            len(self._stability_history) >= 2
            and self._stability_history[1][0] <= cutoff
        ):
            self._stability_history.popleft()

        window_duration = (
            (timestamp - self._stability_history[0][0]) / 1_000_000_000
            if self._stability_history
            else 0.0
        )
        in_window_sample_count = sum(
            sample_timestamp >= cutoff
            for sample_timestamp, _ in self._stability_history
        )
        has_full_window_coverage = self._stability_history[0][0] <= cutoff
        if (
            in_window_sample_count < self.config.stability_min_samples
            or not has_full_window_coverage
        ):
            return self._update(
                StabilizationStatus.FILTER_WARMUP,
                "rolling stability window lacks duration or sample density",
                sequence,
                pressure_raw=pressure_raw,
                pressure_delta_raw=pressure_delta,
                median_filtered_delta_raw=median_filtered,
                smoothed_delta_raw=smoothed,
                window_duration_seconds=window_duration,
            )

        timestamps = [sample[0] for sample in self._stability_history]
        values = [sample[1] for sample in self._stability_history]
        dispersion = _mad(values)
        slope = _slope_raw_per_second(timestamps, values)
        common = {
            "pressure_raw": pressure_raw,
            "pressure_delta_raw": pressure_delta,
            "median_filtered_delta_raw": median_filtered,
            "smoothed_delta_raw": smoothed,
            "dispersion_mad_raw": dispersion,
            "slope_raw_per_second": slope,
            "window_duration_seconds": window_duration,
        }
        if dispersion > self.config.stability_max_mad_raw or abs(slope) > (
            self.config.stability_max_abs_slope_raw_per_second
        ):
            self._stable_windows.clear()
            self._published_center_raw = None
            self._last_evaluation_ns = timestamp
            return self._update(
                StabilizationStatus.UNSTABLE,
                "rolling dispersion or slope exceeds the configured limit",
                sequence,
                **common,
            )

        interval_ns = int(self.config.stable_window_interval_seconds * 1_000_000_000)
        if self._last_evaluation_ns is not None and (
            timestamp - self._last_evaluation_ns < interval_ns
        ):
            return self._update(
                StabilizationStatus.MONITORING,
                "window is stable; waiting for the next time-separated confirmation",
                sequence,
                agreeing_window_count=len(self._stable_windows),
                **common,
            )

        self._last_evaluation_ns = timestamp
        stable_window = _StableWindow(
            center_raw=float(statistics.median(values)),
            dispersion_mad_raw=dispersion,
            slope_raw_per_second=slope,
            started_ns=timestamps[0],
            ended_ns=timestamps[-1],
            sample_count=len(values),
        )
        if (
            self._published_center_raw is not None
            and abs(stable_window.center_raw - self._published_center_raw)
            > self.config.stable_window_agreement_raw
        ):
            previous_center = self._published_center_raw
            self._stable_windows = [stable_window]
            self._published_center_raw = None
            return self._update(
                StabilizationStatus.STABLE_PENDING,
                (
                    f"stable center moved from published {previous_center:.9g} to "
                    f"{stable_window.center_raw:.9g}; confirmation restarted"
                ),
                sequence,
                agreeing_window_count=1,
                **common,
            )
        candidate_windows = [*self._stable_windows, stable_window]
        centers = [window.center_raw for window in candidate_windows]
        if max(centers) - min(centers) > self.config.stable_window_agreement_raw:
            self._stable_windows = [stable_window]
            self._published_center_raw = None
            return self._update(
                StabilizationStatus.STABLE_PENDING,
                "stable window disagreed with the prior streak; confirmation restarted",
                sequence,
                agreeing_window_count=1,
                **common,
            )

        self._stable_windows = candidate_windows[-self.config.stable_windows_required :]
        if len(self._stable_windows) < self.config.stable_windows_required:
            return self._update(
                StabilizationStatus.STABLE_PENDING,
                "stable window accepted; more time-separated confirmations required",
                sequence,
                agreeing_window_count=len(self._stable_windows),
                **common,
            )
        if self._published_center_raw is not None:
            return self._update(
                StabilizationStatus.MONITORING,
                "stable pressure was already published for this uninterrupted run",
                sequence,
                agreeing_window_count=len(self._stable_windows),
                **common,
            )

        centers = [window.center_raw for window in self._stable_windows]
        maximum_mad = max(window.dispersion_mad_raw for window in self._stable_windows)
        maximum_slope_window = max(
            self._stable_windows,
            key=lambda window: abs(window.slope_raw_per_second),
        )
        first_started_ns = self._stable_windows[0].started_ns
        confidence = StabilityConfidence(
            agreeing_window_count=len(self._stable_windows),
            required_window_count=self.config.stable_windows_required,
            agreement_span_raw=max(centers) - min(centers),
            agreement_limit_raw=self.config.stable_window_agreement_raw,
            maximum_window_mad_raw=maximum_mad,
            mad_limit_raw=self.config.stability_max_mad_raw,
            maximum_absolute_slope_raw_per_second=abs(
                maximum_slope_window.slope_raw_per_second
            ),
            slope_limit_raw_per_second=(
                self.config.stability_max_abs_slope_raw_per_second
            ),
        )
        measurement = StablePressureMeasurement(
            pressure_delta_raw=float(statistics.median(centers)),
            dispersion_mad_raw=maximum_mad,
            slope_raw_per_second=maximum_slope_window.slope_raw_per_second,
            duration_seconds=(timestamp - first_started_ns) / 1_000_000_000,
            sequence=sequence,
            host_monotonic_ns=timestamp,
            confidence=confidence,
        )
        self._published_center_raw = measurement.pressure_delta_raw
        return self._update(
            StabilizationStatus.STABLE_PUBLISHED,
            "required time-separated stable windows agree",
            sequence,
            agreeing_window_count=len(self._stable_windows),
            published_measurement=measurement,
            **common,
        )

    def ingest(self, frame: RawFrame) -> Optional[StablePressureMeasurement]:
        """Return a value only when Phase 4 stability has been validated."""

        return self.process(frame).published_measurement
