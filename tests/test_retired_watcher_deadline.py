"""Retired watcher shutdown owns one deadline and serial stop lifetime."""
from __future__ import annotations

import threading
import time

import pytest

import applicant_scout.__main__ as main_mod


def _join_shutdown_worker(tracker):
    worker = getattr(tracker, "_shutdown_worker", None)
    if worker is not None:
        worker.join(timeout=1)
        assert not worker.is_alive()


@pytest.mark.parametrize("stage", ["request", "stop", "snapshot_lock", "stop_lock"])
def test_shutdown_budget_includes_every_blocking_stage(monkeypatch, stage):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 0.04)
    tracker = main_mod._RetiredScreenshotWatchers()
    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()

    class Watcher:
        def request_stop(self):
            if stage == "request":
                entered.set()
                release.wait(timeout=2)
                finished.set()

        def stop(self):
            if stage == "stop":
                entered.set()
                release.wait(timeout=2)
            finished.set()

    watcher = Watcher()
    tracker._watchers[id(watcher)] = watcher
    held_lock = None
    if stage in ("snapshot_lock", "stop_lock"):
        held_lock = tracker._lock if stage == "snapshot_lock" else tracker._stop_lock
        held_lock.acquire()

    def unblock():
        release.set()
        if held_lock is not None and held_lock.locked():
            held_lock.release()

    # Old blocking implementations also get released, keeping regressions bounded.
    timer = threading.Timer(0.4, unblock)
    timer.start()
    started = time.monotonic()
    try:
        tracker.stop_all()
        assert time.monotonic() - started < 0.2
    finally:
        timer.cancel()
        timer.join()
        unblock()
        if stage in ("request", "stop") and entered.is_set():
            assert finished.wait(timeout=1)
        _join_shutdown_worker(tracker)


def test_zero_budget_starts_no_new_stop_and_retains_ownership(monkeypatch):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 0.0)
    tracker = main_mod._RetiredScreenshotWatchers()
    calls = []

    class Watcher:
        def request_stop(self):
            calls.append("request")

        def stop(self):
            calls.append("stop")

    watcher = Watcher()
    tracker._watchers[id(watcher)] = watcher
    tracker.stop_all()
    assert "stop" not in calls
    assert tracker._watchers[id(watcher)] is watcher


def test_queued_retirement_cannot_begin_after_shutdown_deadline(monkeypatch):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 0.0)
    tracker = main_mod._RetiredScreenshotWatchers()
    queued = []
    calls = []

    class Watcher:
        def request_stop(self):
            pass

        def stop(self):
            calls.append(True)

    watcher = Watcher()
    tracker.retire(watcher, stop_runner=queued.append)
    tracker.stop_all()
    queued.pop()()
    assert calls == []
    assert tracker._watchers[id(watcher)] is watcher


def test_multiple_stops_share_budget_and_skip_later_work(monkeypatch):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 0.04)
    tracker = main_mod._RetiredScreenshotWatchers()
    calls = []
    first_finished = threading.Event()

    class Watcher:
        def __init__(self, name):
            self.name = name

        def request_stop(self):
            pass

        def stop(self):
            calls.append(self.name)
            if self.name == "first":
                time.sleep(0.12)
                first_finished.set()

    first, second = Watcher("first"), Watcher("second")
    tracker._watchers = {id(first): first, id(second): second}
    started = time.monotonic()
    tracker.stop_all()
    assert time.monotonic() - started < 0.1
    assert first_finished.wait(timeout=1)
    _join_shutdown_worker(tracker)
    assert calls == ["first"]
    assert tracker._watchers.get(id(second)) is second


def test_failed_retirement_retry_preserves_serial_stop_ownership(monkeypatch):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 1.0)
    tracker = main_mod._RetiredScreenshotWatchers()
    entered = threading.Event()
    release = threading.Event()
    calls = []
    active = 0
    max_active = 0
    lock = threading.Lock()
    threads = []

    class Watcher:
        def request_stop(self):
            pass

        def stop(self):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(active, max_active)
                calls.append(True)
                first = len(calls) == 1
            try:
                if first:
                    entered.set()
                    release.wait(timeout=2)
                    raise RuntimeError("first stop failed")
            finally:
                with lock:
                    active -= 1

    def runner(worker):
        thread = threading.Thread(target=worker, daemon=True)
        threads.append(thread)
        thread.start()

    watcher = Watcher()
    tracker.retire(watcher, stop_runner=runner)
    assert entered.wait(timeout=1)
    closer = threading.Thread(target=tracker.stop_all, daemon=True)
    closer.start()
    try:
        release.set()
        closer.join(timeout=2)
        assert not closer.is_alive()
        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive()
        assert len(calls) == 2
        assert max_active == 1
        assert id(watcher) not in tracker._watchers
    finally:
        release.set()


def test_concurrent_shutdowns_do_not_duplicate_stop(monkeypatch):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 0.04)
    tracker = main_mod._RetiredScreenshotWatchers()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    class Watcher:
        def request_stop(self):
            pass

        def stop(self):
            calls.append(True)
            entered.set()
            release.wait(timeout=2)
            finished.set()

    watcher = Watcher()
    tracker._watchers[id(watcher)] = watcher
    closers = [threading.Thread(target=tracker.stop_all, daemon=True) for _ in range(2)]
    try:
        closers[0].start()
        assert entered.wait(timeout=1)
        closers[1].start()
        for closer in closers:
            closer.join(timeout=0.2)
            assert not closer.is_alive()
        assert calls == [True]
    finally:
        release.set()
        for closer in closers:
            if closer.ident is not None:
                closer.join(timeout=1)
        assert finished.wait(timeout=1)
        _join_shutdown_worker(tracker)


def test_finished_shutdown_worker_reports_retained_failed_stop(monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 1.0)
    tracker = main_mod._RetiredScreenshotWatchers()

    class Watcher:
        def request_stop(self):
            pass

        def stop(self):
            raise RuntimeError("synthetic cleanup failure")

    watcher = Watcher()
    tracker._watchers[id(watcher)] = watcher
    tracker.stop_all()
    _join_shutdown_worker(tracker)
    assert tracker._watchers[id(watcher)] is watcher
    assert "synthetic cleanup failure" in caplog.text
    assert "cleanup incomplete" in caplog.text
    assert "unfinished watchers remain owned" in caplog.text


def test_shutdown_thread_launch_failure_preserves_retirement(monkeypatch, caplog):
    tracker = main_mod._RetiredScreenshotWatchers()
    calls = []

    class Watcher:
        def stop(self):
            calls.append(True)

    watcher = Watcher()
    tracker._watchers[id(watcher)] = watcher

    def fail_start(_thread):
        raise RuntimeError("synthetic thread launch failure")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    tracker.stop_all()
    assert calls == []
    assert tracker._watchers[id(watcher)] is watcher
    assert "synthetic thread launch failure" in caplog.text


def test_shutdown_budget_includes_caller_coordination_lock(monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 0.04)
    tracker = main_mod._RetiredScreenshotWatchers()
    tracker._shutdown_lock.acquire()
    started = time.monotonic()
    try:
        tracker.stop_all()
        assert time.monotonic() - started < 0.2
    finally:
        tracker._shutdown_lock.release()
    assert "shutdown lock exceeded" in caplog.text


@pytest.mark.parametrize("expired", [False, True])
def test_later_shutdown_retries_failure_only_within_original_deadline(monkeypatch, expired):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 0.04 if expired else 1.0)
    tracker = main_mod._RetiredScreenshotWatchers()
    calls = []

    class Watcher:
        def request_stop(self):
            pass

        def stop(self):
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("retryable failure")

    watcher = Watcher()
    tracker._watchers[id(watcher)] = watcher
    tracker.stop_all()
    _join_shutdown_worker(tracker)
    assert len(calls) == 1
    if expired:
        time.sleep(0.06)
        # Another caller's longer budget cannot extend shutdown's first deadline.
        monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 1.0)
    tracker.stop_all()
    _join_shutdown_worker(tracker)
    assert len(calls) == (1 if expired else 2)
    assert (id(watcher) in tracker._watchers) is expired


def test_new_retirement_invalidates_previous_complete_cleanup(monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "_RETIRED_WATCHERS_STOP_BUDGET_S", 0.04)
    tracker = main_mod._RetiredScreenshotWatchers()
    tracker.stop_all()
    _join_shutdown_worker(tracker)
    time.sleep(0.06)
    queued = []
    calls = []

    class Watcher:
        def request_stop(self):
            pass

        def stop(self):
            calls.append(True)

    watcher = Watcher()
    tracker.retire(watcher, stop_runner=queued.append)
    caplog.clear()
    tracker.stop_all()
    queued.pop()()
    assert calls == []
    assert tracker._watchers[id(watcher)] is watcher
    assert "cleanup incomplete" in caplog.text
