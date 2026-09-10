import copy
import json
import unittest
from dataclasses import FrozenInstanceError, asdict, fields
from importlib import resources

from trackpad_scale.phase4_profile import (
    PHASE4_EXACT_TARGET_SCOPE,
    PHASE4_EXPERIMENTAL_STATUS,
    PHASE4_SOURCE_ARTIFACT_SHA256,
    ExperimentalPhase4Profile,
    Phase4ProfileError,
    load_experimental_phase4_profile,
    parse_experimental_phase4_profile,
)
from trackpad_scale.phase4_stabilizer import StabilizerConfig


def _payload():
    resource = (
        resources.files("trackpad_scale")
        .joinpath("phase4_profiles")
        .joinpath("exact_target_experimental_v1.json")
    )
    return json.loads(resource.read_text(encoding="utf-8"))


class Phase4ProfileLoaderTests(unittest.TestCase):
    def test_loads_explicit_immutable_exact_target_profile(self) -> None:
        profile = load_experimental_phase4_profile()

        self.assertIsInstance(profile, ExperimentalPhase4Profile)
        self.assertEqual(profile.status, PHASE4_EXPERIMENTAL_STATUS)
        self.assertEqual(profile.scope, PHASE4_EXACT_TARGET_SCOPE)
        self.assertFalse(profile.calibration_performed)
        self.assertEqual(
            profile.source_phase2_artifact.sha256,
            PHASE4_SOURCE_ARTIFACT_SHA256,
        )
        self.assertEqual(
            profile.source_phase2_artifact.target.hardware_model, "Mac16,8"
        )
        self.assertEqual(
            profile.source_phase2_artifact.target.framework_image_uuid,
            "40D691BB-9166-31E0-959E-351863FF09A0",
        )
        with self.assertRaises(FrozenInstanceError):
            profile.status = "validated"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            profile.config.median_window_samples = 3  # type: ignore[misc]

    def test_config_is_complete_and_uses_replayed_starting_values(self) -> None:
        profile = load_experimental_phase4_profile()
        config = asdict(profile.config)

        self.assertEqual(
            set(config), {field.name for field in fields(StabilizerConfig)}
        )
        self.assertEqual(config["tare_window_samples"], 123)
        self.assertEqual(config["tare_max_absolute_deviation_raw"], 3.0)
        self.assertEqual(config["tare_max_abs_slope_raw_per_second"], 3.0)
        self.assertEqual(config["smoothing_window_samples"], 5)
        self.assertEqual(config["stability_max_abs_slope_raw_per_second"], 3.0)
        self.assertEqual(config["median_window_samples"], 31)
        self.assertEqual(config["stability_window_seconds"], 0.75)
        self.assertEqual(config["outlier_floor_raw"], 12.0)

        replay = profile.source_phase2_artifact.replay
        self.assertEqual(
            (replay.tare_first_sequence, replay.tare_last_sequence), (1351, 1473)
        )
        self.assertEqual(
            (replay.continuation_first_sequence, replay.continuation_last_sequence),
            (1474, 2892),
        )
        self.assertEqual(replay.expected_publication_sequence, 2459)
        self.assertEqual(replay.expected_published_delta_raw, 45.5)

    def test_rejects_missing_unknown_or_wrongly_typed_config_values(self) -> None:
        cases = []

        missing = copy.deepcopy(_payload())
        del missing["config"]["smoothing_window_samples"]
        cases.append(missing)

        unknown = copy.deepcopy(_payload())
        unknown["config"]["invented_threshold"] = 1
        cases.append(unknown)

        boolean_integer = copy.deepcopy(_payload())
        boolean_integer["config"]["median_window_samples"] = True
        cases.append(boolean_integer)

        fractional_integer = copy.deepcopy(_payload())
        fractional_integer["config"]["stability_min_samples"] = 90.0
        cases.append(fractional_integer)

        nonnumeric = copy.deepcopy(_payload())
        nonnumeric["config"]["stability_max_mad_raw"] = "3.0"
        cases.append(nonnumeric)

        for payload in cases:
            with self.subTest(config=payload["config"]):
                with self.assertRaises(Phase4ProfileError):
                    parse_experimental_phase4_profile(payload)

    def test_rejects_broadened_status_scope_or_provenance_claims(self) -> None:
        mutations = (
            ("schema", lambda value: value.__setitem__("schema_version", 1.0)),
            ("status", lambda value: value.__setitem__("status", "validated")),
            ("scope", lambda value: value.__setitem__("scope", "all_macs")),
            ("units", lambda value: value.__setitem__("units", "grams")),
            (
                "calibration",
                lambda value: value.__setitem__("calibration_performed", True),
            ),
            (
                "sha256",
                lambda value: value["source_phase2_artifact"].__setitem__(
                    "sha256", "0" * 64
                ),
            ),
            ("unknown", lambda value: value.__setitem__("approved", True)),
        )
        for name, mutate in mutations:
            payload = copy.deepcopy(_payload())
            mutate(payload)
            with self.subTest(name=name):
                with self.assertRaises(Phase4ProfileError):
                    parse_experimental_phase4_profile(payload)

    def test_rejects_unknown_nested_evidence_keys(self) -> None:
        payload = copy.deepcopy(_payload())
        payload["source_phase2_artifact"]["replay"]["grams"] = 1

        with self.assertRaises(Phase4ProfileError):
            parse_experimental_phase4_profile(payload)


if __name__ == "__main__":
    unittest.main()
