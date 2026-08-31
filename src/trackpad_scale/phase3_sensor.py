"""Safe Phase 3 boundary between diagnostic transport and application code."""

import math
import struct
from typing import Optional, Protocol, Tuple

from .models import (
    PHASE2_REQUIRED_COPIED_FIELDS,
    CaptureStats,
    Phase2CaptureStats,
    RawContact,
    RawFrame,
    RawTouch,
    RawTouchFrame,
    TargetTouchState,
)
from .phase2_sensor import TouchDiagnosticSensor
from .sensor import _validate_timeout


PHASE3_MAX_TOUCHES = 32
_PRESSURE_SENTINEL_BITS = struct.unpack(
    ">I", struct.pack(">f", 43690.0)
)[0]


class RawFrameValidationError(ValueError):
    """A transport frame could not safely cross the application boundary."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class _TransportSource(Protocol):
    @property
    def profile_id(self) -> int:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def read_frame(self, timeout: Optional[float] = None) -> Optional[RawTouchFrame]:
        ...

    def capture_stats(self) -> CaptureStats:
        ...

    def phase2_stats(self) -> Phase2CaptureStats:
        ...

    def supports_force(self) -> Optional[bool]:
        ...

    def is_running(self) -> bool:
        ...

    def close(self) -> None:
        ...


def _reject(reason: str, detail: str) -> None:
    raise RawFrameValidationError(reason, detail)


def _binary32_bits(value: float, field_name: str) -> int:
    if not math.isfinite(value):
        _reject("nonfinite_scalar", f"{field_name} is not finite")
    try:
        return struct.unpack(">I", struct.pack(">f", value))[0]
    except OverflowError:
        _reject("non_binary32_scalar", f"{field_name} cannot be encoded as binary32")
    raise AssertionError("unreachable")


def _validate_scalar_bits(
    touch: RawTouch, value_name: str, bits_name: str, contact_index: int
) -> Tuple[float, int]:
    value = float(getattr(touch, value_name))
    observed_bits = int(getattr(touch, bits_name))
    expected_bits = _binary32_bits(value, f"contact[{contact_index}].{value_name}")
    if observed_bits != expected_bits:
        _reject(
            "binary32_bits_mismatch",
            (
                f"contact[{contact_index}].{value_name} has bits "
                f"0x{observed_bits:08x}, expected 0x{expected_bits:08x}"
            ),
        )
    return value, observed_bits


def raw_frame_from_transport(
    frame: RawTouchFrame, *, expected_profile_id: int
) -> RawFrame:
    """Validate and copy one diagnostic record into the Phase 3 model.

    This is a transport integrity gate only.  It does not select contacts,
    subtract a baseline, filter, stabilize, calibrate, or assign physical units.
    """

    decode_status = int(frame.decode_status)
    if decode_status != 0:
        _reject(
            "decode_status",
            f"native decoder reported 0x{decode_status:08x}",
        )
    profile_id = int(frame.layout_profile_id)
    enabled_profile_id = int(expected_profile_id)
    if profile_id != enabled_profile_id:
        _reject(
            "profile_mismatch",
            (
                f"frame profile {profile_id} does not match "
                f"enabled profile {enabled_profile_id}"
            ),
        )

    raw_count = int(frame.metadata.raw_touch_count_register)
    copied_count = int(frame.copied_touch_count)
    transport_touches = tuple(frame.touches)
    if raw_count < 0 or raw_count > PHASE3_MAX_TOUCHES:
        _reject(
            "touch_count_out_of_range",
            f"callback count {raw_count} is outside 0..{PHASE3_MAX_TOUCHES}",
        )
    if copied_count != raw_count or len(transport_touches) != raw_count:
        _reject(
            "touch_count_mismatch",
            (
                f"callback={raw_count}, copied={copied_count}, "
                f"materialized={len(transport_touches)}"
            ),
        )
    device_timestamp = float(frame.metadata.device_timestamp)
    if not math.isfinite(device_timestamp):
        _reject("nonfinite_timestamp", "device timestamp is not finite")

    contacts = []
    for index, touch in enumerate(transport_touches):
        copied_fields = int(touch.copied_fields)
        if copied_fields != PHASE2_REQUIRED_COPIED_FIELDS:
            _reject(
                "copied_fields_mismatch",
                (
                    f"contact[{index}] mask 0x{copied_fields:08x} "
                    f"does not equal 0x{PHASE2_REQUIRED_COPIED_FIELDS:08x}"
                ),
            )
        state = int(touch.state)
        try:
            TargetTouchState(state)
        except ValueError:
            _reject(
                "invalid_state",
                f"contact[{index}] state {state} is outside the verified table",
            )

        normalized_x, normalized_x_bits = _validate_scalar_bits(
            touch, "normalized_x", "normalized_x_bits", index
        )
        normalized_y, normalized_y_bits = _validate_scalar_bits(
            touch, "normalized_y", "normalized_y_bits", index
        )
        z_total, z_total_bits = _validate_scalar_bits(
            touch, "z_total", "z_total_bits", index
        )
        pressure_candidate, pressure_candidate_bits = _validate_scalar_bits(
            touch, "pressure_candidate", "pressure_candidate_bits", index
        )
        z_density, z_density_bits = _validate_scalar_bits(
            touch, "z_density", "z_density_bits", index
        )
        if pressure_candidate_bits == _PRESSURE_SENTINEL_BITS:
            _reject(
                "pressure_sentinel",
                f"contact[{index}] contains the verified missing-value sentinel",
            )

        contacts.append(
            RawContact(
                path_index=int(touch.path_index),
                state=state,
                finger_code=int(touch.finger_id),
                hand_code=int(touch.hand_id),
                normalized_x=normalized_x,
                normalized_y=normalized_y,
                z_total_raw=z_total,
                pressure_candidate_raw=pressure_candidate,
                z_density_raw=z_density,
                normalized_x_bits=normalized_x_bits,
                normalized_y_bits=normalized_y_bits,
                z_total_bits=z_total_bits,
                pressure_candidate_bits=pressure_candidate_bits,
                z_density_bits=z_density_bits,
            )
        )

    return RawFrame(
        sequence=int(frame.metadata.sequence),
        frame_number=int(frame.metadata.raw_frame_register),
        device_timestamp=device_timestamp,
        host_monotonic_ns=int(frame.metadata.host_monotonic_ns),
        contacts=tuple(contacts),
    )


class RawFrameSensor:
    """Application-facing start/stop/read facade over the native transport.

    Production callers should use the default exact-target source. ``source``
    is a trusted dependency-injection seam for deterministic tests.
    """

    def __init__(self, *, source: Optional[_TransportSource] = None) -> None:
        self._source = source if source is not None else TouchDiagnosticSensor()

    def start(self) -> None:
        self._source.start()

    def stop(self) -> None:
        self._source.stop()

    def read_frame(self, timeout: Optional[float] = None) -> Optional[RawFrame]:
        _validate_timeout(timeout)
        frame = self._source.read_frame(timeout=timeout)
        if frame is None:
            return None
        return raw_frame_from_transport(
            frame, expected_profile_id=self._source.profile_id
        )

    def capture_stats(self) -> CaptureStats:
        return self._source.capture_stats()

    def transport_stats(self) -> Phase2CaptureStats:
        return self._source.phase2_stats()

    def supports_force(self) -> Optional[bool]:
        return self._source.supports_force()

    def is_running(self) -> bool:
        return self._source.is_running()

    def close(self) -> None:
        self._source.close()

    def __enter__(self) -> "RawFrameSensor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
