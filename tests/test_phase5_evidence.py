"""Synthetic file fixtures only: no known-mass hardware evidence exists yet."""

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from trackpad_scale.phase4_profile import (
    PHASE4_EXPERIMENTAL_PROFILE_ID,
    PHASE4_RAW_UNITS,
)
from trackpad_scale.phase5_evidence import (
    Phase5EvidenceError,
    load_known_mass_evidence,
)


_TARGET = {
    "architecture": "arm64",
    "framework_bundle_version": "fixture-framework",
    "framework_image_uuid": "FIXTURE-UUID",
    "hardware_model": "fixture-model",
    "kernel_osversion": "fixture-kernel",
    "os_build": "fixture-build",
}
_PROFILE_ID = "synthetic-phase4-validated-for-phase5-v1"
_GEOMETRY = "physical_mass_single_contact_fixture_v1"


def _capture_stats():
    return {
        "callback_count": 1000,
        "enqueued_count": 1000,
        "queue_overwrite_count": 0,
        "lock_contention_drop_count": 0,
        "callback_device_mismatch_count": 0,
        "late_callback_count": 0,
        "in_flight_callback_count": 0,
        "queue_depth": 0,
        "native_drop_count": 0,
    }


def _transport_stats():
    return {
        "attempted_frame_count": 1000,
        "copied_touch_count": 1000,
        "queue_overwrite_count": 0,
        "lock_contention_drop_count": 0,
        "invalid_count_frame_count": 0,
        "null_records_frame_count": 0,
        "device_mismatch_frame_count": 0,
        "record_frame_mismatch_touch_count": 0,
        "record_timestamp_mismatch_touch_count": 0,
        "invalid_state_touch_count": 0,
        "pressure_sentinel_touch_count": 0,
        "nonfinite_touch_count": 0,
        "queue_depth": 0,
        "native_drop_count": 0,
    }


def _trial(role, session, trial_id, mass):
    return {
        "role": role,
        "session_id": session,
        "trial_id": trial_id,
        "known_mass_grams": mass,
        "mass_reference_id": "independently-weighed-fixture-mass-set-v1",
        "contact_geometry_protocol_id": _GEOMETRY,
        "source": {
            "path": f"{trial_id}.json",
            "sha256": "",
            "publication_sequence": 800,
        },
    }


def _source(trial):
    publication = {
        "pressure_delta_raw": float(trial["known_mass_grams"]) * 0.4,
        "dispersion_mad_raw": 0.5,
        "slope_raw_per_second": 0.1,
        "duration_seconds": 0.76,
        "sequence": 800,
        "host_monotonic_ns": 8_000_000_000,
        "confidence": {
            "agreeing_window_count": 2,
            "required_window_count": 2,
            "agreement_span_raw": 1.0,
            "agreement_limit_raw": 3.0,
            "maximum_window_mad_raw": 0.5,
            "mad_limit_raw": 3.0,
            "maximum_absolute_slope_raw_per_second": 0.1,
            "slope_limit_raw_per_second": 3.0,
        },
    }
    return {
        "schema_version": 1,
        "phase": 5,
        "kind": "physical_known_mass_trial",
        "status": "raw_evidence_only",
        "units": PHASE4_RAW_UNITS,
        "calibration_performed": False,
        "grams_claimed": False,
        "actual_target": dict(_TARGET),
        "profile": {"profile_id": _PROFILE_ID, "status": "validated_for_phase5"},
        "acquisition": {
            "acquisition_protocol_id": "phase5_physical_known_mass_v1",
            "contact_geometry_protocol_id": trial["contact_geometry_protocol_id"],
            "mass_reference_id": trial["mass_reference_id"],
            "mass_application": (
                "measured_zero_additional_mass"
                if trial["known_mass_grams"] == 0
                else "physical_known_mass"
            ),
        },
        "session_id": trial["session_id"],
        "trial_id": trial["trial_id"],
        "known_mass_grams": trial["known_mass_grams"],
        "outcome": "stable_raw_pressure_observed",
        "phase1_capture_stats": _capture_stats(),
        "phase2_transport_stats": _transport_stats(),
        "publication_count": 1,
        "publications": [publication],
        "event_updates": [
            {
                "interval": "measurement",
                "update": {
                    "status": "stable_published",
                    "sequence": 800,
                    "published_measurement": copy.deepcopy(publication),
                },
            }
        ],
    }


def _manifest():
    trials = [
        _trial("training", "train-a", "t-50-a", 50),
        _trial("training", "train-b", "t-50-b", 50),
        _trial("training", "train-a", "t-100-a", 100),
        _trial("training", "train-b", "t-100-b", 100),
        _trial("training", "train-a", "t-150-a", 150),
        _trial("training", "train-b", "t-150-b", 150),
        _trial("heldout", "hold-a", "h-75-a", 75),
        _trial("heldout", "hold-b", "h-75-b", 75),
        _trial("heldout", "hold-a", "h-125-a", 125),
        _trial("heldout", "hold-b", "h-125-b", 125),
    ]
    return {
        "schema_version": 1,
        "phase": 5,
        "purpose": "known_mass_calibration",
        "acquisition_protocol_id": "phase5_physical_known_mass_v1",
        "target": dict(_TARGET),
        "phase4_profile_id": _PROFILE_ID,
        "trials": trials,
    }


class Phase5EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.manifest = _manifest()
        self.sources = [_source(trial) for trial in self.manifest["trials"]]

    def _write(self):
        for trial, source in zip(self.manifest["trials"], self.sources):
            raw = json.dumps(source, sort_keys=True, allow_nan=False).encode("utf-8")
            path = self.directory / trial["source"]["path"]
            path.write_bytes(raw)
            trial["source"]["sha256"] = hashlib.sha256(raw).hexdigest()
        manifest_path = self.directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(self.manifest, sort_keys=True), encoding="utf-8"
        )
        return manifest_path

    def _load(self):
        return load_known_mass_evidence(
            self._write(),
            expected_target=_TARGET,
            expected_phase4_profile_id=_PROFILE_ID,
        )

    def _assert_reason(self, reason):
        with self.assertRaises(Phase5EvidenceError) as raised:
            self._load()
        self.assertEqual(raised.exception.reason, reason)

    def test_synthetic_independent_trials_preserve_immutable_provenance(self):
        records = self._load()
        self.assertEqual(len(records), 10)
        self.assertEqual({r.role for r in records}, {"training", "heldout"})
        self.assertEqual(records[0].pressure_delta_raw, 20.0)
        self.assertEqual(records[0].known_mass_grams, 50.0)
        self.assertEqual(records[0].publication_sequence, 800)
        self.assertEqual(
            records[0].source_sha256,
            self.manifest["trials"][0]["source"]["sha256"],
        )
        self.assertEqual(records[0].confidence.agreeing_window_count, 2)
        with self.assertRaises(FrozenInstanceError):
            records[0].pressure_delta_raw = 999  # type: ignore[misc]

    def test_current_experimental_profile_is_not_a_calibration_profile(self):
        manifest_path = self._write()
        with self.assertRaises(Phase5EvidenceError) as raised:
            load_known_mass_evidence(
                manifest_path,
                expected_target=_TARGET,
                expected_phase4_profile_id=PHASE4_EXPERIMENTAL_PROFILE_ID,
            )
        self.assertEqual(raised.exception.reason, "experimental_profile")

    def test_phase4_fingertip_and_replay_artifacts_cannot_be_relabeled(self):
        for kind in ("phase4_live", "offline_phase4_replay", "phase2_capture"):
            with self.subTest(kind=kind):
                self.sources[0]["phase"] = 4 if kind != "phase2_capture" else 2
                self.sources[0]["kind"] = kind
                self._assert_reason("provenance_mismatch")

    def test_missing_or_corrupted_source_fails_before_parsing(self):
        manifest_path = self._write()
        (self.directory / "t-50-a.json").unlink()
        with self.assertRaises(Phase5EvidenceError) as raised:
            load_known_mass_evidence(
                manifest_path,
                expected_target=_TARGET,
                expected_phase4_profile_id=_PROFILE_ID,
            )
        self.assertEqual(raised.exception.reason, "source_unreadable")

        manifest_path = self._write()
        (self.directory / "t-50-a.json").write_text("corrupted", encoding="utf-8")
        with self.assertRaises(Phase5EvidenceError) as raised:
            load_known_mass_evidence(
                manifest_path,
                expected_target=_TARGET,
                expected_phase4_profile_id=_PROFILE_ID,
            )
        self.assertEqual(raised.exception.reason, "source_sha256_mismatch")

    def test_target_profile_or_mass_reference_mismatch_fails(self):
        self.sources[0]["actual_target"]["hardware_model"] = "other"
        self._assert_reason("target_mismatch")
        self.sources[0] = _source(self.manifest["trials"][0])
        self.sources[0]["profile"]["status"] = "experimental_unvalidated"
        self._assert_reason("provenance_mismatch")
        self.sources[0] = _source(self.manifest["trials"][0])
        self.sources[0]["acquisition"]["mass_reference_id"] = "unknown"
        self._assert_reason("provenance_mismatch")

    def test_missing_mass_reference_or_finger_protocol_fails(self):
        self.manifest["trials"][0]["mass_reference_id"] = ""
        self._assert_reason("schema_invalid")
        self.manifest["trials"][0]["mass_reference_id"] = "ref"
        self.manifest["trials"][0][
            "contact_geometry_protocol_id"
        ] = "fingertip_pressure"
        self._assert_reason("finger_only_protocol")

    def test_publication_must_match_stable_event_and_confidence(self):
        self.sources[0]["event_updates"][0]["update"]["status"] = "monitoring"
        self._assert_reason("publication_invalid")
        self.sources[0] = _source(self.manifest["trials"][0])
        self.sources[0]["publications"][0]["confidence"]["agreement_span_raw"] = 4.0
        self._assert_reason("publication_invalid")
        self.sources[0] = _source(self.manifest["trials"][0])
        self.sources[0]["publications"][0]["sequence"] = 801
        self._assert_reason("publication_invalid")

    def test_transport_drop_or_grams_claim_fails(self):
        self.sources[0]["phase2_transport_stats"]["queue_overwrite_count"] = 1
        self._assert_reason("transport_unclean")
        self.sources[0] = _source(self.manifest["trials"][0])
        self.sources[0]["grams_claimed"] = True
        self._assert_reason("provenance_mismatch")

    def test_duplicate_trials_or_shared_train_heldout_sessions_fail(self):
        self.manifest["trials"][1]["trial_id"] = "t-50-a"
        self._assert_reason("duplicate_trial")
        self.manifest = _manifest()
        self.sources = [_source(trial) for trial in self.manifest["trials"]]
        self.manifest["trials"][6]["session_id"] = "train-a"
        self.sources[6]["session_id"] = "train-a"
        self._assert_reason("training_heldout_overlap")

    def test_two_sessions_per_mass_and_role_are_required(self):
        self.manifest["trials"][1]["session_id"] = "train-a"
        self.sources[1]["session_id"] = "train-a"
        self._assert_reason("insufficient_independent_trials")

    def test_explicit_zero_additional_mass_is_allowed_not_inferred(self):
        for index in (0, 1):
            self.manifest["trials"][index]["known_mass_grams"] = 0
            self.sources[index] = _source(self.manifest["trials"][index])
        records = self._load()
        self.assertEqual([record.known_mass_grams for record in records[:2]], [0, 0])
        self.assertEqual(records[0].pressure_delta_raw, 0)

        self.sources[0]["acquisition"]["mass_application"] = "physical_known_mass"
        self._assert_reason("provenance_mismatch")

    def test_interior_distinct_holdout_masses_required(self):
        for index in (8, 9):
            self.manifest["trials"][index]["known_mass_grams"] = 75
            self.sources[index] = _source(self.manifest["trials"][index])
        self._assert_reason("insufficient_independent_trials")
        for index in (8, 9):
            self.manifest["trials"][index]["known_mass_grams"] = 175
            self.sources[index] = _source(self.manifest["trials"][index])
        self._assert_reason("heldout_mass_outside_training_range")

    def test_duplicate_json_keys_rejected(self):
        manifest_path = self._write()
        manifest_path.write_text('{"phase":5,"phase":5}', encoding="utf-8")
        with self.assertRaises(Phase5EvidenceError) as raised:
            load_known_mass_evidence(
                manifest_path,
                expected_target=_TARGET,
                expected_phase4_profile_id=_PROFILE_ID,
            )
        self.assertEqual(raised.exception.reason, "json_invalid")


if __name__ == "__main__":
    unittest.main()
