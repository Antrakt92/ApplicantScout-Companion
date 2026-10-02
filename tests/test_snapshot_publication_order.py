"""Accepted screenshot publication must not regress after a newer frame."""

from threading import Event, Thread
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from applicant_scout.__main__ import StateMachine
import applicant_scout.screenshot as screenshot
from applicant_scout.snapshot_pipeline import SnapshotApplyQueue
from applicant_scout.state import AppState
from test_screenshot import _large_v9_payload, _parse_fragment, _wrap_fragments


@pytest.mark.parametrize("flush_newer_first", [False, True])
@pytest.mark.parametrize("newer_partial", [False, True])
@pytest.mark.parametrize("older_backlog", [False, True])
def test_older_accepted_frame_cannot_publish_after_newer(
    tmp_path, monkeypatch, flush_newer_first, newer_partial, older_backlog
):
    state = AppState()
    machine = StateMachine(state)
    callbacks = []
    queue = SnapshotApplyQueue(
        machine,
        SimpleNamespace(),
        lambda *_args: None,
        signal_gate=SimpleNamespace(is_current=lambda _generation: True),
        generation=0,
        scheduler=callbacks.append,
    )
    watcher = screenshot.ScreenshotWatcher(tmp_path, cache_dir=tmp_path / "cache")
    reached_publication = Event()
    release_older = Event()
    newer_published = Event()
    errors = []

    def publish(snapshot):
        if snapshot.source.mtime_ns == 1:
            reached_publication.set()
            if not release_older.wait(3):
                raise AssertionError("older publication was not released")
        queue.enqueue_snapshot(snapshot)
        if snapshot.source.mtime_ns == 2:
            newer_published.set()
        return True

    monkeypatch.setattr(watcher, "_emit_snapshot", publish)
    monkeypatch.setattr(screenshot, "_unlink_if_source_matches", lambda *_args: False)
    monkeypatch.setattr(watcher, "_finalize_decode_result", lambda *_args, **_kw: True)

    def dispatch(order, level):
        try:
            path = tmp_path / f"WoWScrnShot-{order}.jpg"
            snapshot = screenshot.Snapshot(
                listing=screenshot.DecodedListing(100, level, "Dungeon", "Listing", ""),
                version=screenshot.DecodedVersion("0.9.13", "12.0.5", 3, "Host-Realm"),
                applicants=[screenshot.DecodedApplicant(1, 8, 62, 200, 2000, 2, "Mage-Realm")],
                roster=[screenshot.DecodedRosterMember(1, 0, 1, 8, 62, 200, order * 1000, 0, name="Friend-Realm")],
            )
            if order == 2 and newer_partial:
                snapshot.listing = None
                snapshot.applicants = []
                snapshot.lfg_unavailable = True
                snapshot.applicants_unavailable = True
            if order == 1 and older_backlog:
                stat = SimpleNamespace(st_mtime_ns=order, st_size=123, st_dev=0, st_ino=1)
                claim = SimpleNamespace(path=path, stat_result=stat)
                watcher._handle_decoded_generation(
                    claim,
                    screenshot._ScreenshotWorkKey(str(path), order, 123),
                    screenshot.DecodeResult(snapshot, True),
                    True,
                    screenshot.BacklogScanCtx(True, False, set(), 0),
                )
            else:
                watcher._dispatch_live_decode_result(
                    path,
                    screenshot.DecodeResult(snapshot, True),
                    screenshot.SnapshotSource(order, str(path), 123),
                    marker_failure_reason="unused",
                    allow_incomplete_retry=False,
                )
        except BaseException as error:
            errors.append(error)

    older = Thread(target=dispatch, args=(1, 12), daemon=True)
    newer = Thread(target=dispatch, args=(2, 14), daemon=True)
    try:
        older.start()
        assert reached_publication.wait(3)
        newer.start()
        newer_has_published = newer_published.wait(0.1)
        if flush_newer_first and newer_has_published:
            queue.flush()
            if not newer_partial:
                assert state.listing.key_level == 14
        release_older.set()
        older.join(3)
        newer.join(3)
        assert not older.is_alive()
        assert not newer.is_alive()
        assert not errors
        queue.flush()
        assert state.listing is not None
        assert state.listing.key_level == (12 if newer_partial else 14)
        assert len(state.applicants) == 1
        assert len(state.party_members) == 1
        assert next(iter(state.party_members.values())).score == 2000
    finally:
        release_older.set()
        older.join(3)
        if newer.ident is not None:
            newer.join(3)
        watcher.request_stop()


def _dispatch(watcher, path, source_order, *, snapshot=None, fragment=None):
    watcher._dispatch_live_decode_result(
        path,
        screenshot.DecodeResult(snapshot, True, fragment=fragment),
        screenshot.SnapshotSource(source_order, str(path), 123),
        marker_failure_reason="unused",
        allow_incomplete_retry=False,
    )


def test_completed_fragment_cannot_publish_after_newer_whole_frame(tmp_path, monkeypatch):
    watcher = screenshot.ScreenshotWatcher(tmp_path, cache_dir=tmp_path / "cache")
    entered = Event()
    release = Event()
    received = []
    errors = []
    frames = [_parse_fragment(raw) for raw in _wrap_fragments(_large_v9_payload())]
    monkeypatch.setattr(screenshot, "_unlink_if_source_matches", lambda *_args: False)
    monkeypatch.setattr(watcher, "_delete_retired_fragment_files", lambda _files: 0)

    def publish(snapshot):
        if snapshot.source.mtime_ns == len(frames):
            entered.set()
            assert release.wait(3)
        received.append(snapshot.source.mtime_ns)
        return True

    monkeypatch.setattr(watcher, "_emit_snapshot", publish)
    for index, fragment in enumerate(frames[:-1], 1):
        _dispatch(watcher, tmp_path / f"chunk-{index}.jpg", index, fragment=fragment)

    def complete():
        try:
            _dispatch(watcher, tmp_path / "last-chunk.jpg", len(frames), fragment=frames[-1])
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=complete, daemon=True)
    try:
        worker.start()
        assert entered.wait(3)
        _dispatch(
            watcher, tmp_path / "new-whole.jpg", len(frames) + 1,
            snapshot=screenshot.Snapshot(None, None),
        )
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert not errors
        assert received == [len(frames), len(frames) + 1]
    finally:
        release.set()
        worker.join(3)
        watcher.request_stop()


def test_reentrant_signal_observer_preserves_order_for_later_observer(tmp_path, monkeypatch):
    watcher = screenshot.ScreenshotWatcher(tmp_path, cache_dir=tmp_path / "cache")
    received = []
    monkeypatch.setattr(screenshot, "_unlink_if_source_matches", lambda *_args: False)

    def reenter(snapshot):
        if snapshot.source.mtime_ns == 1:
            _dispatch(watcher, tmp_path / "second.jpg", 2, snapshot=screenshot.Snapshot(None, None))

    watcher.snapshotReceived.connect(reenter, Qt.ConnectionType.DirectConnection)
    watcher.snapshotReceived.connect(
        lambda snapshot: received.append(snapshot.source.mtime_ns),
        Qt.ConnectionType.DirectConnection,
    )
    try:
        _dispatch(watcher, tmp_path / "first.jpg", 1, snapshot=screenshot.Snapshot(None, None))
        assert received == [1, 2]
    finally:
        watcher.request_stop()


def test_stop_does_not_wait_for_paused_publication_or_emit_pending_frame(tmp_path, monkeypatch):
    watcher = screenshot.ScreenshotWatcher(tmp_path, cache_dir=tmp_path / "cache")
    entered = Event()
    release = Event()
    newer_finished = Event()
    received = []
    errors = []
    native_emit = watcher._emit_snapshot
    watcher.snapshotReceived.connect(
        lambda snapshot: received.append(snapshot.source.mtime_ns),
        Qt.ConnectionType.DirectConnection,
    )
    monkeypatch.setattr(screenshot, "_unlink_if_source_matches", lambda *_args: False)

    def publish(snapshot):
        if snapshot.source.mtime_ns == 1:
            entered.set()
            assert release.wait(3)
        return native_emit(snapshot)

    monkeypatch.setattr(watcher, "_emit_snapshot", publish)

    def send(order):
        try:
            _dispatch(watcher, tmp_path / f"frame-{order}.jpg", order, snapshot=screenshot.Snapshot(None, None))
        except BaseException as error:
            errors.append(error)
        finally:
            if order == 2:
                newer_finished.set()

    older = Thread(target=send, args=(1,), daemon=True)
    newer = Thread(target=send, args=(2,), daemon=True)
    try:
        older.start()
        assert entered.wait(3)
        newer.start()
        assert newer_finished.wait(1), "new producer was blocked by signal delivery"
        watcher.request_stop()
        assert watcher._stopped.is_set()
        release.set()
        older.join(3)
        newer.join(3)
        assert not older.is_alive() and not newer.is_alive()
        assert not errors
        assert received == []
    finally:
        release.set()
        older.join(3)
        if newer.ident is not None:
            newer.join(3)
        watcher.request_stop()
