import unittest

from trackpad_scale.target_profile import TargetFingerprint, compare_target_to_profile


class TargetProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = TargetFingerprint(
            architecture="arm64",
            os_build="build",
            kernel_osversion="kernel-build",
            hardware_model="model",
            framework_bundle_version="version",
            framework_image_uuid="UUID",
        )
        self.profile = {"target": self.target.to_dict()}

    def test_exact_match(self) -> None:
        matches, mismatches = compare_target_to_profile(self.target, self.profile)
        self.assertTrue(matches)
        self.assertEqual(mismatches, [])

    def test_mismatch_is_explicit(self) -> None:
        changed = TargetFingerprint(**{**self.target.to_dict(), "os_build": "new"})
        matches, mismatches = compare_target_to_profile(changed, self.profile)
        self.assertFalse(matches)
        self.assertEqual(len(mismatches), 1)
        self.assertIn("os_build", mismatches[0])


if __name__ == "__main__":
    unittest.main()
