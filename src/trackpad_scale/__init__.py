"""Clean-room trackpad transport and experimental raw-domain stabilization."""

from .models import (
    CaptureStats,
    FrameMetadata,
    Phase2CaptureStats,
    RawContact,
    RawFrame,
    RawTouch,
    RawTouchFrame,
)
from .phase2_sensor import TouchDiagnosticSensor
from .phase3_sensor import (
    RawFrameSensor,
    RawFrameValidationError,
    raw_frame_from_transport,
)
from .phase4_stabilizer import (
    PressureStabilizer,
    StabilizationStatus,
    StabilizationUpdate,
    StabilizerConfig,
    StabilityConfidence,
    StablePressureMeasurement,
    TareResult,
    TareValidationError,
)
from .sensor import TrackpadSensor

__all__ = [
    "CaptureStats",
    "FrameMetadata",
    "Phase2CaptureStats",
    "PressureStabilizer",
    "RawContact",
    "RawFrame",
    "RawFrameSensor",
    "RawFrameValidationError",
    "RawTouch",
    "RawTouchFrame",
    "StabilizationStatus",
    "StabilizationUpdate",
    "StabilizerConfig",
    "StabilityConfidence",
    "StablePressureMeasurement",
    "TareResult",
    "TareValidationError",
    "TouchDiagnosticSensor",
    "TrackpadSensor",
    "raw_frame_from_transport",
]
