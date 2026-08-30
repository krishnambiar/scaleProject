from dataclasses import asdict, dataclass
from typing import Dict


@dataclass(frozen=True)
class FrameMetadata:
    """Application-owned Phase 1 metadata; it contains no touch-record data."""

    sequence: int
    raw_touch_count_register: int
    raw_frame_register: int
    device_timestamp: float
    host_monotonic_ns: int

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


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
