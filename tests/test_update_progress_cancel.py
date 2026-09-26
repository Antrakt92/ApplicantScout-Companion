"""Update cancellation never crosses installer handoff or loses retry UI."""

from contextlib import AbstractContextManager
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from test_config import usage_main_harness as usage_main_harness

from applicant_scout import __main__ as main_mod
from applicant_scout import settings_dialog as settings_mod
from applicant_scout import updater
from applicant_scout.config import Config


class _Response(AbstractContextManager):
    def __init__(self, chunks, headers=None):
        self.chunks = chunks
        self.headers = headers or {}
        self.closed = False

    def __exit__(self, *_args):
        self.closed = True

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield from self.chunks


class _Client:
    def __init__(self, result, checksum, installer):
        self.responses = {result.checksum_url: checksum, result.asset_url: installer}
        self.urls = []

    def stream(self, _method, url, **_kwargs):
        self.urls.append(url)
        return self.responses[url]


def _result():
    return updater.UpdateResult(
        status="available", message="Update available", latest_version="0.2.0",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        asset_url="https://objects.githubusercontent.com/setup.exe",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
        checksum_url="https://objects.githubusercontent.com/setup.exe.sha256",
    )


def _checksum(result, content):
    return f"{hashlib.sha256(content).hexdigest()}  {result.asset_name}\n".encode()


@pytest.mark.parametrize("known_length", [False, True])
def test_download_progress_reports_streamed_bytes_and_verification(
    tmp_path, monkeypatch, known_length,
):
    clock = [10.0]
    monkeypatch.setattr(updater.time, "monotonic", lambda: clock[0])
    progress = []
    control = updater.UpdateDownloadControl(progress.append)
    result = _result()
    content = b"abcdefghij"

    def chunks():
        for part in (content[:5], content[5:]):
            clock[0] += 0.11
            yield part

    response = _Response(chunks(), {"content-length": "10"} if known_length else {})
    client = _Client(result, _Response([_checksum(result, content)]), response)
    path = updater.download_update_installer(
        result, download_dir=tmp_path, client=client, control=control,
    )
    downloads = [item for item in progress if item.phase == "downloading"]
    assert [item.downloaded_bytes for item in downloads] == [0, 5, 10]
    assert all(item.total_bytes == (10 if known_length else None) for item in downloads)
    assert "100%" in downloads[-1].message if known_length else "MB" in downloads[-1].message
    assert progress[-1].phase == "verifying"
    assert path.read_bytes() == content
    assert response.closed
    assert not list(tmp_path.glob("*.tmp"))


def test_download_progress_rate_is_bounded_without_delaying_phase_changes(monkeypatch):
    clock = [10.0]
    delivered = []
    monkeypatch.setattr(updater.time, "monotonic", lambda: clock[0])
    control = updater.UpdateDownloadControl(lambda item: delivered.append((clock[0], item)))
    for index in range(1001):
        clock[0] = 10.0 + index / 1000
        control.report(updater.UpdateProgress("downloading", index, 1000))
    assert len(delivered) <= 11
    assert all(b[0] - a[0] >= 0.1 for a, b in zip(delivered, delivered[1:]))
    control.report(updater.UpdateProgress("verifying"))
    assert delivered[-1][1].phase == "verifying"


@pytest.mark.parametrize("stage", ["before", "checksum", "mid", "final", "verified"])
def test_cancelled_download_never_launches_and_removes_partial_file(
    tmp_path, monkeypatch, stage,
):
    result = _result()
    content = b"firstsecond"
    control = updater.UpdateDownloadControl(
        lambda progress: control.cancel() if stage == "verified" and progress.phase == "verifying" else None
    )

    def checksum_chunks():
        if stage == "checksum":
            control.cancel()
        yield _checksum(result, content)

    def installer_chunks():
        yield b"first"
        if stage == "mid":
            control.cancel()
        yield b"second"
        if stage == "final":
            control.cancel()

    checksum_response = _Response(checksum_chunks())
    installer_response = _Response(installer_chunks())
    client = _Client(result, checksum_response, installer_response)
    launches = []
    monkeypatch.setattr(main_mod, "_safe_check_for_update", lambda _version: result)
    monkeypatch.setattr(
        main_mod, "download_update_installer",
        lambda value, *, control: updater.download_update_installer(
            value, download_dir=tmp_path, client=client, control=control,
        ),
    )
    monkeypatch.setattr(main_mod, "launch_update_installer", lambda *args, **kwargs: launches.append((args, kwargs)))
    gate = main_mod._UpdateQuitGate()
    gate.set_update_in_progress(True)
    if stage == "before":
        control.cancel()
    outcome = main_mod._check_updates(update_quit_gate=gate, control=control)

    assert isinstance(outcome, settings_mod.SettingsUpdateResult)
    assert outcome.cancelled
    assert not outcome.installer_handoff
    assert not gate.can_control_quit()
    assert launches == []
    assert not list(tmp_path.glob("*.tmp"))
    if stage != "verified":
        assert not (tmp_path / result.asset_name).exists()
    if stage == "before":
        assert client.urls == []
    else:
        assert checksum_response.closed
    if stage in {"mid", "final", "verified"}:
        assert installer_response.closed


def test_cancellation_and_installation_have_one_atomic_winner():
    control = updater.UpdateDownloadControl()
    barrier = threading.Barrier(3)
    outcomes = {}

    def cancel():
        barrier.wait()
        outcomes["cancel"] = control.cancel()

    def install():
        barrier.wait()
        try:
            control.begin_installation()
            outcomes["install"] = True
        except updater.UpdateCancelled:
            outcomes["install"] = False

    workers = [threading.Thread(target=cancel), threading.Thread(target=install)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert outcomes["cancel"] is not outcomes["install"]
    if outcomes["install"]:
        assert control.cancel() is False
        control.checkpoint()
    else:
        with pytest.raises(updater.UpdateCancelled):
            control.begin_installation()


def test_installer_handoff_rejects_late_cancel(monkeypatch, tmp_path):
    result = _result()
    control = updater.UpdateDownloadControl()
    installer = tmp_path / result.asset_name
    launch = object()
    calls = []
    monkeypatch.setattr(main_mod, "_safe_check_for_update", lambda _version: result)
    monkeypatch.setattr(main_mod, "download_update_installer", lambda _result, **_kwargs: installer)

    def launch_installer(_path, **_kwargs):
        calls.append(control.cancel())
        return launch

    monkeypatch.setattr(main_mod, "launch_update_installer", launch_installer)
    gate = main_mod._UpdateQuitGate()
    gate.set_update_in_progress(True)
    outcome = main_mod._check_updates(update_quit_gate=gate, control=control)
    assert calls == [False]
    assert outcome.installer_handoff
    assert outcome.installer_launch is launch
    assert not outcome.cancelled


def test_settings_cancel_restores_controls_and_keeps_update_available(qtbot, tmp_path):
    cfg = Config(
        wcl_client_id="client", wcl_client_secret="secret", region="EU",
        chatlog_path=tmp_path / "Logs" / "WoWChatLog.txt",
        cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
        screenshots_path=tmp_path / "Screenshots", log_dir=tmp_path / "logs",
    )
    control = updater.UpdateDownloadControl()
    dialog = settings_mod.SettingsDialog(cfg, cancel_update=control.cancel)
    qtbot.addWidget(dialog)
    dialog.updateFinished.connect(lambda _error: dialog.set_update_in_progress(False))
    completed = []
    dialog.updateCompleted.connect(lambda: completed.append(True))
    dialog.set_update_available("0.2.0")
    dialog.set_update_in_progress(True)
    dialog.set_update_progress(updater.UpdateProgress("downloading", 1, 10))
    assert not dialog.test_button.isEnabled()
    assert dialog.cancel_update_button.isEnabled()
    dialog.cancel_update_button.click()
    assert "Cancelling" in dialog.status_label.text()
    dialog.set_update_progress(updater.UpdateProgress("downloading", 9, 10))
    assert "Cancelling" in dialog.status_label.text()
    assert not dialog.cancel_update_button.isEnabled()
    dialog._finish_async_action(settings_mod._AsyncActionResult(
        dialog.update_button, "Update cancelled.", cancelled=True,
    ))
    assert dialog.test_button.isEnabled()
    assert dialog.update_button.isEnabled()
    assert not dialog.update_button.isHidden()
    assert dialog.cancel_update_button.isHidden()
    assert dialog._latest_update_version == "0.2.0"
    assert completed == []
    assert dialog.status_label.text() == "Update cancelled."
    dialog.set_update_progress(updater.UpdateProgress("installing"))
    assert dialog.status_label.text() == "Update cancelled."


@pytest.mark.parametrize("phase", ["cancelled", "installing"])
def test_repeated_busy_state_preserves_cancel_or_install_phase(qtbot, tmp_path, phase):
    cfg = Config(
        wcl_client_id="client", wcl_client_secret="secret", region="EU",
        chatlog_path=tmp_path / "Logs" / "WoWChatLog.txt",
        cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
    )
    control = updater.UpdateDownloadControl()
    dialog = settings_mod.SettingsDialog(cfg, cancel_update=control.cancel)
    qtbot.addWidget(dialog)
    dialog.set_update_in_progress(True)
    if phase == "cancelled":
        dialog.cancel_update_button.click()
    else:
        dialog.set_update_progress(updater.UpdateProgress("installing"))
    # A background update check can reapply busy=True for this same attempt.
    dialog.set_update_in_progress(True)
    if phase == "cancelled":
        dialog.set_update_progress(updater.UpdateProgress("downloading", 5, 10))
        assert "Cancelling" in dialog.status_label.text()
        assert not dialog.cancel_update_button.isEnabled()
    else:
        assert dialog.cancel_update_button.isHidden()


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("phase", ["checksum", "installer"])
def test_network_failure_after_cancel_reports_cancelled_not_failed(
    monkeypatch, tmp_path, cancel, phase,
):
    result = _result()
    control = updater.UpdateDownloadControl()
    monkeypatch.setattr(main_mod, "_safe_check_for_update", lambda _version: result)

    def stalled_chunks():
        yield b"partial"
        if cancel:
            control.cancel()
        raise httpx.ReadTimeout("synthetic stalled download")

    client = _Client(
        result,
        _Response(stalled_chunks() if phase == "checksum" else [_checksum(result, b"partial")]),
        _Response(stalled_chunks()),
    )
    monkeypatch.setattr(
        main_mod, "download_update_installer",
        lambda result, *, control: updater.download_update_installer(
            result, download_dir=tmp_path, client=client, control=control,
        ),
    )
    launch = MagicMock()
    monkeypatch.setattr(main_mod, "launch_update_installer", launch)
    gate = main_mod._UpdateQuitGate()
    gate.set_update_in_progress(True)
    if cancel:
        outcome = main_mod._check_updates(update_quit_gate=gate, control=control)
        assert isinstance(outcome, settings_mod.SettingsUpdateResult)
        assert outcome.cancelled
    else:
        with pytest.raises(httpx.ReadTimeout):
            main_mod._check_updates(update_quit_gate=gate, control=control)
    launch.assert_not_called()
    assert not list(tmp_path.glob("*.tmp"))


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


def test_main_controls_share_attempt_and_drop_old_progress_after_retry(
    usage_main_harness, monkeypatch,
):
    signals = MagicMock(checked=_Signal(), completed=_Signal(), progressed=_Signal())
    monkeypatch.setattr(main_mod, "UpdateSignals", lambda _app: signals)
    tray = MagicMock()
    callbacks = {}
    workers = []
    created_controls = []
    received_controls = []
    dialog = MagicMock()
    dialog.flush_pending_values.return_value = True
    dialog_arguments = {}

    def make_dialog(*_args, **kwargs):
        dialog_arguments.update(kwargs)
        return dialog

    def make_tray(*_args, **kwargs):
        callbacks.update(kwargs)
        return tray

    def make_control(on_progress):
        control = updater.UpdateDownloadControl(on_progress)
        created_controls.append(control)
        return control

    def finish_cancelled(*, update_quit_gate, control):
        assert update_quit_gate.update_in_progress
        received_controls.append(control)
        with pytest.raises(updater.UpdateCancelled):
            control.checkpoint()
        return settings_mod.SettingsUpdateResult("Update cancelled.", cancelled=True)

    monkeypatch.setattr(main_mod, "SettingsDialog", make_dialog)
    monkeypatch.setattr(main_mod, "_create_tray_controller", make_tray)
    monkeypatch.setattr(main_mod, "UpdateDownloadControl", make_control)
    monkeypatch.setattr(main_mod, "_check_updates", finish_cancelled)
    monkeypatch.setattr(main_mod, "_start_daemon_thread", lambda callback, **_kwargs: workers.append(callback))

    def event_loop(*_args, **_kwargs):
        signals.checked.emit(0, _result())
        callbacks["run_update"]()
        assert len(created_controls) == 1
        first = created_controls[0]
        first.report(updater.UpdateProgress("downloading", 5, 10))
        assert dialog.set_update_progress.call_args.args[0].downloaded_bytes == 5
        assert dialog_arguments["cancel_update"]()
        workers.pop(0)()
        assert received_controls == [first]
        assert tray.set_update_available.call_args.args[0] == "0.2.0"
        progress_count = dialog.set_update_progress.call_count
        first.report(updater.UpdateProgress("verifying"))
        assert dialog.set_update_progress.call_count == progress_count
        callbacks["run_update"]()
        assert len(created_controls) == 2
        second = created_controls[1]
        signals.progressed.emit(first, updater.UpdateProgress("installing"))
        assert dialog.set_update_progress.call_count == progress_count
        second.report(updater.UpdateProgress("checking"))
        assert dialog.set_update_progress.call_count == progress_count + 1
        assert dialog_arguments["cancel_update"]()
        workers.pop(0)()
        assert received_controls == [first, second]
        assert tray.set_update_available.call_args.args[0] == "0.2.0"
        return 0

    usage_main_harness.run_loop.side_effect = event_loop
    assert main_mod.main([]) == 0

class _ProgressFakeTimer:
    instances: "list[_ProgressFakeTimer]" = []

    def __init__(self, _parent=None) -> None:
        self.interval = None
        self.started = False
        self.stopped = False
        self.timeout = _Signal()
        _ProgressFakeTimer.instances.append(self)

    def setInterval(self, interval: int) -> None:
        self.interval = interval

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _progress_controller(monotonic, recovered):
    return main_mod._UpdateHandoffRecoveryController(
        None,
        on_recover=lambda message, retry: recovered.append((message, retry)),
        timer_factory=_ProgressFakeTimer,
        monotonic=monotonic,
        timeout_ms=60_000,
        poll_interval_ms=50,
    )


def test_install_progress_reports_percent_then_holds_and_caps(tmp_path):
    _ProgressFakeTimer.instances.clear()
    clock = [0.0]
    recovered = []
    seen: list[updater.UpdateProgress] = []
    state = {"exited": False}
    launch = SimpleNamespace(poll=lambda: 0 if state["exited"] else None)
    controller = _progress_controller(lambda: clock[0], recovered)
    staging = tmp_path / "staging"
    staging.mkdir()

    controller.arm(
        launch,
        on_install_progress=seen.append,
        install_estimator=updater.InstallProgressEstimator(3000),
        install_staging_dir=staging,
    )

    assert [timer.interval for timer in _ProgressFakeTimer.instances] == [50, 300]
    recovery_timer, progress_timer = _ProgressFakeTimer.instances
    (staging / "part.bin").write_bytes(b"x" * 1000)
    progress_timer.timeout.emit()
    (staging / "part.bin").write_bytes(b"x" * 1500)
    progress_timer.timeout.emit()
    # Promotion phase moves the payload away: progress must hold, not regress.
    (staging / "part.bin").unlink()
    staging.rmdir()
    progress_timer.timeout.emit()
    # Anchor overshoot (factor underestimate) still caps below completion.
    staging.mkdir()
    (staging / "part.bin").write_bytes(b"x" * 9000)
    progress_timer.timeout.emit()

    assert [item.message for item in seen] == [
        "Installing update\u2026 33%",
        "Installing update\u2026 50%",
        "Installing update\u2026 50%",
        "Installing update\u2026 99%",
    ]
    assert not progress_timer.stopped

    state["exited"] = True
    progress_timer.timeout.emit()

    assert seen[-1] == updater.UpdateProgress("installing", 3000, 3000)
    assert seen[-1].message == "Installing update\u2026 100%"
    assert progress_timer.stopped
    # The existing handoff/recovery verification is untouched: process exit
    # still recovers with retry through the recovery timer.
    assert recovered == []
    assert not recovery_timer.stopped
    recovery_timer.timeout.emit()
    assert recovered == [(main_mod.UPDATE_HANDOFF_INSTALLER_EXITED_MESSAGE, True)]
    assert recovery_timer.stopped


def test_install_progress_stops_quietly_when_poll_fails():
    _ProgressFakeTimer.instances.clear()
    recovered = []
    seen: list[updater.UpdateProgress] = []

    def fail_poll():
        raise RuntimeError("synthetic poll failure")

    controller = _progress_controller(lambda: 0.0, recovered)
    controller.arm(
        SimpleNamespace(poll=fail_poll),
        on_install_progress=seen.append,
        install_estimator=updater.InstallProgressEstimator(3000),
        install_staging_dir=Path("."),
    )

    _ProgressFakeTimer.instances[-1].timeout.emit()

    # No definitive exit observed, so no 100%; the failure is fail-quiet and
    # the existing recovery tick still owns the exit verdict.
    assert seen == []
    assert _ProgressFakeTimer.instances[-1].stopped
    assert recovered == []


def test_install_handoff_without_progress_basis_arms_single_timer():
    _ProgressFakeTimer.instances.clear()
    recovered = []
    controller = _progress_controller(lambda: 0.0, recovered)

    controller.arm(SimpleNamespace(poll=lambda: None))

    assert len(_ProgressFakeTimer.instances) == 1
    assert _ProgressFakeTimer.instances[0].interval == 50


def test_begin_installation_with_total_keeps_installation_non_cancellable():
    control = updater.UpdateDownloadControl()
    control.begin_installation(total_bytes=3000)

    assert control.cancel() is False
    control.checkpoint()

    late = updater.UpdateDownloadControl()
    assert late.cancel() is True
    with pytest.raises(updater.UpdateCancelled):
        late.begin_installation(total_bytes=3000)


def test_settings_install_progress_shows_percent_and_hides_cancel(qtbot, tmp_path):
    cfg = Config(
        wcl_client_id="client", wcl_client_secret="secret", region="EU",
        chatlog_path=tmp_path / "Logs" / "WoWChatLog.txt",
        cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
        screenshots_path=tmp_path / "Screenshots", log_dir=tmp_path / "logs",
    )
    control = updater.UpdateDownloadControl()
    dialog = settings_mod.SettingsDialog(cfg, cancel_update=control.cancel)
    qtbot.addWidget(dialog)
    dialog.set_update_available("0.2.0")
    dialog.set_update_in_progress(True)
    control.begin_installation(total_bytes=3000)

    dialog.set_update_progress(updater.UpdateProgress("installing", 1500, 3000))

    assert dialog.status_label.text() == "Installing update\u2026 50%"
    assert dialog.cancel_update_button.isHidden()
    assert control.cancel() is False
