from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from applicant_scout.config import Config
from applicant_scout.settings_dialog import SettingsDialog


def _dialog(qtbot, tmp_path: Path, provider, *, first_run=False):
    cfg = Config(
        wcl_client_id="client", wcl_client_secret="secret",
        chatlog_path=tmp_path / "Logs" / "WoWChatLog.txt", region="EU",
        cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
        screenshots_path=tmp_path / "Screenshots", log_dir=tmp_path / "logs",
    )
    cfg.sync_with_wow = True
    dialog = SettingsDialog(cfg, wow_startup_state=provider, first_run=first_run)
    qtbot.addWidget(dialog)
    return dialog


@pytest.mark.parametrize(("state", "text"), [
    ("disabled", "Windows has blocked the WoW launch watcher."),
    ("missing", "The WoW launch watcher is not set up."),
    ("unknown", "Could not check the WoW launch watcher."),
])
def test_saved_preference_does_not_hide_windows_failure(qtbot, tmp_path, state, text):
    dialog = _dialog(qtbot, tmp_path, lambda: state)
    assert dialog.sync_with_wow_check.isChecked()
    assert dialog.values().sync_with_wow
    assert not dialog._wow_startup_warning_row.isHidden()
    assert dialog.wow_startup_status_label.text() == text
    assert dialog.wow_startup_repair_button.isEnabled()


def test_repair_requires_click_and_pending_prevents_duplicate(qtbot, tmp_path):
    state = ["disabled"]
    dialog = _dialog(qtbot, tmp_path, lambda: state[0])
    requests = []

    def repair():
        requests.append(True)
        dialog.set_wow_sync_repair_pending(True)

    dialog.wowSyncRepairRequested.connect(repair)
    dialog.refresh_wow_sync_status()
    assert requests == []
    qtbot.mouseClick(dialog.wow_startup_repair_button, Qt.MouseButton.LeftButton)
    assert requests == [True]
    assert not dialog.wow_startup_repair_button.isEnabled()
    assert dialog.wow_startup_status_label.text() == "Enabling background WoW detection…"
    dialog._request_wow_sync_repair()
    assert requests == [True]
    state[0] = "enabled"
    dialog.set_wow_sync_repair_pending(False)
    assert dialog._wow_startup_warning_row.isHidden()


def test_external_windows_change_is_reflected_while_open(qtbot, tmp_path):
    state = ["enabled"]
    dialog = _dialog(qtbot, tmp_path, lambda: state[0])
    dialog.show()
    assert dialog._wow_startup_warning_row.isHidden()
    state[0] = "disabled"
    dialog._poll_wow_startup_status()
    assert not dialog._wow_startup_warning_row.isHidden()
    assert dialog.sync_with_wow_check.isChecked()
    dialog.hide()
    dialog._poll_wow_startup_status()
    assert not dialog._wow_startup_status_timer.isActive()


def test_first_run_does_not_warn_before_startup_has_been_created(qtbot, tmp_path):
    def unexpected_read():
        pytest.fail("First-run setup must not check the not-yet-created shortcut")

    dialog = _dialog(qtbot, tmp_path, unexpected_read, first_run=True)
    assert dialog._wow_startup_warning_row.isHidden()


def test_disabled_preference_hides_warning_and_does_not_request_repair(qtbot, tmp_path):
    dialog = _dialog(qtbot, tmp_path, lambda: "disabled")
    requests = []
    dialog.wowSyncRepairRequested.connect(lambda: requests.append(True))
    dialog.sync_with_wow_check.setChecked(False)
    dialog._request_wow_sync_repair()
    assert dialog._wow_startup_warning_row.isHidden()
    assert requests == []


def test_status_read_failure_is_not_displayed_as_enabled(qtbot, tmp_path):
    def denied():
        raise PermissionError("denied")

    dialog = _dialog(qtbot, tmp_path, denied)
    assert dialog.wow_startup_status_label.text() == "Could not check the WoW launch watcher."


def test_update_and_pending_state_keep_repair_disabled(qtbot, tmp_path):
    dialog = _dialog(qtbot, tmp_path, lambda: "disabled")
    dialog.set_update_in_progress(True)
    dialog.refresh_wow_sync_status()
    assert not dialog.wow_startup_repair_button.isEnabled()
    dialog.set_wow_sync_repair_pending(True)
    dialog.set_update_in_progress(False)
    assert not dialog.wow_startup_repair_button.isEnabled()
    dialog.set_wow_sync_repair_pending(False)
    assert dialog.wow_startup_repair_button.isEnabled()
