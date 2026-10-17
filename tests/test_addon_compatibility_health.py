"""Addon compatibility warnings point to the component that needs updating."""

import pytest

from applicant_scout import compatibility
from applicant_scout.overlay_health import HealthChipState, health_chip_state


_SUPPORTED = tuple(map(int, compatibility.LATEST_SUPPORTED_ADDON_VERSION.split(".")))
_FUTURE_ADDONS = [
    f"{_SUPPORTED[0]}.{_SUPPORTED[1]}.{_SUPPORTED[2] + 1}",
    f"{_SUPPORTED[0]}.{_SUPPORTED[1] + 1}.0",
    f"{_SUPPORTED[0] + 1}.0.0",
]


def _chip(warning: str | None, app_update_version: str | None = None) -> HealthChipState:
    return health_chip_state(
        restored_pending=False, restored_saved_at=None, restored_deadline=None,
        wire_rejects=0, wire_reject_threshold=3, wire_reject_message="",
        wire_reject_active=False, failed_at=None, last_decode_time=100,
        failed_path="", failed_reason="", addon_warning=warning,
        app_update_version=app_update_version, applicants_unavailable=False,
        roster_unavailable=False, lfg_unavailable=False, now=101,
    )


@pytest.mark.parametrize("version", [
    "0.13.5", "0.13.6", "0.13.99", "0.14.0", "0.14.1", "0.14.2",
    compatibility.LATEST_SUPPORTED_ADDON_VERSION,
    f"v{compatibility.LATEST_SUPPORTED_ADDON_VERSION}",
    f" {compatibility.LATEST_SUPPORTED_ADDON_VERSION} ",
])
def test_tested_compatible_addon_range_does_not_request_an_unneeded_update(version):
    warning = compatibility.addon_version_warning(version)
    assert warning is None
    assert _chip(warning).chip_state == "neutral"


def test_release_pair_advances_without_raising_the_wire_compatibility_baseline():
    assert compatibility.MINIMUM_ADDON_VERSION == "0.13.5"
    paired = tuple(map(int, compatibility.PAIRED_ADDON_VERSION.split(".")))
    minimum = tuple(map(int, compatibility.MINIMUM_ADDON_VERSION.split(".")))
    assert minimum < paired <= _SUPPORTED


@pytest.mark.parametrize("version", _FUTURE_ADDONS)
def test_untested_future_addon_requests_companion_update_and_correct_footer(version):
    warning = compatibility.addon_version_warning(version)
    assert warning is not None
    assert version in warning
    assert compatibility.LATEST_SUPPORTED_ADDON_VERSION in warning
    assert "Update the companion app" in warning
    assert "Update the WoW addon" not in warning
    chip = _chip(warning)
    assert (chip.text, chip.chip_state) == ("App update", "warning")
    assert chip.tooltip == warning
    assert chip.accessible == warning


def test_outdated_addon_still_requests_addon_update_and_preserves_lower_bound():
    warning = compatibility.addon_version_warning("0.13.4")
    assert warning is not None
    assert "0.13.5" in warning
    assert "Update the WoW addon" in warning
    assert "Update the companion app" not in warning
    chip = _chip(warning)
    assert (chip.text, chip.chip_state) == ("Addon update", "warning")
    assert chip.tooltip == warning


def test_future_addon_and_available_app_update_have_one_truthful_component_label():
    warning = compatibility.addon_version_warning(_FUTURE_ADDONS[0])
    assert warning is not None
    chip = _chip(warning, "0.23.0")
    assert (chip.text, chip.chip_state) == ("App update", "warning")
    assert warning in chip.tooltip
    assert "Companion 0.23.0 is available" in chip.tooltip


def test_compatible_addon_does_not_hide_an_independent_available_app_update():
    warning = compatibility.addon_version_warning("0.14.0")
    assert warning is None
    chip = _chip(warning, "0.23.0")
    assert (chip.text, chip.chip_state) == ("App update", "warning")
    assert "Companion 0.23.0 is available" in chip.tooltip
