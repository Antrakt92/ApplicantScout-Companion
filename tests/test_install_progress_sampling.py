"""Install progress must not block Qt on filesystem sampling."""
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread, QTimer

import applicant_scout.__main__ as main
import applicant_scout.updater as updater


def _arm(controller, path, seen, code):
    controller.arm(
        SimpleNamespace(poll=lambda: code[0]),
        on_install_progress=seen.append,
        install_estimator=updater.InstallProgressEstimator(100),
        install_staging_dir=path,
    )


@pytest.fixture
def stalled_scan(qapp, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    @contextmanager
    def scan(path):
        calls.append((path, QThread.currentThread() is qapp.thread()))
        entered.set()
        # Bound the old synchronous implementation's failure as well.
        release.wait(3)
        try:
            yield []
        finally:
            finished.set()

    monkeypatch.setattr(updater.os, "scandir", scan)
    yield entered, release, calls
    release.set()
    if entered.is_set():
        assert finished.wait(1)


def test_actual_stalled_scandir_keeps_qt_and_control_quit_available(qapp, tmp_path, stalled_scan):
    entered, release, calls = stalled_scan
    seen = []
    controller = main._UpdateHandoffRecoveryController(None, on_recover=lambda *_: None)
    _arm(controller, tmp_path, seen, [None])
    gate = main._UpdateQuitGate()
    gate.begin_update_attempt()
    gate.mark_installer_handoff_started()
    ticks = []
    try:
        controller._tick_install_progress()
        assert entered.wait(1)
        QTimer.singleShot(0, lambda: ticks.append(gate.prepare_control_quit(lambda: False)))
        qapp.processEvents()
        assert calls == [(tmp_path, False)]
        assert ticks == [True]
        assert seen == []
    finally:
        controller.disarm()
        release.set()


def test_rearm_waits_for_retired_sampler_and_discards_old_measurement(
    tmp_path, stalled_scan,
):
    entered, release, calls = stalled_scan
    newer = tmp_path / "new"
    newer.mkdir()
    old_seen = []
    new_seen = []
    controller = main._UpdateHandoffRecoveryController(None, on_recover=lambda *_: None)
    _arm(controller, tmp_path, old_seen, [None])
    try:
        controller._tick_install_progress()
        assert entered.wait(1)
        assert calls == [(tmp_path, False)]
        retired = controller._progress_sample
        controller.disarm()
        _arm(controller, newer, new_seen, [None])
        for _ in range(5):
            controller._tick_install_progress()
        assert calls == [(tmp_path, False)]
        assert controller._progress_sample is retired
        release.set()
        assert retired.done.wait(1)
        controller._tick_install_progress()
        fresh = controller._progress_sample
        assert fresh is not retired
        assert fresh.done.wait(1)
        controller._tick_install_progress()
        assert old_seen == []
        assert new_seen == [updater.UpdateProgress("installing", 0, 100)]
        assert calls == [(tmp_path, False), (newer, False)]
    finally:
        controller.disarm()
        release.set()


@pytest.mark.parametrize("code", [0, 3])
def test_installer_exit_is_handled_before_blocked_sample(tmp_path, stalled_scan, code):
    entered, release, calls = stalled_scan
    seen = []
    recovered = []
    state = [None]
    controller = main._UpdateHandoffRecoveryController(None, on_recover=lambda *args: recovered.append(args))
    _arm(controller, tmp_path, seen, state)
    try:
        controller._tick_install_progress()
        assert entered.wait(1)
        assert calls == [(tmp_path, False)]
        job = controller._progress_sample
        state[0] = code
        controller._tick_install_progress()
        assert not job.done.is_set()
        assert controller._progress_timer is None
        assert seen == ([updater.UpdateProgress("installing", 100, 100)] if code == 0 else [])
        assert controller._timer is not None
        controller._tick()
        assert len(recovered) == 1
        assert not job.done.is_set()
        release.set()
        assert job.done.wait(1)
        controller._tick_install_progress()
        assert len(seen) == (1 if code == 0 else 0)
    finally:
        controller.disarm()
        release.set()


def test_completion_callback_rearm_is_not_stopped_by_old_tick(qapp, tmp_path):
    assert qapp is not None
    controller = main._UpdateHandoffRecoveryController(None, on_recover=lambda *_: None)
    successor = []

    def complete(_progress):
        _arm(controller, tmp_path, successor, [None])

    controller.arm(
        SimpleNamespace(poll=lambda: 0),
        on_install_progress=complete,
        install_estimator=updater.InstallProgressEstimator(100),
        install_staging_dir=tmp_path,
    )
    try:
        controller._tick_install_progress()
        assert controller._progress_timer is not None
        assert controller._install_staging_dir == tmp_path
        assert controller._on_install_progress == successor.append
    finally:
        controller.disarm()


@pytest.mark.parametrize("stage", ["constructor", "start"])
def test_sampler_launch_failure_has_no_synchronous_io(qapp, monkeypatch, tmp_path, stage):
    assert qapp is not None
    seen = []
    calls = []
    controller = main._UpdateHandoffRecoveryController(None, on_recover=lambda *_: None)
    _arm(controller, tmp_path, seen, [None])
    monkeypatch.setattr(main, "measure_directory_bytes", lambda path: calls.append(path) or 50)

    def fail(*_args, **_kwargs):
        raise RuntimeError("cannot start sampler")

    if stage == "constructor":
        monkeypatch.setattr(main.threading, "Thread", fail)
    else:
        monkeypatch.setattr(main.threading.Thread, "start", fail)
    try:
        controller._tick_install_progress()
        assert calls == []
        assert seen == []
        assert controller._progress_timer is None
        assert controller._timer is not None
        assert controller._progress_sample is None
    finally:
        controller.disarm()


def test_sampling_error_stops_optional_progress_without_recovery_verdict(qapp, monkeypatch, tmp_path):
    assert qapp is not None
    seen = []
    recovered = []
    controller = main._UpdateHandoffRecoveryController(None, on_recover=lambda *args: recovered.append(args))
    _arm(controller, tmp_path, seen, [None])

    def fail(_path):
        raise OSError("synthetic staging read failure")

    monkeypatch.setattr(main, "measure_directory_bytes", fail)
    try:
        controller._tick_install_progress()
        sample = controller._progress_sample
        assert sample is not None and sample.done.wait(1)
        controller._tick_install_progress()
        assert seen == [] and recovered == []
        assert controller._progress_timer is None
        assert controller._timer is not None
    finally:
        controller.disarm()


@pytest.mark.parametrize("raises", [False, True])
def test_sample_callback_rearm_survives_old_completion_and_callback_error(
    qapp, monkeypatch, tmp_path, raises,
):
    assert qapp is not None
    controller = main._UpdateHandoffRecoveryController(None, on_recover=lambda *_: None)
    seen = []
    successor = []
    monkeypatch.setattr(main, "measure_directory_bytes", lambda _path: 50)

    def progress(value):
        seen.append(value)
        _arm(controller, tmp_path, successor, [None])
        if raises:
            raise RuntimeError("old progress receiver failed after rearm")

    controller.arm(
        SimpleNamespace(poll=lambda: None),
        on_install_progress=progress,
        install_estimator=updater.InstallProgressEstimator(100),
        install_staging_dir=tmp_path,
    )
    try:
        controller._tick_install_progress()
        sample = controller._progress_sample
        assert sample is not None and sample.done.wait(1)
        controller._tick_install_progress()
        assert seen == [updater.UpdateProgress("installing", 50, 100)]
        assert controller._progress_timer is not None
        assert controller._on_install_progress == successor.append
        controller._tick_install_progress()
        fresh = controller._progress_sample
        assert fresh is not None and fresh.generation != sample.generation
        assert fresh.done.wait(1)
        controller._tick_install_progress()
        assert successor == [updater.UpdateProgress("installing", 50, 100)]
    finally:
        controller.disarm()
