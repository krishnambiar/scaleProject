"""Offline Phase 5 candidate-fit assessment; never a public scale output.

This combines file-hashed known-mass claims with pure calibration mathematics.
The source protocol and physical masses still require independent human review.
There is intentionally no live capture or automatic weight publication here.
"""

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, fields
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .phase5_calibration import (
    CalibrationConfig,
    CalibrationSeries,
    fit_and_validate_calibration,
)
from .phase5_evidence import KnownMassPublication, load_known_mass_evidence
from .target_profile import current_target_fingerprint


class Phase5AssessmentError(ValueError):
    """An assessment input or output is not safe to use."""


def _unique_object(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Phase5AssessmentError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise Phase5AssessmentError(f"nonstandard JSON number {value}")


def load_criteria(path: Path) -> CalibrationConfig:
    """Load every numeric tolerance explicitly, without a production default."""

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise Phase5AssessmentError(f"cannot read criteria: {error}") from error
    if type(payload) is not dict:
        raise Phase5AssessmentError("criteria must be a JSON object")
    expected = {field.name for field in fields(CalibrationConfig)}
    observed = set(payload)
    if observed != expected:
        raise Phase5AssessmentError(
            f"criteria missing={sorted(expected - observed)}; "
            f"unknown={sorted(observed - expected)}"
        )
    if type(payload["min_repeats_per_mass"]) is not int:
        raise Phase5AssessmentError("min_repeats_per_mass must be an integer")
    for key in sorted(expected - {"min_repeats_per_mass"}):
        value = payload[key]
        if type(value) not in (int, float):
            raise Phase5AssessmentError(f"{key} must be a finite number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise Phase5AssessmentError(f"{key} must be a finite number")
    return CalibrationConfig(**payload)


def _series(
    records: Sequence[KnownMassPublication], role: str
) -> Tuple[CalibrationSeries, ...]:
    groups: Dict[float, List[float]] = defaultdict(list)
    for record in records:
        if record.role == role:
            groups[record.known_mass_grams].append(record.pressure_delta_raw)
    return tuple(
        CalibrationSeries(grams, tuple(groups[grams])) for grams in sorted(groups)
    )


def assess_known_mass_manifest(
    manifest_path: Path,
    config: CalibrationConfig,
    *,
    expected_target: Mapping[str, str],
    expected_phase4_profile_id: str,
) -> Dict[str, object]:
    """Return a versioned mathematical report, never a public weight output."""

    manifest_path = Path(manifest_path).resolve()
    manifest_before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    records = load_known_mass_evidence(
        manifest_path,
        expected_target=expected_target,
        expected_phase4_profile_id=expected_phase4_profile_id,
    )
    manifest_after = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if manifest_before != manifest_after:
        raise Phase5AssessmentError("manifest changed while it was being assessed")
    validation = fit_and_validate_calibration(
        _series(records, "training"), _series(records, "heldout"), config
    )
    return {
        "schema_version": 1,
        "phase": 5,
        "kind": "offline_candidate_calibration_assessment",
        "status": (
            "mathematical_candidate_only" if validation.accepted else "rejected"
        ),
        "public_weight_available": False,
        "hardware_validated": False,
        "grams_claimed": False,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_after,
        "phase4_profile_id": expected_phase4_profile_id,
        "target": dict(expected_target),
        "criteria": asdict(config),
        "source_trials": [
            {
                "role": record.role,
                "session_id": record.session_id,
                "trial_id": record.trial_id,
                "known_mass_grams": record.known_mass_grams,
                "mass_reference_id": record.mass_reference_id,
                "contact_geometry_protocol_id": record.contact_geometry_protocol_id,
                "source_path": str(record.source_path),
                "source_sha256": record.source_sha256,
                "publication_sequence": record.publication_sequence,
                "pressure_delta_raw": record.pressure_delta_raw,
            }
            for record in records
        ],
        "validation": asdict(validation),
        "limitations": [
            "File hashes and schema checks cannot prove that a physical known "
            "mass was applied or that its reference value is accurate.",
            "Selected successful trials cannot prove that failed or unstable "
            "attempts were retained; a reviewed acquisition log is required.",
            "A mathematical candidate does not authorize public weight, "
            "bottle, or hydration output.",
            "The profile status and physical-protocol labels are self-attested "
            "until separately reviewed and bound to immutable evidence.",
            "A saved criteria file does not prove its tolerances were fixed "
            "before anyone examined the held-out trials.",
        ],
    }


def write_assessment(path: Path, report: Mapping[str, object]) -> None:
    """Preserve every assessment by refusing to overwrite an existing file."""

    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
    except FileExistsError as error:
        raise Phase5AssessmentError(f"refusing to overwrite {path}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assess future known-mass evidence as a candidate fit only. "
            "Never publishes a hardware-validated weight."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--criteria", type=Path, required=True)
    parser.add_argument(
        "--phase4-profile-id",
        required=True,
        help="caller-supplied reviewed-profile claim; no trust anchor exists yet",
    )
    parser.add_argument("--json-out", type=Path, required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        criteria_path = args.criteria.resolve()
        criteria_before = hashlib.sha256(criteria_path.read_bytes()).hexdigest()
        config = load_criteria(criteria_path)
        criteria_after = hashlib.sha256(criteria_path.read_bytes()).hexdigest()
        if criteria_before != criteria_after:
            raise Phase5AssessmentError("criteria changed while being assessed")
        target = current_target_fingerprint().to_dict()
        report = assess_known_mass_manifest(
            args.manifest,
            config,
            expected_target=target,
            expected_phase4_profile_id=args.phase4_profile_id,
        )
        report["criteria_path"] = str(criteria_path)
        report["criteria_sha256"] = criteria_after
        write_assessment(args.json_out, report)
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(f"Phase 5 assessment refused: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "validation_reason": report["validation"]["reason"],
                "public_weight_available": False,
                "report_path": str(args.json_out),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "mathematical_candidate_only" else 3


if __name__ == "__main__":
    raise SystemExit(main())
