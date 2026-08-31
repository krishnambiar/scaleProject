import math
from dataclasses import asdict, dataclass
from enum import IntEnum, IntFlag
from typing import Dict, Tuple, Union


JsonFloat = Union[float, str]


class Phase2DecodeStatus(IntFlag):
    """Project-owned decoder findings; zero is the only clean decode."""

    OK = 0
    INVALID_COUNT = 1 << 0
    NULL_RECORDS = 1 << 1
    DEVICE_MISMATCH = 1 << 2
    RECORD_FRAME_MISMATCH = 1 << 3
    RECORD_TIMESTAMP_MISMATCH = 1 << 4
    NONFINITE_SCALAR = 1 << 5
    INVALID_STATE = 1 << 6
    PRESSURE_SENTINEL = 1 << 7


class Phase2TouchCopiedField(IntFlag):
    PATH_INDEX = 1 << 0
    STATE = 1 << 1
    FINGER_ID = 1 << 2
    HAND_ID = 1 << 3
    NORMALIZED_X = 1 << 4
    NORMALIZED_Y = 1 << 5
    Z_TOTAL = 1 << 6
    PRESSURE_CANDIDATE = 1 << 7
    Z_DENSITY = 1 << 8


PHASE2_REQUIRED_COPIED_FIELDS = int(
    Phase2TouchCopiedField.PATH_INDEX
    | Phase2TouchCopiedField.STATE
    | Phase2TouchCopiedField.FINGER_ID
    | Phase2TouchCopiedField.HAND_ID
    | Phase2TouchCopiedField.NORMALIZED_X
    | Phase2TouchCopiedField.NORMALIZED_Y
    | Phase2TouchCopiedField.Z_TOTAL
    | Phase2TouchCopiedField.PRESSURE_CANDIDATE
    | Phase2TouchCopiedField.Z_DENSITY
)


class TargetTouchState(IntEnum):
    """Lifecycle values found in this exact framework image's string table."""

    NOT_TRACKING = 0
    START_IN_RANGE = 1
    HOVER_IN_RANGE = 2
    MAKE_TOUCH = 3
    TOUCHING = 4
    BREAK_TOUCH = 5
    LINGER_IN_RANGE = 6
    OUT_OF_RANGE = 7


def phase2_decode_status_names(value: int) -> Tuple[str, ...]:
    if value == 0:
        return (Phase2DecodeStatus.OK.name,)
    known_mask = 0
    names = []
    for member in Phase2DecodeStatus:
        known_mask |= int(member)
        if value & int(member):
            names.append(member.name)
    unknown = value & ~known_mask
    if unknown:
        names.append(f"UNKNOWN_BITS_0x{unknown:08x}")
    return tuple(names)


def _json_float(value: float) -> JsonFloat:
    if math.isfinite(value):
        return value
    if math.isnan(value):
        return "nan"
    return "inf" if value > 0 else "-inf"


@dataclass(frozen=True)
class FrameMetadata:
    """Application-owned Phase 1 metadata; it contains no touch-record data."""

    sequence: int
    raw_touch_count_register: int
    raw_frame_register: int
    device_timestamp: float
    host_monotonic_ns: int

    def to_dict(self) -> Dict[str, object]:
        result = asdict(self)
        result["device_timestamp"] = _json_float(self.device_timestamp)
        return result


@dataclass(frozen=True)
class CaptureStats:
    callback_count: int
    enqueued_count: int
    queue_overwrite_count: int
    lock_contention_drop_count: int
    callback_device_mismatch_count: int
    late_callback_count: int
    in_flight_callback_count: int
    queue_depth: int

    @property
    def native_drop_count(self) -> int:
        return self.queue_overwrite_count + self.lock_contention_drop_count

    def to_dict(self) -> Dict[str, int]:
        result = asdict(self)
        result["native_drop_count"] = self.native_drop_count
        return result


@dataclass(frozen=True)
class RawTouch:
    """Copied Phase 2 scalars; copied_fields never implies semantic validity."""

    copied_fields: int
    path_index: int
    state: int
    finger_id: int
    hand_id: int
    normalized_x: float
    normalized_y: float
    z_total: float
    pressure_candidate: float
    z_density: float
    normalized_x_bits: int
    normalized_y_bits: int
    z_total_bits: int
    pressure_candidate_bits: int
    z_density_bits: int

    def to_dict(self) -> Dict[str, object]:
        result = asdict(self)
        for name in (
            "normalized_x",
            "normalized_y",
            "z_total",
            "pressure_candidate",
            "z_density",
        ):
            result[name] = _json_float(float(result[name]))
        result["pressure_units"] = "arbitrary raw sensor coordinate; not grams"
        result["z_total_units"] = "arbitrary raw sensor coordinate"
        result["z_density_units"] = "arbitrary raw sensor coordinate"
        try:
            result["state_label"] = TargetTouchState(self.state).name
        except ValueError:
            result["state_label"] = "UNRECOGNIZED"
        return result


@dataclass(frozen=True)
class RawTouchFrame:
    metadata: FrameMetadata
    layout_profile_id: int
    decode_status: int
    copied_touch_count: int
    touches: Tuple[RawTouch, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "metadata": self.metadata.to_dict(),
            "layout_profile_id": self.layout_profile_id,
            "decode_status": self.decode_status,
            "decode_status_names": list(
                phase2_decode_status_names(self.decode_status)
            ),
            "copied_touch_count": self.copied_touch_count,
            "touches": [touch.to_dict() for touch in self.touches],
        }


@dataclass(frozen=True)
class Phase2CaptureStats:
    attempted_frame_count: int
    copied_touch_count: int
    queue_overwrite_count: int
    lock_contention_drop_count: int
    invalid_count_frame_count: int
    null_records_frame_count: int
    device_mismatch_frame_count: int
    record_frame_mismatch_touch_count: int
    record_timestamp_mismatch_touch_count: int
    invalid_state_touch_count: int
    pressure_sentinel_touch_count: int
    nonfinite_touch_count: int
    queue_depth: int

    @property
    def native_drop_count(self) -> int:
        return self.queue_overwrite_count + self.lock_contention_drop_count

    def to_dict(self) -> Dict[str, int]:
        result = asdict(self)
        result["native_drop_count"] = self.native_drop_count
        return result
