"""ctypes binding for the project-owned bridge ABI, not Apple's private ABI."""

import ctypes
import os
import threading
from pathlib import Path
from typing import Optional

from .models import CaptureStats, FrameMetadata
from .target_profile import require_verified_target


class Phase1BridgeError(RuntimeError):
    def __init__(self, operation: str, status: int, detail: str = "") -> None:
        message = f"{operation} failed with bridge status {status}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.operation = operation
        self.status = status
        self.detail = detail


class _Capture(ctypes.Structure):
    pass


_CapturePointer = ctypes.POINTER(_Capture)


class _FrameMetadata(ctypes.Structure):
    _fields_ = [
        ("sequence", ctypes.c_uint64),
        ("raw_touch_count_register", ctypes.c_uint64),
        ("raw_frame_register", ctypes.c_uint64),
        ("device_timestamp", ctypes.c_double),
        ("host_monotonic_ns", ctypes.c_uint64),
    ]


class _CaptureStats(ctypes.Structure):
    _fields_ = [
        ("callback_count", ctypes.c_uint64),
        ("enqueued_count", ctypes.c_uint64),
        ("queue_overwrite_count", ctypes.c_uint64),
        ("lock_contention_drop_count", ctypes.c_uint64),
        ("callback_device_mismatch_count", ctypes.c_uint64),
        ("late_callback_count", ctypes.c_uint64),
        ("in_flight_callback_count", ctypes.c_uint64),
        ("queue_depth", ctypes.c_uint64),
    ]


def _default_library_path() -> Path:
    override = os.environ.get("MT_PHASE1_LIBRARY")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "build" / "libmt_phase1.dylib"


class NativePhase1Capture:
    """Owns one native device capture and exposes only copied metadata."""

    ERROR_BUFFER_SIZE = 1024
    EXPECTED_ABI_VERSION = 2

    def __init__(
        self,
        library_path: Optional[Path] = None,
        allow_unverified_target: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self._allow_unverified_target = allow_unverified_target
        require_verified_target(allow_unverified_target=allow_unverified_target)
        self.library_path = (library_path or _default_library_path()).resolve()
        if not self.library_path.is_file():
            raise Phase1BridgeError(
                "load project bridge",
                -1,
                f"{self.library_path} does not exist; run `make` first",
            )
        self._library = ctypes.CDLL(str(self.library_path))
        self._configure_signatures()

        version = int(self._library.mt_phase1_bridge_abi_version())
        if version != self.EXPECTED_ABI_VERSION:
            raise Phase1BridgeError(
                "validate project bridge ABI",
                -1,
                f"expected {self.EXPECTED_ABI_VERSION}, got {version}",
            )
        native_frame_size = int(self._library.mt_phase1_frame_metadata_size())
        python_frame_size = ctypes.sizeof(_FrameMetadata)
        native_stats_size = int(self._library.mt_phase1_capture_stats_size())
        python_stats_size = ctypes.sizeof(_CaptureStats)
        if (native_frame_size, native_stats_size) != (
            python_frame_size,
            python_stats_size,
        ):
            raise Phase1BridgeError(
                "validate project bridge struct sizes",
                -1,
                (
                    f"native frame/stats={native_frame_size}/{native_stats_size}, "
                    f"Python frame/stats={python_frame_size}/{python_stats_size}"
                ),
            )

        capture = _CapturePointer()
        error = ctypes.create_string_buffer(self.ERROR_BUFFER_SIZE)
        status = int(
            self._library.mt_phase1_capture_create(
                ctypes.byref(capture), error, len(error)
            )
        )
        if status != 0:
            raise Phase1BridgeError("create capture", status, self._decode(error))
        self._capture = capture
        self.last_start_native_status: Optional[int] = None
        self.last_stop_native_status: Optional[int] = None

    def _configure_signatures(self) -> None:
        lib = self._library
        lib.mt_phase1_bridge_abi_version.argtypes = []
        lib.mt_phase1_bridge_abi_version.restype = ctypes.c_uint32
        lib.mt_phase1_frame_metadata_size.argtypes = []
        lib.mt_phase1_frame_metadata_size.restype = ctypes.c_size_t
        lib.mt_phase1_capture_stats_size.argtypes = []
        lib.mt_phase1_capture_stats_size.restype = ctypes.c_size_t
        lib.mt_phase1_framework_path.argtypes = []
        lib.mt_phase1_framework_path.restype = ctypes.c_char_p
        lib.mt_phase1_capture_create.argtypes = [
            ctypes.POINTER(_CapturePointer),
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.mt_phase1_capture_create.restype = ctypes.c_int32
        lib.mt_phase1_capture_start.argtypes = [
            _CapturePointer,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.mt_phase1_capture_start.restype = ctypes.c_int32
        lib.mt_phase1_capture_stop.argtypes = [
            _CapturePointer,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.mt_phase1_capture_stop.restype = ctypes.c_int32
        lib.mt_phase1_capture_destroy.argtypes = [
            _CapturePointer,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.mt_phase1_capture_destroy.restype = ctypes.c_int32
        lib.mt_phase1_capture_poll.argtypes = [
            _CapturePointer,
            ctypes.POINTER(_FrameMetadata),
        ]
        lib.mt_phase1_capture_poll.restype = ctypes.c_int32
        lib.mt_phase1_capture_get_stats.argtypes = [
            _CapturePointer,
            ctypes.POINTER(_CaptureStats),
        ]
        lib.mt_phase1_capture_get_stats.restype = ctypes.c_int32
        lib.mt_phase1_capture_supports_force.argtypes = [_CapturePointer]
        lib.mt_phase1_capture_supports_force.restype = ctypes.c_int32
        lib.mt_phase1_capture_is_built_in.argtypes = [_CapturePointer]
        lib.mt_phase1_capture_is_built_in.restype = ctypes.c_int32
        lib.mt_phase1_capture_is_running.argtypes = [_CapturePointer]
        lib.mt_phase1_capture_is_running.restype = ctypes.c_int32

    @staticmethod
    def _decode(buffer: ctypes.Array) -> str:
        return bytes(buffer.value).decode("utf-8", errors="replace")

    @property
    def framework_path(self) -> str:
        with self._lock:
            value = self._library.mt_phase1_framework_path()
            return value.decode("utf-8")

    @property
    def supports_force(self) -> Optional[bool]:
        with self._lock:
            value = int(self._library.mt_phase1_capture_supports_force(self._capture))
            return None if value < 0 else bool(value)

    @property
    def is_built_in(self) -> Optional[bool]:
        with self._lock:
            value = int(self._library.mt_phase1_capture_is_built_in(self._capture))
            return None if value < 0 else bool(value)

    @property
    def is_running(self) -> bool:
        with self._lock:
            capture = getattr(self, "_capture", None)
            if not capture:
                return False
            return bool(self._library.mt_phase1_capture_is_running(capture))

    def start(self, start_options: int = 0) -> None:
        if not 0 <= start_options <= 0xFFFFFFFF:
            raise ValueError("start_options must fit in an unsigned 32-bit register")
        if start_options != 0 and not self._allow_unverified_target:
            raise ValueError(
                "only MTDeviceStart option value 0 is verified; nonzero values "
                "require the diagnostic-only unverified-target override"
            )
        with self._lock:
            native_status = ctypes.c_int32()
            error = ctypes.create_string_buffer(self.ERROR_BUFFER_SIZE)
            status = int(
                self._library.mt_phase1_capture_start(
                    self._capture,
                    start_options,
                    ctypes.byref(native_status),
                    error,
                    len(error),
                )
            )
            self.last_start_native_status = int(native_status.value)
            if status != 0:
                raise Phase1BridgeError("start capture", status, self._decode(error))

    def stop(self) -> None:
        with self._lock:
            native_status = ctypes.c_int32()
            error = ctypes.create_string_buffer(self.ERROR_BUFFER_SIZE)
            status = int(
                self._library.mt_phase1_capture_stop(
                    self._capture,
                    ctypes.byref(native_status),
                    error,
                    len(error),
                )
            )
            self.last_stop_native_status = int(native_status.value)
            if status != 0:
                raise Phase1BridgeError("stop capture", status, self._decode(error))

    def poll(self) -> Optional[FrameMetadata]:
        with self._lock:
            capture = getattr(self, "_capture", None)
            if not capture:
                return None
            raw = _FrameMetadata()
            status = int(self._library.mt_phase1_capture_poll(capture, raw))
            if status < 0:
                raise Phase1BridgeError("poll capture", status)
            if status == 0:
                return None
            return FrameMetadata(
                sequence=int(raw.sequence),
                raw_touch_count_register=int(raw.raw_touch_count_register),
                raw_frame_register=int(raw.raw_frame_register),
                device_timestamp=float(raw.device_timestamp),
                host_monotonic_ns=int(raw.host_monotonic_ns),
            )

    def drain(self) -> None:
        while self.poll() is not None:
            pass

    def stats(self) -> CaptureStats:
        with self._lock:
            raw = _CaptureStats()
            status = int(
                self._library.mt_phase1_capture_get_stats(
                    self._capture, ctypes.byref(raw)
                )
            )
            if status != 0:
                raise Phase1BridgeError("read capture stats", status)
            return CaptureStats(
                callback_count=int(raw.callback_count),
                enqueued_count=int(raw.enqueued_count),
                queue_overwrite_count=int(raw.queue_overwrite_count),
                lock_contention_drop_count=int(raw.lock_contention_drop_count),
                callback_device_mismatch_count=int(raw.callback_device_mismatch_count),
                late_callback_count=int(raw.late_callback_count),
                in_flight_callback_count=int(raw.in_flight_callback_count),
                queue_depth=int(raw.queue_depth),
            )

    def close(self) -> None:
        with self._lock:
            capture = getattr(self, "_capture", None)
            if not capture:
                return
            if self.is_running:
                self.stop()
            error = ctypes.create_string_buffer(self.ERROR_BUFFER_SIZE)
            status = int(
                self._library.mt_phase1_capture_destroy(capture, error, len(error))
            )
            if status != 0:
                raise Phase1BridgeError("destroy capture", status, self._decode(error))
            self._capture = _CapturePointer()

    def __enter__(self) -> "NativePhase1Capture":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
