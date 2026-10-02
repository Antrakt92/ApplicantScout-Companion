"""Same metadata does not make a replacement the previously decoded file."""

import os
import json

import pytest
from PySide6.QtCore import Qt

from applicant_scout.__main__ import StateMachine, _SnapshotSourceGate
import applicant_scout.screenshot as screenshot
from applicant_scout.state import AppState


def _replace(path, original_stat):
    replacement = path.with_name("replacement.jpg")
    replacement.write_bytes(b"new")
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_ino != original_stat.st_ino
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert path.stat().st_size == original_stat.st_size


def _snapshot(level):
    return screenshot.Snapshot(
        screenshot.DecodedListing(100, level, "Dungeon", "Listing", ""),
        screenshot.DecodedVersion("0.9.13", "12.0.5", 3, "Host-Realm"),
    )


@pytest.mark.parametrize("during_decode", [True, False])
def test_live_same_metadata_replacement_reaches_state_without_stale_decode(
    tmp_path, monkeypatch, during_decode,
):
    path = tmp_path / "WoWScrnShot_test.jpg"
    path.write_bytes(b"old")
    original_stat = path.stat()
    watcher = screenshot.ScreenshotWatcher(tmp_path, cache_dir=tmp_path / "cache")
    state, seen, calls = AppState(), [], []
    machine = StateMachine(state)
    gate = _SnapshotSourceGate()

    def apply(snap):
        assert gate.accept(snap.source)
        seen.append(snap.listing.key_level)
        machine.apply_snapshot(snap)

    watcher.snapshotReceived.connect(
        apply,
        Qt.ConnectionType.DirectConnection,
    )

    def decode(_path):
        calls.append(True)
        if len(calls) == 1 and during_decode:
            _replace(path, original_stat)
        return screenshot.DecodeResult(_snapshot(5 if len(calls) == 1 else 15), True)

    monkeypatch.setattr(screenshot, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(screenshot, "_decode_screenshot_result", decode)
    monkeypatch.setattr(screenshot, "_unlink_if_source_matches", lambda *_a: False)
    try:
        watcher._on_new_file(path)
        if not during_decode:
            _replace(path, original_stat)
        watcher._on_new_file(path)
        assert len(calls) == 2
        assert seen == ([15] if during_decode else [5, 15])
        assert state.listing.key_level == 15
    finally:
        watcher.request_stop()


def test_unchanged_generation_after_recent_ttl_is_not_republished(tmp_path, monkeypatch):
    path = tmp_path / "WoWScrnShot_test.jpg"
    path.write_bytes(b"old")
    watcher = screenshot.ScreenshotWatcher(tmp_path)
    gate = _SnapshotSourceGate()
    sources = []
    monkeypatch.setattr(screenshot, "_RECENT_WORK_KEY_TTL_SECONDS", 0)
    monkeypatch.setattr(screenshot, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(screenshot, "_decode_screenshot_result", lambda _path: screenshot.DecodeResult(_snapshot(5), True))
    monkeypatch.setattr(screenshot, "_unlink_if_source_matches", lambda *_a: False)
    watcher.snapshotReceived.connect(sources.append, Qt.ConnectionType.DirectConnection)
    try:
        watcher._on_new_file(path)
        assert gate.accept(sources[0].source)
        watcher._on_new_file(path)
        assert len(sources) == 1
        source = sources[0].source
        repeated = screenshot.SnapshotSource(
            source.mtime_ns, source.file_id, source.size,
            source.file_identity, source.observation_order + 100,
        )
        assert not gate.accept(repeated)
    finally:
        watcher.request_stop()


def test_source_gate_orders_replacements_by_observation_not_inode():
    gate = _SnapshotSourceGate()
    old = screenshot.SnapshotSource(1, "same.jpg", 3, (1, 900), 1)
    newer = screenshot.SnapshotSource(1, "same.jpg", 3, (1, 100), 2)
    assert gate.accept(old)
    assert gate.accept(newer)
    assert not gate.accept(old)
    assert not gate.accept(screenshot.SnapshotSource(1, "same.jpg", 3))
    assert _SnapshotSourceGate().accept(None)


@pytest.mark.parametrize("legacy_version", [2, 3])
def test_legacy_metadata_only_manual_record_is_reclassified_once(
    tmp_path, monkeypatch, legacy_version,
):
    path = tmp_path / "WoWScrnShot_test.jpg"
    path.write_bytes(b"old")
    stat = path.stat()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    index_path = screenshot._manual_index_path(cache_dir, tmp_path)
    index_path.write_text(json.dumps({
        "version": legacy_version,
        "manual": [[screenshot._normalized_work_path(path), stat.st_mtime_ns, stat.st_size]],
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr(screenshot, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(screenshot, "_decode_screenshot_result", lambda _path: calls.append(True) or screenshot.DecodeResult(None, False))
    watcher = screenshot.ScreenshotWatcher(tmp_path, cache_dir=cache_dir)
    try:
        watcher._on_new_file(path)
        watcher._on_new_file(path)
        assert calls == [True]
        persisted = json.loads(index_path.read_text(encoding="utf-8"))
        assert persisted["version"] == 3
        assert persisted["manual"][0][3] == [stat.st_dev, stat.st_ino]
        assert len(persisted["manual"][0]) == 4
    finally:
        watcher.request_stop()


@pytest.mark.parametrize("replace_file", [True, False])
def test_persisted_manual_fingerprint_distinguishes_replacement_on_restart(
    tmp_path, monkeypatch, replace_file,
):
    path = tmp_path / "WoWScrnShot_test.jpg"
    path.write_bytes(b"old")
    original_stat = path.stat()
    watcher = screenshot.ScreenshotWatcher(tmp_path, cache_dir=tmp_path / "cache")
    calls, seen = [], []
    monkeypatch.setattr(screenshot, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(screenshot, "_unlink_if_source_matches", lambda *_a: False)

    def decode(_path):
        calls.append(True)
        return (
            screenshot.DecodeResult(None, False) if len(calls) == 1
            else screenshot.DecodeResult(_snapshot(15), True)
        )

    monkeypatch.setattr(screenshot, "_decode_screenshot_result", decode)
    try:
        watcher._on_new_file(path)
        index_path = watcher._manual_index._state_path
        assert index_path.exists()
        watcher.request_stop()
        if replace_file:
            _replace(path, original_stat)
        restarted = screenshot.ScreenshotWatcher(tmp_path, cache_dir=tmp_path / "cache")
        # Fresh process registries reload the same persisted file; avoid carrying
        # in-process recent-work keys across this simulated restart.
        restarted._work_claims = screenshot._ScreenshotWorkClaims()
        restarted._manual_index = screenshot._ManualScreenshotIndex(index_path)
        restarted.snapshotReceived.connect(
            lambda snap: seen.append(snap.listing.key_level), Qt.ConnectionType.DirectConnection,
        )
        try:
            restarted._scan_recent_backlog()
            assert len(calls) == (2 if replace_file else 1)
            assert seen == ([15] if replace_file else [])
        finally:
            restarted.request_stop()
    finally:
        watcher.request_stop()
