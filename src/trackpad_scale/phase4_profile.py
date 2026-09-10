"""Fail-closed loader for the exact-target Phase 4 experiment profile.

The bundled profile is an auditable starting point for replay and further
pressure-domain experiments.  Loading it does not validate the thresholds,
calibrate the pressure candidate, or assign a physical unit.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from importlib import resources
from typing import Any, Tuple

from .phase4_stabilizer import StabilizerConfig


PHASE4_PROFILE_SCHEMA_VERSION = 1
PHASE4_EXPERIMENTAL_PROFILE_ID = (
    "mac16_8-macos_25D771280a-mts_9430_5-"
    "uuid_40D691BB916631E0959E351863FF09A0-phase4-experimental-v1"
)
PHASE4_EXPERIMENTAL_STATUS = "experimental_unvalidated"
PHASE4_EXACT_TARGET_SCOPE = "exact_target_only"
PHASE4_RAW_UNITS = "arbitrary raw sensor coordinate; not grams"
PHASE4_SOURCE_ARTIFACT_SHA256 = (
    "370c03f642b71b2e4d99274f2f56ba84ce26de13aa5143b8a7c687220a7bf3d0"
)

_PROFILE_FILENAME = "exact_target_experimental_v1.json"

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "profile_id",
        "status",
        "scope",
        "units",
        "calibration_performed",
        "source_phase2_artifact",
        "config",
        "rationale",
        "caveats",
    }
)
_SOURCE_KEYS = frozenset({"sha256", "target", "replay"})
_TARGET_KEYS = frozenset(
    {
        "architecture",
        "framework_bundle_version",
        "framework_image_uuid",
        "hardware_model",
        "kernel_osversion",
        "os_build",
    }
)
_REPLAY_KEYS = frozenset(
    {
        "tare_cycle",
        "tare_stage",
        "tare_period",
        "tare_trailing_sample_count",
        "tare_first_sequence",
        "tare_last_sequence",
        "tare_first_frame_number",
        "tare_last_frame_number",
        "continuation_first_sequence",
        "continuation_last_sequence",
        "pressure_cycle",
        "pressure_stage",
        "pressure_period",
        "pressure_first_sequence",
        "pressure_last_sequence",
        "expected_tare_baseline_raw",
        "expected_tare_mad_raw",
        "expected_tare_max_absolute_deviation_raw",
        "expected_tare_slope_raw_per_second",
        "expected_publication_sequence",
        "expected_publication_frame_number",
        "expected_published_delta_raw",
    }
)
_CONFIG_KEYS = frozenset(field.name for field in fields(StabilizerConfig))
_CONFIG_INTEGER_KEYS = frozenset(
    {
        "tare_window_samples",
        "median_window_samples",
        "smoothing_window_samples",
        "outlier_window_samples",
        "stability_min_samples",
        "stable_windows_required",
    }
)

_EXPECTED_TARGET = {
    "architecture": "arm64",
    "framework_bundle_version": "9430.5",
    "framework_image_uuid": "40D691BB-9166-31E0-959E-351863FF09A0",
    "hardware_model": "Mac16,8",
    "kernel_osversion": "25D2128",
    "os_build": "25D771280a",
}


class Phase4ProfileError(ValueError):
    """A profile failed schema, provenance, status, or configuration checks."""


@dataclass(frozen=True)
class ExactTarget:
    architecture: str
    framework_bundle_version: str
    framework_image_uuid: str
    hardware_model: str
    kernel_osversion: str
    os_build: str


@dataclass(frozen=True)
class ReplayEvidence:
    tare_cycle: int
    tare_stage: str
    tare_period: str
    tare_trailing_sample_count: int
    tare_first_sequence: int
    tare_last_sequence: int
    tare_first_frame_number: int
    tare_last_frame_number: int
    continuation_first_sequence: int
    continuation_last_sequence: int
    pressure_cycle: int
    pressure_stage: str
    pressure_period: str
    pressure_first_sequence: int
    pressure_last_sequence: int
    expected_tare_baseline_raw: float
    expected_tare_mad_raw: float
    expected_tare_max_absolute_deviation_raw: float
    expected_tare_slope_raw_per_second: float
    expected_publication_sequence: int
    expected_publication_frame_number: int
    expected_published_delta_raw: float


@dataclass(frozen=True)
class Phase2ArtifactEvidence:
    sha256: str
    target: ExactTarget
    replay: ReplayEvidence


@dataclass(frozen=True)
class ExperimentalPhase4Profile:
    schema_version: int
    profile_id: str
    status: str
    scope: str
    units: str
    calibration_performed: bool
    source_phase2_artifact: Phase2ArtifactEvidence
    config: StabilizerConfig
    rationale: Tuple[str, ...]
    caveats: Tuple[str, ...]


def _fail(context: str, detail: str) -> None:
    raise Phase4ProfileError(f"{context}: {detail}")


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(context, "must be an object")
    if not all(type(key) is str for key in value):
        _fail(context, "all keys must be strings")
    return value


def _exact_keys(value: Mapping[str, object], expected: frozenset, context: str) -> None:
    observed = frozenset(value)
    missing = sorted(expected - observed)
    unknown = sorted(observed - expected)
    if missing or unknown:
        _fail(context, f"missing keys={missing}; unknown keys={unknown}")


def _string(value: object, context: str) -> str:
    if type(value) is not str or not value:
        _fail(context, "must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        _fail(context, "must be an integer")
    return value


def _number(value: object, context: str) -> float:
    if type(value) not in (int, float):
        _fail(context, "must be a number, not a boolean or string")
    result = float(value)
    if not math.isfinite(result):
        _fail(context, "must be finite")
    return result


def _string_tuple(value: object, context: str) -> Tuple[str, ...]:
    if type(value) is not list or not value:
        _fail(context, "must be a non-empty array")
    return tuple(
        _string(item, f"{context}[{index}]") for index, item in enumerate(value)
    )


def _parse_target(value: object) -> ExactTarget:
    payload = _mapping(value, "source_phase2_artifact.target")
    _exact_keys(payload, _TARGET_KEYS, "source_phase2_artifact.target")
    observed = {
        key: _string(payload[key], f"source_phase2_artifact.target.{key}")
        for key in sorted(_TARGET_KEYS)
    }
    if observed != _EXPECTED_TARGET:
        _fail(
            "source_phase2_artifact.target",
            "does not equal the exact target bound to this profile",
        )
    return ExactTarget(**observed)


def _parse_replay(value: object) -> ReplayEvidence:
    payload = _mapping(value, "source_phase2_artifact.replay")
    _exact_keys(payload, _REPLAY_KEYS, "source_phase2_artifact.replay")
    integer_names = {
        "tare_cycle",
        "tare_trailing_sample_count",
        "tare_first_sequence",
        "tare_last_sequence",
        "tare_first_frame_number",
        "tare_last_frame_number",
        "continuation_first_sequence",
        "continuation_last_sequence",
        "pressure_cycle",
        "pressure_first_sequence",
        "pressure_last_sequence",
        "expected_publication_sequence",
        "expected_publication_frame_number",
    }
    string_names = {
        "tare_stage",
        "tare_period",
        "pressure_stage",
        "pressure_period",
    }
    values = {}
    for name in integer_names:
        values[name] = _integer(payload[name], f"source_phase2_artifact.replay.{name}")
    for name in string_names:
        values[name] = _string(payload[name], f"source_phase2_artifact.replay.{name}")
    for name in _REPLAY_KEYS - integer_names - string_names:
        values[name] = _number(payload[name], f"source_phase2_artifact.replay.{name}")
    return ReplayEvidence(**values)


def _parse_source_evidence(value: object) -> Phase2ArtifactEvidence:
    payload = _mapping(value, "source_phase2_artifact")
    _exact_keys(payload, _SOURCE_KEYS, "source_phase2_artifact")
    sha256 = _string(payload["sha256"], "source_phase2_artifact.sha256")
    if sha256 != PHASE4_SOURCE_ARTIFACT_SHA256:
        _fail(
            "source_phase2_artifact.sha256",
            "does not equal the immutable Phase 2 evidence digest",
        )
    return Phase2ArtifactEvidence(
        sha256=sha256,
        target=_parse_target(payload["target"]),
        replay=_parse_replay(payload["replay"]),
    )


def _parse_config(value: object) -> StabilizerConfig:
    payload = _mapping(value, "config")
    _exact_keys(payload, _CONFIG_KEYS, "config")
    values = {}
    for name in _CONFIG_KEYS:
        raw = payload[name]
        if name in _CONFIG_INTEGER_KEYS:
            values[name] = _integer(raw, f"config.{name}")
        elif name == "max_position_deviation_normalized" and raw is None:
            values[name] = None
        else:
            values[name] = _number(raw, f"config.{name}")
    try:
        return StabilizerConfig(**values)
    except (TypeError, ValueError) as error:
        _fail("config", f"invalid StabilizerConfig: {error}")
    raise AssertionError("unreachable")


def parse_experimental_phase4_profile(
    value: Mapping[str, Any],
) -> ExperimentalPhase4Profile:
    """Parse one profile while rejecting incomplete or broadened claims."""

    payload = _mapping(value, "profile")
    _exact_keys(payload, _TOP_LEVEL_KEYS, "profile")

    schema_version = _integer(payload["schema_version"], "schema_version")
    if schema_version != PHASE4_PROFILE_SCHEMA_VERSION:
        _fail("schema_version", f"unsupported value {schema_version}")

    profile_id = _string(payload["profile_id"], "profile_id")
    if profile_id != PHASE4_EXPERIMENTAL_PROFILE_ID:
        _fail("profile_id", "does not identify the built-in exact-target profile")

    status = _string(payload["status"], "status")
    if status != PHASE4_EXPERIMENTAL_STATUS:
        _fail("status", "must remain experimental_unvalidated")

    scope = _string(payload["scope"], "scope")
    if scope != PHASE4_EXACT_TARGET_SCOPE:
        _fail("scope", "must remain exact_target_only")

    units = _string(payload["units"], "units")
    if units != PHASE4_RAW_UNITS:
        _fail("units", "must remain an uncalibrated raw sensor coordinate")

    calibration_performed = payload["calibration_performed"]
    if type(calibration_performed) is not bool:
        _fail("calibration_performed", "must be a boolean")
    if calibration_performed:
        _fail("calibration_performed", "must remain false in Phase 4")

    return ExperimentalPhase4Profile(
        schema_version=schema_version,
        profile_id=profile_id,
        status=status,
        scope=scope,
        units=units,
        calibration_performed=calibration_performed,
        source_phase2_artifact=_parse_source_evidence(
            payload["source_phase2_artifact"]
        ),
        config=_parse_config(payload["config"]),
        rationale=_string_tuple(payload["rationale"], "rationale"),
        caveats=_string_tuple(payload["caveats"], "caveats"),
    )


def load_experimental_phase4_profile() -> ExperimentalPhase4Profile:
    """Load the single bundled exact-target experimental profile."""

    resource = (
        resources.files("trackpad_scale")
        .joinpath("phase4_profiles")
        .joinpath(_PROFILE_FILENAME)
    )
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise Phase4ProfileError(f"profile resource is unreadable: {error}") from error
    if not isinstance(payload, Mapping):
        _fail("profile", "top-level JSON value must be an object")
    return parse_experimental_phase4_profile(payload)
