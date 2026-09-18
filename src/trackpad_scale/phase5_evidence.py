"""Fail-closed provenance gate for *future* physical known-mass trials.

This module only checks recorded consistency. It cannot prove that a physical
mass touched the trackpad, that a mass reference is accurate, that failed
attempts were all retained, or that the Phase 4 profile was truly validated.
Those require a reviewed acquisition protocol and human evidence. In
particular, a Phase 4 fingertip diagnostic cannot become a calibration trial
by attaching a grams label. No current project artifact passes this gate.

Manifest v1 is an object with exactly ``schema_version``, ``phase``,
``purpose``, ``acquisition_protocol_id``, ``target``,
``phase4_profile_id``, and ``trials``. Each trial has exactly ``role``
(training/heldout), ``session_id``, ``trial_id``, ``known_mass_grams``,
``mass_reference_id``, ``contact_geometry_protocol_id``, and ``source``.
``source`` contains a path, lowercase SHA-256, and publication sequence.
Paths are relative to the manifest unless absolute.

The source is a distinct Phase 5 JSON artifact, not Phase 4 replay/probe
output. Its exact top-level fields are ``schema_version``, ``phase``,
``kind``, ``status``, ``units``, ``calibration_performed``,
``grams_claimed``, ``actual_target``, ``profile`` (profile_id/status),
``acquisition`` (acquisition_protocol_id/contact_geometry_protocol_id/
mass_reference_id/mass_application), ``session_id``, ``trial_id``,
``known_mass_grams``, ``outcome``, ``phase1_capture_stats``,
``phase2_transport_stats``, ``publication_count``, ``publications``, and
``event_updates``. The one publication must appear in a measurement-interval
``stable_published`` update with identical raw measurement and confidence.

The manifest lists selected successful trials; completeness of attempted
trials cannot be inferred from it. Selection/cherry-picking remains a caveat
until an acquisition protocol records and audits *all* attempts.
An explicitly measured zero-additional-mass trial may be recorded with
``mass_application: measured_zero_additional_mass``; it is not inferred from
Phase 4's arbitrary reference-contact tare.
This gate cross-checks recorded publication fields but does not replay raw
sensor frames. A separate reviewed capture and replay path is required before
the evidence can authorize a physical weight claim.
"""

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .phase4_profile import PHASE4_EXPERIMENTAL_PROFILE_ID, PHASE4_RAW_UNITS
from .phase4_stabilizer import StabilityConfidence


MANIFEST_SCHEMA_VERSION = 1
SOURCE_SCHEMA_VERSION = 1
ACQUISITION_PROTOCOL_ID = "phase5_physical_known_mass_v1"
VALIDATED_PROFILE_STATUS = "validated_for_phase5"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
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
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "phase",
        "purpose",
        "acquisition_protocol_id",
        "target",
        "phase4_profile_id",
        "trials",
    }
)
_TRIAL_KEYS = frozenset(
    {
        "role",
        "session_id",
        "trial_id",
        "known_mass_grams",
        "mass_reference_id",
        "contact_geometry_protocol_id",
        "source",
    }
)
_SOURCE_REF_KEYS = frozenset({"path", "sha256", "publication_sequence"})
_SOURCE_KEYS = frozenset(
    {
        "schema_version",
        "phase",
        "kind",
        "status",
        "units",
        "calibration_performed",
        "grams_claimed",
        "actual_target",
        "profile",
        "acquisition",
        "session_id",
        "trial_id",
        "known_mass_grams",
        "outcome",
        "phase1_capture_stats",
        "phase2_transport_stats",
        "publication_count",
        "publications",
        "event_updates",
    }
)
_PROFILE_KEYS = frozenset({"profile_id", "status"})
_ACQUISITION_KEYS = frozenset(
    {
        "acquisition_protocol_id",
        "contact_geometry_protocol_id",
        "mass_reference_id",
        "mass_application",
    }
)
_PUBLICATION_KEYS = frozenset(
    {
        "pressure_delta_raw",
        "dispersion_mad_raw",
        "slope_raw_per_second",
        "duration_seconds",
        "sequence",
        "host_monotonic_ns",
        "confidence",
    }
)
_CONFIDENCE_KEYS = frozenset(
    {
        "agreeing_window_count",
        "required_window_count",
        "agreement_span_raw",
        "agreement_limit_raw",
        "maximum_window_mad_raw",
        "mad_limit_raw",
        "maximum_absolute_slope_raw_per_second",
        "slope_limit_raw_per_second",
    }
)
_EVENT_KEYS = frozenset({"interval", "update"})
_UPDATE_KEYS = frozenset({"status", "sequence", "published_measurement"})
_CAPTURE_KEYS = frozenset(
    {
        "callback_count",
        "enqueued_count",
        "queue_overwrite_count",
        "lock_contention_drop_count",
        "callback_device_mismatch_count",
        "late_callback_count",
        "in_flight_callback_count",
        "queue_depth",
        "native_drop_count",
    }
)
_TRANSPORT_KEYS = frozenset(
    {
        "attempted_frame_count",
        "copied_touch_count",
        "queue_overwrite_count",
        "lock_contention_drop_count",
        "invalid_count_frame_count",
        "null_records_frame_count",
        "device_mismatch_frame_count",
        "record_frame_mismatch_touch_count",
        "record_timestamp_mismatch_touch_count",
        "invalid_state_touch_count",
        "pressure_sentinel_touch_count",
        "nonfinite_touch_count",
        "queue_depth",
        "native_drop_count",
    }
)


class Phase5EvidenceError(ValueError):
    """An evidence source cannot be used for physical calibration."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class KnownMassPublication:
    """One source-verified raw publication with immutable provenance."""

    role: str
    session_id: str
    trial_id: str
    known_mass_grams: float
    mass_reference_id: str
    contact_geometry_protocol_id: str
    acquisition_protocol_id: str
    phase4_profile_id: str
    target: Tuple[Tuple[str, str], ...]
    source_path: Path
    source_sha256: str
    publication_sequence: int
    pressure_delta_raw: float
    dispersion_mad_raw: float
    slope_raw_per_second: float
    duration_seconds: float
    host_monotonic_ns: int
    confidence: StabilityConfidence


def _fail(reason: str, detail: str) -> None:
    raise Phase5EvidenceError(reason, detail)


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(type(k) is str for k in value):
        _fail("schema_invalid", f"{context} must be an object with string keys")
    return value


def _exact(value: Mapping[str, object], keys: frozenset, context: str) -> None:
    missing = sorted(keys - value.keys())
    extra = sorted(value.keys() - keys)
    if missing or extra:
        _fail("schema_invalid", f"{context}: missing={missing}; unknown={extra}")


def _string(value: object, context: str) -> str:
    if type(value) is not str or not value.strip():
        _fail("schema_invalid", f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("schema_invalid", f"{context} must be an integer >= {minimum}")
    return value


def _number(value: object, context: str, *, minimum: Optional[float] = None) -> float:
    if type(value) not in (int, float):
        _fail("schema_invalid", f"{context} must be a finite number")
    try:
        result = float(value)
    except OverflowError:
        _fail("schema_invalid", f"{context} must be a finite number")
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        _fail("schema_invalid", f"{context} must be finite and >= {minimum}")
    return result


def _literal(value: object, expected: object, context: str) -> None:
    if type(value) is not type(expected) or value != expected:
        _fail("provenance_mismatch", f"{context}: expected {expected!r}, got {value!r}")


def _sha256(value: object, context: str) -> str:
    digest = _string(value, context)
    if _SHA256.fullmatch(digest) is None:
        _fail("schema_invalid", f"{context} must be lowercase SHA-256 hex")
    return digest


def _json_bytes(data: bytes, context: str) -> Mapping[str, object]:
    def unique_object(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
        result: Dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"nonstandard JSON constant {value}")

    def finite_float(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"non-finite JSON number {value}")
        return result

    try:
        parsed = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        _fail("json_invalid", f"{context}: {error}")
    return _mapping(parsed, context)


def _read(
    path: Path, context: str, expected_sha256: Optional[str] = None
) -> Mapping[str, object]:
    try:
        data = path.read_bytes()
    except OSError as error:
        _fail(
            "source_unreadable" if expected_sha256 else "manifest_unreadable",
            f"{context}: {error}",
        )
    if expected_sha256 is not None:
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            _fail(
                "source_sha256_mismatch",
                f"{context}: expected {expected_sha256}, got {actual}",
            )
    return _json_bytes(data, context)


def _target(value: object, context: str) -> Tuple[Tuple[str, str], ...]:
    mapping = _mapping(value, context)
    _exact(mapping, _TARGET_KEYS, context)
    return tuple(
        (key, _string(mapping[key], f"{context}.{key}")) for key in sorted(_TARGET_KEYS)
    )


def _confidence(value: object) -> StabilityConfidence:
    payload = _mapping(value, "publication.confidence")
    _exact(payload, _CONFIDENCE_KEYS, "publication.confidence")
    agreeing = _integer(
        payload["agreeing_window_count"], "agreeing_window_count", minimum=2
    )
    required = _integer(
        payload["required_window_count"], "required_window_count", minimum=2
    )
    agreement = _number(payload["agreement_span_raw"], "agreement_span_raw", minimum=0)
    agreement_limit = _number(
        payload["agreement_limit_raw"], "agreement_limit_raw", minimum=0
    )
    mad = _number(
        payload["maximum_window_mad_raw"], "maximum_window_mad_raw", minimum=0
    )
    mad_limit = _number(payload["mad_limit_raw"], "mad_limit_raw", minimum=0)
    slope = _number(
        payload["maximum_absolute_slope_raw_per_second"],
        "maximum_absolute_slope_raw_per_second",
        minimum=0,
    )
    slope_limit = _number(
        payload["slope_limit_raw_per_second"], "slope_limit_raw_per_second", minimum=0
    )
    if (
        agreeing < required
        or agreement > agreement_limit
        or mad > mad_limit
        or slope > slope_limit
    ):
        _fail(
            "publication_invalid",
            "stable confidence does not satisfy its recorded limits",
        )
    return StabilityConfidence(
        agreeing_window_count=agreeing,
        required_window_count=required,
        agreement_span_raw=agreement,
        agreement_limit_raw=agreement_limit,
        maximum_window_mad_raw=mad,
        mad_limit_raw=mad_limit,
        maximum_absolute_slope_raw_per_second=slope,
        slope_limit_raw_per_second=slope_limit,
    )


def _publication(
    value: object, expected_sequence: int
) -> Tuple[float, float, float, float, int, StabilityConfidence]:
    payload = _mapping(value, "publication")
    _exact(payload, _PUBLICATION_KEYS, "publication")
    sequence = _integer(payload["sequence"], "publication.sequence", minimum=1)
    if sequence != expected_sequence:
        _fail(
            "publication_invalid",
            f"expected sequence {expected_sequence}, got {sequence}",
        )
    delta = _number(payload["pressure_delta_raw"], "pressure_delta_raw")
    dispersion = _number(payload["dispersion_mad_raw"], "dispersion_mad_raw", minimum=0)
    slope = _number(payload["slope_raw_per_second"], "slope_raw_per_second")
    duration = _number(payload["duration_seconds"], "duration_seconds", minimum=0)
    if duration == 0:
        _fail("publication_invalid", "stable window duration must be positive")
    host_ns = _integer(payload["host_monotonic_ns"], "host_monotonic_ns", minimum=1)
    confidence = _confidence(payload["confidence"])
    if (
        dispersion > confidence.maximum_window_mad_raw
        or abs(slope) > confidence.maximum_absolute_slope_raw_per_second
    ):
        _fail("publication_invalid", "publication metrics exceed confidence maxima")
    return delta, dispersion, slope, duration, host_ns, confidence


def _clean_stats(source: Mapping[str, object]) -> None:
    capture = _mapping(source["phase1_capture_stats"], "phase1_capture_stats")
    transport = _mapping(source["phase2_transport_stats"], "phase2_transport_stats")
    _exact(capture, _CAPTURE_KEYS, "phase1_capture_stats")
    _exact(transport, _TRANSPORT_KEYS, "phase2_transport_stats")
    capture_counts = {
        key: _integer(value, f"phase1_capture_stats.{key}")
        for key, value in capture.items()
    }
    transport_counts = {
        key: _integer(value, f"phase2_transport_stats.{key}")
        for key, value in transport.items()
    }
    if (
        capture_counts["callback_count"] <= 0
        or capture_counts["enqueued_count"] != capture_counts["callback_count"]
    ):
        _fail("transport_unclean", "Phase 1 callbacks were not all enqueued")
    if (
        transport_counts["attempted_frame_count"] <= 0
        or transport_counts["copied_touch_count"] <= 0
    ):
        _fail(
            "transport_unclean",
            "Phase 2 recorded no attempted frames or copied touches",
        )
    for label, counts, allowed_positive in (
        ("Phase 1", capture_counts, {"callback_count", "enqueued_count"}),
        ("Phase 2", transport_counts, {"attempted_frame_count", "copied_touch_count"}),
    ):
        dirty = {
            key: value
            for key, value in counts.items()
            if key not in allowed_positive and value != 0
        }
        if dirty:
            _fail(
                "transport_unclean",
                f"{label} nonzero error/drop/pending counts: {dirty}",
            )


def _source_record(
    source: Mapping[str, object],
    *,
    trial: Mapping[str, object],
    source_path: Path,
    source_sha256: str,
    expected_target: Tuple[Tuple[str, str], ...],
    profile_id: str,
    publication_sequence: int,
) -> KnownMassPublication:
    _exact(source, _SOURCE_KEYS, "source")
    _literal(source["schema_version"], SOURCE_SCHEMA_VERSION, "source.schema_version")
    _literal(source["phase"], 5, "source.phase")
    _literal(source["kind"], "physical_known_mass_trial", "source.kind")
    _literal(source["status"], "raw_evidence_only", "source.status")
    _literal(source["units"], PHASE4_RAW_UNITS, "source.units")
    _literal(source["calibration_performed"], False, "source.calibration_performed")
    _literal(source["grams_claimed"], False, "source.grams_claimed")
    if _target(source["actual_target"], "source.actual_target") != expected_target:
        _fail("target_mismatch", f"{source_path} does not match expected target")

    profile = _mapping(source["profile"], "source.profile")
    _exact(profile, _PROFILE_KEYS, "source.profile")
    _literal(profile["profile_id"], profile_id, "source.profile.profile_id")
    _literal(profile["status"], VALIDATED_PROFILE_STATUS, "source.profile.status")

    acquisition = _mapping(source["acquisition"], "source.acquisition")
    _exact(acquisition, _ACQUISITION_KEYS, "source.acquisition")
    for key in (
        "acquisition_protocol_id",
        "contact_geometry_protocol_id",
        "mass_reference_id",
    ):
        _literal(acquisition[key], trial[key], f"source.acquisition.{key}")
    mass_application = (
        "measured_zero_additional_mass"
        if trial["known_mass_grams"] == 0
        else "physical_known_mass"
    )
    _literal(
        acquisition["mass_application"],
        mass_application,
        "source.acquisition.mass_application",
    )
    for key in ("session_id", "trial_id"):
        _literal(source[key], trial[key], f"source.{key}")
    source_grams = _number(
        source["known_mass_grams"], "source.known_mass_grams", minimum=0
    )
    if source_grams != trial["known_mass_grams"]:
        _fail("provenance_mismatch", "source known mass differs from manifest")
    _literal(source["outcome"], "stable_raw_pressure_observed", "source.outcome")
    _clean_stats(source)

    _literal(source["publication_count"], 1, "source.publication_count")
    publications = source["publications"]
    if type(publications) is not list or len(publications) != 1:
        _fail("publication_invalid", "source must contain exactly one publication")
    pub = _mapping(publications[0], "source.publications[0]")
    delta, dispersion, slope, duration, host_ns, confidence = _publication(
        pub, publication_sequence
    )

    events = source["event_updates"]
    if type(events) is not list or not events:
        _fail(
            "publication_invalid",
            "source.event_updates must contain a stable publication",
        )
    matched = 0
    for index, value in enumerate(events):
        event = _mapping(value, f"event_updates[{index}]")
        _exact(event, _EVENT_KEYS, f"event_updates[{index}]")
        interval = _string(event["interval"], f"event_updates[{index}].interval")
        update = _mapping(event["update"], f"event_updates[{index}].update")
        _exact(update, _UPDATE_KEYS, f"event_updates[{index}].update")
        status = _string(update.get("status"), f"event_updates[{index}].update.status")
        recorded = update.get("published_measurement")
        if recorded is None:
            if status == "stable_published":
                _fail(
                    "publication_invalid", "stable_published event has no measurement"
                )
            continue
        if status != "stable_published" or interval != "measurement":
            _fail(
                "publication_invalid",
                "publication outside measurement stable_published event",
            )
        if (
            _integer(update.get("sequence"), "event update sequence", minimum=1)
            != publication_sequence
        ):
            _fail(
                "publication_invalid",
                "event sequence differs from selected publication",
            )
        if recorded != pub:
            _fail("publication_invalid", "event measurement differs from publication")
        matched += 1
    if matched != 1:
        _fail(
            "publication_invalid", f"expected one matching stable event, got {matched}"
        )

    return KnownMassPublication(
        role=str(trial["role"]),
        session_id=str(trial["session_id"]),
        trial_id=str(trial["trial_id"]),
        known_mass_grams=float(trial["known_mass_grams"]),
        mass_reference_id=str(trial["mass_reference_id"]),
        contact_geometry_protocol_id=str(trial["contact_geometry_protocol_id"]),
        acquisition_protocol_id=str(trial["acquisition_protocol_id"]),
        phase4_profile_id=profile_id,
        target=expected_target,
        source_path=source_path,
        source_sha256=source_sha256,
        publication_sequence=publication_sequence,
        pressure_delta_raw=delta,
        dispersion_mad_raw=dispersion,
        slope_raw_per_second=slope,
        duration_seconds=duration,
        host_monotonic_ns=host_ns,
        confidence=confidence,
    )


def load_known_mass_evidence(
    manifest_path: Path,
    *,
    expected_target: Mapping[str, str],
    expected_phase4_profile_id: str,
) -> Tuple[KnownMassPublication, ...]:
    """Load repeated train/heldout publications only after strict gates pass.

    A passing result establishes internally coherent, file-hashed *claims*,
    not physical truth, accuracy, or approval to display calibrated grams.
    """

    profile_id = _string(expected_phase4_profile_id, "expected_phase4_profile_id")
    if profile_id == PHASE4_EXPERIMENTAL_PROFILE_ID:
        _fail(
            "experimental_profile",
            "the bundled Phase 4 profile is not validated for calibration",
        )
    target = _target(expected_target, "expected_target")
    manifest_path = Path(manifest_path).resolve()
    manifest = _read(manifest_path, "manifest")
    _exact(manifest, _MANIFEST_KEYS, "manifest")
    _literal(
        manifest["schema_version"], MANIFEST_SCHEMA_VERSION, "manifest.schema_version"
    )
    _literal(manifest["phase"], 5, "manifest.phase")
    _literal(manifest["purpose"], "known_mass_calibration", "manifest.purpose")
    _literal(
        manifest["acquisition_protocol_id"],
        ACQUISITION_PROTOCOL_ID,
        "manifest.acquisition_protocol_id",
    )
    _literal(manifest["phase4_profile_id"], profile_id, "manifest.phase4_profile_id")
    if _target(manifest["target"], "manifest.target") != target:
        _fail("target_mismatch", "manifest target differs from expected target")

    trials = manifest["trials"]
    if type(trials) is not list or not trials:
        _fail("schema_invalid", "manifest.trials must be a non-empty array")
    records: List[KnownMassPublication] = []
    trial_ids = set()
    source_paths = set()
    groups = defaultdict(set)
    role_sessions = defaultdict(set)
    geometry_ids = set()
    for index, value in enumerate(trials):
        context = f"trials[{index}]"
        trial = _mapping(value, context)
        _exact(trial, _TRIAL_KEYS, context)
        role = _string(trial["role"], f"{context}.role")
        if role not in ("training", "heldout"):
            _fail("schema_invalid", f"{context}.role must be training or heldout")
        session_id = _string(trial["session_id"], f"{context}.session_id")
        trial_id = _string(trial["trial_id"], f"{context}.trial_id")
        if trial_id in trial_ids:
            _fail("duplicate_trial", f"trial_id {trial_id!r} is reused")
        trial_ids.add(trial_id)
        grams = _number(
            trial["known_mass_grams"], f"{context}.known_mass_grams", minimum=0
        )
        mass_reference = _string(
            trial["mass_reference_id"], f"{context}.mass_reference_id"
        )
        geometry = _string(
            trial["contact_geometry_protocol_id"],
            f"{context}.contact_geometry_protocol_id",
        )
        if any(
            token in geometry.casefold()
            for token in ("finger", "hand_pressure", "fingertip")
        ):
            _fail("finger_only_protocol", f"{context} names a finger-pressure protocol")
        geometry_ids.add(geometry)
        source_ref = _mapping(trial["source"], f"{context}.source")
        _exact(source_ref, _SOURCE_REF_KEYS, f"{context}.source")
        source_name = _string(source_ref["path"], f"{context}.source.path")
        source_path = Path(source_name)
        if not source_path.is_absolute():
            source_path = manifest_path.parent / source_path
        source_path = source_path.resolve()
        if source_path in source_paths:
            _fail("duplicate_trial", f"source path {source_path} is reused")
        source_paths.add(source_path)
        digest = _sha256(source_ref["sha256"], f"{context}.source.sha256")
        sequence = _integer(
            source_ref["publication_sequence"],
            f"{context}.source.publication_sequence",
            minimum=1,
        )
        normalized_trial = {
            "role": role,
            "session_id": session_id,
            "trial_id": trial_id,
            "known_mass_grams": grams,
            "mass_reference_id": mass_reference,
            "contact_geometry_protocol_id": geometry,
            "acquisition_protocol_id": ACQUISITION_PROTOCOL_ID,
        }
        source = _read(source_path, f"{context}.source", digest)
        records.append(
            _source_record(
                source,
                trial=normalized_trial,
                source_path=source_path,
                source_sha256=digest,
                expected_target=target,
                profile_id=profile_id,
                publication_sequence=sequence,
            )
        )
        groups[(role, grams)].add(session_id)
        role_sessions[role].add(session_id)

    if len(geometry_ids) != 1:
        _fail(
            "protocol_mismatch",
            "all trials must use the same contact geometry protocol",
        )
    training_masses = {mass for role, mass in groups if role == "training"}
    heldout_masses = {mass for role, mass in groups if role == "heldout"}
    if len(training_masses) < 3:
        _fail(
            "insufficient_independent_trials",
            "training requires at least three distinct known masses",
        )
    if len(heldout_masses) < 2:
        _fail(
            "insufficient_independent_trials",
            "heldout requires at least two distinct known masses",
        )
    if training_masses & heldout_masses:
        _fail("training_heldout_overlap", "a known mass appears in both roles")
    if any(
        mass <= min(training_masses) or mass >= max(training_masses)
        for mass in heldout_masses
    ):
        _fail(
            "heldout_mass_outside_training_range",
            "each heldout mass must be strictly inside the training mass range",
        )
    for (role, grams), session_ids in groups.items():
        if len(session_ids) < 2:
            _fail(
                "insufficient_independent_trials",
                f"{role} {grams:g} g has fewer than two independent sessions",
            )
    overlap = role_sessions["training"] & role_sessions["heldout"]
    if overlap:
        _fail(
            "training_heldout_overlap",
            f"sessions used in both roles: {sorted(overlap)}",
        )
    return tuple(records)
