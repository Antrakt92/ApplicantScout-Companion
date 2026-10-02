"""Unavailable consent workers must not turn Settings into a disk worker."""

from threading import Event, Thread
import time

import pytest
from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication

from applicant_scout import atomic_io
from applicant_scout.settings_dialog import SettingsDialog
import applicant_scout.settings_dialog as settings_mod
import applicant_scout.usage as usage
from test_usage_settings import dialog_for


@pytest.fixture(autouse=True)
def local_private_writes(monkeypatch):
    monkeypatch.setattr(atomic_io, "apply_private_directory_mode", lambda *_a, **_kw: True)
    monkeypatch.setattr(atomic_io, "apply_private_file_mode", lambda *_a, **_kw: True)
    monkeypatch.setattr(SettingsDialog, "_start_screenshots_validation", lambda *_a, **_kw: None)


class _FailedThread:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        raise RuntimeError("synthetic worker launch failure")


def _client(tmp_path, enabled=False):
    return usage.UsageClient(tmp_path, "0.18.2", endpoint="", consent=enabled)


@pytest.mark.parametrize("initial", [False, True])
def test_actual_settings_launch_failure_never_saves_on_gui_and_reports_retry(
    qtbot, tmp_path, monkeypatch, initial,
):
    client = _client(tmp_path, initial)
    before = client._path.read_bytes()
    dialog = dialog_for(qtbot, tmp_path, client)
    writes = []
    original = usage.atomic_write_text

    def observed_write(*args, **kwargs):
        writes.append(QThread.currentThread() is QApplication.instance().thread())
        return original(*args, **kwargs)

    monkeypatch.setattr(usage, "atomic_write_text", observed_write)
    monkeypatch.setattr(settings_mod.threading, "Thread", _FailedThread)
    outcomes = []
    dialog.usageConsentSaveFinished.connect(lambda *args: outcomes.append(args))
    try:
        dialog.usage_check.setChecked(not initial)
        qtbot.waitUntil(lambda: bool(outcomes))
        assert writes == []
        assert not outcomes[-1][1]
        assert not client.consent_enabled
        assert not dialog.usage_check.isChecked()
        assert client._path.read_bytes() == before
        text = dialog.status_label.text().lower()
        assert "background" in text and "retry" in text
        assert "previous choice" in text and "restart" in text
        assert "writable" not in text
    finally:
        client.close()


def test_launch_failure_does_not_wait_for_prior_state_writer(qtbot, tmp_path, monkeypatch):
    client = _client(tmp_path, True)
    dialog = dialog_for(qtbot, tmp_path, client)
    ready, released = Event(), Event()

    def hold_prior_writer():
        with client._state_lock:
            ready.set()
            time.sleep(0.2)
        released.set()

    helper = Thread(target=hold_prior_writer)
    helper.start()
    assert ready.wait(1)
    monkeypatch.setattr(settings_mod.threading, "Thread", _FailedThread)
    ticks = []
    try:
        QTimer.singleShot(20, lambda: ticks.append(released.is_set()))
        dialog.usage_check.setChecked(False)
        qtbot.waitUntil(lambda: bool(ticks))
        assert ticks == [False], "The GUI waited for the prior persistence lock"
        assert not client.consent_enabled
    finally:
        helper.join(1)
        client.close()


def test_missing_dispatcher_reports_failure_without_synchronous_save(qtbot, tmp_path, monkeypatch):
    client = _client(tmp_path)
    dialog = dialog_for(qtbot, tmp_path, client)
    before = client._path.read_bytes()
    writes = []
    original = usage.atomic_write_text

    def observed_write(*args, **kwargs):
        writes.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(usage, "atomic_write_text", observed_write)
    monkeypatch.setattr(settings_mod, "_dialog_worker_dispatcher", lambda _app: None)
    outcomes = []
    dialog.usageConsentSaveFinished.connect(lambda *args: outcomes.append(args))
    try:
        dialog.usage_check.setChecked(True)
        assert writes == []
        assert outcomes and not outcomes[-1][1]
        assert not client.consent_enabled
        assert not dialog.usage_check.isChecked()
        assert client._path.read_bytes() == before
    finally:
        client.close()

def test_client_launch_failure_only_retires_current_pending_request(tmp_path):
    client = _client(tmp_path)
    try:
        old = client.request_consent(True)
        latest = client.request_consent(False)
        before = client._path.read_bytes()
        assert not client.fail_consent_request(old)
        assert not client._failed
        assert client.apply_consent(latest)
        assert client._path.read_bytes() == before
        assert not client.fail_consent_request(latest)
        optin = client.request_consent(True)
        assert client.fail_consent_request(optin)
        assert not client._consent_write_pending
        assert client._failed
        assert not client.apply_consent(optin)
        assert client._path.read_bytes() == before
        retry = client.request_consent(True)
        assert client.apply_consent(retry)
        assert client.consent_enabled
    finally:
        client.close()


def test_closed_or_foreign_request_failure_does_not_touch_saved_choice(tmp_path):
    first, second = _client(tmp_path / "first"), _client(tmp_path / "second", True)
    request = first.request_consent(True)
    before = second._path.read_bytes()
    try:
        assert not second.fail_consent_request(request)
        assert second.consent_enabled
        assert second._path.read_bytes() == before
        second.close()
        assert not second.fail_consent_request(second.request_consent(False))
        assert second._path.read_bytes() == before
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("dispatcher_missing", [False, True])
def test_identical_established_choice_needs_no_worker_or_state_writer_lock(
    qtbot, tmp_path, monkeypatch, dispatcher_missing,
):
    client = _client(tmp_path, True)
    dialog = dialog_for(qtbot, tmp_path, client)
    before = client._path.read_bytes()
    ready, released = Event(), Event()

    def hold_writer():
        with client._state_lock:
            ready.set()
            assert released.wait(2)

    helper = Thread(target=hold_writer)
    helper.start()
    assert ready.wait(1)
    monkeypatch.setattr(settings_mod.threading, "Thread", _FailedThread)
    if dispatcher_missing:
        monkeypatch.setattr(settings_mod, "_dialog_worker_dispatcher", lambda _app: None)
    changes = []
    dialog.usageConsentChanged.connect(changes.append)
    try:
        # Re-emitting the unchanged value is metadata-only, even with no worker.
        dialog.usage_check.toggled.emit(True)
        qtbot.waitUntil(lambda: changes == [True])
        assert not released.is_set()
        assert client.consent_enabled
        assert not client._failed
        assert dialog.usage_check.isChecked()
        assert client._path.read_bytes() == before
    finally:
        released.set()
        helper.join(1)
        client.close()


def test_old_worker_launch_failure_cannot_abort_newly_registered_choice(
    qtbot, tmp_path, monkeypatch,
):
    client = _client(tmp_path)
    dialog = dialog_for(qtbot, tmp_path, client)
    latest = []

    class SupersededLaunch:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            latest.append(client.request_consent(False))
            raise RuntimeError("old launch failed after a newer intent")

    monkeypatch.setattr(settings_mod.threading, "Thread", SupersededLaunch)
    results = []
    dialog.usageConsentSaveFinished.connect(lambda *args: results.append(args))
    try:
        dialog.usage_check.setChecked(True)
        QApplication.processEvents()
        assert not results
        assert not client._failed
        assert client.apply_consent(latest[0])
        assert not client.consent_enabled
    finally:
        client.close()
