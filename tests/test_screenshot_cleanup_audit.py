"""Additional adversarial checks for cleanup scheduling and generation ownership."""
import os
import threading
from pathlib import Path

import pytest

import applicant_scout.screenshot as mod
from test_background_screenshot_cleanup import _clock, _shot


def test_live_arrival_interrupts_historical_batch(monkeypatch, tmp_path):
    paths = [_shot(tmp_path, i) for i in range(8)]
    watcher = mod.ScreenshotWatcher(tmp_path)
    clock = _clock(monkeypatch)
    decoded = []

    def decode(path):
        decoded.append(path)
        watcher._last_live_activity = clock.now
        return mod.DecodeResult(None, True)

    monkeypatch.setattr(mod, "_decode_screenshot_result", decode)
    watcher._cleanup_tick()
    assert len(decoded) == 1
    assert sum(path.exists() for path in paths) == 7


def test_duplicate_delete_request_does_not_reset_pending_retry(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    source = watcher._source_from_stat(path, path.stat())
    clock = _clock(monkeypatch)
    attempts = []

    def blocked(*_args):
        attempts.append(True)
        raise PermissionError("busy")

    monkeypatch.setattr(mod, "_unlink_if_source_matches", blocked)
    watcher._delete_owned_screenshot(path, source)
    expected = dict(watcher._cleanup_pending)
    clock.now = 0.9
    watcher._delete_owned_screenshot(path, source)
    assert watcher._cleanup_pending == expected
    assert len(attempts) == 1


def test_failure_pruning_is_bounded_and_rotates(monkeypatch, tmp_path):
    watcher = mod.ScreenshotWatcher(tmp_path)
    paths = [_shot(tmp_path, i) for i in range(20)]
    for path in paths:
        watcher._cleanup_failed[mod._normalized_work_path(path)] = watcher._source_from_stat(path, path.stat())
    inspected = []
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        if path in paths:
            inspected.append(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    watcher._historical_next_sweep = float("inf")
    for _ in range(5):
        before = len(inspected)
        watcher._cleanup_tick()
        assert len(inspected) - before <= mod._BACKLOG_HISTORICAL_CLEANUP_LIMIT
    assert set(inspected) == set(paths)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_stale_retry_cannot_report_failure_for_new_pending_generation(tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    old = watcher._source_from_stat(path, path.stat())
    replacement = tmp_path / "replacement.jpg"
    replacement.write_bytes(b"new owned transport")
    os.replace(replacement, path)
    new = watcher._source_from_stat(path, path.stat())
    key = mod._normalized_work_path(path)
    watcher._cleanup_pending[key] = (path, new, 1, 5.0)
    with path.open("r+b"):
        assert not watcher._delete_owned_screenshot(path, old, attempt=1)
    assert watcher._cleanup_pending[key][1] is new
    assert not watcher._cleanup_failed
    assert path.read_bytes() == b"new owned transport"


@pytest.mark.parametrize("old_finishes_last", [False, True])
def test_concurrent_generations_keep_new_retry_ownership(monkeypatch, tmp_path, old_finishes_last):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    old = watcher._source_from_stat(path, path.stat())
    replacement = tmp_path / "replacement.jpg"
    replacement.write_bytes(b"new transport")
    os.replace(replacement, path)
    new = watcher._source_from_stat(path, path.stat())
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def unlink(_path, source):
        slow = old if old_finishes_last else new
        if source is slow:
            entered.set()
            assert release.wait(2)
        raise PermissionError("new error" if source is new else "old error")

    def run(source):
        try:
            watcher._delete_owned_screenshot(path, source)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(mod, "_unlink_if_source_matches", unlink)
    slow = old if old_finishes_last else new
    fast = new if old_finishes_last else old
    worker = threading.Thread(target=run, args=(slow,))
    worker.start()
    try:
        assert entered.wait(2)
        run(fast)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert errors == []
    pending = watcher._cleanup_pending[mod._normalized_work_path(path)]
    assert pending[1] is new
    assert not watcher._cleanup_failed
    assert not watcher._cleanup_active
    assert path.read_bytes() == b"new transport"


def test_stop_during_historical_decode_preserves_file(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def decode(_path):
        entered.set()
        assert release.wait(2)
        return mod.DecodeResult(None, True)

    monkeypatch.setattr(mod, "_decode_screenshot_result", decode)
    worker = threading.Thread(target=watcher._cleanup_tick)
    worker.start()
    try:
        assert entered.wait(2)
        watcher.request_stop()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert path.exists()
    assert not watcher._cleanup_pending
    assert not watcher._cleanup_active


def test_duplicate_inflight_delete_is_coalesced(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    source = watcher._source_from_stat(path, path.stat())
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def unlink(*_args):
        calls.append(True)
        entered.set()
        assert release.wait(2)
        raise PermissionError("busy")

    monkeypatch.setattr(mod, "_unlink_if_source_matches", unlink)
    worker = threading.Thread(target=watcher._delete_owned_screenshot, args=(path, source))
    worker.start()
    try:
        assert entered.wait(2)
        assert not watcher._delete_owned_screenshot(path, source)
        assert len(calls) == 1
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert len(watcher._cleanup_pending) == 1
    assert not watcher._cleanup_active


def test_full_warning_set_can_update_existing_path_generation(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    old = watcher._source_from_stat(path, path.stat())
    key = mod._normalized_work_path(path)
    watcher._cleanup_failed[key] = old
    path.write_bytes(b"new owned generation")
    new = watcher._source_from_stat(path, path.stat())
    attempt = len(mod._CLEANUP_RETRY_DELAYS)
    watcher._cleanup_pending[key] = (path, new, attempt, 0)
    monkeypatch.setattr(mod, "_CLEANUP_PENDING_LIMIT", 1)
    monkeypatch.setattr(mod, "_unlink_if_source_matches",
                        lambda *_args: (_ for _ in ()).throw(PermissionError("still busy")))
    watcher._delete_owned_screenshot(path, new, attempt=attempt)
    assert watcher._cleanup_failed[key] is new
    assert len(watcher._cleanup_failed) == 1
