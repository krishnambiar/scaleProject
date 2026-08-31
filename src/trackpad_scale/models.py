import math
from dataclasses import asdict, dataclass
from enum import IntEnum, IntFlag
from typing import Dict, Mapping, Tuple, Union


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


def _float_from_json(value: object) -> float:
    """Reverse the lossless non-finite encoding used by the evidence files."""

    if isinstance(value, bool):
        raise ValueError("boolean is not a valid floating-point value")
    if isinstance(value, (int, float)):
        return float(value)
    if value == "nan":
        return float("nan")
    if value == "inf":
        return float("inf")
    if value == "-inf":
        return float("-inf")
    raise ValueError(f"invalid JSON floating-point value: {value!r}")


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

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FrameMetadata":
        return cls(
            sequence=int(value["sequence"]),
            raw_touch_count_register=int(value["raw_touch_count_register"]),
            raw_frame_register=int(value["raw_frame_register"]),
            device_timestamp=_float_from_json(value["device_timestamp"]),
            host_monotonic_ns=int(value["host_monotonic_ns"]),
        )


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

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RawTouch":
        return cls(
            copied_fields=int(value["copied_fields"]),
            path_index=int(value["path_index"]),
            state=int(value["state"]),
            finger_id=int(value["finger_id"]),
            hand_id=int(value["hand_id"]),
            normalized_x=_float_from_json(value["normalized_x"]),
            normalized_y=_float_from_json(value["normalized_y"]),
            z_total=_float_from_json(value["z_total"]),
            pressure_candidate=_float_from_json(value["pressure_candidate"]),
            z_density=_float_from_json(value["z_density"]),
            normalized_x_bits=int(value["normalized_x_bits"]),
            normalized_y_bits=int(value["normalized_y_bits"]),
            z_total_bits=int(value["z_total_bits"]),
            pressure_candidate_bits=int(value["pressure_candidate_bits"]),
            z_density_bits=int(value["z_density_bits"]),
        )


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

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RawTouchFrame":
        metadata = value["metadata"]
        touches = value["touches"]
        if not isinstance(metadata, Mapping):
            raise ValueError("frame metadata must be an object")
        if not isinstance(touches, list):
            raise ValueError("frame touches must be an array")
        materialized = []
        for touch in touches:
            if not isinstance(touch, Mapping):
                raise ValueError("each frame touch must be an object")
            materialized.append(RawTouch.from_dict(touch))
        copied_touch_count = int(value["copied_touch_count"])
        if copied_touch_count != len(materialized):
            raise ValueError(
                "saved copied_touch_count does not match materialized touches"
            )
        return cls(
            metadata=FrameMetadata.from_dict(metadata),
            layout_profile_id=int(value["layout_profile_id"]),
            decode_status=int(value["decode_status"]),
            copied_touch_count=copied_touch_count,
            touches=tuple(materialized),
        )


@dataclass(frozen=True)
class RawContact:
    """Phase 3 application contact in uncalibrated sensor coordinates.

    ``pressure_candidate_raw`` has no claimed physical unit.  The finger and
    hand values are descriptive codes, not contact-continuity identifiers.
    """

    path_index: int
    state: int
    finger_code: int
    hand_code: int
    normalized_x: float
    normalized_y: float
    z_total_raw: float
    pressure_candidate_raw: float
    z_density_raw: float
    normalized_x_bits: int
    normalized_y_bits: int
    z_total_bits: int
    pressure_candidate_bits: int
    z_density_bits: int


@dataclass(frozen=True)
class RawFrame:
    """Private-ABI-free Phase 3 frame consumed by application code."""

    sequence: int
    frame_number: int
    device_timestamp: float
    host_monotonic_ns: int
    contacts: Tuple[RawContact, ...]

    @property
    def touch_count(self) -> int:
        return len(self.contacts)


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
