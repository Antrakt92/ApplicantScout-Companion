"""Expiry cleanup must not block shutdown or publish failures after newer data."""

from dataclasses import replace
import os
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from applicant_scout.__main__ import _RetiredScreenshotWatchers, _quiesce_screenshot_ingestion
import applicant_scout.screenshot as screenshot
from test_screenshot import (
    _FakeClock, _FakeTimerFactory, _large_v9_payload, _parse_fragment, _wrap_fragments,
)


def _pending(tmp_path):
    path = tmp_path / "WoWScrnShot_expiring.jpg"
    path.write_bytes(b"fragment")
    clock, timers = _FakeClock(), _FakeTimerFactory()
    watcher = screenshot.ScreenshotWatcher(
        tmp_path, fragment_clock=clock, fragment_timer_factory=timers,
    )
    fragment = replace(
        _parse_fragment(_wrap_fragments(_large_v9_payload())[0]),
        source=watcher._source_from_stat(path, path.stat()),
    )
    watcher._accept_fragment_for_publication(fragment, path)
    return watcher, fragment, path, clock, timers


def _blocked_cleanup(watcher, monkeypatch):
    entered, release = Event(), Event()
    original = watcher._delete_retired_fragment_files

    def cleanup(files):
        assert files
        entered.set()
        assert release.wait(3)
        return original(files)

    monkeypatch.setattr(watcher, "_delete_retired_fragment_files", cleanup)
    return entered, release


def _expire_thread(clock, timers):
    clock.advance(screenshot.APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS + 1)
    thread = Thread(target=timers.timers[0].fire)
    thread.start()
    return thread


@pytest.mark.parametrize("caller", ["quiesce", "retire"])
def test_actual_shutdown_call_returns_while_fragment_cleanup_is_blocked(
    tmp_path, monkeypatch, caller,
):
    watcher, _fragment, _path, clock, timers = _pending(tmp_path)
    entered, release = _blocked_cleanup(watcher, monkeypatch)
    returned = Event()
    queued = []
    invalidated = []
    failures = []
    watcher.decodeFailed.connect(
        lambda *_args: failures.append(_args), Qt.ConnectionType.DirectConnection,
    )

    def stop():
        if caller == "quiesce":
            _quiesce_screenshot_ingestion(
                watcher, SimpleNamespace(invalidate=lambda: invalidated.append(True)),
            )
        else:
            _RetiredScreenshotWatchers().retire(watcher, stop_runner=queued.append)
        returned.set()

    expiry = _expire_thread(clock, timers)
    stopping = Thread(target=stop)
    try:
        assert entered.wait(1)
        stopping.start()
        assert returned.wait(0.3), "GUI shutdown waited for retired screenshot deletion"
        assert watcher._stopped.is_set()
        assert bool(invalidated if caller == "quiesce" else queued)
    finally:
        release.set()
        expiry.join(3)
        stopping.join(3)
        watcher.stop()
    assert not expiry.is_alive() and not stopping.is_alive()
    assert failures == []


@pytest.mark.parametrize("kind", ["same_fragment", "new_fragment", "whole"])
def test_new_transport_arrival_suppresses_old_timeout_after_blocked_cleanup(
    tmp_path, monkeypatch, kind,
):
    watcher, fragment, path, clock, timers = _pending(tmp_path)
    entered, release = _blocked_cleanup(watcher, monkeypatch)
    failures = []
    arrivals = []
    watcher.decodeFailed.connect(
        lambda *_args: failures.append(_args), Qt.ConnectionType.DirectConnection,
    )
    watcher.snapshotReceived.connect(
        arrivals.append, Qt.ConnectionType.DirectConnection,
    )
    accepted = Event()
    outcomes = []
    newer_path = tmp_path / "WoWScrnShot_newer.jpg"
    newer_path.write_bytes(b"new-fragment")
    newer_source = replace(
        watcher._source_from_stat(newer_path, newer_path.stat()),
        mtime_ns=fragment.source.mtime_ns + 1,
    )

    def accept():
        if kind == "whole":
            outcomes.append(watcher._accept_snapshot_for_publication(
                screenshot.Snapshot(listing=None, version=None, source=newer_source),
            ))
        else:
            outcomes.append(watcher._accept_fragment_for_publication(
                replace(
                    fragment, source=newer_source,
                    generation=fragment.generation + (kind == "new_fragment"),
                ),
                newer_path,
            ))
        accepted.set()

    expiry = _expire_thread(clock, timers)
    accepting = Thread(target=accept)
    try:
        assert entered.wait(1)
        accepting.start()
        assert accepted.wait(0.3), "New transport waited for retired-file deletion"
        assert outcomes[0][0].accepted and outcomes[0][1]
        release.set()
        expiry.join(3)
        accepting.join(3)
        assert failures == []
        assert len(arrivals) == (1 if kind == "whole" else 0)
        assert newer_path.exists()
        assert not path.exists()
        if kind != "whole":
            assert watcher._fragment_expiry_identity == (
                fragment.stream_id,
                fragment.generation + (kind == "new_fragment"),
            )
            assert timers.timers[-1].started
            assert not watcher._fragment_degraded_reported
    finally:
        release.set()
        expiry.join(3)
        accepting.join(3)
        watcher.stop()


def test_expiry_preserves_replaced_file_generation(tmp_path, monkeypatch):
    watcher, _fragment, path, clock, timers = _pending(tmp_path)
    entered, release = _blocked_cleanup(watcher, monkeypatch)
    expiry = _expire_thread(clock, timers)
    try:
        assert entered.wait(1)
        path.unlink()
        path.write_bytes(b"manual replacement must survive")
        release.set()
        expiry.join(3)
        assert path.read_bytes() == b"manual replacement must survive"
    finally:
        release.set()
        expiry.join(3)
        watcher.stop()


def test_timeout_callback_can_reenter_stop_without_deadlock(tmp_path):
    watcher, _fragment, _path, clock, timers = _pending(tmp_path)
    handled = []
    watcher.decodeFailed.connect(
        lambda *_args: (watcher.request_stop(), handled.append(True)),
        Qt.ConnectionType.DirectConnection,
    )
    expiry = _expire_thread(clock, timers)
    expiry.join(1)
    try:
        assert not expiry.is_alive()
        assert handled == [True]
    finally:
        watcher.stop()

def test_timeout_handoff_is_serialized_before_new_whole_snapshot(tmp_path, monkeypatch):
    watcher, fragment, _path, clock, timers = _pending(tmp_path)
    entered, release = Event(), Event()
    original = watcher._emit_decode_failed
    events = []

    def blocked_emit(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(watcher, "_emit_decode_failed", blocked_emit)
    watcher.decodeFailed.connect(
        lambda *_args: events.append("timeout"), Qt.ConnectionType.DirectConnection,
    )
    watcher.snapshotReceived.connect(
        lambda _snap: events.append("new snapshot"), Qt.ConnectionType.DirectConnection,
    )
    expiry = _expire_thread(clock, timers)
    try:
        assert entered.wait(1)
        newer = screenshot.Snapshot(
            listing=None, version=None,
            source=replace(fragment.source, mtime_ns=fragment.source.mtime_ns + 1),
        )
        outcome, alive = watcher._accept_snapshot_for_publication(newer)
        assert outcome.accepted and alive
        assert events == []  # One publisher owns the handed-off timeout.
        release.set()
        expiry.join(3)
        assert events == ["timeout", "new snapshot"]
    finally:
        release.set()
        expiry.join(3)
        watcher.stop()


def test_queued_timeout_is_retired_before_handoff_by_newer_snapshot(tmp_path, monkeypatch):
    watcher, fragment, _path, clock, timers = _pending(tmp_path)
    entered, release = Event(), Event()
    original = watcher._emit_snapshot
    arrivals, failures = [], []

    def blocked_first(snap):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(snap)

    monkeypatch.setattr(watcher, "_emit_snapshot", blocked_first)
    watcher.snapshotReceived.connect(arrivals.append, Qt.ConnectionType.DirectConnection)
    watcher.decodeFailed.connect(
        lambda *_args: failures.append(_args), Qt.ConnectionType.DirectConnection,
    )
    first = screenshot.Snapshot(
        listing=None, version=None,
        source=replace(fragment.source, mtime_ns=fragment.source.mtime_ns + 1),
    )
    publisher = Thread(target=lambda: watcher._accept_snapshot_for_publication(first))
    publisher.start()
    try:
        assert entered.wait(1)
        next_path = tmp_path / "WoWScrnShot_pending-next.jpg"
        next_path.write_bytes(b"next")
        # File creation timestamps can be equal on the Windows runner.
        next_time_ns = first.source.mtime_ns + 1_000_000_000
        os.utime(next_path, ns=(next_time_ns, next_time_ns))
        next_source = watcher._source_from_stat(next_path, next_path.stat())
        assert next_source.mtime_ns > first.source.mtime_ns
        accepted, alive = watcher._accept_fragment_for_publication(
            replace(fragment, generation=fragment.generation + 1, source=next_source),
            next_path,
        )
        assert accepted.accepted and alive
        clock.advance(screenshot.APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS + 1)
        timers.timers[-1].fire()
        assert failures == [] and not next_path.exists()
        newer = replace(first, source=replace(next_source, mtime_ns=next_source.mtime_ns + 1))
        accepted, alive = watcher._accept_snapshot_for_publication(newer)
        assert accepted.accepted and alive
        release.set()
        publisher.join(3)
        assert arrivals == [first, newer]
        assert failures == []
    finally:
        release.set()
        publisher.join(3)
        watcher.stop()


def test_early_expiry_rearms_then_reports_one_normal_timeout(tmp_path):
    watcher, _fragment, path, clock, timers = _pending(tmp_path)
    failures = []
    watcher.decodeFailed.connect(
        lambda *_args: failures.append(_args), Qt.ConnectionType.DirectConnection,
    )
    try:
        timers.timers[0].fire()
        assert len(timers.timers) == 2
        assert failures == [] and path.exists()
        clock.advance(screenshot.APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS + 1)
        timers.timers[-1].fire()
        timers.timers[-1].fire()
        assert len(failures) == 1
        assert failures[0][0] == str(path)
        assert failures[0][1] == "v10 fragment assembly timed out"
        assert failures[0][2] is not None
        assert not path.exists()
    finally:
        watcher.stop()


def test_expiry_without_owned_source_preserves_manual_screenshot(tmp_path):
    path = tmp_path / "WoWScrnShot_manual.jpg"
    path.write_bytes(b"manual screenshot")
    clock, timers = _FakeClock(), _FakeTimerFactory()
    watcher = screenshot.ScreenshotWatcher(
        tmp_path, fragment_clock=clock, fragment_timer_factory=timers,
    )
    fragment = _parse_fragment(_wrap_fragments(_large_v9_payload())[0])
    try:
        watcher._accept_fragment_for_publication(fragment, path)
        clock.advance(screenshot.APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS + 1)
        timers.timers[0].fire()
        assert path.read_bytes() == b"manual screenshot"
    finally:
        watcher.stop()
