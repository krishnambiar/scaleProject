import math
import struct
import unittest
from dataclasses import FrozenInstanceError, replace
from typing import List, Optional
from unittest.mock import patch

from trackpad_scale.models import (
    PHASE2_REQUIRED_COPIED_FIELDS,
    CaptureStats,
    FrameMetadata,
    Phase2CaptureStats,
    RawTouch,
    RawTouchFrame,
)
from trackpad_scale.phase3_sensor import (
    PHASE3_MAX_TOUCHES,
    RawFrameSensor,
    RawFrameValidationError,
    raw_frame_from_transport,
)
from trackpad_scale.phase2_sensor import TouchDiagnosticSensor
from trackpad_scale.sensor import TrackpadSensor


def _binary32(value: float) -> tuple[float, int]:
    packed = struct.pack(">f", value)
    return struct.unpack(">f", packed)[0], struct.unpack(">I", packed)[0]


def _touch(path_index: int = 3, finger_id: int = 2) -> RawTouch:
    x, x_bits = _binary32(0.25)
    y, y_bits = _binary32(0.75)
    z_total, z_total_bits = _binary32(11.5)
    pressure, pressure_bits = _binary32(82.0)
    density, density_bits = _binary32(0.5)
    return RawTouch(
        copied_fields=PHASE2_REQUIRED_COPIED_FIELDS,
        path_index=path_index,
        state=4,
        finger_id=finger_id,
        hand_id=1,
        normalized_x=x,
        normalized_y=y,
        z_total=z_total,
        pressure_candidate=pressure,
        z_density=density,
        normalized_x_bits=x_bits,
        normalized_y_bits=y_bits,
        z_total_bits=z_total_bits,
        pressure_candidate_bits=pressure_bits,
        z_density_bits=density_bits,
    )


def _frame(
    touches: tuple[RawTouch, ...] = (_touch(),),
    *,
    raw_count: Optional[int] = None,
    copied_count: Optional[int] = None,
    decode_status: int = 0,
    profile_id: int = 1,
    device_timestamp: float = 2.5,
) -> RawTouchFrame:
    count = len(touches) if raw_count is None else raw_count
    copied = len(touches) if copied_count is None else copied_count
    return RawTouchFrame(
        metadata=FrameMetadata(
            sequence=7,
            raw_touch_count_register=count,
            raw_frame_register=41,
            device_timestamp=device_timestamp,
            host_monotonic_ns=123456,
        ),
        layout_profile_id=profile_id,
        decode_status=decode_status,
        copied_touch_count=copied,
        touches=touches,
    )


def _capture_stats() -> CaptureStats:
    return CaptureStats(1, 1, 0, 0, 0, 0, 0, 0)


def _transport_stats() -> Phase2CaptureStats:
    return Phase2CaptureStats(1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


class _FakeSource:
    def __init__(self, frames: Optional[List[RawTouchFrame]] = None) -> None:
        self.profile_id = 1
        self.frames = list(frames or [])
        self.running = False
        self.start_count = 0
        self.stop_count = 0
        self.close_count = 0
        self.read_timeouts: List[Optional[float]] = []
        self.read_error: Optional[Exception] = None

    def start(self) -> None:
        self.start_count += 1
        self.running = True

    def stop(self) -> None:
        self.stop_count += 1
        self.running = False

    def read_frame(self, timeout: Optional[float] = None) -> Optional[RawTouchFrame]:
        self.read_timeouts.append(timeout)
        if self.read_error is not None:
            raise self.read_error
        return self.frames.pop(0) if self.frames else None

    def capture_stats(self) -> CaptureStats:
        return _capture_stats()

    def phase2_stats(self) -> Phase2CaptureStats:
        return _transport_stats()

    def supports_force(self) -> Optional[bool]:
        return True

    def is_running(self) -> bool:
        return self.running

    def close(self) -> None:
        self.close_count += 1
        self.running = False


class _MutableNumber:
    def __init__(self, value: float) -> None:
        self.value = value

    def __float__(self) -> float:
        return float(self.value)

    def __int__(self) -> int:
        return int(self.value)


class _FakePollingNative:
    def __init__(self, phase2: bool) -> None:
        self.phase2 = phase2
        self.poll_count = 0
        self.running_checks = 0

    def poll(self) -> None:
        if not self.phase2:
            self.poll_count += 1
        return None

    def poll_touch_frame(self) -> None:
        self.poll_count += 1
        return None

    @property
    def is_running(self) -> bool:
        self.running_checks += 1
        return True


class RawFrameConversionTests(unittest.TestCase):
    def test_exact_values_bits_and_descriptive_codes_are_preserved(self) -> None:
        transport = _frame((_touch(path_index=9, finger_id=7),))

        result = raw_frame_from_transport(transport, expected_profile_id=1)

        self.assertEqual(result.sequence, 7)
        self.assertEqual(result.frame_number, 41)
        self.assertEqual(result.touch_count, 1)
        self.assertIsInstance(result.contacts, tuple)
        contact = result.contacts[0]
        self.assertEqual(contact.path_index, 9)
        self.assertEqual(contact.finger_code, 7)
        self.assertEqual(
            contact.pressure_candidate_raw,
            transport.touches[0].pressure_candidate,
        )
        self.assertEqual(
            contact.pressure_candidate_bits,
            transport.touches[0].pressure_candidate_bits,
        )
        with self.assertRaises(FrozenInstanceError):
            contact.path_index = 10  # type: ignore[misc]

    def test_output_owns_primitive_copies_of_number_like_input(self) -> None:
        mutable_path = _MutableNumber(3)
        mutable_x = _MutableNumber(0.25)
        mutable_sequence = _MutableNumber(7)
        transport = _frame(
            (replace(_touch(), path_index=mutable_path, normalized_x=mutable_x),)
        )
        transport = replace(
            transport,
            metadata=replace(transport.metadata, sequence=mutable_sequence),
        )

        result = raw_frame_from_transport(transport, expected_profile_id=1)
        mutable_path.value = 99
        mutable_x.value = 0.5
        mutable_sequence.value = 100

        self.assertIs(type(result.sequence), int)
        self.assertIs(type(result.contacts[0].path_index), int)
        self.assertIs(type(result.contacts[0].normalized_x), float)
        self.assertEqual(result.sequence, 7)
        self.assertEqual(result.contacts[0].path_index, 3)
        self.assertEqual(result.contacts[0].normalized_x, 0.25)

    def test_zero_and_multiple_contacts_remain_valid_and_ordered(self) -> None:
        empty = raw_frame_from_transport(_frame(()), expected_profile_id=1)
        multiple = raw_frame_from_transport(
            _frame((_touch(path_index=8), _touch(path_index=2))),
            expected_profile_id=1,
        )

        self.assertEqual(empty.touch_count, 0)
        self.assertEqual([contact.path_index for contact in multiple.contacts], [8, 2])

    def test_classification_change_does_not_change_path_identity(self) -> None:
        first = raw_frame_from_transport(
            _frame((_touch(path_index=4, finger_id=2),)), expected_profile_id=1
        )
        second = raw_frame_from_transport(
            _frame((_touch(path_index=4, finger_id=7),)), expected_profile_id=1
        )

        self.assertEqual(first.contacts[0].path_index, second.contacts[0].path_index)
        self.assertNotEqual(first.contacts[0].finger_code, second.contacts[0].finger_code)

    def assert_rejected(self, frame: RawTouchFrame, reason: str) -> None:
        with self.assertRaises(RawFrameValidationError) as raised:
            raw_frame_from_transport(frame, expected_profile_id=1)
        self.assertEqual(raised.exception.reason, reason)

    def test_decode_findings_and_unknown_bits_fail_closed(self) -> None:
        for status in (1, 1 << 31):
            with self.subTest(status=status):
                self.assert_rejected(_frame(decode_status=status), "decode_status")

    def test_profile_and_count_inconsistencies_fail_closed(self) -> None:
        self.assert_rejected(_frame(profile_id=2), "profile_mismatch")
        self.assert_rejected(
            _frame(raw_count=PHASE3_MAX_TOUCHES + 1),
            "touch_count_out_of_range",
        )
        self.assert_rejected(_frame(raw_count=2), "touch_count_mismatch")
        self.assert_rejected(_frame(copied_count=2), "touch_count_mismatch")

    def test_copied_field_masks_must_match_exactly(self) -> None:
        base = _touch()
        self.assert_rejected(
            _frame((replace(base, copied_fields=base.copied_fields & ~1),)),
            "copied_fields_mismatch",
        )
        self.assert_rejected(
            _frame((replace(base, copied_fields=base.copied_fields | (1 << 20)),)),
            "copied_fields_mismatch",
        )

    def test_state_timestamp_and_scalar_guards_are_rechecked(self) -> None:
        self.assert_rejected(
            _frame((_touch(),), device_timestamp=math.nan),
            "nonfinite_timestamp",
        )
        self.assert_rejected(
            _frame((replace(_touch(), state=8),)),
            "invalid_state",
        )
        self.assert_rejected(
            _frame((replace(_touch(), normalized_x=math.inf),)),
            "nonfinite_scalar",
        )
        self.assert_rejected(
            _frame((replace(_touch(), normalized_x=1e39),)),
            "non_binary32_scalar",
        )

    def test_sentinel_and_bit_mismatch_fail_closed(self) -> None:
        sentinel, sentinel_bits = _binary32(43690.0)
        self.assert_rejected(
            _frame(
                (
                    replace(
                        _touch(),
                        pressure_candidate=sentinel,
                        pressure_candidate_bits=sentinel_bits,
                    ),
                )
            ),
            "pressure_sentinel",
        )
        self.assert_rejected(
            replace(
                _frame(),
                touches=(replace(_touch(), pressure_candidate_bits=0),),
            ),
            "binary32_bits_mismatch",
        )


class RawFrameSensorTests(unittest.TestCase):
    def test_lifecycle_capability_stats_and_timeout_are_forwarded(self) -> None:
        source = _FakeSource([_frame()])
        sensor = RawFrameSensor(source=source)

        sensor.start()
        result = sensor.read_frame(timeout=0.25)
        sensor.stop()

        self.assertIsNotNone(result)
        self.assertEqual(source.read_timeouts, [0.25])
        self.assertEqual((source.start_count, source.stop_count), (1, 1))
        self.assertTrue(sensor.supports_force())
        self.assertEqual(sensor.capture_stats(), _capture_stats())
        self.assertEqual(sensor.transport_stats(), _transport_stats())

    def test_empty_reads_and_source_errors_are_not_hidden(self) -> None:
        source = _FakeSource()
        sensor = RawFrameSensor(source=source)
        self.assertIsNone(sensor.read_frame(timeout=0))
        source.read_error = RuntimeError("native poll failed")
        with self.assertRaisesRegex(RuntimeError, "native poll failed"):
            sensor.read_frame(timeout=0)

    def test_nonfinite_and_negative_timeouts_are_rejected_before_poll(self) -> None:
        source = _FakeSource()
        sensor = RawFrameSensor(source=source)
        for timeout in (-1.0, math.inf, -math.inf, math.nan):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    sensor.read_frame(timeout=timeout)
        self.assertEqual(source.read_timeouts, [])

    def test_retained_frame_survives_source_close(self) -> None:
        source = _FakeSource([_frame()])
        sensor = RawFrameSensor(source=source)
        result = sensor.read_frame(timeout=0)
        sensor.close()
        source.frames[:] = [_frame((_touch(path_index=99),))]

        self.assertEqual(source.close_count, 1)
        self.assertIsNotNone(result)
        self.assertEqual(result.contacts[0].path_index, 3)  # type: ignore[union-attr]

    def test_phase1_and_diagnostic_readers_do_not_poll_after_deadline(self) -> None:
        phase1_native = _FakePollingNative(phase2=False)
        phase1 = TrackpadSensor.__new__(TrackpadSensor)
        phase1._native = phase1_native
        with patch("trackpad_scale.sensor.time.monotonic", side_effect=[0.0, 1.0]):
            self.assertIsNone(phase1.read_frame(timeout=1.0))
        self.assertEqual(phase1_native.poll_count, 1)

        phase2_native = _FakePollingNative(phase2=True)
        phase2 = TouchDiagnosticSensor.__new__(TouchDiagnosticSensor)
        phase2._native = phase2_native
        with patch(
            "trackpad_scale.phase2_sensor.time.monotonic",
            side_effect=[0.0, 1.0],
        ):
            self.assertIsNone(phase2.read_frame(timeout=1.0))
        self.assertEqual(phase2_native.poll_count, 1)


if __name__ == "__main__":
    unittest.main()
