"""Quit waits are bounded even when native persistence cannot be cancelled."""

from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

import pytest

import applicant_scout.live_snapshot_cache as cache
from support.snapshot_builders import _live_snapshot
from applicant_scout.screenshot import Snapshot


def _writer(tmp_path, timeout=0.05):
    return cache.LiveSnapshotCacheWriter(
        tmp_path, save_debounce_seconds=60, close_timeout_seconds=timeout,
    )


@pytest.mark.parametrize("kind", ["save", "clear"])
def test_close_bounds_pending_native_io_and_concurrent_callers(tmp_path, monkeypatch, kind):
    writer = _writer(tmp_path)
    entered, release, returned = Event(), Event(), Event()
    original = writer._perform_operation
    calls, results = [], []

    def blocked(operation):
        calls.append(operation.kind)
        entered.set()
        assert release.wait(3)
        return original(operation)

    monkeypatch.setattr(writer, "_perform_operation", blocked)
    writer.submit(_live_snapshot() if kind == "save" else Snapshot(None, None), now=100)
    caller = Thread(target=lambda: (results.append(writer.close()), returned.set()), daemon=True)
    try:
        caller.start()
        assert entered.wait(1)
        assert returned.wait(0.5), "native I/O blocked the close caller"
        assert results == [False]
        assert writer.close() is False
        assert calls == [kind], "concurrent close duplicated native persistence"
        writer.submit(_live_snapshot(), now=101)
        release.set()
        caller.join(1)
        worker = getattr(writer, "_close_worker", None)
        if worker is not None:
            worker.join(1)
        assert writer.close() is True
        assert calls == [kind]
    finally:
        release.set()
        caller.join(3)
        worker = getattr(writer, "_close_worker", None)
        if worker is not None:
            worker.join(3)


def test_expired_close_does_not_start_retry_but_later_close_recovers(tmp_path, monkeypatch):
    writer = _writer(tmp_path)
    entered, release, returned = Event(), Event(), Event()
    original = writer._perform_operation
    calls, results = [], []

    def fail_after_timeout(operation):
        calls.append(operation.kind)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            return False
        return original(operation)

    monkeypatch.setattr(writer, "_perform_operation", fail_after_timeout)
    writer.submit(_live_snapshot(), now=100)
    caller = Thread(target=lambda: (results.append(writer.close()), returned.set()), daemon=True)
    try:
        caller.start()
        assert entered.wait(1)
        assert returned.wait(0.5), "close did not honor its deadline"
        release.set()
        caller.join(1)
        worker = getattr(writer, "_close_worker", None)
        if worker is not None:
            worker.join(1)
        assert results == [False]
        assert calls == ["save"]
        writer._close_timeout_seconds = 2
        assert writer.close()
        assert calls == ["save", "save"]
        assert cache.load_live_snapshot(tmp_path, now=101) is not None
    finally:
        release.set()
        caller.join(3)
        worker = getattr(writer, "_close_worker", None)
        if worker is not None:
            worker.join(3)


def test_zero_budget_does_not_begin_pending_io(tmp_path, monkeypatch):
    writer = _writer(tmp_path, timeout=0)
    calls = []
    monkeypatch.setattr(writer, "_perform_operation", lambda op: calls.append(op.kind) or True)
    writer.submit(_live_snapshot(), now=100)
    assert writer.close() is False
    assert calls == []
    writer._close_timeout_seconds = 1
    assert writer.close()
    assert calls == ["save"]


@pytest.mark.parametrize("lock_name", ["_lock", "_write_lock", "_close_lock"])
def test_close_budget_includes_owned_lock_contention(tmp_path, lock_name):
    writer = _writer(tmp_path)
    returned, results = Event(), []
    lock = getattr(writer, lock_name)
    lock.acquire()
    caller = Thread(target=lambda: (results.append(writer.close()), returned.set()), daemon=True)
    try:
        caller.start()
        assert returned.wait(0.5), "close waited unboundedly for an owned lock"
        assert results == [False]
    finally:
        lock.release()
        caller.join(3)
        worker = getattr(writer, "_close_worker", None)
        if worker is not None:
            worker.join(3)
    writer.submit(_live_snapshot(), now=100)
    assert writer.close()
    assert cache.load_live_snapshot(tmp_path, now=101) is None


def test_close_thread_start_failure_retains_pending_operation(tmp_path, monkeypatch):
    writer = _writer(tmp_path)
    writer.submit(_live_snapshot(), now=100)
    real_threading = cache.threading

    class FailedThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start worker")

    monkeypatch.setattr(cache, "threading", SimpleNamespace(Thread=FailedThread))
    assert writer.close() is False
    monkeypatch.setattr(cache, "threading", real_threading)
    writer._close_timeout_seconds = 2
    try:
        assert writer.close()
        assert cache.load_live_snapshot(tmp_path, now=101) is not None
    finally:
        worker = getattr(writer, "_close_worker", None)
        if worker is not None:
            worker.join(3)


def test_failed_inflight_flush_requeues_for_later_close(tmp_path, monkeypatch):
    writer = _writer(tmp_path)
    entered, release = Event(), Event()
    original = writer._perform_operation
    calls = []

    def blocked(operation):
        calls.append(operation.kind)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            return False
        return original(operation)

    monkeypatch.setattr(writer, "_perform_operation", blocked)
    writer.submit(_live_snapshot(), now=100)
    worker = Thread(target=writer.flush, daemon=True)
    worker.start()
    try:
        assert entered.wait(1)
        started = monotonic()
        assert not writer.close()
        assert monotonic() - started < 0.5
        release.set()
        worker.join(1)
        close_worker = getattr(writer, "_close_worker", None)
        if close_worker is not None:
            close_worker.join(1)
        writer._close_timeout_seconds = 2
        assert writer.close()
        assert cache.load_live_snapshot(tmp_path, now=101) is not None
    finally:
        release.set()
        worker.join(3)
        close_worker = getattr(writer, "_close_worker", None)
        if close_worker is not None:
            close_worker.join(3)


def test_close_worker_exception_keeps_operation_retryable(tmp_path, monkeypatch):
    writer = _writer(tmp_path)
    original = writer._perform_operation
    writer.submit(_live_snapshot(), now=100)
    calls = []

    def fail_once(operation):
        calls.append(operation.kind)
        if len(calls) == 1:
            raise OSError("native persistence failed")
        return original(operation)

    monkeypatch.setattr(writer, "_perform_operation", fail_once)
    assert not writer.close()
    writer._close_timeout_seconds = 2
    try:
        assert writer.close()
        assert calls == ["save", "save"]
        assert cache.load_live_snapshot(tmp_path, now=101) is not None
    finally:
        worker = getattr(writer, "_close_worker", None)
        if worker is not None:
            worker.join(3)
