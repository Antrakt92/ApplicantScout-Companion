"""GUI-thread responsiveness: async settings pipeline, first-run apply, dialogs.

Covers the H2/H3/M1/M2/M3/M4-tray/L1 behavior changes plus the M4 manual
rollback message. Fassuite runs headless (offscreen) unless noted.
"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import weakref

import pytest
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication

import applicant_scout.__main__ as main_mod
import applicant_scout.settings_dialog as settings_mod
from applicant_scout.config import Config
from applicant_scout.settings_dialog import SettingsDialog


def _cfg(tmp_path: Path, **overrides) -> Config:
    screenshots = tmp_path / "Screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "wcl_client_id": "example-client",
        "wcl_client_secret": "example-secret",
        "chatlog_path": tmp_path / "Logs/WoWChatLog.txt",
        "region": "EU",
        "cache_dir": tmp_path / "cache",
        "config_dir": tmp_path / "config",
        "config_path": tmp_path / "config/config.env",
        "screenshots_path": screenshots,
        "sync_with_wow": True,
    }
    kwargs.update(overrides)
    return Config(**kwargs)


def _values(cfg: Config, **overrides):
    payload = {
        "wcl_client_id": cfg.wcl_client_id,
        "wcl_client_secret": cfg.wcl_client_secret,
        "region": cfg.region,
        "screenshots_path": str(cfg.screenshots_path or ""),
        "metric_preferences": cfg.metric_preferences,
        "sync_with_wow": cfg.sync_with_wow,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


@pytest.fixture
def isolated_overrides(monkeypatch: pytest.MonkeyPatch):
    for key in main_mod._settings_env_override_keys():
        monkeypatch.delenv(key, raising=False)


def _applier(monkeypatch, cfg, runner):
    for key in main_mod._settings_env_override_keys():
        monkeypatch.delenv(key, raising=False)
    return main_mod._CoalescedSettingsApplier(
        None, current_cfg=lambda: cfg, runner=runner
    )


def test_apply_pipeline_coalesces_bursts_into_latest_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # H3: a burst collapses into a single watcher/overlay commit with the
    # latest values; the superseded worker result is skipped.
    cfg = _cfg(tmp_path)
    workers: list = []
    commits: list = []
    busy = []
    applier = _applier(monkeypatch, cfg, workers.append)
    applier.configure(on_busy=lambda: busy.append(True), on_ready=commits.append)

    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    applier.submit(_values(cfg, region="KR"), apply_credentials=False)
    applier.submit(_values(cfg, region="TW"), apply_credentials=False)

    assert busy == [True, True, True]
    assert len(workers) == 1
    workers.pop(0)()  # first generation finishes superseded: no commit
    assert commits == []
    assert len(workers) == 1
    workers.pop(0)()  # latest generation commits
    assert [c.values.region for c in commits] == ["TW"]
    assert commits[0].ok is True
    assert commits[0].prepared is not None
    assert commits[0].prepared.new_cfg.region == "TW"


def test_apply_pipeline_never_drops_credential_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # H3: a superseded commit still runs when it promotes credentials that a
    # newer draft-only item would not repeat.
    cfg = _cfg(tmp_path)
    workers: list = []
    commits: list = []
    applier = _applier(monkeypatch, cfg, workers.append)
    applier.configure(on_ready=commits.append)

    applier.submit(_values(cfg, region="US"), apply_credentials=True)
    applier.submit(_values(cfg, region="KR"), apply_credentials=False)
    workers.pop(0)()
    assert [(c.values.region, c.apply_credentials) for c in commits] == [("US", True)]
    assert len(workers) == 1
    workers.pop(0)()
    assert [(c.values.region, c.apply_credentials) for c in commits] == [
        ("US", True),
        ("KR", False),
    ]


def test_apply_pipeline_reports_worker_failure_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = _cfg(tmp_path)
    workers: list = []
    commits: list = []
    applier = _applier(monkeypatch, cfg, workers.append)
    applier.configure(on_ready=commits.append)

    real_prepare = main_mod._prepare_settings_apply

    def fail_prepare(*_args, **_kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(main_mod, "_prepare_settings_apply", fail_prepare)
    applier.submit(_values(cfg), apply_credentials=False)
    workers.pop(0)()
    assert len(commits) == 1
    assert commits[0].ok is False
    assert "disk gone" in str(commits[0].error)

    monkeypatch.setattr(main_mod, "_prepare_settings_apply", real_prepare)
    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    assert len(workers) == 1
    workers.pop(0)()
    assert len(commits) == 2
    assert commits[1].ok is True


def test_apply_pipeline_drain_waits_for_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = _cfg(tmp_path)
    workers: list = []
    commits: list = []
    applier = _applier(monkeypatch, cfg, workers.append)
    applier.configure(on_ready=commits.append)

    applier.submit(_values(cfg), apply_credentials=False)
    assert applier.drain(timeout_s=0.05) is False
    workers.pop(0)()
    assert len(commits) == 1
    assert applier.drain(timeout_s=2.0) is True


def test_apply_pipeline_commits_from_worker_thread(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # H3: end to end across a real worker thread — persist off the GUI thread,
    # commit marshalled back through the finished signal.
    cfg = _cfg(tmp_path)
    commits: list = []
    applier = _applier(monkeypatch, cfg, None)
    applier.configure(on_ready=commits.append)

    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    qtbot.waitUntil(lambda: bool(commits), timeout=5000)

    assert len(commits) == 1
    assert commits[0].ok is True
    assert commits[0].prepared is not None
    assert commits[0].prepared.new_cfg.region == "US"


def test_apply_pipeline_inline_runner_is_fully_synchronous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = _cfg(tmp_path)
    commits: list = []
    applier = _applier(monkeypatch, cfg, lambda _worker: _worker())
    applier.configure(on_ready=commits.append)

    applier.submit(_values(cfg, region="US"), apply_credentials=False)

    assert len(commits) == 1
    assert commits[0].ok is True
    assert commits[0].prepared is not None
    assert commits[0].prepared.new_cfg.region == "US"


def _dialog_cfg(tmp_path: Path) -> Config:
    retail_root = tmp_path / "World of Warcraft" / "_retail_"
    (retail_root / "Interface" / "AddOns").mkdir(parents=True, exist_ok=True)
    return Config(
        wcl_client_id="client",
        wcl_client_secret="secret",
        chatlog_path=retail_root / "Logs" / "WoWChatLog.txt",
        region="EU",
        cache_dir=tmp_path / "cache",
        config_dir=tmp_path / "config",
        screenshots_path=retail_root / "Screenshots",
        log_dir=tmp_path / "logs",
    )


class _ProbeKillSignal:
    """Minimal Qt-signal stand-in for the fake probe process.

    The dialog only connects/disconnects its slots; nothing is emitted
    because stuck-probe tests drive completion and cancellation
    synchronously instead of racing a real child process. Bound methods
    are held weakly so the fake never keeps a deleted dialog wrapper
    alive (mirrors Qt connection lifetime).
    """

    def __init__(self) -> None:
        self._callbacks: list = []

    def connect(self, callback) -> None:
        try:
            callback = weakref.WeakMethod(callback)
        except TypeError:
            pass
        self._callbacks.append(callback)

    def disconnect(self, *callbacks) -> None:
        if not callbacks:
            self._callbacks.clear()
            return
        targets = set(callbacks)
        kept = []
        for stored in self._callbacks:
            live = stored() if isinstance(stored, weakref.WeakMethod) else stored
            if live is None or live not in targets:
                kept.append(stored)
        self._callbacks = kept


class FakeProbeProcess:
    """Deterministic stuck-probe double injected as settings_mod.QProcess.

    Follows the proven FakeProcess pattern: start() stays Running until
    kill(), waitForFinished()/waitForStarted() return immediately, and no
    real child is spawned, so kill/timeout assertions never depend on
    process-startup timing.
    """

    ProcessState = QProcess.ProcessState
    ExitStatus = QProcess.ExitStatus
    ProcessError = QProcess.ProcessError

    def __init__(self, _parent: object = None) -> None:
        self._running = False
        self._properties: dict = {}
        self.program: str | None = None
        self.arguments: list = []
        self.kill_calls = 0
        self.wait_for_finished_calls: list = []
        self.wait_for_started_calls: list = []
        self.delete_later_calls = 0
        self.finished = _ProbeKillSignal()
        self.errorOccurred = _ProbeKillSignal()
        self.destroyed = _ProbeKillSignal()

    def setProperty(self, name: str, value: object) -> bool:
        self._properties[name] = value
        return True

    def property(self, name: str) -> object:
        return self._properties.get(name)

    def start(self, program: str, arguments: list) -> None:
        self.program = program
        self.arguments = list(arguments)
        self._running = True

    def state(self) -> QProcess.ProcessState:
        if self._running:
            return QProcess.ProcessState.Running
        return QProcess.ProcessState.NotRunning

    def kill(self) -> None:
        self.kill_calls += 1
        self._running = False

    def waitForFinished(self, msecs: int) -> bool:
        self.wait_for_finished_calls.append(msecs)
        return True

    def waitForStarted(self, msecs: int) -> bool:
        self.wait_for_started_calls.append(msecs)
        return True

    def deleteLater(self) -> None:
        self.delete_later_calls += 1


def _stuck_probe_helper(tmp_path: Path, name: str = "stuck_probe.py") -> Path:
    helper = tmp_path / name
    helper.write_text(
        "\n".join(
            (
                "import json",
                "from pathlib import Path",
                "import sys",
                "import tempfile",
                "import time",
                "token = sys.argv[2]",
                "time.sleep(30)",
                "result = Path(tempfile.gettempdir()) / f'applicant-scout-path-probe-{token}.json'",
                "result.write_text(json.dumps({'warning': None}), encoding='utf-8')",
            )
        ),
        encoding="utf-8",
    )
    return helper


def test_settings_saves_unrelated_fields_while_screenshots_probe_pending(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # M3: typing a screenshots path must not hold credentials/region/metrics
    # hostage on the pending probe. The save goes out promptly with the
    # screenshots path pinned to the last validated one; the pending status
    # stays visible until the probe lands.
    dialog = SettingsDialog(_dialog_cfg(tmp_path))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(
        lambda: dialog._screenshots_validation_ready_generation
        == dialog._screenshots_validation_generation,
        timeout=10000,
    )
    initial_path = dialog.screenshots_edit.text().strip()
    monkeypatch.setattr(settings_mod, "QProcess", FakeProbeProcess)
    saved = []
    dialog.valuesChanged.connect(saved.append)
    try:
        dialog.screenshots_edit.setText(
            str(tmp_path / "fresh" / "_retail_" / "Screenshots")
        )
        assert not dialog.flush_pending_values()
        assert saved == []
        qtbot.waitUntil(
            lambda: dialog._screenshots_validation_process is not None,
            timeout=10000,
        )
        stuck = dialog._screenshots_validation_process
        assert isinstance(stuck, FakeProbeProcess)

        dialog.region_combo.setCurrentText("US")
        qtbot.waitUntil(lambda: bool(saved), timeout=10000)

        assert saved[-1].region == "US"
        assert saved[-1].screenshots_path == initial_path
        assert "Checking Screenshots" in dialog.status_label.text()
    finally:
        dialog._cancel_screenshots_validation_process()
    assert stuck.kill_calls == 1
    assert stuck.wait_for_finished_calls == [1000]


def test_update_check_proceeds_while_screenshots_probe_pending_real_process(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # H3: the pre-action flush snapshots values and returns immediately; a
    # pending screenshots probe no longer stalls the update worker start.
    # Real-process integration: covers the actual QProcess pending path
    # while the fake-based sibling above asserts the same M3 semantics
    # deterministically.
    helper = _stuck_probe_helper(tmp_path, "stuck_probe_update.py")
    monkeypatch.setattr(
        settings_mod,
        "_screenshots_path_probe_program_args",
        lambda raw_path, token: (sys.executable, [str(helper), raw_path, token]),
    )
    entered = threading.Event()

    def check_updates():
        entered.set()
        return "up to date"

    dialog = SettingsDialog(_dialog_cfg(tmp_path), check_updates=check_updates)
    qtbot.addWidget(dialog)
    dialog.set_update_available("v0.2.0")
    saved = []
    dialog.valuesChanged.connect(saved.append)
    try:
        dialog.screenshots_edit.setText(
            str(tmp_path / "fresh" / "_retail_" / "Screenshots")
        )
        dialog.region_combo.setCurrentText("US")
        dialog.update_button.click()

        assert entered.wait(10)
        qtbot.waitUntil(lambda: bool(saved), timeout=10000)
        assert saved[-1].region == "US"
    finally:
        dialog._cancel_screenshots_validation_process()


def test_open_folder_opens_existing_dir_synchronously(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # M4-tray: the pre-created log dir opens on the GUI thread, no mkdir.
    opened: list[str] = []
    monkeypatch.setattr(
        settings_mod.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    target = tmp_path / "logs"
    target.mkdir()

    assert settings_mod.open_folder(target) is True
    assert len(opened) == 1
    assert opened[0].rstrip("/").endswith("logs")


def test_open_folder_creates_missing_dir_off_gui_thread(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # M4-tray: a missing directory is created in a worker; openUrl still runs
    # on the GUI thread. Returns True optimistically.
    opened: list[str] = []
    monkeypatch.setattr(
        settings_mod.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    target = tmp_path / "missing" / "logs"

    assert settings_mod.open_folder(target) is True
    qtbot.waitUntil(lambda: target.is_dir(), timeout=2000)
    qtbot.waitUntil(lambda: bool(opened), timeout=2000)


def test_update_handoff_rollback_message_points_to_updates_dir():
    # M4: the kept (but never auto-referenced) rollback candidate installer
    # must be discoverable from the user message via the updates/ dir.
    text = main_mod._update_handoff_rollback_message("Update installer exited.")
    assert text.startswith("Update installer exited.")
    expected_dir = str(main_mod.user_cache_dir() / main_mod.UPDATE_DOWNLOADS_DIR_NAME)
    assert expected_dir in text
    assert "manually" in text.lower()


def _visible_release_notes_dialogs():
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if widget.objectName() == "releaseNotesDialog" and widget.isVisible()
    ]


def _live_release_notes_dialogs():
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if widget.objectName() == "releaseNotesDialog" and widget.isVisible()
    ]


def _release_notes_widgets():
    return [
        widget
        for widget in QApplication.allWidgets()
        if widget.objectName().startswith("releaseNotes")
    ]


def test_show_release_notes_dialog_does_not_accumulate_widgets(
    qtbot, monkeypatch: pytest.MonkeyPatch
):
    """P1-dialog-leak: 10 opens/closes must leave flat widget count + set."""
    monkeypatch.setattr(main_mod, "_RELEASE_NOTES_TEXT_CACHE", None)
    monkeypatch.setattr(
        main_mod, "_load_release_notes_text", lambda: "# Leak probe"
    )
    # NOTE: the module-level set may already hold a non-Qt test double from
    # another test module (no destroyed signal to discard it), so assert the
    # delta, not an absolute empty set.
    tracked_before = len(main_mod._open_release_notes_dialogs)
    try:
        for _ in range(10):
            main_mod._show_release_notes_dialog(None)
            for widget in _live_release_notes_dialogs():
                widget.close()
            qtbot.waitUntil(lambda: len(_live_release_notes_dialogs()) == 0, timeout=2000)
        QApplication.processEvents()
        assert len(main_mod._open_release_notes_dialogs) == tracked_before
        assert _live_release_notes_dialogs() == []

        before = len(_release_notes_widgets())
        main_mod._show_release_notes_dialog(None)
        for widget in _live_release_notes_dialogs():
            widget.close()
        qtbot.waitUntil(lambda: len(_live_release_notes_dialogs()) == 0, timeout=2000)
        QApplication.processEvents()
        assert len(_release_notes_widgets()) == before
        assert len(main_mod._open_release_notes_dialogs) == tracked_before
    finally:
        for widget in list(QApplication.topLevelWidgets()):
            if widget.objectName() == "releaseNotesDialog":
                widget.close()
        QApplication.processEvents()


def test_show_release_notes_dialog_is_modeless_cached_and_deferred(
    qtbot, monkeypatch: pytest.MonkeyPatch
):
    # M2: show() instead of exec(), text cached, markdown deferred to paint.
    loads: list[int] = []
    monkeypatch.setattr(main_mod, "_RELEASE_NOTES_TEXT_CACHE", None)
    monkeypatch.setattr(
        main_mod, "_load_release_notes_text", lambda: loads.append(1) or "# Cached notes"
    )
    try:
        main_mod._show_release_notes_dialog(None)
        dialogs = _visible_release_notes_dialogs()
        assert len(dialogs) == 1
        dialog = dialogs[0]
        assert not dialog.isModal()
        qtbot.waitUntil(
            lambda: "Cached notes" in dialog.notes_browser.toPlainText(), timeout=2000
        )
        assert loads == [1]
        dialog.close()

        main_mod._show_release_notes_dialog(None)
        assert loads == [1]
        assert len(_visible_release_notes_dialogs()) == 1
    finally:
        for widget in list(QApplication.topLevelWidgets()):
            if widget.objectName() == "releaseNotesDialog":
                widget.close()


class _ResponsiveFirstRunDialog:
    """Minimal first-run double with a progress surface."""

    def __init__(self, values, calls: list) -> None:
        self._values = values
        self.calls = calls

    def setWindowIcon(self, _icon) -> None:
        pass

    def exec(self):
        self.calls.append("exec")
        return main_mod.QDialog.DialogCode.Accepted

    def values(self):
        return self._values

    def set_status(self, text, **_kwargs) -> None:
        self.calls.append(("status", text))

    def show(self) -> None:
        self.calls.append("show")

    def close(self) -> None:
        self.calls.append("close")


@pytest.fixture
def responsive_first_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg = _cfg(tmp_path)
    values = _values(cfg, screenshots_path=str(tmp_path / "Screenshots"))
    calls: list = []
    warnings: list[str] = []
    downstream: list[str] = []

    def configure(enabled, *, restore_windows_approval):
        assert restore_windows_approval is enabled
        downstream.append(f"shortcut:{enabled}")

    dialog_holder: dict = {}

    def make_dialog(*_args, **_kwargs):
        dialog = _ResponsiveFirstRunDialog(values, calls)
        dialog_holder["dialog"] = dialog
        return dialog

    monkeypatch.setattr(main_mod, "SettingsDialog", make_dialog)
    monkeypatch.setattr(main_mod, "_app_icon", lambda: None)
    monkeypatch.setattr(main_mod, "configure_wow_sync_startup", configure)
    monkeypatch.setattr(
        main_mod, "start_wow_sync_watcher", lambda **_kw: downstream.append("watcher")
    )
    monkeypatch.setattr(
        main_mod,
        "_stop_current_session_watcher_best_effort",
        lambda: downstream.append("stop"),
    )
    monkeypatch.setattr(
        main_mod.QMessageBox, "warning", lambda _p, _t, text: warnings.append(text)
    )
    for key in main_mod._settings_env_override_keys():
        monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(
        cfg=cfg,
        values=values,
        calls=calls,
        warnings=warnings,
        downstream=downstream,
        inline=lambda worker: worker(),
    )


def test_first_run_responsive_apply_configures_and_starts(
    qtbot, responsive_first_run
):
    # H2: post-Accept work runs through the configurator worker with progress.
    state = responsive_first_run
    assert main_mod._run_first_run_settings(state.cfg, _work_runner=state.inline) is True
    qtbot.waitUntil(lambda: "close" in state.calls, timeout=2000)
    assert ("status", "Saving settings…") in state.calls
    assert "show" in state.calls
    assert "close" in state.calls
    assert state.downstream == ["shortcut:True", "watcher"]
    assert state.warnings == []


def test_first_run_responsive_disable_stops_watcher(
    qtbot, responsive_first_run
):
    state = responsive_first_run
    state.values.sync_with_wow = False
    assert main_mod._run_first_run_settings(state.cfg, _work_runner=state.inline) is True
    qtbot.waitUntil(lambda: "close" in state.calls, timeout=2000)
    assert state.downstream == ["shortcut:False", "stop"]


def test_first_run_responsive_save_failure_warns(
    qtbot, responsive_first_run, monkeypatch: pytest.MonkeyPatch
):
    state = responsive_first_run

    def fail_save(*_args, **_kwargs):
        raise OSError("config write denied")

    monkeypatch.setattr(main_mod, "_persist_settings_values", fail_save)
    assert (
        main_mod._run_first_run_settings(state.cfg, _work_runner=state.inline) is False
    )
    qtbot.waitUntil(lambda: bool(state.warnings), timeout=2000)
    assert state.downstream == []
    assert "config write denied" in state.warnings[0]


def test_first_run_responsive_shortcut_failure_rolls_back(
    qtbot, responsive_first_run, monkeypatch: pytest.MonkeyPatch
):
    state = responsive_first_run

    def fail_shortcut(_enabled, **_kwargs):
        raise RuntimeError("shortcut denied")

    monkeypatch.setattr(main_mod, "configure_wow_sync_startup", fail_shortcut)
    assert (
        main_mod._run_first_run_settings(state.cfg, _work_runner=state.inline) is False
    )
    qtbot.waitUntil(lambda: bool(state.warnings), timeout=2000)
    assert state.downstream == []
    assert "shortcut denied" in state.warnings[0]


def test_first_run_responsive_disable_shortcut_failure_still_succeeds(
    qtbot, responsive_first_run, monkeypatch: pytest.MonkeyPatch
):
    state = responsive_first_run
    state.values.sync_with_wow = False

    def fail_shortcut(_enabled, **_kwargs):
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(main_mod, "configure_wow_sync_startup", fail_shortcut)
    assert main_mod._run_first_run_settings(state.cfg, _work_runner=state.inline) is True
    qtbot.waitUntil(lambda: "close" in state.calls, timeout=2000)
    assert state.downstream == ["stop"]
    assert "Settings were saved" in state.warnings[0]


def test_first_run_responsive_watcher_failure_warns_but_succeeds(
    qtbot, responsive_first_run, monkeypatch: pytest.MonkeyPatch
):
    state = responsive_first_run

    def fail_watcher(**_kwargs):
        raise RuntimeError("watcher denied")

    monkeypatch.setattr(main_mod, "start_wow_sync_watcher", fail_watcher)
    assert main_mod._run_first_run_settings(state.cfg, _work_runner=state.inline) is True
    qtbot.waitUntil(lambda: bool(state.warnings), timeout=2000)
    assert state.downstream == ["shortcut:True"]
    assert "watcher" in state.warnings[0].lower()


def test_first_run_responsive_wait_is_bounded(
    qtbot, responsive_first_run, monkeypatch: pytest.MonkeyPatch
):
    # A worker that never reports must bound the wait instead of hanging.
    state = responsive_first_run
    monkeypatch.setattr(main_mod, "_FIRST_RUN_APPLY_STEP_TIMEOUT_S", 0.05)
    assert (
        main_mod._run_first_run_settings(state.cfg, _work_runner=lambda _worker: None)
        is False
    )
    qtbot.waitUntil(lambda: "close" in state.calls, timeout=2000)
