"""Pebble's loopback-only GUI and single-owner sensor worker.

The browser exposes an explicitly estimated gram readout.
Display zeroing is independent of the experimental calibration workflow.
"""

import argparse
import copy
import hashlib
import json
import math
import queue
import secrets
import struct
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Optional

from .models import RawContact, RawFrame, TargetTouchState
from .live_readout import ESTIMATE_BASIS, STALE_FRAME_SECONDS, LiveReadout
from .phase3_sensor import RawFrameSensor
from .phase4_profile import load_experimental_phase4_profile
from .phase4_stabilizer import PressureStabilizer, TareValidationError
from .phase5_assessment import assess_known_mass_manifest, load_criteria
from .phase5_calibration import (
    CalibrationConfig,
    CalibrationSeries,
    fit_and_validate_calibration,
)
from .target_profile import compare_target_to_profile, current_target_fingerprint


TARE_DELAY_SECONDS = 3.0
TARE_TIMEOUT_SECONDS = 12.0


def live_sensor():
    profile = load_experimental_phase4_profile()
    actual = current_target_fingerprint()
    matches, mismatches = compare_target_to_profile(
        actual, {"target": asdict(profile.source_phase2_artifact.target)}
    )
    if not matches:
        raise RuntimeError(
            "This Mac does not match the sensor profile: " + "; ".join(mismatches)
        )
    return RawFrameSensor()


class DemoSensor:
    """Explicitly synthetic, deterministic frames; never touches the hardware."""

    def __init__(self):
        self.sequence = 0
        self.loaded_at = 0
        self.origin = time.monotonic_ns()
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    close = stop

    def supports_force(self):
        return True

    def is_running(self):
        return self.running

    def read_frame(self, timeout=None):
        time.sleep(1 / 123)
        self.sequence += 1
        delta = 0.0
        if self.loaded_at is not None:
            delta = min(42.0, max(0.0, (self.sequence - self.loaded_at - 123) / 4))
        pressure = 10.0 + delta

        def bits(number):
            return struct.unpack(">I", struct.pack(">f", number))[0]

        contact = RawContact(
            path_index=1,
            state=int(TargetTouchState.TOUCHING),
            finger_code=1,
            hand_code=0,
            normalized_x=0.5,
            normalized_y=0.5,
            z_total_raw=1.0,
            pressure_candidate_raw=pressure,
            z_density_raw=1.0,
            normalized_x_bits=bits(0.5),
            normalized_y_bits=bits(0.5),
            z_total_bits=bits(1.0),
            pressure_candidate_bits=bits(pressure),
            z_density_bits=bits(1.0),
        )
        return RawFrame(
            sequence=self.sequence,
            frame_number=self.sequence,
            device_timestamp=self.sequence / 123,
            host_monotonic_ns=self.origin + round(self.sequence * 1e9 / 123),
            contacts=(contact,),
        )


class ScaleController:
    """Keep native start/read/stop/close on one thread, away from HTTP handlers."""

    def __init__(self, *, demo=False, sensor_factory=live_sensor):
        self.profile = load_experimental_phase4_profile()
        self.sensor_factory = sensor_factory
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.commands = queue.Queue()
        self.worker = None
        self.events = deque(maxlen=80)
        self.trace = deque(maxlen=240)
        self.frames = deque(maxlen=15000)
        self.frame_count = 0
        self.state = {
            "running": False,
            "mode": "demo" if demo else "live",
            "status": "idle",
            "message": "A little moment to find your balance.",
            "error": None,
            "raw": None,
            "live_reading": None,
            "reading_issue": None,
            "zero_issue": None,
            "last_reading": None,
            "delta": None,
            "stable": None,
            "last_capture": None,
            "tare": None,
            "update": None,
            "contacts": [],
            "countdown": 0,
            "tare_progress": 0,
            "tare_requested": False,
            "tare_issue": None,
            "transport": None,
            "sequence": 0,
        }

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(
                {
                    **self.state,
                    "trace": list(self.trace),
                    "events": list(self.events),
                    "profile_id": self.profile.profile_id,
                    "profile_status": self.profile.status,
                    "config": asdict(self.profile.config),
                    "estimate_basis": ESTIMATE_BASIS,
                    "hardware_validated": False,
                }
            )

    def _event(self, message):
        self.events.append({"time": time.strftime("%H:%M:%S"), "message": message})

    def start(self, mode):
        if mode not in ("live", "demo"):
            raise ValueError("Choose live or demo mode.")
        with self.lifecycle_lock, self.lock:
            if self.worker is not None and self.worker.is_alive():
                raise ValueError("Stop the current session before starting another.")
            self.stop_event.clear()
            self.commands = queue.Queue()
            self.trace.clear()
            self.frames.clear()
            self.events.clear()
            self.frame_count = 0
            self.state.update(
                running=True,
                mode=mode,
                status="connecting",
                error=None,
                message="Connecting to your trackpad…",
                raw=None,
                live_reading=None,
                reading_issue=None,
                zero_issue=None,
                last_reading=None,
                delta=None,
                stable=None,
                last_capture=None,
                tare=None,
                update=None,
                contacts=[],
                transport=None,
                sequence=0,
                countdown=0,
                tare_progress=0,
                tare_requested=False,
                tare_issue=None,
            )
            self.worker = threading.Thread(target=self._run, args=(mode,), daemon=True)
            self.worker.start()

    def stop(self):
        with self.lifecycle_lock:
            self.stop_event.set()
            worker = self.worker
            if worker is not None:
                worker.join(timeout=3)
                if worker.is_alive():
                    raise RuntimeError("The sensor is still stopping. Please wait.")

    def command(self, action):
        if action not in ("zero", "tare", "restart"):
            raise ValueError("Unknown sensor command.")
        with self.lock:
            if not self.state["running"]:
                raise ValueError("Start a session first.")
            reading = self.state["live_reading"]
            if action == "zero" and reading is None:
                raise ValueError("Rest one finger lightly on the trackpad before zeroing.")
            self.commands.put((action, reading["contact_epoch"] if action == "zero" else None))

    def _run(self, mode):
        sensor = None
        readout = LiveReadout()
        stabilizer = PressureStabilizer(self.profile.config)
        candidate = deque(maxlen=self.profile.config.tare_window_samples)
        tare_at = time.monotonic() + TARE_DELAY_SECONDS
        deadline = tare_at + TARE_TIMEOUT_SECONDS
        acquiring = False
        last_frame_at = time.monotonic()
        last_trace_at = 0.0
        last_stats_at = 0.0
        try:
            sensor = DemoSensor() if mode == "demo" else self.sensor_factory()
            sensor.start()
            if sensor.supports_force() is not True:
                raise RuntimeError("This trackpad does not report force support.")
            with self.lock:
                self.state.update(
                    status="listening", message="Ready for live pressure."
                )
                self._event(
                    "Synthetic demo started."
                    if mode == "demo"
                    else "Live sensor connected."
                )
            while not self.stop_event.is_set():
                now = time.monotonic()
                try:
                    action, zero_epoch = self.commands.get_nowait()
                except queue.Empty:
                    action = None
                    zero_epoch = None
                if action == "tare":
                    stabilizer.reset()
                    candidate.clear()
                    tare_at = now + TARE_DELAY_SECONDS
                    deadline, acquiring = tare_at + TARE_TIMEOUT_SECONDS, True
                    if mode == "demo":
                        sensor.loaded_at = None
                    with self.lock:
                        self.state.update(
                            tare=None,
                            stable=None,
                            delta=None,
                            update=None,
                            tare_requested=True,
                            tare_issue=None,
                        )
                        self._event("New reference tare requested.")
                elif action == "restart":
                    with self.lock:
                        if stabilizer.tare_result is None:
                            self._event("Capture a tare before restarting stability.")
                        else:
                            stabilizer.restart_stability_search()
                            self.state.update(stable=None, update=None)
                            self._event(
                                "Stability search restarted; baseline preserved."
                            )

                frame = sensor.read_frame(timeout=0.02)
                now = time.monotonic()
                with self.lock:
                    if acquiring and now >= deadline:
                        acquiring = False
                        candidate.clear()
                        self.state.update(
                            status="tare_required",
                            countdown=0,
                            tare_progress=0,
                            message="No steady contact yet. Try zeroing again.",
                            tare_issue=f"No steady baseline was captured within {TARE_TIMEOUT_SECONDS:g} seconds. Live pressure is still available.",
                        )
                        self._event("Tare timed out; no baseline accepted.")
                    elif acquiring:
                        self.state.update(
                            status="countdown" if now < tare_at else "taring",
                            countdown=max(0, math.ceil(tare_at - now)),
                            message="Rest one fingertip in the center and keep it still.",
                        )
                    if frame is None:
                        if action == "zero":
                            self.state["zero_issue"] = "No fresh contact. Keep one finger on the trackpad and press Zero again."
                        if not sensor.is_running():
                            raise RuntimeError(
                                "The sensor stopped. Start a new session to reconnect."
                            )
                        if now - last_frame_at > STALE_FRAME_SECONDS:
                            had_tare = stabilizer.tare_result is not None
                            stabilizer.reset()
                            readout.reset()
                            candidate.clear()
                            self.state.update(
                                raw=None,
                                live_reading=None,
                                reading_issue=None,
                                delta=None,
                                stable=None,
                                tare=None,
                                update=None,
                                contacts=[],
                                tare_progress=0,
                            )
                            if not acquiring:
                                self.state.update(
                                    status=(
                                        "tare_required"
                                        if self.state["tare_requested"]
                                        else "listening"
                                    ),
                                    message="Waiting for fresh pressure frames.",
                                )
                                if had_tare:
                                    self.state["tare_issue"] = (
                                        "The pressure stream stopped. Capture a new baseline for another calibration test."
                                    )
                        continue
                    last_frame_at = now
                    self.frame_count += 1
                    self.frames.append(asdict(frame))
                    self.state.update(
                        sequence=frame.sequence,
                        contacts=[
                            {
                                "x": c.normalized_x,
                                "y": c.normalized_y,
                                "path": c.path_index,
                                "state": c.state,
                                "pressure_raw": c.pressure_candidate_raw,
                            }
                            for c in frame.contacts
                        ],
                    )
                    reading = readout.process(frame)
                    if action == "zero":
                        try:
                            reading = readout.zero(zero_epoch)
                        except ValueError as error:
                            self.state["zero_issue"] = str(error)
                        else:
                            self.state["zero_issue"] = None
                            self._event("Display zero set for the current contact.")
                    raw = reading.pressure_raw if reading else None
                    self.state["raw"] = raw
                    self.state["live_reading"] = asdict(reading) if reading else None
                    self.state["reading_issue"] = readout.issue
                    if reading is not None:
                        self.state["last_reading"] = {
                            **asdict(reading),
                            "mode": mode,
                        }
                    if acquiring and now >= tare_at:
                        if (
                            len(frame.contacts) != 1
                            or frame.contacts[0].state != TargetTouchState.TOUCHING
                        ):
                            candidate.clear()
                        else:
                            candidate.append(frame)
                        self.state["tare_progress"] = len(candidate) / candidate.maxlen
                        if len(candidate) == candidate.maxlen:
                            try:
                                tare = stabilizer.tare(tuple(candidate))
                            except TareValidationError as error:
                                self.state["message"] = (
                                    "Keep resting gently. " + error.detail
                                )
                            else:
                                acquiring = False
                                self.state.update(
                                    tare=asdict(tare),
                                    status="filter_warmup",
                                    message="Zero is set. Gently increase pressure.",
                                    countdown=0,
                                )
                                self._event("Reference tare frozen.")
                                if mode == "demo":
                                    sensor.loaded_at = sensor.sequence
                    elif not acquiring and stabilizer.tare_result is not None:
                        update = stabilizer.process(frame)
                        self.state.update(
                            status=update.status.value,
                            message=update.reason,
                            update=asdict(update),
                            delta=update.smoothed_delta_raw,
                            tare=(
                                asdict(stabilizer.tare_result)
                                if stabilizer.tare_result
                                else None
                            ),
                        )
                        if update.published_measurement is not None:
                            self.state["stable"] = asdict(update.published_measurement)
                            self.state["last_capture"] = {
                                "measurement": asdict(update.published_measurement),
                                "mode": mode,
                                "time": time.strftime("%H:%M:%S"),
                            }
                            self._event(
                                f"Stable reading: {update.published_measurement.pressure_delta_raw:.2f} raw."
                            )
                        elif update.status.value != "monitoring":
                            self.state["stable"] = None
                        if stabilizer.tare_result is None:
                            issues = {
                                "tare_expired": f"The experimental baseline expired after {self.profile.config.maximum_tare_age_seconds:g} seconds.",
                                "contact_unsupported": "The finger was lifted or another contact was detected.",
                                "position_changed": "The finger moved away from the baseline position.",
                                "stream_discontinuity": "The pressure stream or contact changed.",
                            }
                            self.state["tare_issue"] = (
                                issues.get(update.status.value, update.reason)
                                + " Live pressure is still available; capture a new baseline only to repeat the calibration test."
                            )
                    if now - last_trace_at >= 0.1:
                        self.trace.append(
                            {
                                "t": round(now, 2),
                                "raw": raw,
                                "delta": self.state["delta"],
                            }
                        )
                        last_trace_at = now
                    if mode == "live" and now - last_stats_at >= 0.5:
                        self.state["transport"] = sensor.transport_stats().to_dict()
                        last_stats_at = now
        except Exception as error:
            with self.lock:
                self.state.update(error=str(error), status="error", message=str(error))
                self._event(str(error))
        finally:
            if sensor is not None:
                try:
                    sensor.close()
                except Exception as error:
                    with self.lock:
                        self.state.update(
                            error=str(error), status="error", message=str(error)
                        )
            with self.lock:
                self.state.update(
                    running=False,
                    stable=None,
                    raw=None,
                    live_reading=None,
                    reading_issue=None,
                    delta=None,
                    tare=None,
                    update=None,
                    contacts=[],
                    countdown=0,
                    tare_progress=0,
                )
                if self.state["status"] != "error":
                    self.state.update(
                        status="idle", message="Session stopped. Ready when you are."
                    )
                self._event("Sensor closed.")

    def diagnostic(self):
        with self.lock:
            return {
                "schema_version": 1,
                "kind": "gui_diagnostic_snapshot",
                "synthetic": self.state["mode"] == "demo",
                "public_weight_available": False,
                "estimated_weight_available": self.state["live_reading"] is not None,
                "estimate_basis": ESTIMATE_BASIS,
                "hardware_validated": False,
                "calibration_performed": False,
                "profile": asdict(self.profile),
                "state": self.snapshot(),
                "raw_frames": list(self.frames),
                "total_frames": self.frame_count,
                "frames_truncated": self.frame_count > len(self.frames),
                "scope": "Bounded diagnostic history, not Phase 5 known-mass evidence.",
            }


def assess_playground(payload):
    """User-entered exploratory math never passes through as physical evidence."""

    def series(role):
        rows = payload[role]
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Provide at most 100 mass levels per set.")
        return tuple(
            CalibrationSeries(row["mass"], tuple(row["readings"])) for row in rows
        )

    config = CalibrationConfig(**payload["criteria"])
    validation = fit_and_validate_calibration(
        series("training"), series("heldout"), config
    )
    return {
        "kind": "gui_calibration_playground",
        "status": "exploratory_math_only",
        "public_weight_available": False,
        "hardware_validated": False,
        "grams_claimed": False,
        "validation": asdict(validation),
        "inputs": payload,
    }


def assess_evidence(payload):
    criteria_path = Path(payload["criteria_path"]).expanduser().resolve()
    before = hashlib.sha256(criteria_path.read_bytes()).hexdigest()
    config = load_criteria(criteria_path)
    after = hashlib.sha256(criteria_path.read_bytes()).hexdigest()
    if before != after:
        raise ValueError("Criteria changed during assessment. Please try again.")
    report = assess_known_mass_manifest(
        Path(payload["manifest_path"]).expanduser(),
        config,
        expected_target=current_target_fingerprint().to_dict(),
        expected_phase4_profile_id=payload["profile_id"],
    )
    report.update(criteria_path=str(criteria_path), criteria_sha256=after)
    return report


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, controller, port=0):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), GuiHandler)


class GuiHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, value, status=200, content_type="application/json"):
        body = (
            json.dumps(value, allow_nan=False).encode()
            if content_type == "application/json"
            else value
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _local_request(self):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            self._send({"error": "Invalid local host."}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + expected:
            self._send({"error": "Only this local GUI can access the scale."}, 403)
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        if self.path == "/api/state":
            self._send(self.server.controller.snapshot())
        elif self.path == "/api/session":
            self._send({"token": self.server.token})
        elif self.path == "/api/diagnostic":
            self._send(self.server.controller.diagnostic())
        else:
            asset = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/view.js": ("view.js", "text/javascript; charset=utf-8"),
                "/scale_icon.png": ("scale_icon.png", "image/png"),
            }.get(self.path)
            if asset is None:
                self._send({"error": "Not found."}, 404)
                return
            body = (
                resources.files("trackpad_scale")
                .joinpath("gui_assets", asset[0])
                .read_bytes()
            )
            self._send(body, content_type=asset[1])

    def do_POST(self):
        if not self._local_request():
            return
        if not secrets.compare_digest(
            self.headers.get("X-Pebble-Token", ""), self.server.token
        ):
            self._send({"error": "Reload the page to reconnect."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 256_000:
                raise ValueError("Request must be between 1 and 256,000 bytes.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request must be a JSON object.")
            controller = self.server.controller
            if self.path == "/api/start":
                controller.start(payload.get("mode", "live"))
            elif self.path == "/api/stop":
                controller.stop()
            elif self.path == "/api/tare":
                controller.command("tare")
            elif self.path == "/api/zero":
                controller.command("zero")
            elif self.path == "/api/restart":
                controller.command("restart")
            elif self.path == "/api/calibration":
                self._send(assess_playground(payload))
                return
            elif self.path == "/api/assessment":
                self._send(assess_evidence(payload))
                return
            else:
                self._send({"error": "Not found."}, 404)
                return
            self._send(controller.snapshot())
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
        ) as error:
            self._send({"error": str(error)}, 400)


def main(argv: Optional[list] = None):
    parser = argparse.ArgumentParser(
        description="Open Pebble, the local trackpad scale GUI."
    )
    parser.add_argument(
        "--port", type=int, default=0, help="loopback port; default chooses a free port"
    )
    parser.add_argument(
        "--demo", action="store_true", help="select clearly labelled synthetic input"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the URL without opening a browser",
    )
    args = parser.parse_args(argv)
    controller = ScaleController(demo=args.demo)
    server = GuiServer(controller, args.port)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Pebble is ready at {url} — Ctrl+C to close.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
