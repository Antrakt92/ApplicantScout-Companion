"""Consent intent, disk publication and reporting follow the latest user choice."""

import json
import threading

import pytest

from applicant_scout import atomic_io
from applicant_scout.config import Config
from applicant_scout.settings_dialog import SettingsDialog
import applicant_scout.settings_dialog as settings_mod
import applicant_scout.usage as usage


@pytest.fixture(autouse=True)
def private_local_writes(monkeypatch):
    monkeypatch.setattr(atomic_io, "apply_private_directory_mode", lambda *_a, **_kw: True)
    monkeypatch.setattr(atomic_io, "apply_private_file_mode", lambda *_a, **_kw: True)
    monkeypatch.setattr(SettingsDialog, "_start_screenshots_validation", lambda *_a, **_kw: None)


def _client(tmp_path):
    return usage.UsageClient(tmp_path, "0.18.2", endpoint="", consent=False)


def _saved(client):
    return json.loads(client._path.read_text(encoding="utf-8"))


def _dialog(qtbot, tmp_path, client):
    cfg = Config(
        wcl_client_id="", wcl_client_secret="", region="EU",
        chatlog_path=tmp_path / "Logs/WoWChatLog.txt",
        cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
        screenshots_path=tmp_path / "Screenshots", log_dir=tmp_path / "logs",
    )
    dialog = SettingsDialog(cfg, first_run=True, usage_client=client)
    qtbot.addWidget(dialog)
    return dialog


@pytest.mark.parametrize("reopen", [False, True])
def test_delayed_settings_workers_cannot_reverse_latest_optout(qtbot, tmp_path, monkeypatch, reopen):
    client = _client(tmp_path)
    dialog = _dialog(qtbot, tmp_path, client)
    callbacks = []

    class QueuedThread:
        def __init__(self, *, target, **_kwargs):
            callbacks.append(target)

        def start(self):
            pass

    monkeypatch.setattr(settings_mod.threading, "Thread", QueuedThread)
    try:
        dialog.usage_check.setChecked(True)
        if reopen:
            dialog = _dialog(qtbot, tmp_path, client)
            dialog.usage_check.setChecked(True)
        dialog.usage_check.setChecked(False)
        assert callbacks
        # The latest worker starts first; older UI work must not re-register intent.
        for callback in reversed(callbacks):
            callback()
        qtbot.waitUntil(lambda: not dialog.usage_check.isChecked())
        assert not client.consent_enabled
        assert _saved(client)["consent"] is False
    finally:
        client.close()

def test_publication_guard_checks_after_fsync_and_cleans_retired_candidate(tmp_path, monkeypatch):
    client = _client(tmp_path)
    request = client.request_consent(True)
    original_fsync = atomic_io.os.fsync
    newer = []

    def fsync_then_revoke(fd):
        original_fsync(fd)
        if not newer:
            newer.append(client.request_consent(False))

    monkeypatch.setattr(atomic_io.os, "fsync", fsync_then_revoke)
    try:
        assert client.apply_consent(request) is False
        assert _saved(client)["consent"] is False
        assert not list(tmp_path.glob(".usage.json.*.tmp"))
        assert client.apply_consent(newer[0]) is True
        assert not client.consent_enabled
    finally:
        client.close()


def test_registered_requests_are_client_owned_and_replays_preserve_identity(tmp_path):
    first = _client(tmp_path / "first")
    second = _client(tmp_path / "second")
    try:
        request = first.request_consent(True)
        assert second.apply_consent(request) is False
        assert _saved(second)["consent"] is False
        assert first.apply_consent(request)
        first._install_id = "established-identity"
        first._seen = ["established-history"]
        before = first._path.read_bytes()
        assert first.apply_consent(request)
        assert first._install_id == "established-identity"
        assert first._seen == ["established-history"]
        assert first._path.read_bytes() == before
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_latest_failure_stays_off_and_identical_choice_can_retry(tmp_path, monkeypatch, enabled):
    client = _client(tmp_path)
    if not enabled:
        client.set_consent(True)
    original = usage.atomic_write_text

    def failed(*_args, **_kwargs):
        raise OSError("synthetic disk failure")

    try:
        request = client.request_consent(enabled)
        monkeypatch.setattr(usage, "atomic_write_text", failed)
        with pytest.raises(usage.UsagePersistenceError):
            client.apply_consent(request)
        assert not client.consent_enabled
        assert not client.record("addon_received")
        monkeypatch.setattr(usage, "atomic_write_text", original)
        retry = client.request_consent(enabled)
        assert client.apply_consent(retry)
        assert client.consent_enabled is enabled
        assert _saved(client)["consent"] is enabled
    finally:
        client.close()


def test_stale_write_failure_does_not_poison_latest_choice(tmp_path, monkeypatch):
    client = _client(tmp_path)
    old = client.request_consent(True)
    newer = []
    original = usage.atomic_write_text

    def superseded_failure(path, text, **kwargs):
        if '"consent": true' in text:
            newer.append(client.request_consent(False))
            raise OSError("old synthetic failure")
        original(path, text, **kwargs)

    monkeypatch.setattr(usage, "atomic_write_text", superseded_failure)
    try:
        assert client.apply_consent(old) is False
        assert client.apply_consent(newer[0])
        assert not client._failed
        assert not client.consent_enabled
        assert _saved(client)["consent"] is False
    finally:
        client.close()


def test_retirement_between_publication_and_activation_cannot_enable(tmp_path, monkeypatch):
    client = _client(tmp_path)
    original = client._enable
    newer = []

    def delayed_enable(request=None):
        newer.append(client.request_consent(False))
        assert client.apply_consent(newer[-1])
        return original(request)

    monkeypatch.setattr(client, "_enable", delayed_enable)
    try:
        assert client.apply_consent(client.request_consent(True)) is False
        assert not client.consent_enabled
        assert _saved(client)["consent"] is False
    finally:
        client.close()


def test_settings_thread_start_failure_uses_registered_intent(qtbot, tmp_path, monkeypatch):
    client = _client(tmp_path)
    dialog = _dialog(qtbot, tmp_path, client)

    class UnavailableThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("synthetic thread exhaustion")

    monkeypatch.setattr(settings_mod.threading, "Thread", UnavailableThread)
    try:
        dialog.usage_check.setChecked(True)
        qtbot.waitUntil(lambda: client.consent_enabled)
        dialog.usage_check.setChecked(False)
        qtbot.waitUntil(lambda: not client.consent_enabled)
        assert _saved(client)["consent"] is False
    finally:
        client.close()


def test_close_preserves_established_choice_but_rejects_later_requests(tmp_path):
    client = _client(tmp_path)
    client.set_consent(True)
    before = client._path.read_bytes()
    client.close()
    assert client.apply_consent(client.request_consent(False)) is False
    assert client._path.read_bytes() == before
    assert not client.record("addon_received")


@pytest.mark.parametrize("phase", ["prepare", "acknowledgement"])
def test_reporting_worker_cannot_publish_old_consent_after_revocation(tmp_path, monkeypatch, phase):
    entered, release = threading.Event(), threading.Event()
    sent = []
    original_write = usage.atomic_write_text
    original_replace = atomic_io.os.replace
    published_after_revoke = []
    revoked = threading.Event()

    def blocked(path, text, **kwargs):
        data = json.loads(text)
        match = bool(data.get("install_id")) and (
            not data.get("seen") if phase == "prepare" else bool(data.get("seen"))
        )
        if match and not entered.is_set():
            entered.set()
            assert release.wait(3)
        original_write(path, text, **kwargs)

    def replace_record(source, target):
        if revoked.is_set() and target.name == "usage.json":
            published_after_revoke.append(json.loads(source.read_text(encoding="utf-8")))
        return original_replace(source, target)

    monkeypatch.setattr(usage, "atomic_write_text", blocked)
    monkeypatch.setattr(atomic_io.os, "replace", replace_record)
    client = usage.UsageClient(
        tmp_path, "0.18.2", endpoint="https://usage.example.com/v1/events",
        consent=True, _sender=lambda _endpoint, payload: sent.append(payload) or 204,
    )
    try:
        assert entered.wait(2)
        request = client.request_consent(False)
        revoked.set()
        assert not client.consent_enabled
        release.set()
        assert client.apply_consent(request)
        assert _saved(client) == {"schema": 1, "consent": False}
        assert all(row["consent"] is False for row in published_after_revoke)
        assert len(sent) == (0 if phase == "prepare" else 1)
        assert not client.record("addon_received")
    finally:
        release.set()
        client.close()


def test_reporting_thread_start_failure_is_fail_closed_and_retryable(tmp_path, monkeypatch):
    client = usage.UsageClient(
        tmp_path, "0.18.2", endpoint="https://usage.example.com/v1/events",
        consent=False, _sender=lambda *_args: 204,
    )
    real_thread = usage.threading.Thread

    class UnavailableThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("synthetic thread exhaustion")

    try:
        monkeypatch.setattr(usage.threading, "Thread", UnavailableThread)
        client.set_consent(True)
        assert client.consent_enabled  # Explicit saved choice survives collector failure.
        assert client._failed
        assert not client.record("addon_received")
        monkeypatch.setattr(usage.threading, "Thread", real_thread)
        client.set_consent(True)
        assert client.consent_enabled
    finally:
        client.close()


def test_settings_optout_retires_optin_already_preparing_disk(qtbot, tmp_path, monkeypatch):
    client = _client(tmp_path)
    dialog = _dialog(qtbot, tmp_path, client)
    entered, release = threading.Event(), threading.Event()
    original = usage.atomic_write_text

    def blocked(path, text, **kwargs):
        if '"consent": true' in text:
            entered.set()
            assert release.wait(3)
        original(path, text, **kwargs)

    monkeypatch.setattr(usage, "atomic_write_text", blocked)
    outcomes = []
    dialog.usageConsentSaveFinished.connect(lambda *args: outcomes.append(args))
    try:
        dialog.usage_check.setChecked(True)
        assert entered.wait(2)
        dialog.usage_check.setChecked(False)
        assert not dialog.usage_check.isChecked()
        assert not client.consent_enabled
        release.set()
        qtbot.waitUntil(lambda: any(result[0] == 2 for result in outcomes), timeout=3000)
        assert len(outcomes) == 1  # Retired completion cannot announce success.
        assert not client.consent_enabled
        assert _saved(client)["consent"] is False
        assert not dialog.usage_check.isChecked()
        assert not list(tmp_path.glob(".usage.json.*.tmp"))
    finally:
        release.set()
        client.close()


def test_close_retires_preparing_optin_without_publishing_it(tmp_path, monkeypatch):
    client = _client(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = usage.atomic_write_text

    def blocked(path, text, **kwargs):
        entered.set()
        assert release.wait(3)
        original(path, text, **kwargs)

    monkeypatch.setattr(usage, "atomic_write_text", blocked)
    worker = threading.Thread(target=client.set_consent, args=(True,))
    worker.start()
    try:
        assert entered.wait(2)
        client.close()
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert not client.consent_enabled
        assert _saved(client)["consent"] is False
    finally:
        release.set()
        client.close()


def test_identical_established_consent_preserves_install_identity(tmp_path):
    client = _client(tmp_path)
    try:
        client.set_consent(True)
        before = client._path.read_bytes()
        generation = client._generation
        client.set_consent(True)
        assert client.consent_enabled
        assert client._path.read_bytes() == before
        assert client._generation == generation
    finally:
        client.close()


def test_settings_revokes_running_consent_before_worker_is_scheduled(qtbot, tmp_path, monkeypatch):
    client = _client(tmp_path)
    client.set_consent(True)
    dialog = _dialog(qtbot, tmp_path, client)
    callbacks = []

    class QueuedThread:
        def __init__(self, *, target, **_kwargs):
            callbacks.append(target)

        def start(self):
            pass

    monkeypatch.setattr(settings_mod.threading, "Thread", QueuedThread)
    try:
        dialog.usage_check.setChecked(False)
        assert callbacks  # Persistence has not run yet.
        assert not client.consent_enabled
        assert not client.record("addon_received")
        callbacks[0]()
        assert _saved(client)["consent"] is False
    finally:
        client.close()


@pytest.mark.parametrize("choices", [(True, False, True), (True, True, False)])
def test_registered_bursts_follow_intent_order_not_application_order(tmp_path, choices):
    client = _client(tmp_path)
    try:
        requests = [client.request_consent(choice) for choice in choices]
        assert client.apply_consent(requests[-1])
        for request in reversed(requests[:-1]):
            assert client.apply_consent(request) is False
        assert client.consent_enabled is choices[-1]
        assert _saved(client)["consent"] is choices[-1]
    finally:
        client.close()
