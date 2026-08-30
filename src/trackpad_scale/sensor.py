"""Phase 1 sensor facade; future hydration code can depend on this boundary."""

import time
from typing import Optional

from .models import CaptureStats, FrameMetadata
from .native_bridge import NativePhase1Capture


class TrackpadSensor:
    """Reads copied frame metadata without exposing Apple private objects."""

    def __init__(self, allow_unverified_target: bool = False) -> None:
        # The application-facing facade refuses to inherit private-ABI assumptions
        # across an OS, framework, architecture, or hardware change.
        self._native = NativePhase1Capture(
            allow_unverified_target=allow_unverified_target
        )

    def start(self, start_options: int = 0) -> None:
        self._native.start(start_options=start_options)

    def stop(self) -> None:
        self._native.stop()

    def read_frame(self, timeout: Optional[float] = None) -> Optional[FrameMetadata]:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            frame = self._native.poll()
            if frame is not None:
                return frame
            if not self.is_running():
                return None
            if deadline is not None and time.monotonic() >= deadline:
                return None
            if timeout == 0:
                return None
            time.sleep(0.001)

    def drain(self) -> None:
        self._native.drain()

    def stats(self) -> CaptureStats:
        return self._native.stats()

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
    def last_start_native_status(self) -> Optional[int]:
        return self._native.last_start_native_status

    @property
    def last_stop_native_status(self) -> Optional[int]:
        return self._native.last_stop_native_status

    def close(self) -> None:
        self._native.close()

    def __enter__(self) -> "TrackpadSensor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
