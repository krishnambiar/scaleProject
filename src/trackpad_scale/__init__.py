"""Clean-room trackpad transport through the uncalibrated Phase 3 boundary."""

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
from .sensor import TrackpadSensor

__all__ = [
    "CaptureStats",
    "FrameMetadata",
    "Phase2CaptureStats",
    "RawContact",
    "RawFrame",
    "RawFrameSensor",
    "RawFrameValidationError",
    "RawTouch",
    "RawTouchFrame",
    "TouchDiagnosticSensor",
    "TrackpadSensor",
    "raw_frame_from_transport",
]
