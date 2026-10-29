"""Compatibility checks for the paired ApplicantScout WoW addon."""

from __future__ import annotations

import logging
import re


MINIMUM_ADDON_VERSION = "0.13.5"
PAIRED_ADDON_VERSION = "0.14.7"
# Supported QR versions may extend beyond the historical release pair.
LATEST_SUPPORTED_ADDON_VERSION = "0.14.7"
COMPANION_UPDATE_ACTION = "Update the companion app, then /reload."
_SEMVER_RE = re.compile(r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)$")
_log = logging.getLogger("applicant_scout.compatibility")


def _parse_semver(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _SEMVER_RE.fullmatch(value.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    try:
        return int(major), int(minor), int(patch)
    except ValueError:
        # Over-long digit runs exceed the int() string-digit limit.
        return None


def addon_version_warning(addon_version: object) -> str | None:
    """Warn when the addon is outside this companion's supported version range."""
    installed = _parse_semver(addon_version)
    if installed is None:
        if isinstance(addon_version, str) and addon_version.strip():
            _log.warning("Unknown ApplicantScout addon version: %r", addon_version)
        else:
            _log.debug("ApplicantScout addon version is missing: %r", addon_version)
        return None
    required = _parse_semver(MINIMUM_ADDON_VERSION)
    supported = _parse_semver(LATEST_SUPPORTED_ADDON_VERSION)
    if required is None or supported is None:
        return None
    if installed < required:
        return (
            f"ApplicantScout addon {addon_version} is older than required "
            f"{MINIMUM_ADDON_VERSION}.\n"
            "Update the WoW addon from the latest release, then /reload."
        )
    if installed > supported:
        return (
            f"ApplicantScout addon {addon_version} is newer than this companion "
            f"(supports addons through {LATEST_SUPPORTED_ADDON_VERSION}).\n"
            f"{COMPANION_UPDATE_ACTION}"
        )
    return None
