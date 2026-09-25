from __future__ import annotations

import logging

from applicant_scout.compatibility import (
    MINIMUM_ADDON_VERSION,
    PAIRED_ADDON_VERSION,
    addon_version_warning,
)


def test_older_addon_version_gets_actionable_warning():
    warning = addon_version_warning("0.5.1")

    assert warning is not None
    assert "0.5.1" in warning
    assert MINIMUM_ADDON_VERSION in warning
    assert "/reload" in warning


def test_paired_addon_version_is_accepted():
    assert addon_version_warning(MINIMUM_ADDON_VERSION) is None
    assert addon_version_warning(PAIRED_ADDON_VERSION) is None
    assert addon_version_warning(f"v{PAIRED_ADDON_VERSION}") is None


def test_newer_addon_version_warns_to_update_companion():
    warning = addon_version_warning("0.13.0")

    assert warning is not None
    assert "0.13.0" in warning
    assert PAIRED_ADDON_VERSION in warning
    assert "/reload" in warning


def test_missing_or_malformed_addon_version_does_not_false_alarm():
    assert addon_version_warning(None) is None
    assert addon_version_warning("") is None
    assert addon_version_warning("dev-build") is None


def test_unparsable_addon_version_logs_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="applicant_scout.compatibility"):
        assert addon_version_warning("dev-build") is None

    assert "dev-build" in caplog.text


def test_missing_addon_version_logs_without_warning_noise(caplog):
    with caplog.at_level(logging.DEBUG, logger="applicant_scout.compatibility"):
        assert addon_version_warning(None) is None
        assert addon_version_warning("") is None
        assert addon_version_warning("   ") is None

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_known_addon_versions_stay_log_quiet(caplog):
    with caplog.at_level(logging.DEBUG, logger="applicant_scout.compatibility"):
        assert addon_version_warning(PAIRED_ADDON_VERSION) is None
        assert addon_version_warning("0.5.1") is not None

    assert caplog.records == []
