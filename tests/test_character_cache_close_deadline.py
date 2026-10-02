"""Bounded quit preserves a single owner and terminal persistence results."""

from threading import Event, Thread, Timer, current_thread
from types import SimpleNamespace

import pytest

import applicant_scout.__main__ as main
import applicant_scout.wcl as wcl


def _cache(tmp_path, budget=0.05):
    cache = wcl.CharacterCache(tmp_path, defer_saves=True, save_debounce_seconds=60)
    cache._close_timeout_seconds = budget
    return cache


def _put(cache):
    return cache.put("Scout", "ravencrest", "EU", 71, wcl.CharacterRanks.empty())


def _join_owner(cache):
    worker = getattr(cache, "_close_worker", None)
    if worker is not None:
        worker.join(3)
        assert not worker.is_alive()


def test_runtime_close_bounds_native_write_and_releases_downstream_client(tmp_path, monkeypatch):
    cache = _cache(tmp_path)
    assert _put(cache)
    entered, release, done, client_closed = Event(), Event(), Event(), Event()
    calls = []

    def blocked(*_args, **_kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(wcl, "atomic_write_text", blocked)
    window = SimpleNamespace(shutdown_fetches=lambda: True)
    client = SimpleNamespace(close=client_closed.set)
    caller = Thread(target=lambda: (main._shutdown_runtime(None, window, cache, None, client), done.set()), daemon=True)
    try:
        caller.start()
        assert entered.wait(1)
        assert done.wait(0.5), "native cache persistence blocked runtime cleanup"
        assert client_closed.is_set()
        assert cache.close() is False
        assert calls == [True]
        assert not _put(cache)
        release.set()
        _join_owner(cache)
        assert cache.close() is True
    finally:
        release.set()
        caller.join(3)
        _join_owner(cache)


@pytest.mark.parametrize("lock_name", ["_lock", "_write_lock", "_close_lock"])
def test_close_bounds_lock_contention_and_rejects_later_mutation(tmp_path, lock_name):
    cache = _cache(tmp_path)
    done, results = Event(), []
    lock = getattr(cache, lock_name)
    lock.acquire()
    caller = Thread(target=lambda: (results.append(cache.close()), done.set()), daemon=True)
    try:
        caller.start()
        assert done.wait(0.5), "shutdown lock wait exceeded caller budget"
        assert results == [False]
        assert not _put(cache)
        assert not cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, {})
        assert not cache.clear()
    finally:
        lock.release()
        caller.join(3)
        _join_owner(cache)
    assert not _put(cache)
    assert cache.close() is True


def test_timed_out_failure_attempts_are_counted_across_close_owners(tmp_path, monkeypatch):
    cache = _cache(tmp_path)
    assert _put(cache)
    entered, release, done = Event(), Event(), Event()
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        raise PermissionError("synthetic locked file")

    monkeypatch.setattr(wcl, "atomic_write_text", fail)
    results = []
    caller = Thread(target=lambda: (results.append(cache.close()), done.set()), daemon=True)
    try:
        caller.start()
        assert entered.wait(1)
        assert done.wait(0.5)
        assert results == [False]
        release.set()
        _join_owner(cache)
        assert calls == [True]
        assert cache._close_result is None
        assert not cache._close_done.is_set()
        cache._close_timeout_seconds = 1
        assert not cache.close()
        assert calls == [True, True]
        assert cache._close_result is False
        assert cache._close_done.is_set()
        assert not cache.close()
        assert calls == [True, True]
        assert cache._dirty
    finally:
        release.set()
        caller.join(3)
        _join_owner(cache)


def test_zero_budget_does_not_start_io_then_later_close_recovers(tmp_path, monkeypatch):
    cache = _cache(tmp_path, budget=0)
    assert _put(cache)
    calls = []
    monkeypatch.setattr(wcl, "atomic_write_text", lambda *_a, **_kw: calls.append(True))
    assert not cache.close()
    assert calls == []
    cache._close_timeout_seconds = 1
    assert cache.close()
    assert calls == [True]


def test_thread_start_failure_keeps_pending_work_recoverable(tmp_path, monkeypatch):
    cache = _cache(tmp_path)
    assert _put(cache)
    calls = []
    monkeypatch.setattr(wcl, "atomic_write_text", lambda *_a, **_kw: calls.append(True))
    real_threading = wcl.threading

    class FailedThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("synthetic start failure")

    monkeypatch.setattr(wcl, "threading", SimpleNamespace(Thread=FailedThread, current_thread=current_thread))
    assert not cache.close()
    assert calls == [] and cache._dirty
    monkeypatch.setattr(wcl, "threading", real_threading)
    cache._close_timeout_seconds = 1
    assert cache.close()
    assert calls == [True]


def test_serialization_expiry_does_not_start_native_io_or_consume_attempt(tmp_path, monkeypatch):
    cache = _cache(tmp_path)
    assert _put(cache)
    entered, release, done = Event(), Event(), Event()
    calls = []
    original = cache._encode_snapshot

    def blocked(snapshot):
        entered.set()
        assert release.wait(3)
        return original(snapshot)

    monkeypatch.setattr(cache, "_encode_snapshot", blocked)
    monkeypatch.setattr(wcl, "atomic_write_text", lambda *_a, **_kw: calls.append(True))
    caller = Thread(target=lambda: (cache.close(), done.set()), daemon=True)
    try:
        caller.start()
        assert entered.wait(1)
        assert done.wait(0.5)
        release.set()
        _join_owner(cache)
        assert calls == []
        assert cache._close_failed_attempts == 0
        assert cache._dirty and cache._close_result is None
        assert not cache._close_done.is_set()
        cache._close_timeout_seconds = 1
        assert cache.close()
        assert calls == [True]
        assert cache._close_done.is_set()
    finally:
        release.set()
        caller.join(3)
        _join_owner(cache)


def test_started_deferred_callback_cannot_write_after_terminal_close_failure(tmp_path, monkeypatch):
    cache = _cache(tmp_path, budget=1)
    cache._save_debounce_seconds = 0
    entered, release = Event(), Event()
    original = cache.flush
    calls = []

    def delayed_flush(**kwargs):
        if isinstance(current_thread(), Timer):
            entered.set()
            assert release.wait(3)
        return original(**kwargs)

    def fail(*_args, **_kwargs):
        calls.append(True)
        raise PermissionError("synthetic locked file")

    monkeypatch.setattr(cache, "flush", delayed_flush)
    monkeypatch.setattr(wcl, "atomic_write_text", fail)
    assert _put(cache)
    timer = cache._save_timer
    try:
        assert entered.wait(1)
        assert cache.close() is False
        assert cache._close_result is False
        assert calls == [True, True]
        release.set()
        timer.join(3)
        assert not timer.is_alive()
        assert calls == [True, True]
        assert cache._dirty
        # Explicit flush retains its preexisting recovery contract.
        monkeypatch.setattr(wcl, "atomic_write_text", lambda *_a, **_kw: calls.append(True))
        assert cache.flush()
        assert calls == [True, True, True]
        assert cache.close() is False
    finally:
        release.set()
        timer.join(3)
        _join_owner(cache)


def test_close_tracks_inflight_accepted_clear_even_when_not_dirty(tmp_path, monkeypatch):
    cache = _cache(tmp_path)
    assert _put(cache)
    assert cache.flush()
    entered, release = Event(), Event()
    original = wcl.Path.unlink

    def blocked(path, *args, **kwargs):
        if path == cache._path:
            entered.set()
            assert release.wait(3)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(wcl.Path, "unlink", blocked)
    worker = Thread(target=cache.clear, daemon=True)
    try:
        worker.start()
        assert entered.wait(1)
        assert not cache._dirty
        assert not cache.close()
        release.set()
        worker.join(3)
        _join_owner(cache)
        cache._close_timeout_seconds = 1
        assert cache.close()
        assert not cache._path.exists()
    finally:
        release.set()
        worker.join(3)
        _join_owner(cache)
