"""ctypes binding for the project-owned bridge ABI, not Apple's private ABI."""

import ctypes
import os
import threading
from pathlib import Path
from typing import Dict, Optional

from .models import (
    CaptureStats,
    FrameMetadata,
    Phase2CaptureStats,
    RawTouch,
    RawTouchFrame,
)
from .target_profile import load_verified_profile, require_verified_target


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


_PHASE2_MAX_TOUCHES = 32


class _Phase2Touch(ctypes.Structure):
    _fields_ = [
        ("copied_fields", ctypes.c_uint32),
        ("path_index", ctypes.c_uint32),
        ("state", ctypes.c_uint32),
        ("finger_id", ctypes.c_uint32),
        ("hand_id", ctypes.c_int32),
        ("reserved0", ctypes.c_uint32),
        ("normalized_x", ctypes.c_float),
        ("normalized_y", ctypes.c_float),
        ("z_total", ctypes.c_float),
        ("pressure_candidate", ctypes.c_float),
        ("z_density", ctypes.c_float),
        ("normalized_x_bits", ctypes.c_uint32),
        ("normalized_y_bits", ctypes.c_uint32),
        ("z_total_bits", ctypes.c_uint32),
        ("pressure_candidate_bits", ctypes.c_uint32),
        ("z_density_bits", ctypes.c_uint32),
    ]


class _Phase2Frame(ctypes.Structure):
    _fields_ = [
        ("metadata", _FrameMetadata),
        ("layout_profile_id", ctypes.c_uint32),
        ("decode_status", ctypes.c_uint32),
        ("copied_touch_count", ctypes.c_uint32),
        ("reserved0", ctypes.c_uint32),
        ("touches", _Phase2Touch * _PHASE2_MAX_TOUCHES),
    ]


class _Phase2Stats(ctypes.Structure):
    _fields_ = [
        ("attempted_frame_count", ctypes.c_uint64),
        ("copied_touch_count", ctypes.c_uint64),
        ("queue_overwrite_count", ctypes.c_uint64),
        ("lock_contention_drop_count", ctypes.c_uint64),
        ("invalid_count_frame_count", ctypes.c_uint64),
        ("null_records_frame_count", ctypes.c_uint64),
        ("device_mismatch_frame_count", ctypes.c_uint64),
        ("record_frame_mismatch_touch_count", ctypes.c_uint64),
        ("record_timestamp_mismatch_touch_count", ctypes.c_uint64),
        ("invalid_state_touch_count", ctypes.c_uint64),
        ("pressure_sentinel_touch_count", ctypes.c_uint64),
        ("nonfinite_touch_count", ctypes.c_uint64),
        ("queue_depth", ctypes.c_uint64),
    ]


class _Phase2SourceLayout(ctypes.Structure):
    _fields_ = [
        ("descriptor_version", ctypes.c_uint32),
        ("profile_id", ctypes.c_uint32),
        ("record_size", ctypes.c_uint32),
        ("maximum_touch_count", ctypes.c_uint32),
        ("record_frame_offset", ctypes.c_uint32),
        ("record_frame_size", ctypes.c_uint32),
        ("record_timestamp_offset", ctypes.c_uint32),
        ("record_timestamp_size", ctypes.c_uint32),
        ("path_index_offset", ctypes.c_uint32),
        ("path_index_size", ctypes.c_uint32),
        ("state_offset", ctypes.c_uint32),
        ("state_size", ctypes.c_uint32),
        ("finger_id_offset", ctypes.c_uint32),
        ("finger_id_size", ctypes.c_uint32),
        ("hand_id_offset", ctypes.c_uint32),
        ("hand_id_size", ctypes.c_uint32),
        ("normalized_x_offset", ctypes.c_uint32),
        ("normalized_x_size", ctypes.c_uint32),
        ("normalized_y_offset", ctypes.c_uint32),
        ("normalized_y_size", ctypes.c_uint32),
        ("z_total_offset", ctypes.c_uint32),
        ("z_total_size", ctypes.c_uint32),
        ("pressure_candidate_offset", ctypes.c_uint32),
        ("pressure_candidate_size", ctypes.c_uint32),
        ("z_density_offset", ctypes.c_uint32),
        ("z_density_size", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 6),
    ]


def _phase2_output_layout_fingerprint() -> int:
    values = [
        ctypes.sizeof(_Phase2Touch),
        ctypes.alignment(_Phase2Touch),
        *[getattr(_Phase2Touch, name).offset for name, _ in _Phase2Touch._fields_ if name != "reserved0"],
        ctypes.sizeof(_Phase2Frame),
        ctypes.alignment(_Phase2Frame),
        _Phase2Frame.metadata.offset,
        _Phase2Frame.layout_profile_id.offset,
        _Phase2Frame.decode_status.offset,
        _Phase2Frame.copied_touch_count.offset,
        _Phase2Frame.touches.offset,
        ctypes.sizeof(_Phase2Stats),
        ctypes.alignment(_Phase2Stats),
        *[getattr(_Phase2Stats, name).offset for name, _ in _Phase2Stats._fields_],
    ]
    value = 14695981039346656037
    for item in values:
        for shift in range(0, 64, 8):
            value ^= (item >> shift) & 0xFF
            value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return value


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
        required_symbols = (
            "mt_phase1_bridge_abi_version",
            "mt_phase1_frame_metadata_size",
            "mt_phase1_capture_stats_size",
            "mt_phase1_framework_path",
            "mt_phase1_capture_create",
            "mt_phase1_capture_start",
            "mt_phase1_capture_stop",
            "mt_phase1_capture_destroy",
            "mt_phase1_capture_poll",
            "mt_phase1_capture_get_stats",
            "mt_phase1_capture_supports_force",
            "mt_phase1_capture_is_built_in",
            "mt_phase1_capture_is_running",
        )
        missing = [name for name in required_symbols if not hasattr(self._library, name)]
        if missing:
            raise Phase1BridgeError(
                "load project bridge",
                -1,
                "missing Phase 1 ABI symbols: " + ", ".join(missing),
            )
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


class NativePhase2Capture(NativePhase1Capture):
    """Exact-target rich capture; it never accepts caller-supplied offsets."""

    EXPECTED_PHASE2_ABI_VERSION = 1

    REQUIRED_PHASE2_SYMBOLS = (
        "mt_phase2_bridge_abi_version",
        "mt_phase2_touch_size",
        "mt_phase2_frame_size",
        "mt_phase2_capture_stats_size",
        "mt_phase2_source_layout_size",
        "mt_phase2_output_layout_fingerprint",
        "mt_phase2_verified_profile_name",
        "mt_phase2_get_source_layout",
        "mt_phase2_capture_enable_profile",
        "mt_phase2_capture_poll",
        "mt_phase2_capture_get_stats",
    )

    def __init__(self, library_path: Optional[Path] = None) -> None:
        resolved_path = (library_path or _default_library_path()).resolve()
        self._preflight_phase2_library(resolved_path)
        super().__init__(library_path=library_path, allow_unverified_target=False)
        try:
            self._configure_phase2_signatures()
            self._validate_and_enable_phase2()
        except Exception:
            self.close()
            raise

    @classmethod
    def _preflight_phase2_library(cls, library_path: Path) -> None:
        if not library_path.is_file():
            raise Phase1BridgeError(
                "load Phase 2 bridge",
                -1,
                f"{library_path} does not exist; run `make` first",
            )
        library = ctypes.CDLL(str(library_path))
        missing = [
            name for name in cls.REQUIRED_PHASE2_SYMBOLS if not hasattr(library, name)
        ]
        if missing:
            raise Phase1BridgeError(
                "load Phase 2 bridge",
                -1,
                "missing additive symbols: " + ", ".join(missing),
            )
        version_function = library.mt_phase2_bridge_abi_version
        version_function.argtypes = []
        version_function.restype = ctypes.c_uint32
        version = int(version_function())
        if version != cls.EXPECTED_PHASE2_ABI_VERSION:
            raise Phase1BridgeError(
                "validate Phase 2 bridge ABI",
                -1,
                f"expected {cls.EXPECTED_PHASE2_ABI_VERSION}, got {version}",
            )

    def _configure_phase2_signatures(self) -> None:
        lib = self._library
        missing = [
            name for name in self.REQUIRED_PHASE2_SYMBOLS if not hasattr(lib, name)
        ]
        if missing:
            raise Phase1BridgeError(
                "load Phase 2 bridge",
                -1,
                "missing additive symbols: " + ", ".join(missing),
            )

        lib.mt_phase2_bridge_abi_version.argtypes = []
        lib.mt_phase2_bridge_abi_version.restype = ctypes.c_uint32
        lib.mt_phase2_touch_size.argtypes = []
        lib.mt_phase2_touch_size.restype = ctypes.c_size_t
        lib.mt_phase2_frame_size.argtypes = []
        lib.mt_phase2_frame_size.restype = ctypes.c_size_t
        lib.mt_phase2_capture_stats_size.argtypes = []
        lib.mt_phase2_capture_stats_size.restype = ctypes.c_size_t
        lib.mt_phase2_source_layout_size.argtypes = []
        lib.mt_phase2_source_layout_size.restype = ctypes.c_size_t
        lib.mt_phase2_output_layout_fingerprint.argtypes = []
        lib.mt_phase2_output_layout_fingerprint.restype = ctypes.c_uint64
        lib.mt_phase2_verified_profile_name.argtypes = []
        lib.mt_phase2_verified_profile_name.restype = ctypes.c_char_p
        lib.mt_phase2_get_source_layout.argtypes = [
            ctypes.POINTER(_Phase2SourceLayout)
        ]
        lib.mt_phase2_get_source_layout.restype = ctypes.c_int32
        lib.mt_phase2_capture_enable_profile.argtypes = [
            _CapturePointer,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.mt_phase2_capture_enable_profile.restype = ctypes.c_int32
        lib.mt_phase2_capture_poll.argtypes = [
            _CapturePointer,
            ctypes.POINTER(_Phase2Frame),
        ]
        lib.mt_phase2_capture_poll.restype = ctypes.c_int32
        lib.mt_phase2_capture_get_stats.argtypes = [
            _CapturePointer,
            ctypes.POINTER(_Phase2Stats),
        ]
        lib.mt_phase2_capture_get_stats.restype = ctypes.c_int32

    def _validate_and_enable_phase2(self) -> None:
        lib = self._library
        version = int(lib.mt_phase2_bridge_abi_version())
        if version != self.EXPECTED_PHASE2_ABI_VERSION:
            raise Phase1BridgeError(
                "validate Phase 2 bridge ABI",
                -1,
                f"expected {self.EXPECTED_PHASE2_ABI_VERSION}, got {version}",
            )

        observed_sizes = (
            int(lib.mt_phase2_touch_size()),
            int(lib.mt_phase2_frame_size()),
            int(lib.mt_phase2_capture_stats_size()),
            int(lib.mt_phase2_source_layout_size()),
        )
        expected_sizes = (
            ctypes.sizeof(_Phase2Touch),
            ctypes.sizeof(_Phase2Frame),
            ctypes.sizeof(_Phase2Stats),
            ctypes.sizeof(_Phase2SourceLayout),
        )
        if observed_sizes != expected_sizes:
            raise Phase1BridgeError(
                "validate Phase 2 project-owned struct sizes",
                -1,
                f"native={observed_sizes}, Python={expected_sizes}",
            )

        native_fingerprint = int(lib.mt_phase2_output_layout_fingerprint())
        python_fingerprint = _phase2_output_layout_fingerprint()
        if native_fingerprint != python_fingerprint:
            raise Phase1BridgeError(
                "validate Phase 2 project-owned layout fingerprint",
                -1,
                (
                    f"native=0x{native_fingerprint:016x}, "
                    f"Python=0x{python_fingerprint:016x}"
                ),
            )

        raw_layout = _Phase2SourceLayout()
        status = int(lib.mt_phase2_get_source_layout(ctypes.byref(raw_layout)))
        if status != 0:
            raise Phase1BridgeError("read Phase 2 source layout", status)

        profile = load_verified_profile()["phase2_source_layout"]
        profile_name = lib.mt_phase2_verified_profile_name().decode("ascii")
        if profile_name != profile["profile_name"]:
            raise Phase1BridgeError(
                "validate Phase 2 profile name",
                -1,
                f"native={profile_name!r}, profile={profile['profile_name']!r}",
            )
        self._validate_source_layout(raw_layout, profile)
        self._phase2_profile_name = profile_name
        self._phase2_source_layout = self._source_layout_dict(raw_layout)
        self._phase2_profile_id = int(profile["profile_id"])
        self._phase2_abi_evidence = {
            "bridge_abi_version": version,
            "native_project_owned_struct_sizes": list(observed_sizes),
            "python_project_owned_struct_sizes": list(expected_sizes),
            "output_layout_fingerprint": f"0x{native_fingerprint:016x}",
            "profile_id": self._phase2_profile_id,
            "profile_name": profile_name,
            "source_layout": dict(self._phase2_source_layout),
        }

        error = ctypes.create_string_buffer(self.ERROR_BUFFER_SIZE)
        status = int(
            lib.mt_phase2_capture_enable_profile(
                self._capture,
                int(profile["profile_id"]),
                error,
                len(error),
            )
        )
        if status != 0:
            raise Phase1BridgeError(
                "enable verified Phase 2 profile", status, self._decode(error)
            )

    @staticmethod
    def _source_layout_dict(raw: _Phase2SourceLayout) -> Dict[str, int]:
        return {
            name: int(getattr(raw, name))
            for name, _ in raw._fields_
            if name != "reserved"
        }

    @classmethod
    def _validate_source_layout(
        cls, raw: _Phase2SourceLayout, profile: Dict[str, object]
    ) -> None:
        top_level = {
            "descriptor_version": int(profile["descriptor_version"]),
            "profile_id": int(profile["profile_id"]),
            "record_size": int(profile["record_size"]),
            "maximum_touch_count": int(profile["maximum_touch_count"]),
        }
        for name, expected in top_level.items():
            observed = int(getattr(raw, name))
            if observed != expected:
                raise Phase1BridgeError(
                    "validate Phase 2 source layout",
                    -1,
                    f"{name}: expected {expected}, got {observed}",
                )

        fields = profile["fields"]
        for name, expected in fields.items():
            for attribute in ("offset", "size"):
                native_name = f"{name}_{attribute}"
                observed = int(getattr(raw, native_name))
                wanted = int(expected[attribute])
                if observed != wanted:
                    raise Phase1BridgeError(
                        "validate Phase 2 source layout",
                        -1,
                        f"{native_name}: expected {wanted}, got {observed}",
                    )

    @property
    def phase2_profile_name(self) -> str:
        return self._phase2_profile_name

    @property
    def phase2_source_layout(self) -> Dict[str, int]:
        return dict(self._phase2_source_layout)

    @property
    def phase2_profile_id(self) -> int:
        return self._phase2_profile_id

    @property
    def phase2_abi_evidence(self) -> Dict[str, object]:
        evidence = dict(self._phase2_abi_evidence)
        evidence["source_layout"] = dict(self._phase2_source_layout)
        return evidence

    def poll_touch_frame(self) -> Optional[RawTouchFrame]:
        with self._lock:
            raw = _Phase2Frame()
            status = int(
                self._library.mt_phase2_capture_poll(self._capture, ctypes.byref(raw))
            )
            if status < 0:
                raise Phase1BridgeError("poll Phase 2 capture", status)
            if status == 0:
                return None

            copied_touch_count = int(raw.copied_touch_count)
            if copied_touch_count > _PHASE2_MAX_TOUCHES:
                raise Phase1BridgeError(
                    "validate Phase 2 copied touch count",
                    -1,
                    (
                        f"native returned {copied_touch_count}, maximum is "
                        f"{_PHASE2_MAX_TOUCHES}"
                    ),
                )
            observed_profile_id = int(raw.layout_profile_id)
            if observed_profile_id != self._phase2_profile_id:
                raise Phase1BridgeError(
                    "validate Phase 2 frame profile",
                    -1,
                    (
                        f"expected {self._phase2_profile_id}, got "
                        f"{observed_profile_id}; touch scalars were quarantined"
                    ),
                )

            metadata = FrameMetadata(
                sequence=int(raw.metadata.sequence),
                raw_touch_count_register=int(
                    raw.metadata.raw_touch_count_register
                ),
                raw_frame_register=int(raw.metadata.raw_frame_register),
                device_timestamp=float(raw.metadata.device_timestamp),
                host_monotonic_ns=int(raw.metadata.host_monotonic_ns),
            )
            touches = tuple(
                RawTouch(
                    copied_fields=int(touch.copied_fields),
                    path_index=int(touch.path_index),
                    state=int(touch.state),
                    finger_id=int(touch.finger_id),
                    hand_id=int(touch.hand_id),
                    normalized_x=float(touch.normalized_x),
                    normalized_y=float(touch.normalized_y),
                    z_total=float(touch.z_total),
                    pressure_candidate=float(touch.pressure_candidate),
                    z_density=float(touch.z_density),
                    normalized_x_bits=int(touch.normalized_x_bits),
                    normalized_y_bits=int(touch.normalized_y_bits),
                    z_total_bits=int(touch.z_total_bits),
                    pressure_candidate_bits=int(touch.pressure_candidate_bits),
                    z_density_bits=int(touch.z_density_bits),
                )
                for touch in raw.touches[:copied_touch_count]
            )
            return RawTouchFrame(
                metadata=metadata,
                layout_profile_id=observed_profile_id,
                decode_status=int(raw.decode_status),
                copied_touch_count=copied_touch_count,
                touches=touches,
            )

    def phase2_stats(self) -> Phase2CaptureStats:
        with self._lock:
            raw = _Phase2Stats()
            status = int(
                self._library.mt_phase2_capture_get_stats(
                    self._capture, ctypes.byref(raw)
                )
            )
            if status != 0:
                raise Phase1BridgeError("read Phase 2 capture stats", status)
            return Phase2CaptureStats(
                **{
                    name: int(getattr(raw, name))
                    for name, _ in raw._fields_
                }
            )
