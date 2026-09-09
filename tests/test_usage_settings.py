from __future__ import annotations

import json

import pytest

from applicant_scout.config import Config
from applicant_scout.settings_dialog import SettingsDialog
from applicant_scout.usage import UsageClient, UsagePersistenceError


class UsageStub:
    reporting_available = True
    collection_available = True

    def __init__(self, enabled=True, fail=False):
        self.consent_enabled = enabled
        self.fail = fail
        self.changes = []
        self.events = []

    def set_consent(self, enabled):
        self.changes.append(enabled)
        self.consent_enabled = False
        if self.fail:
            raise UsagePersistenceError("cannot save")
        self.consent_enabled = enabled

    def record(self, event):
        self.events.append(event)
        return self.consent_enabled


def dialog_for(qtbot, tmp_path, usage):
    cfg = Config(wcl_client_id="", wcl_client_secret="", region="EU",
                 chatlog_path=tmp_path / "Logs/WoWChatLog.txt",
                 cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
                 screenshots_path=tmp_path / "Screenshots", log_dir=tmp_path / "logs")
    dialog = SettingsDialog(cfg, first_run=True, usage_client=usage)
    qtbot.addWidget(dialog)
    return dialog


def test_opening_settings_preserves_explicit_optout_without_writes(qtbot, tmp_path):
    usage = UsageStub(enabled=False)
    dialog = dialog_for(qtbot, tmp_path, usage)
    assert not dialog.usage_check.isChecked()
    assert usage.changes == []
    assert usage.events == []
    assert not (tmp_path / "config/usage.json").exists()


def test_consent_saves_immediately_even_with_invalid_wcl(qtbot, tmp_path):
    usage = UsageStub(enabled=False)
    dialog = dialog_for(qtbot, tmp_path, usage)
    assert dialog.client_id_edit.text() == ""
    assert dialog.client_secret_edit.text() == ""
    changes = []
    dialog.usageConsentChanged.connect(changes.append)
    dialog.usage_check.click()
    assert changes == [True]
    assert usage.consent_enabled
    assert usage.changes == [True]
    assert dialog.usage_check.isChecked()
    assert "setup_completed" not in usage.events


def test_revocation_is_immediate_independent_of_wcl_validation(qtbot, tmp_path):
    usage = UsageStub(enabled=True)
    dialog = dialog_for(qtbot, tmp_path, usage)
    assert dialog.usage_check.isChecked()
    assert usage.changes == []
    changes = []
    dialog.usageConsentChanged.connect(changes.append)
    dialog.usage_check.click()
    assert changes == [False]
    assert usage.changes == [False]
    assert not usage.consent_enabled
    assert not dialog.usage_check.isChecked()


def test_persistence_failure_rolls_checkbox_back_off_without_reentry(qtbot, tmp_path):
    usage = UsageStub(enabled=False, fail=True)
    dialog = dialog_for(qtbot, tmp_path, usage)
    changes = []
    dialog.usageConsentChanged.connect(changes.append)
    dialog.usage_check.click()
    assert changes == [False]
    assert usage.changes == [True]
    assert not usage.consent_enabled
    assert not dialog.usage_check.isChecked()
    assert "off" in dialog.status_label.text().lower()
    assert "could not be saved" in dialog.status_label.text().lower()


def test_revocation_save_failure_still_stops_this_session(qtbot, tmp_path):
    usage = UsageStub(enabled=True, fail=True)
    dialog = dialog_for(qtbot, tmp_path, usage)
    changes = []
    dialog.usageConsentChanged.connect(changes.append)
    dialog.usage_check.click()
    assert changes == [False]
    assert usage.changes == [False]
    assert not usage.consent_enabled
    assert not dialog.usage_check.isChecked()


def test_missing_usage_client_has_disabled_unchecked_control(qtbot, tmp_path):
    dialog = dialog_for(qtbot, tmp_path, None)
    assert not dialog.usage_check.isEnabled()
    assert not dialog.usage_check.isChecked()


@pytest.mark.parametrize("saved_consent", [None, False, True])
def test_unavailable_collection_preserves_real_default_or_saved_checkbox_and_allows_optout(
    qtbot, tmp_path, saved_consent,
):
    path = tmp_path / "config" / "usage.json"
    if saved_consent is not None:
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema": 1, "consent": saved_consent}), encoding="utf-8")
    usage = UsageClient(path.parent, "0.17.1", endpoint="")
    try:
        initial_bytes = path.read_bytes()
        dialog = dialog_for(qtbot, tmp_path, usage)
        assert not usage.collection_available
        assert usage._thread is None
        assert dialog.usage_check.isEnabled()
        assert dialog.usage_check.isChecked() is (saved_consent is not False)
        assert path.read_bytes() == initial_bytes
        assert not usage.record("addon_received")
        dialog.usage_check.click()
        assert dialog.usage_check.isChecked() is (saved_consent is False)
        assert json.loads(path.read_text())["consent"] is (saved_consent is False)
        assert usage._thread is None
    finally:
        usage.close()


def test_corrupt_usage_state_is_not_enabled_by_opening_settings(qtbot, tmp_path):
    path = tmp_path / "config" / "usage.json"
    path.parent.mkdir(parents=True)
    path.write_text("{corrupt", encoding="utf-8")
    usage = UsageClient(path.parent, "0.17.1", endpoint="")
    try:
        dialog = dialog_for(qtbot, tmp_path, usage)
        assert not dialog.usage_check.isChecked()
        assert path.read_text(encoding="utf-8") == "{corrupt"
    finally:
        usage.close()



def test_first_run_actions_stay_visible_while_small_window_scrolls(qtbot, tmp_path):
    from PyQt6.QtCore import QPoint, QRect
    from PyQt6.QtWidgets import QApplication

    dialog = dialog_for(qtbot, tmp_path, UsageStub())
    dialog.show()
    dialog.resize(560, 420)
    QApplication.processEvents()
    assert dialog.width() == 560
    assert dialog.height() == 420
    assert dialog.start_button is not None
    assert dialog.setup_quit_button is not None
    actions = (dialog.start_button, dialog.setup_quit_button, dialog.test_button)
    scrollbar = dialog.body_scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0, "small first-run fixture must need scrolling"
    for position in (scrollbar.minimum(), scrollbar.maximum()):
        scrollbar.setValue(position)
        QApplication.processEvents()
        for button in actions:
            assert button.isVisibleTo(dialog)
            assert not dialog.body_scroll.isAncestorOf(button)
            bounds = QRect(button.mapTo(dialog, QPoint()), button.size())
            assert dialog.rect().contains(bounds), "primary action was clipped"
    dialog.body_scroll.ensureWidgetVisible(dialog.usage_check)
    QApplication.processEvents()
    viewport = dialog.body_scroll.viewport()
    center = dialog.usage_check.mapTo(viewport, dialog.usage_check.rect().center())
    assert viewport.rect().contains(center), "usage choice cannot be reached by scrolling"
    assert dialog.usage_check.isChecked()
    dialog.usage_check.click()
    assert not dialog.usage_check.isChecked()
