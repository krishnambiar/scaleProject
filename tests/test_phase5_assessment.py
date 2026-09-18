import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from trackpad_scale.phase4_stabilizer import StabilityConfidence
from trackpad_scale.phase5_assessment import (
    Phase5AssessmentError,
    assess_known_mass_manifest,
    load_criteria,
    main,
    write_assessment,
)
from trackpad_scale.phase5_calibration import CalibrationConfig
from trackpad_scale.phase5_evidence import KnownMassPublication


def _config() -> CalibrationConfig:
    return CalibrationConfig(
        min_repeats_per_mass=2,
        max_repeated_mad_raw=0.0,
        max_repeated_absolute_deviation_raw=0.0,
        max_training_absolute_residual_grams=0.0,
        nonlinear_residual_trigger_grams=1.0,
        max_piecewise_training_cv_absolute_error_grams=1.0,
        minimum_piecewise_training_cv_improvement_grams=1.0,
        max_holdout_absolute_error_grams=0.0,
        allowed_mass_min_grams=50.0,
        allowed_mass_max_grams=150.0,
    )


def _records():
    confidence = StabilityConfidence(
        agreeing_window_count=2,
        required_window_count=2,
        agreement_span_raw=0.0,
        agreement_limit_raw=1.0,
        maximum_window_mad_raw=0.0,
        mad_limit_raw=1.0,
        maximum_absolute_slope_raw_per_second=0.0,
        slope_limit_raw_per_second=1.0,
    )
    result = []
    for role, masses in (("training", (0, 100, 200)), ("heldout", (50, 150))):
        for mass in masses:
            for repeat in range(2):
                result.append(
                    KnownMassPublication(
                        role=role,
                        session_id=f"{role}-{repeat}",
                        trial_id=f"{role}-{mass}-{repeat}",
                        known_mass_grams=float(mass),
                        mass_reference_id=f"reference-{mass}",
                        contact_geometry_protocol_id="sealed-single-contact",
                        acquisition_protocol_id="phase5_physical_known_mass_v1",
                        phase4_profile_id="reviewed-profile-v2",
                        target=(("hardware_model", "Mac16,8"),),
                        source_path=Path(f"/tmp/future-{role}-{mass}-{repeat}.json"),
                        source_sha256="a" * 64,
                        publication_sequence=repeat + 1,
                        pressure_delta_raw=float(mass) / 10.0,
                        dispersion_mad_raw=0.0,
                        slope_raw_per_second=0.0,
                        duration_seconds=1.0,
                        host_monotonic_ns=repeat + 1,
                        confidence=confidence,
                    )
                )
    return tuple(result)


class Phase5AssessmentTests(unittest.TestCase):
    def test_criteria_require_every_predeclared_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "criteria.json"
            values = asdict(_config())
            path.write_text(json.dumps(values), encoding="utf-8")
            self.assertEqual(load_criteria(path), _config())
            del values["max_holdout_absolute_error_grams"]
            path.write_text(json.dumps(values), encoding="utf-8")
            with self.assertRaises(Phase5AssessmentError):
                load_criteria(path)
            values["max_holdout_absolute_error_grams"] = True
            path.write_text(json.dumps(values), encoding="utf-8")
            with self.assertRaises(Phase5AssessmentError):
                load_criteria(path)

    def test_duplicate_json_keys_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "criteria.json"
            path.write_text('{"min_repeats_per_mass":2,"min_repeats_per_mass":3}')
            with self.assertRaisesRegex(Phase5AssessmentError, "duplicate JSON key"):
                load_criteria(path)

    def test_synthetic_candidate_remains_nonpublic_and_bound_to_sources(self):
        records = _records()
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "future-manifest.json"
            manifest.write_text('{"future":"fixture"}', encoding="utf-8")
            with patch(
                "trackpad_scale.phase5_assessment.load_known_mass_evidence",
                return_value=records,
            ) as loader:
                report = assess_known_mass_manifest(
                    manifest,
                    _config(),
                    expected_target={"hardware_model": "Mac16,8"},
                    expected_phase4_profile_id="reviewed-profile-v2",
                )
        loader.assert_called_once()
        self.assertEqual(report["status"], "mathematical_candidate_only")
        self.assertFalse(report["public_weight_available"])
        self.assertFalse(report["hardware_validated"])
        self.assertFalse(report["grams_claimed"])
        self.assertEqual(len(report["source_trials"]), len(records))
        self.assertEqual(len(report["manifest_sha256"]), 64)
        self.assertEqual(report["validation"]["model"]["kind"], "linear")
        json.dumps(report, allow_nan=False)

    def test_report_writer_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            write_assessment(path, {"status": "rejected"})
            first = path.read_bytes()
            with self.assertRaisesRegex(Phase5AssessmentError, "refusing to overwrite"):
                write_assessment(path, {"status": "accepted"})
            self.assertEqual(path.read_bytes(), first)

    def test_cli_records_criteria_digest_without_public_weight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            criteria = root / "criteria.json"
            criteria.write_text(json.dumps(asdict(_config())), encoding="utf-8")
            output = root / "assessment.json"
            with (
                patch(
                    "trackpad_scale.phase5_assessment.current_target_fingerprint"
                ) as fingerprint,
                patch(
                    "trackpad_scale.phase5_assessment.assess_known_mass_manifest",
                    return_value={
                        "status": "rejected",
                        "validation": {"reason": "independent_heldout_error_exceeded"},
                        "public_weight_available": False,
                    },
                ),
            ):
                fingerprint.return_value.to_dict.return_value = {
                    "hardware_model": "Mac16,8"
                }
                with redirect_stdout(io.StringIO()):
                    status = main(
                        [
                            "--manifest",
                            str(root / "manifest.json"),
                            "--criteria",
                            str(criteria),
                            "--phase4-profile-id",
                            "reviewed-profile-v2",
                            "--json-out",
                            str(output),
                        ]
                    )
            self.assertEqual(status, 3)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(report["criteria_sha256"]), 64)
            self.assertEqual(report["criteria_path"], str(criteria.resolve()))
            self.assertFalse(report["public_weight_available"])


if __name__ == "__main__":
    unittest.main()
