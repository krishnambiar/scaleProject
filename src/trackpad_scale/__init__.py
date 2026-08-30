"""Clean-room MacBook trackpad scale work, currently limited to Phase 1."""

from .models import CaptureStats, FrameMetadata
from .sensor import TrackpadSensor

__all__ = ["CaptureStats", "FrameMetadata", "TrackpadSensor"]

