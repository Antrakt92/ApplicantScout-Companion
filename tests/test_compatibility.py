from applicant_scout.compatibility import (
    MINIMUM_ADDON_VERSION,
    addon_version_warning,
)


def test_older_addon_version_gets_actionable_warning():
    warning = addon_version_warning("0.5.1")

    assert warning is not None
    assert "0.5.1" in warning
    assert MINIMUM_ADDON_VERSION in warning
    assert "/reload" in warning


def test_current_or_newer_addon_version_is_accepted():
    assert addon_version_warning(MINIMUM_ADDON_VERSION) is None
    assert addon_version_warning("v99.0.0") is None


def test_missing_or_malformed_addon_version_does_not_false_alarm():
    assert addon_version_warning(None) is None
    assert addon_version_warning("") is None
    assert addon_version_warning("dev-build") is None
