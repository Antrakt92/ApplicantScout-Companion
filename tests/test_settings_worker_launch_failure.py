"""Settings worker launch failures never move persistence onto the GUI."""
import threading

import pytest
from PySide6.QtCore import QThread, QTimer

import applicant_scout.__main__ as main
from applicant_scout.config import read_user_config_values
from test_settings_transaction_chain import _setup, _values


def _fail_start(_thread):
    raise RuntimeError("cannot start new thread")


@pytest.mark.parametrize("stage", ["constructor", "start"])
def test_launch_failure_reports_without_any_disk_prepare(qapp, monkeypatch, tmp_path, stage):
    cfg = _setup(monkeypatch, tmp_path)
    before = cfg.config_path.read_bytes()
    outcomes = []
    disk_threads = []
    real_capture = main._capture_persisted_config_snapshot

    def capture(current):
        disk_threads.append(QThread.currentThread() is qapp.thread())
        return real_capture(current)

    monkeypatch.setattr(main, "_capture_persisted_config_snapshot", capture)
    if stage == "constructor":
        def fail_constructor(**_kwargs):
            raise RuntimeError("cannot allocate settings thread")
        monkeypatch.setattr(main.threading, "Thread", fail_constructor)
    else:
        monkeypatch.setattr(main.threading.Thread, "start", _fail_start)
    applier = main._CoalescedSettingsApplier(current_cfg=lambda: cfg)
    applier.configure(on_ready=outcomes.append)
    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    assert disk_threads == []
    assert cfg.config_path.read_bytes() == before
    assert len(outcomes) == 1 and not outcomes[0].ok
    assert outcomes[0].prepared is None
    assert "background task" in str(outcomes[0].error)
    assert "retry" in str(outcomes[0].error).lower()
    assert applier.drain(0.01)


def test_failed_launch_does_not_wait_for_prior_config_writer(qapp, monkeypatch, tmp_path):
    cfg = _setup(monkeypatch, tmp_path)
    held = threading.Event()
    release = threading.Event()
    timer_fired = []

    def writer():
        with main.CONFIG_WRITE_LOCK:
            held.set()
            release.wait(0.3)

    helper = threading.Thread(target=writer)
    helper.start()
    assert held.wait(1)
    monkeypatch.setattr(main.threading.Thread, "start", _fail_start)
    outcomes = []
    applier = main._CoalescedSettingsApplier(current_cfg=lambda: cfg)
    applier.configure(on_ready=outcomes.append)
    try:
        applier.submit(_values(cfg, region="US"), apply_credentials=False)
        QTimer.singleShot(0, lambda: timer_fired.append(True))
        qapp.processEvents()
        assert helper.is_alive(), "submit waited until the unrelated writer released its lock"
        assert timer_fired == [True]
        assert len(outcomes) == 1 and not outcomes[0].ok
    finally:
        release.set()
        helper.join(1)
    assert not helper.is_alive()


def test_failed_successor_launch_keeps_carried_baseline_for_later_restore(
    qapp, monkeypatch, tmp_path,
):
    cfg = _setup(monkeypatch, tmp_path)
    original = cfg.config_path.read_bytes()
    workers = []
    outcomes = []
    applier = main._CoalescedSettingsApplier(current_cfg=lambda: cfg, runner=workers.append)
    applier.configure(on_ready=outcomes.append)
    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    applier.submit(_values(cfg, region="KR"), apply_credentials=False)
    applier._runner = None
    monkeypatch.setattr(main.threading.Thread, "start", _fail_start)
    workers.pop(0)()
    qapp.processEvents()
    assert len(outcomes) == 1 and not outcomes[0].ok
    assert read_user_config_values(cfg.config_path)["APSCOUT_REGION"] == "US"
    carried = outcomes[0].rollback_snapshot
    assert carried is not None and carried.contents == original
    assert applier._rollback_snapshot is carried
    assert applier.drain(0.01)

    real_persist = main._persist_settings_values

    def persist_then_fail(*args, **kwargs):
        real_persist(*args, **kwargs)
        raise OSError("save failed after write")

    monkeypatch.setattr(main, "_persist_settings_values", persist_then_fail)
    applier._runner = workers.append
    applier.submit(_values(cfg, region="TW"), apply_credentials=False)
    workers.pop(0)()
    assert len(outcomes) == 2 and not outcomes[-1].ok
    assert outcomes[-1].rollback_snapshot is carried
    assert cfg.config_path.read_bytes() == original
    assert applier.drain(0.01)


def test_launch_failure_callback_can_queue_successor_without_reentrant_worker(
    qapp, monkeypatch, tmp_path,
):
    cfg = _setup(monkeypatch, tmp_path)
    current = [cfg]
    workers = []
    outcomes = []
    monkeypatch.setattr(main.threading.Thread, "start", _fail_start)
    applier = main._CoalescedSettingsApplier(current_cfg=lambda: current[0])

    def ready(outcome):
        outcomes.append(outcome)
        if not outcome.ok:
            applier._runner = workers.append
            applier.submit(_values(cfg, region="KR"), apply_credentials=False)
            assert workers == []
        else:
            current[0] = outcome.prepared.new_cfg

    applier.configure(on_ready=ready)
    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    qapp.processEvents()
    assert len(outcomes) == 1 and not outcomes[0].ok
    assert len(workers) == 1
    workers.pop(0)()
    assert len(outcomes) == 2 and outcomes[-1].ok
    assert current[0].region == "KR"
    assert applier._rollback_snapshot is None
    assert applier.drain(0.01)


def test_explicit_retry_uses_real_background_worker_after_launch_recovers(
    qapp, qtbot, monkeypatch, tmp_path,
):
    cfg = _setup(monkeypatch, tmp_path)
    outcomes = []
    disk_on_gui = []
    real_capture = main._capture_persisted_config_snapshot

    def capture(current):
        disk_on_gui.append(QThread.currentThread() is qapp.thread())
        return real_capture(current)

    monkeypatch.setattr(main, "_capture_persisted_config_snapshot", capture)
    applier = main._CoalescedSettingsApplier(current_cfg=lambda: cfg)
    applier.configure(on_ready=outcomes.append)
    with monkeypatch.context() as broken:
        broken.setattr(main.threading.Thread, "start", _fail_start)
        applier.submit(_values(cfg, region="US"), apply_credentials=False)
    assert len(outcomes) == 1 and not outcomes[0].ok
    assert disk_on_gui == []
    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    qtbot.waitUntil(lambda: len(outcomes) == 2)
    assert outcomes[-1].ok
    assert disk_on_gui == [False]
    assert read_user_config_values(cfg.config_path)["APSCOUT_REGION"] == "US"
    assert applier.drain(0.01)
