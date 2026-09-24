"""Compatibility checks for the paired ApplicantScout WoW addon."""

from __future__ import annotations

import re


MINIMUM_ADDON_VERSION = "0.12.0"
PAIRED_ADDON_VERSION = "0.12.0"
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse_semver(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _SEMVER_RE.fullmatch(value.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def addon_version_warning(addon_version: object) -> str | None:
    """Return a user-facing warning for a mismatched paired addon version."""
    installed = _parse_semver(addon_version)
    required = _parse_semver(MINIMUM_ADDON_VERSION)
    paired = _parse_semver(PAIRED_ADDON_VERSION)
    if installed is None or required is None or paired is None:
        return None
    if installed < required:
        return (
            f"ApplicantScout addon {addon_version} is older than required "
            f"{MINIMUM_ADDON_VERSION}.\n"
            "Update the WoW addon from the latest release, then /reload."
        )
    if installed > paired:
        return (
            f"ApplicantScout addon {addon_version} is newer than this companion "
            f"(paired with {PAIRED_ADDON_VERSION}).\n"
            "Update the companion app, then /reload."
        )
    return None
