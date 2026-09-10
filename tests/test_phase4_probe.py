import struct
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import List, Optional

from trackpad_scale.models import (
    CaptureStats,
    Phase2CaptureStats,
    RawContact,
    RawFrame,
)
from trackpad_scale.phase4_probe import (
    Phase4DiagnosticError,
    _write_json,
    build_parser,
    run_live_trial,
)
from trackpad_scale.phase4_profile import load_experimental_phase4_profile


def _bits(value: float) -> int:
    return struct.unpack(">I", struct.pack(">f", value))[0]


def _contact(pressure: float) -> RawContact:
    return RawContact(
        path_index=9,
        state=4,
        finger_code=2,
        hand_code=1,
        normalized_x=0.5,
        normalized_y=0.5,
        z_total_raw=1.0,
        pressure_candidate_raw=pressure,
        z_density_raw=1.0,
        normalized_x_bits=_bits(0.5),
        normalized_y_bits=_bits(0.5),
        z_total_bits=_bits(1.0),
        pressure_candidate_bits=_bits(pressure),
        z_density_bits=_bits(1.0),
    )


def _frame(sequence: int, pressure: float, contacts: int = 1) -> RawFrame:
    timestamp_ns = (sequence - 1) * 8_000_000
    materialized = (_contact(pressure),) if contacts == 1 else ()
    return RawFrame(
        sequence=sequence,
        frame_number=sequence + 100,
        device_timestamp=timestamp_ns / 1_000_000_000,
        host_monotonic_ns=timestamp_ns,
        contacts=materialized,
    )


def _capture_stats() -> CaptureStats:
    return CaptureStats(1, 1, 0, 0, 0, 0, 0, 0)


def _transport_stats() -> Phase2CaptureStats:
    return Phase2CaptureStats(1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


class _FakeSensor:
    def __init__(self, frames: List[RawFrame]) -> None:
        self.frames = list(frames)
        self.running = False
        self.start_count = 0
        self.stop_count = 0

    def start(self) -> None:
        self.start_count += 1
        self.running = True

    def stop(self) -> None:
        self.stop_count += 1
        self.running = False

    def read_frame(self, timeout: Optional[float] = None) -> Optional[RawFrame]:
        if timeout == 0.0:
            return None
        return self.frames.pop(0) if self.frames else None

    def supports_force(self) -> Optional[bool]:
        return True

    def is_running(self) -> bool:
        return self.running

    def capture_stats(self) -> CaptureStats:
        return _capture_stats()

    def transport_stats(self) -> Phase2CaptureStats:
        return _transport_stats()


def _successful_frames() -> List[RawFrame]:
    frames = []
    sequence = 1
    for _ in range(123):
        frames.append(_frame(sequence, 10.0))
        sequence += 1
    for _ in range(377):
        frames.append(_frame(sequence, 50.0))
        sequence += 1
    for _ in range(752):
        frames.append(_frame(sequence, 50.0))
        sequence += 1
    return frames


class Phase4LiveDiagnosticTests(unittest.TestCase):
    def test_live_trial_tares_restarts_and_publishes_only_raw_delta(self) -> None:
        sensor = _FakeSensor(_successful_frames())
        announcements = []

        report = run_live_trial(
            sensor,  # type: ignore[arg-type]
            load_experimental_phase4_profile(),
            transition_seconds=3.0,
            measurement_seconds=6.0,
            tare_timeout_seconds=1.0,
            prompt=lambda _: "",
            announce=announcements.append,
        )

        self.assertEqual((sensor.start_count, sensor.stop_count), (1, 1))
        self.assertEqual(report["outcome"], "stable_raw_pressure_observed")
        self.assertEqual(report["tare"]["baseline_pressure_raw"], 10.0)
        self.assertGreaterEqual(report["publication_count"], 1)
        self.assertEqual(report["publications"][0]["pressure_delta_raw"], 40.0)
        self.assertEqual(report["units"], "arbitrary raw sensor coordinate; not grams")
        self.assertFalse(report["calibration_performed"])
        self.assertFalse(report["grams_claimed"])
        self.assertEqual(len(announcements), 2)

    def test_contact_loss_fails_closed_and_stops_sensor(self) -> None:
        frames = [_frame(sequence, 10.0) for sequence in range(1, 124)]
        frames.append(_frame(124, 0.0, contacts=0))
        sensor = _FakeSensor(frames)

        with self.assertRaises(Phase4DiagnosticError) as raised:
            run_live_trial(
                sensor,  # type: ignore[arg-type]
                load_experimental_phase4_profile(),
                transition_seconds=3.0,
                measurement_seconds=6.0,
                tare_timeout_seconds=1.0,
                prompt=lambda _: "",
                announce=lambda _: None,
            )

        self.assertEqual(raised.exception.reason, "contact_unsupported")
        self.assertEqual(sensor.stop_count, 1)

    def test_cli_rejects_nonpositive_protocol_duration(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["--measurement-seconds", "0"])

    def test_evidence_writer_never_overwrites_a_trial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "trial.json"
            _write_json(output, {"phase": 4})
            before = output.read_bytes()
            with self.assertRaises(Phase4DiagnosticError) as raised:
                _write_json(output, {"phase": 5})
            self.assertEqual(raised.exception.reason, "output_exists")
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
