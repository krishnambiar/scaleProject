"""Diagnostic-only Phase 2 sensor boundary.

The future hydration application can remain coupled to public, project-owned
models.  All private-framework ownership and target-specific decoding stays in
the native bridge behind this facade.
"""

import time
from typing import Dict, Optional

from .models import CaptureStats, Phase2CaptureStats, RawTouchFrame
from .native_bridge import NativePhase2Capture
from .sensor import _validate_timeout


class TouchDiagnosticSensor:
    """Reads copied raw touch scalars from the one exact verified target."""

    def __init__(self) -> None:
        self._native = NativePhase2Capture()

    def start(self) -> None:
        # Phase 2 intentionally exposes no option parameter: zero is the only
        # start value admitted by the verified profile.
        self._native.start(start_options=0)

    def stop(self) -> None:
        self._native.stop()

    def read_frame(self, timeout: Optional[float] = None) -> Optional[RawTouchFrame]:
        _validate_timeout(timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        first_poll = True
        while True:
            if (
                not first_poll
                and deadline is not None
                and time.monotonic() >= deadline
            ):
                return None
            first_poll = False
            frame = self._native.poll_touch_frame()
            if frame is not None:
                # The legacy Phase 1 queue remains additive and receives the
                # same callbacks. Keep it drained so a long Phase 2 experiment
                # does not manufacture Phase 1 queue-overwrite noise.
                self._native.poll()
                return frame
            if not self.is_running():
                return None
            if deadline is None:
                time.sleep(0.001)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.001, remaining))

    def drain_phase1_metadata(self) -> None:
        while self._native.poll() is not None:
            pass

    def capture_stats(self) -> CaptureStats:
        return self._native.stats()

    def phase2_stats(self) -> Phase2CaptureStats:
        return self._native.phase2_stats()

    def supports_force(self) -> Optional[bool]:
        return self._native.supports_force

    def is_built_in(self) -> Optional[bool]:
        return self._native.is_built_in

    def is_running(self) -> bool:
        return self._native.is_running

    @property
    def framework_path(self) -> str:
        return self._native.framework_path

    @property
    def profile_name(self) -> str:
        return self._native.phase2_profile_name

    @property
    def source_layout(self) -> Dict[str, int]:
        return self._native.phase2_source_layout

    @property
    def profile_id(self) -> int:
        return self._native.phase2_profile_id

    @property
    def abi_evidence(self) -> Dict[str, object]:
        return self._native.phase2_abi_evidence

    @property
    def last_start_native_status(self) -> Optional[int]:
        return self._native.last_start_native_status

    @property
    def last_stop_native_status(self) -> Optional[int]:
        return self._native.last_stop_native_status

    def close(self) -> None:
        self._native.close()

    def __enter__(self) -> "TouchDiagnosticSensor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
