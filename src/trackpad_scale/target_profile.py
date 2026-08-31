"""Exact-target gate for the locally verified private ABI profile."""

import json
import platform
import plistlib
import re
import subprocess
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Dict, List, Tuple


FRAMEWORK_INFO = Path(
    "/System/Library/PrivateFrameworks/MultitouchSupport.framework/"
    "Versions/A/Resources/Info.plist"
)
FRAMEWORK_BINARY = Path(
    "/System/Library/PrivateFrameworks/MultitouchSupport.framework/"
    "MultitouchSupport"
)


@dataclass(frozen=True)
class TargetFingerprint:
    architecture: str
    os_build: str
    kernel_osversion: str
    hardware_model: str
    framework_bundle_version: str
    framework_image_uuid: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


def _command_output(arguments: List[str]) -> str:
    result = subprocess.run(
        arguments,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return result.stdout.strip()


def current_target_fingerprint() -> TargetFingerprint:
    with FRAMEWORK_INFO.open("rb") as stream:
        framework_info = plistlib.load(stream)
    uuid_output = _command_output(["/usr/bin/dyld_info", "-uuid", str(FRAMEWORK_BINARY)])
    uuid_match = re.search(
        r"\b[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\b",
        uuid_output,
    )
    if uuid_match is None:
        raise RuntimeError("could not parse the framework image UUID from dyld_info")
    return TargetFingerprint(
        architecture=platform.machine(),
        os_build=_command_output(["/usr/bin/sw_vers", "-buildVersion"]),
        kernel_osversion=_command_output(
            ["/usr/sbin/sysctl", "-n", "kern.osversion"]
        ),
        hardware_model=_command_output(["/usr/sbin/sysctl", "-n", "hw.model"]),
        framework_bundle_version=str(framework_info["CFBundleVersion"]),
        framework_image_uuid=uuid_match.group(0).upper(),
    )


def load_verified_profile() -> Dict[str, object]:
    profile = resources.files("trackpad_scale").joinpath(
        "abi_profiles/macos_25D771280a_arm64.json"
    )
    return json.loads(profile.read_text(encoding="utf-8"))


def compare_target_to_profile(
    actual: TargetFingerprint, profile: Dict[str, object]
) -> Tuple[bool, List[str]]:
    expected = profile["target"]
    mismatches = []
    for field, actual_value in actual.to_dict().items():
        expected_value = expected.get(field)
        if actual_value != expected_value:
            mismatches.append(
                f"{field}: expected {expected_value!r}, observed {actual_value!r}"
            )
    return not mismatches, mismatches


def require_verified_target(allow_unverified_target: bool = False) -> TargetFingerprint:
    actual = current_target_fingerprint()
    matches, mismatches = compare_target_to_profile(actual, load_verified_profile())
    if not matches and not allow_unverified_target:
        details = "; ".join(mismatches)
        raise RuntimeError(
            "private ABI target does not match the locally verified profile: " + details
        )
    return actual
