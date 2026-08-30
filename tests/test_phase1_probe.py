import argparse
import unittest

from trackpad_scale.phase1_probe import run


class ProbeArgumentTests(unittest.TestCase):
    def test_nonzero_start_options_require_diagnostic_override(self) -> None:
        arguments = argparse.Namespace(
            duration=1.0,
            lead_in=0.0,
            trials=1,
            guided=False,
            confirm_stages=False,
            start_options=1,
            allow_unverified_target=False,
        )

        with self.assertRaisesRegex(ValueError, "only start-options 0 is verified"):
            run(arguments)


if __name__ == "__main__":
    unittest.main()
