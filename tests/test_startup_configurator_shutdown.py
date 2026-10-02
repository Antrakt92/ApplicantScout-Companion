"""Startup shortcut shutdown has one deadline, including stalled OS work."""
from __future__ import annotations

import threading
import time

import pytest

import applicant_scout.__main__ as main_mod


@pytest.mark.parametrize("queued", [False, True])
def test_close_bounds_inflight_and_shutdown_reconciliation(monkeypatch, queued):
    monkeypatch.setattr(main_mod, "_WOW_SYNC_CLOSE_JOIN_TIMEOUT_S", 0.03)
    entered = threading.Event()
    release = threading.Event()
    callbacks = []

    def configure(_enabled):
        entered.set()
        release.wait(timeout=2)

    configurator = main_mod._WowSyncStartupConfigurator(
        configure=configure,
        runner=(lambda _worker: None) if queued else None,
        notify=callbacks.append,
    )
    configurator.request(True, on_success=lambda: pytest.fail("closed callback"))
    if not queued:
        assert entered.wait(timeout=1)
    # Release even the old unbounded implementation so a failed regression
    # cannot retain the test process forever.
    timer = threading.Timer(0.4, release.set)
    timer.start()
    started = time.monotonic()
    try:
        error = configurator.close()
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert isinstance(error, TimeoutError)
        assert configurator.close() is error
        assert callbacks == []
    finally:
        release.set()
        timer.cancel()
        timer.join()


def test_close_joins_retained_workers_once_with_shared_budget(monkeypatch):
    monkeypatch.setattr(main_mod, "_WOW_SYNC_CLOSE_JOIN_TIMEOUT_S", 0.03)
    configurator = main_mod._WowSyncStartupConfigurator()
    joins = []

    class RetainedWorker:
        name = "retained-worker"

        def join(self, timeout=None):
            joins.append(timeout)
            if len(joins) > 2:
                raise AssertionError("shutdown repeatedly joined retained worker")
            time.sleep(timeout)

        def is_alive(self):
            return True

    configurator._threads.update((RetainedWorker(), RetainedWorker()))
    started = time.monotonic()
    configurator.close()
    assert time.monotonic() - started < 0.2
    assert len(joins) == 2
    assert sum(joins) <= 0.03


def test_stalled_startup_worker_cannot_retain_process_lifetime():
    entered = threading.Event()
    release = threading.Event()

    def configure(_enabled):
        entered.set()
        release.wait(timeout=2)

    configurator = main_mod._WowSyncStartupConfigurator(configure=configure)
    try:
        configurator.request(True)
        assert entered.wait(timeout=1)
        assert all(worker.daemon for worker in configurator._threads)
    finally:
        release.set()
        configurator.close()


def test_close_bounds_stalled_approval_and_latches_shutdown(monkeypatch):
    monkeypatch.setattr(main_mod, "_WOW_SYNC_CLOSE_JOIN_TIMEOUT_S", 0.03)
    entered = threading.Event()
    release = threading.Event()
    delivered = []

    def approve():
        entered.set()
        release.wait(timeout=2)

    configurator = main_mod._WowSyncStartupConfigurator(
        configure=lambda _enabled: None,
        enable_approval=approve,
        notify=lambda callback: callback(),
    )
    configurator.request(True, restore_windows_approval=True,
                         on_success=lambda: delivered.append(True))
    assert entered.wait(timeout=1)
    try:
        started = time.monotonic()
        error = configurator.close()
        assert time.monotonic() - started < 0.2
        assert isinstance(error, TimeoutError)
        assert configurator.close() is error
        assert not configurator._is_current(configurator._generation)
        with pytest.raises(RuntimeError, match="closed"):
            configurator.request(False)
        workers = tuple(configurator._threads)
    finally:
        release.set()
    for worker in workers:
        worker.join(timeout=1)
        assert not worker.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        configurator.request(False)
    assert delivered == []


def test_reconciliation_does_not_start_new_mutation_after_deadline(monkeypatch):
    monkeypatch.setattr(main_mod, "_WOW_SYNC_CLOSE_JOIN_TIMEOUT_S", 0.03)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def configure(enabled):
        calls.append(enabled)
        entered.set()
        release.wait(timeout=2)

    configurator = main_mod._WowSyncStartupConfigurator(configure=configure)
    configurator.request(True)
    assert entered.wait(timeout=1)
    configurator.request(False)
    workers = tuple(configurator._threads)
    try:
        assert isinstance(configurator.close(), TimeoutError)
    finally:
        release.set()
    for worker in workers:
        worker.join(timeout=1)
        assert not worker.is_alive()
    assert calls == [True]
