import hashlib
import io
import json
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from trackpad_scale.models import (
    PHASE2_REQUIRED_COPIED_FIELDS,
    FrameMetadata,
    RawTouch,
    RawTouchFrame,
)
from trackpad_scale.phase4_profile import (
    Phase2ArtifactEvidence,
    ReplayEvidence,
    load_experimental_phase4_profile,
)
from trackpad_scale.phase4_replay import (
    Phase4ReplayError,
    audit_phase4_replay,
    main,
)
from trackpad_scale.phase4_stabilizer import StabilizerConfig


def _bits(value: float) -> int:
    return struct.unpack(">I", struct.pack(">f", value))[0]


def _transport_frame(sequence: int, pressure: float) -> RawTouchFrame:
    timestamp = (sequence - 1) / 10
    touch = RawTouch(
        copied_fields=PHASE2_REQUIRED_COPIED_FIELDS,
        path_index=7,
        state=4,
        finger_id=2,
        hand_id=1,
        normalized_x=0.5,
        normalized_y=0.5,
        z_total=1.0,
        pressure_candidate=pressure,
        z_density=1.0,
        normalized_x_bits=_bits(0.5),
        normalized_y_bits=_bits(0.5),
        z_total_bits=_bits(1.0),
        pressure_candidate_bits=_bits(pressure),
        z_density_bits=_bits(1.0),
    )
    return RawTouchFrame(
        metadata=FrameMetadata(
            sequence=sequence,
            raw_touch_count_register=1,
            raw_frame_register=sequence + 100,
            device_timestamp=timestamp,
            host_monotonic_ns=int(timestamp * 1_000_000_000),
        ),
        layout_profile_id=1,
        decode_status=0,
        copied_touch_count=1,
        touches=(touch,),
    )


def _config() -> StabilizerConfig:
    return StabilizerConfig(
        tare_window_samples=5,
        tare_min_duration_seconds=0.4,
        tare_max_mad_raw=1.0,
        tare_max_absolute_deviation_raw=1.0,
        tare_max_abs_slope_raw_per_second=1.0,
        median_window_samples=3,
        smoothing_window_samples=2,
        outlier_window_samples=3,
        outlier_mad_multiplier=3.0,
        outlier_floor_raw=50.0,
        stability_window_seconds=0.2,
        stability_min_samples=3,
        stability_max_mad_raw=0.01,
        stability_max_abs_slope_raw_per_second=0.01,
        stable_window_interval_seconds=0.2,
        stable_windows_required=2,
        stable_window_agreement_raw=0.01,
        max_sample_gap_seconds=0.11,
        maximum_tare_age_seconds=20.0,
        max_position_deviation_normalized=0.05,
    )


def _raw_entry(sequence: int, pressure: float):
    stage = "rest" if sequence <= 7 else "light"
    return {
        "callback_window": {
            "cycle": 1,
            "stage": stage,
            "period": "settled",
        },
        "collection_tag": {
            "cycle": 1,
            "stage": stage,
            "period": "settled",
            "collected_utc": "2026-01-01T00:00:00+00:00",
        },
        "frame": _transport_frame(sequence, pressure).to_dict(),
    }


def _artifact_payload(target):
    return {
        "schema_version": 1,
        "phase": 2,
        "experiment": "raw pressure candidate ordinal response",
        "completed": True,
        "preflight_status": "accepted",
        "target_profile_match": True,
        "units": "raw sensor coordinates; not grams",
        "calibration_performed": False,
        "actual_target": dict(target),
        "expected_target": dict(target),
        "expected_phase2_source_layout": {"profile_id": 1},
        "validated_bridge_abi": {
            "profile_id": 1,
            "source_layout": {"profile_id": 1},
        },
        "raw_frames": [
            _raw_entry(
                sequence,
                99.0 if sequence <= 2 else (10.0 if sequence <= 7 else 20.0),
            )
            for sequence in range(1, 16)
        ],
    }


def _serialized(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


def _test_profile(payload_bytes: bytes, **replay_overrides):
    base = load_experimental_phase4_profile()
    replay_values = {
        "tare_cycle": 1,
        "tare_stage": "rest",
        "tare_period": "settled",
        "tare_trailing_sample_count": 5,
        "tare_first_sequence": 3,
        "tare_last_sequence": 7,
        "tare_first_frame_number": 103,
        "tare_last_frame_number": 107,
        "continuation_first_sequence": 8,
        "continuation_last_sequence": 15,
        "pressure_cycle": 1,
        "pressure_stage": "light",
        "pressure_period": "settled",
        "pressure_first_sequence": 8,
        "pressure_last_sequence": 15,
        "expected_tare_baseline_raw": 10.0,
        "expected_tare_mad_raw": 0.0,
        "expected_tare_max_absolute_deviation_raw": 0.0,
        "expected_tare_slope_raw_per_second": 0.0,
        "expected_publication_sequence": 15,
        "expected_publication_frame_number": 115,
        "expected_published_delta_raw": 10.0,
    }
    replay_values.update(replay_overrides)
    source_evidence = Phase2ArtifactEvidence(
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
        target=base.source_phase2_artifact.target,
        replay=ReplayEvidence(**replay_values),
    )
    return replace(base, source_phase2_artifact=source_evidence, config=_config())


class Phase4ReplayTests(unittest.TestCase):
    def _write_fixture(self, directory: str, payload) -> tuple[Path, bytes]:
        source = Path(directory) / "phase2.json"
        payload_bytes = _serialized(payload)
        source.write_bytes(payload_bytes)
        return source, payload_bytes

    def test_replays_all_frames_and_returns_a_json_serializable_audit(self) -> None:
        base = load_experimental_phase4_profile()
        payload = _artifact_payload(asdict(base.source_phase2_artifact.target))
        with tempfile.TemporaryDirectory() as directory:
            source, payload_bytes = self._write_fixture(directory, payload)
            profile = _test_profile(payload_bytes)
            with patch(
                "trackpad_scale.phase4_replay.load_experimental_phase4_profile",
                return_value=profile,
            ):
                report = audit_phase4_replay(source)

        json.dumps(report, allow_nan=False)
        self.assertEqual(report["outcome"], "profile_expectations_verified")
        self.assertEqual(report["units"], profile.units)
        self.assertFalse(report["calibration_performed"])
        self.assertEqual(
            report["profile"]["source_phase2_artifact"]["replay"][
                "expected_publication_sequence"
            ],
            15,
        )
        self.assertEqual(report["provenance"]["saved_frame_count"], 15)
        self.assertTrue(
            report["provenance"]["all_saved_frames_parsed_through_phase3_gate"]
        )
        self.assertEqual(report["selection"]["tare"]["matching_stage_frame_count"], 7)
        self.assertEqual(report["selection"]["tare"]["first_sequence"], 3)
        self.assertEqual(report["selection"]["continuation"]["frame_count"], 8)
        self.assertEqual(sum(report["status_counts"].values()), 8)
        self.assertEqual(len(report["publications"]), 1)
        self.assertEqual(report["publications"][0]["sequence"], 15)
        self.assertEqual(report["publications"][0]["pressure_delta_raw"], 10.0)
        self.assertTrue(report["publications"][0]["units"].endswith("not grams"))
        self.assertTrue(
            {"grams", "mass", "weight", "calibration_model"}.isdisjoint(
                set(_all_keys(report))
            )
        )

    def test_sha_target_and_unused_malformed_frame_fail_closed(self) -> None:
        base = load_experimental_phase4_profile()
        target = asdict(base.source_phase2_artifact.target)
        cases = []

        valid_payload = _artifact_payload(target)
        valid_bytes = _serialized(valid_payload)
        bad_sha_profile = _test_profile(valid_bytes)
        bad_sha_profile = replace(
            bad_sha_profile,
            source_phase2_artifact=replace(
                bad_sha_profile.source_phase2_artifact, sha256="0" * 64
            ),
        )
        cases.append((valid_payload, bad_sha_profile, "source_sha256_mismatch"))

        wrong_target = _artifact_payload(target)
        wrong_target["actual_target"]["hardware_model"] = "DifferentMac"
        wrong_target_bytes = _serialized(wrong_target)
        cases.append(
            (wrong_target, _test_profile(wrong_target_bytes), "target_mismatch")
        )

        malformed_unused = _artifact_payload(target)
        malformed_unused["raw_frames"].append(
            {
                "callback_window": {
                    "cycle": 2,
                    "stage": "rest",
                    "period": "settled",
                },
                "collection_tag": {},
                "frame": {},
            }
        )
        malformed_bytes = _serialized(malformed_unused)
        cases.append(
            (
                malformed_unused,
                _test_profile(malformed_bytes),
                "transport_frame_invalid",
            )
        )

        for payload, profile, reason in cases:
            with (
                self.subTest(reason=reason),
                tempfile.TemporaryDirectory() as directory,
            ):
                source, _ = self._write_fixture(directory, payload)
                with patch(
                    "trackpad_scale.phase4_replay.load_experimental_phase4_profile",
                    return_value=profile,
                ):
                    with self.assertRaises(Phase4ReplayError) as raised:
                        audit_phase4_replay(source)
                self.assertEqual(raised.exception.reason, reason)

    def test_profile_declared_expectations_are_enforced(self) -> None:
        base = load_experimental_phase4_profile()
        payload = _artifact_payload(asdict(base.source_phase2_artifact.target))
        with tempfile.TemporaryDirectory() as directory:
            source, payload_bytes = self._write_fixture(directory, payload)
            profile = _test_profile(payload_bytes, expected_publication_sequence=14)
            with patch(
                "trackpad_scale.phase4_replay.load_experimental_phase4_profile",
                return_value=profile,
            ):
                with self.assertRaises(Phase4ReplayError) as raised:
                    audit_phase4_replay(source)
        self.assertEqual(raised.exception.reason, "replay_expectation_mismatch")

    def test_cli_writes_new_sidecar_and_never_overwrites_source(self) -> None:
        base = load_experimental_phase4_profile()
        payload = _artifact_payload(asdict(base.source_phase2_artifact.target))
        with tempfile.TemporaryDirectory() as directory:
            source, payload_bytes = self._write_fixture(directory, payload)
            profile = _test_profile(payload_bytes)
            output = Path(directory) / "phase4-replay.json"
            with (
                patch(
                    "trackpad_scale.phase4_replay.load_experimental_phase4_profile",
                    return_value=profile,
                ),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                result = main(["--source", str(source), "--json-out", str(output)])
            self.assertEqual(result, 0)
            self.assertTrue(output.is_file())
            self.assertEqual(
                json.loads(output.read_text()), json.loads(stdout.getvalue())
            )

            before = source.read_bytes()
            errors = io.StringIO()
            with redirect_stderr(errors):
                result = main(["--source", str(source), "--json-out", str(source)])
            self.assertEqual(result, 2)
            self.assertIn("output_conflicts_source", errors.getvalue())
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
