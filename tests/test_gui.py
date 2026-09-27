"""GUI integration: transport ownership, stale readings, calibration and HTTP gates."""

import http.client
import json
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from trackpad_scale.gui import GuiServer, ScaleController, assess_playground
from trackpad_scale.models import RawContact, RawFrame, TargetTouchState


def playground_payload():
    return {
        "training": [{"mass": m, "readings": [m / 10, m / 10]} for m in (0, 100, 200)],
        "heldout": [{"mass": m, "readings": [m / 10, m / 10]} for m in (50, 150)],
        "criteria": {
            "min_repeats_per_mass": 2,
            "max_repeated_mad_raw": 0,
            "max_repeated_absolute_deviation_raw": 0,
            "max_training_absolute_residual_grams": 0,
            "nonlinear_residual_trigger_grams": 1,
            "max_piecewise_training_cv_absolute_error_grams": 1,
            "minimum_piecewise_training_cv_improvement_grams": 1,
            "max_holdout_absolute_error_grams": 0,
            "allowed_mass_min_grams": 50,
            "allowed_mass_max_grams": 150,
        },
    }


class TestSignal:
    """Already validated application frames, on a faster deterministic clock."""

    def __init__(self):
        self.sequence = 0
        self.running = False
        self.closed = False
        self.release = False
        self.silent = False
        self.raise_on_read = False
        self.interference = False
        self.thread_ids = set()

    def _owner(self):
        self.thread_ids.add(threading.get_ident())

    def start(self):
        self._owner()
        self.running = True

    def close(self):
        self._owner()
        self.running = False
        self.closed = True

    def is_running(self):
        return self.running

    def supports_force(self):
        return True

    def transport_stats(self):
        class Stats:
            def to_dict(self):
                return {"queue_overwrite_count": 0, "lock_contention_drop_count": 0}

        return Stats()

    def read_frame(self, timeout=None):
        self._owner()
        time.sleep(0.002)
        if self.raise_on_read:
            raise RuntimeError("Injected sensor failure")
        if self.silent:
            return None
        self.sequence += 1
        contact = RawContact(
            path_index=1,
            state=int(TargetTouchState.TOUCHING),
            finger_code=1,
            hand_code=0,
            normalized_x=0.5,
            normalized_y=0.5,
            z_total_raw=1,
            pressure_candidate_raw=10 if self.sequence < 160 else 30,
            z_density_raw=1,
            normalized_x_bits=0,
            normalized_y_bits=0,
            z_total_bits=0,
            pressure_candidate_bits=0,
            z_density_bits=0,
        )
        contacts = (contact,)
        if self.interference:
            contacts += (replace(contact, path_index=2,
                                 state=TargetTouchState.HOVER_IN_RANGE,
                                 pressure_candidate_raw=124),)
        return RawFrame(
            sequence=self.sequence,
            frame_number=self.sequence,
            device_timestamp=self.sequence / 123,
            host_monotonic_ns=round(self.sequence * 1e9 / 123),
            contacts=() if self.release else contacts,
        )


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.delay = patch("trackpad_scale.gui.TARE_DELAY_SECONDS", 0)
        self.delay.start()
        self.sensor = TestSignal()
        self.controller = ScaleController(sensor_factory=lambda: self.sensor)

    def tearDown(self):
        self.controller.stop()
        self.delay.stop()

    def wait_state(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.controller.snapshot()
            if predicate(state):
                return state
            time.sleep(0.01)
        self.fail(
            f"Expected controller state not reached: {self.controller.snapshot()}"
        )

    def start_stable(self):
        self.controller.start("live")
        self.controller.command("tare")
        return self.wait_state(
            lambda state: state["stable"] is not None
            and state["stable"]["pressure_delta_raw"] == 20
        )

    def test_live_stability_release_and_thread_ownership(self):
        state = self.start_stable()
        self.assertEqual(state["tare"]["baseline_pressure_raw"], 10)
        self.sensor.release = True
        state = self.wait_state(lambda state: not state["contacts"])
        self.assertIsNone(state["stable"])
        self.assertIsNone(state["tare"])
        self.assertIsNone(state["delta"])
        self.assertEqual(state["last_capture"]["measurement"]["pressure_delta_raw"], 20)
        self.controller.stop()
        self.assertTrue(self.sensor.closed)
        self.assertEqual(len(self.sensor.thread_ids), 1)
        self.assertNotIn(threading.get_ident(), self.sensor.thread_ids)
        self.assertIsNone(self.controller.snapshot()["raw"])

    def test_silent_transport_clears_reading_and_tare(self):
        self.start_stable()
        self.sensor.silent = True
        state = self.wait_state(lambda state: state["status"] == "tare_required")
        for key in ("stable", "tare", "delta", "raw"):
            self.assertIsNone(state[key])
        self.assertEqual(state["contacts"], [])

    def test_sensor_error_closes_and_can_restart(self):
        self.sensor.raise_on_read = True
        self.controller.start("live")
        state = self.wait_state(lambda state: not state["running"])
        self.assertEqual(state["status"], "error")
        self.assertIn("Injected sensor failure", state["error"])
        self.assertTrue(self.sensor.closed)
        self.sensor.raise_on_read = False
        self.controller.start("live")
        self.wait_state(lambda state: state["sequence"] > 0)

    def test_duplicate_start_and_tare_while_stopped_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Start a session"):
            self.controller.command("tare")
        self.controller.start("live")
        with self.assertRaisesRegex(ValueError, "Stop the current"):
            self.controller.start("demo")

    def test_tare_timeout_and_snapshot_does_not_claim_calibration(self):
        self.sensor.release = True
        with patch("trackpad_scale.gui.TARE_TIMEOUT_SECONDS", 0.05):
            self.controller.start("live")
            self.controller.command("tare")
            self.wait_state(lambda state: state["status"] == "tare_required")
        packet = self.controller.diagnostic()
        self.assertFalse(packet["calibration_performed"])
        self.assertFalse(packet["public_weight_available"])
        self.assertFalse(packet["synthetic"])
        self.assertTrue(packet["raw_frames"])
        self.assertFalse(packet["frames_truncated"])
        json.dumps(packet, allow_nan=False)

    def test_retare_explicitly_clears_previous_measurement(self):
        self.start_stable()
        with patch("trackpad_scale.gui.TARE_DELAY_SECONDS", 0.1):
            self.controller.command("tare")
            state = self.wait_state(lambda state: state["status"] == "countdown")
        self.assertIsNone(state["stable"])
        self.assertIsNone(state["tare"])
        self.assertIsNone(state["delta"])

    def test_first_pressure_is_available_without_tare_or_countdown(self):
        self.controller.start("live")
        state = self.wait_state(lambda state: state["raw"] is not None)
        self.assertEqual(state["raw"], 10)
        self.assertEqual(state["live_reading"]["estimated_grams"], 10)
        self.assertFalse(state["hardware_validated"])
        self.assertEqual(state["last_reading"]["pressure_raw"], 10)
        self.assertFalse(state["tare_requested"])
        self.assertIsNone(state["tare"])
        self.assertIsNone(state["stable"])
        self.assertEqual(state["status"], "listening")

    def test_display_zero_is_immediate_independent_and_reset_on_release(self):
        self.controller.start("live")
        self.wait_state(lambda state: state["raw"] == 30)
        self.controller.command("zero")
        zeroed = self.wait_state(lambda state: state["live_reading"]["zeroed"])
        self.assertEqual(zeroed["live_reading"]["estimated_grams"], 0)
        self.assertEqual(zeroed["live_reading"]["zero_offset_raw"], 30)
        self.assertFalse(zeroed["tare_requested"])
        self.assertIsNone(zeroed["tare"])
        self.assertIsNone(zeroed["stable"])
        self.sensor.release = True
        released = self.wait_state(lambda state: not state["contacts"])
        self.assertIsNone(released["live_reading"])
        self.assertEqual(released["last_reading"]["estimated_grams"], 0)
        with self.assertRaisesRegex(ValueError, "Rest one finger"):
            self.controller.command("zero")
        self.sensor.release = False
        resumed = self.wait_state(lambda state: state["live_reading"] is not None)
        self.assertEqual(resumed["live_reading"]["estimated_grams"], 30)
        self.assertFalse(resumed["live_reading"]["zeroed"])

    def test_silence_clears_display_zero_before_retouch(self):
        self.controller.start("live")
        self.wait_state(lambda state: state["raw"] == 30)
        self.controller.command("zero")
        self.wait_state(lambda state: state["live_reading"]["zeroed"])
        self.sensor.silent = True
        self.wait_state(lambda state: state["live_reading"] is None)
        self.sensor.silent = False
        resumed = self.wait_state(lambda state: state["live_reading"] is not None)
        self.assertFalse(resumed["live_reading"]["zeroed"])

    def test_object_pressure_updates_without_losing_display_zero(self):
        self.controller.start("live")
        self.wait_state(lambda state: state["raw"] == 30)
        self.controller.command("zero")
        self.wait_state(lambda state: state["live_reading"]["zeroed"])
        self.sensor.interference = True
        loaded = self.wait_state(lambda state: state["live_reading"]["estimated_grams"] == 124)
        self.assertIsNone(loaded["reading_issue"])
        self.assertEqual(loaded["raw"], 154)
        self.assertEqual(loaded["contacts"][1]["pressure_raw"], 124)
        self.assertEqual(loaded["live_reading"]["contributing_paths"], (1, 2))
        self.assertTrue(loaded["live_reading"]["zeroed"])
        self.assertTrue(self.controller.diagnostic()["estimated_weight_available"])
        self.assertFalse(self.controller.diagnostic()["hardware_validated"])
        self.sensor.interference = False
        resumed = self.wait_state(lambda state: state["live_reading"]["estimated_grams"] == 0)
        self.assertIsNone(resumed["reading_issue"])
        self.assertTrue(resumed["live_reading"]["zeroed"])

    def test_multiple_contact_reading_clears_when_stream_stops(self):
        self.sensor.interference = True
        self.controller.start("live")
        self.wait_state(lambda state: state["raw"] == 134)
        self.sensor.silent = True
        self.wait_state(lambda state: state["live_reading"] is None)
        self.assertIsNone(self.controller.snapshot()["reading_issue"])

    def test_lifting_preserves_labelled_history_and_retouch_needs_no_zero(self):
        self.controller.start("live")
        self.wait_state(lambda state: state["raw"] == 30)
        self.sensor.release = True
        released = self.wait_state(lambda state: not state["contacts"])
        self.assertIsNone(released["raw"])
        self.assertEqual(released["last_reading"]["pressure_raw"], 30)
        self.sensor.release = False
        resumed = self.wait_state(lambda state: state["raw"] == 30)
        self.assertFalse(resumed["tare_requested"])
        self.assertIsNone(resumed["tare"])
        self.controller.stop()
        self.assertIsNone(self.controller.snapshot()["raw"])
        self.assertEqual(self.controller.snapshot()["last_reading"]["pressure_raw"], 30)

    def test_expired_calibration_does_not_interrupt_live_pressure(self):
        self.controller.profile = replace(
            self.controller.profile,
            config=replace(
                self.controller.profile.config, maximum_tare_age_seconds=2.5
            ),
        )
        self.start_stable()
        expired = self.wait_state(lambda state: state["status"] == "tare_expired")
        self.assertIsNone(expired["tare"])
        self.assertIsNone(expired["stable"])
        self.assertIsNone(expired["delta"])
        self.assertEqual(expired["raw"], 30)
        self.assertEqual(expired["last_reading"]["pressure_raw"], 30)
        self.assertIn("expired", expired["tare_issue"])

    def test_failed_calibration_has_a_persistent_explanation(self):
        self.sensor.release = True
        with patch("trackpad_scale.gui.TARE_TIMEOUT_SECONDS", 0.05):
            self.controller.start("live")
            self.controller.command("tare")
            failed = self.wait_state(lambda state: bool(state["tare_issue"]))
        self.sensor.release = False
        resumed = self.wait_state(lambda state: state["raw"] is not None)
        self.assertEqual(resumed["tare_issue"], failed["tare_issue"])
        self.assertIsNone(resumed["tare"])


class CalibrationTests(unittest.TestCase):
    def test_fit_is_exploratory_even_when_mathematics_pass(self):
        report = assess_playground(playground_payload())
        self.assertTrue(report["validation"]["accepted"])
        self.assertEqual(report["status"], "exploratory_math_only")
        self.assertFalse(report["public_weight_available"])
        self.assertFalse(report["hardware_validated"])
        json.dumps(report, allow_nan=False)

    def test_holdout_failures_and_missing_tolerances_not_bypassed(self):
        payload = playground_payload()
        payload["heldout"][0]["readings"] = [6, 6]
        report = assess_playground(payload)
        self.assertFalse(report["validation"]["accepted"])
        self.assertEqual(
            report["validation"]["reason"], "independent_heldout_error_exceeded"
        )
        del payload["criteria"]["max_holdout_absolute_error_grams"]
        with self.assertRaises(TypeError):
            assess_playground(payload)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.controller = ScaleController()
        self.server = GuiServer(self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port
        )

    def tearDown(self):
        self.connection.close()
        self.controller.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        self.connection.request(method, path, body, headers or {})
        response = self.connection.getresponse()
        return response.status, response.read()

    def test_assets_and_same_origin_gates(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Scale reading", body)
        self.assertEqual(self.request("GET", "/../pyproject.toml")[0], 404)
        self.assertEqual(
            self.request("GET", "/api/state", headers={"Host": "evil.test"})[0], 403
        )
        self.assertEqual(
            self.request(
                "GET", "/api/session", headers={"Origin": "https://evil.test"}
            )[0],
            403,
        )
        self.assertEqual(self.request("POST", "/api/tare", "{}")[0], 403)
        self.assertEqual(self.request("POST", "/api/zero", "{}")[0], 403)

    def test_zero_api_uses_the_sensor_worker_and_requires_contact(self):
        headers = {"X-Pebble-Token": self.server.token, "Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/zero", "{}", headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/start", '{"mode":"demo"}', headers)[0], 200)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.controller.snapshot()["live_reading"] is None:
            time.sleep(0.01)
        self.assertIsNotNone(self.controller.snapshot()["live_reading"])
        self.assertEqual(self.request("POST", "/api/zero", "{}", headers)[0], 200)
        while time.monotonic() < deadline and not self.controller.snapshot()["live_reading"]["zeroed"]:
            time.sleep(0.01)
        self.assertTrue(self.controller.snapshot()["live_reading"]["zeroed"])

    def test_calibration_api_rejects_bad_requests_and_preserves_gates(self):
        headers = {
            "X-Pebble-Token": self.server.token,
            "Content-Type": "application/json",
        }
        status, body = self.request(
            "POST", "/api/calibration", json.dumps(playground_payload()), headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["validation"]["accepted"])
        self.assertFalse(json.loads(body)["public_weight_available"])
        self.assertEqual(
            self.request("POST", "/api/calibration", "[]", headers)[0], 400
        )
        self.assertEqual(
            self.request("POST", "/api/start", '{"mode":"unknown"}', headers)[0], 400
        )
        self.assertEqual(self.request("POST", "/api/assessment", "{}", headers)[0], 400)


if __name__ == "__main__":
    unittest.main()
