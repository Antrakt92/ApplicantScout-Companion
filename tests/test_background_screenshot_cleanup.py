"""Background retirement never authorizes deletion from appearance or filename."""
import os
from types import SimpleNamespace

import pytest

import applicant_scout.screenshot as mod


def _shot(root, index, age=1000):
    path = root / f"WoWScrnShot_{index}.jpg"
    path.write_bytes(b"screenshot")
    timestamp = mod.time.time() - age
    os.utime(path, (timestamp, timestamp))
    return path


def _clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock.now)
    return clock


def test_background_continues_past_first_four_and_preserves_unowned(monkeypatch, tmp_path):
    paths = [_shot(tmp_path, i) for i in range(14)]
    watcher = mod.ScreenshotWatcher(tmp_path)
    decoded = []
    published = []
    watcher.snapshotReceived.connect(published.append)

    def decode(path):
        decoded.append(path)
        # Unrelated QR and unreadable transport must both be kept.
        return mod.DecodeResult(None, path in paths[:10], transport_suspected=path == paths[12])

    monkeypatch.setattr(mod, "_decode_screenshot_result", decode)
    clock = _clock(monkeypatch)
    for _ in range(6):
        watcher._cleanup_tick()
        clock.now += 5
    assert set(decoded) == set(paths)
    assert not any(path.exists() for path in paths[:10])
    assert all(path.exists() for path in paths[10:])
    assert published == []
    assert watcher._historical_candidates is None


def test_live_and_recent_fragments_take_priority(monkeypatch, tmp_path):
    old = _shot(tmp_path, 1)
    recent = _shot(tmp_path, 2, age=10)
    watcher = mod.ScreenshotWatcher(tmp_path)
    decoded = []
    monkeypatch.setattr(mod, "_decode_screenshot_result",
                        lambda path: decoded.append(path) or mod.DecodeResult(None, True))
    clock = _clock(monkeypatch)
    watcher._last_live_activity = 0
    watcher._cleanup_tick()
    assert decoded == []
    clock.now = 5
    watcher._cleanup_tick()
    assert decoded == [old]
    assert recent.exists()


def test_delete_retries_same_generation_after_sharing_failure(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    source = watcher._source_from_stat(path, path.stat())
    real_unlink = mod._unlink_if_source_matches
    calls = []

    def unlink(candidate, expected):
        calls.append(expected)
        if len(calls) == 1:
            raise PermissionError("temporarily open")
        return real_unlink(candidate, expected)

    monkeypatch.setattr(mod, "_unlink_if_source_matches", unlink)
    clock = _clock(monkeypatch)
    assert not watcher._delete_owned_screenshot(path, source)
    assert path.exists()
    watcher._cleanup_tick()
    assert len(calls) == 1
    clock.now = 5
    watcher._cleanup_tick()
    assert calls == [source, source]
    assert not path.exists()
    assert not watcher._cleanup_pending


def test_retry_preserves_equal_metadata_replacement(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    source = watcher._source_from_stat(path, path.stat())
    original = path.stat()
    real_unlink = mod._unlink_if_source_matches
    monkeypatch.setattr(mod, "_unlink_if_source_matches",
                        lambda *_args: (_ for _ in ()).throw(PermissionError("busy")))
    clock = _clock(monkeypatch)
    watcher._delete_owned_screenshot(path, source)
    replacement = tmp_path / "replacement.jpg"
    replacement.write_bytes(b"manualshot")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, path)
    monkeypatch.setattr(mod, "_unlink_if_source_matches", real_unlink)
    clock.now = 5
    watcher._historical_next_sweep = 1000
    watcher._cleanup_tick()
    assert path.read_bytes() == b"manualshot"
    assert not watcher._cleanup_pending


def test_historical_replacement_during_decode_is_preserved(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)

    def decode(candidate):
        replacement = tmp_path / "replacement.jpg"
        replacement.write_bytes(b"manual photograph")
        os.replace(replacement, candidate)
        return mod.DecodeResult(None, True)

    monkeypatch.setattr(mod, "_decode_screenshot_result", decode)
    retries = []
    monkeypatch.setattr(watcher, "_request_backlog_rescan_after_deferred_resolution", lambda: retries.append(True))
    watcher._cleanup_tick()
    assert retries == [True]
    assert path.read_bytes() == b"manual photograph"
    assert not watcher._cleanup_pending


def test_retry_exhaustion_warns_and_stop_cancels(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    source = watcher._source_from_stat(path, path.stat())
    statuses = []
    watcher.cleanupStatus.connect(statuses.append)
    calls = []

    def blocked(*_args):
        calls.append(True)
        raise PermissionError("access denied")

    monkeypatch.setattr(mod, "_unlink_if_source_matches", blocked)
    clock = _clock(monkeypatch)
    watcher._historical_next_sweep = 10000
    watcher._delete_owned_screenshot(path, source)
    for _ in mod._CLEANUP_RETRY_DELAYS:
        clock.now += 100
        watcher._cleanup_tick()
    assert len(calls) == 6
    assert not watcher._cleanup_pending
    assert "1 confirmed ApplicantScout" in statuses[-1]
    assert "access denied" in statuses[-1]
    watcher.request_stop()
    watcher._cleanup_tick()
    assert len(calls) == 6
    assert path.exists()


def test_retry_queue_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_CLEANUP_PENDING_LIMIT", 2)
    monkeypatch.setattr(mod, "_unlink_if_source_matches",
                        lambda *_args: (_ for _ in ()).throw(PermissionError("busy")))
    watcher = mod.ScreenshotWatcher(tmp_path)
    for i in range(5):
        path = _shot(tmp_path, i)
        watcher._delete_owned_screenshot(path, watcher._source_from_stat(path, path.stat()))
    assert len(watcher._cleanup_pending) == 2
    assert len(watcher._cleanup_failed) <= 2
    assert len(list(tmp_path.glob("*.jpg"))) == 5


def test_real_supervisor_drains_multiple_batches(monkeypatch, tmp_path):
    paths = [_shot(tmp_path, i) for i in range(13)]
    watcher = mod.ScreenshotWatcher(tmp_path)
    monkeypatch.setattr(watcher, "ensure_running", lambda: True)
    monkeypatch.setattr(mod, "_decode_screenshot_result", lambda _path: mod.DecodeResult(None, True))
    clock = _clock(monkeypatch)
    waits = []

    def wait(_seconds):
        clock.now += 5
        waits.append(True)
        return len(waits) > 6

    watcher._stopped = SimpleNamespace(wait=wait, is_set=lambda: False)
    watcher._supervise_observer()
    assert not any(path.exists() for path in paths)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_live_owned_screenshot_retries_native_writer_lock(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    source = watcher._source_from_stat(path, path.stat())
    clock = _clock(monkeypatch)
    with path.open("r+b"):
        watcher._dispatch_live_decode_result(
            path, mod.DecodeResult(None, True), source,
            marker_failure_reason="invalid payload", allow_incomplete_retry=True,
        )
        assert path.exists()
        assert watcher._cleanup_pending
    clock.now = 5
    watcher._cleanup_tick()
    assert not path.exists()


def test_unowned_live_screenshot_never_enters_delete_queue(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    monkeypatch.setattr(mod, "_unlink_if_source_matches",
                        lambda *_args: pytest.fail("unowned file reached deletion"))
    for image in [mod.DecodeResult(None, False),
                  mod.DecodeResult(None, False, transport_suspected=True),
                  mod.DecodeResult(None, False, decoder_unavailable=True)]:
        watcher._dispatch_live_decode_result(
            path, image, watcher._source_from_stat(path, path.stat()),
            marker_failure_reason="invalid", allow_incomplete_retry=False,
        )
    assert path.exists()
    assert not watcher._cleanup_pending


def test_replaced_failed_file_clears_warning_without_deleting(tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    source = watcher._source_from_stat(path, path.stat())
    watcher._cleanup_failed[mod._normalized_work_path(path)] = source
    path.write_bytes(b"a new manual screenshot")
    watcher._historical_next_sweep = float("inf")
    statuses = []
    watcher.cleanupStatus.connect(statuses.append)
    watcher._cleanup_tick()
    assert not watcher._cleanup_failed
    assert statuses == [""]
    assert path.exists()


def test_cleanup_status_handoff_ignores_retired_watcher(tmp_path):
    from applicant_scout.__main__ import _connect_screenshot_watcher

    watcher = mod.ScreenshotWatcher(tmp_path)
    active = SimpleNamespace(value=True)
    messages = []
    scheduled = []
    _connect_screenshot_watcher(
        watcher, SimpleNamespace(),
        SimpleNamespace(note_screenshot_cleanup_status=messages.append),
        lambda *_args: None,
        signal_gate=SimpleNamespace(is_current=lambda _generation: active.value),
        source_gate=SimpleNamespace(accept=lambda *_args, **_kwargs: True),
        generation=1, scheduler=scheduled.append,
    )
    watcher.cleanupStatus.emit("cleanup failed")
    assert messages == []
    scheduled.pop()()
    assert messages == ["cleanup failed"]
    watcher.cleanupStatus.emit("old delayed warning")
    active.value = False
    scheduled.pop()()
    watcher.cleanupStatus.emit("retired")
    assert not scheduled
    assert messages == ["cleanup failed"]


@pytest.mark.parametrize("replacement_boundary", ["claim", "refresh"])
def test_fresh_replacement_reaches_live_consumer_before_deletion(monkeypatch, tmp_path, replacement_boundary):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    retries = []
    received = []
    watcher.snapshotReceived.connect(received.append)
    monkeypatch.setattr(watcher, "_request_backlog_rescan_after_deferred_resolution", lambda: retries.append(True))
    monkeypatch.setattr(mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(mod, "_decode_screenshot_result",
                        lambda _path: mod.DecodeResult(mod.Snapshot(None, None), True))
    original_claim = watcher._work_claims.try_claim
    replaced = []

    def replace():
        if not replaced:
            replaced.append(True)
            replacement = tmp_path / "replacement.jpg"
            replacement.write_bytes(b"fresh live capture")
            os.replace(replacement, path)

    def claim(candidate):
        if replacement_boundary == "claim":
            replace()
        result = original_claim(candidate)
        if result is not None and replacement_boundary == "refresh":
            refresh = result.refresh

            def refresh_after_replace():
                replace()
                return refresh()

            result.refresh = refresh_after_replace
        return result

    monkeypatch.setattr(watcher._work_claims, "try_claim", claim)
    watcher._cleanup_tick()
    assert path.exists()
    assert received == []
    assert retries == [True]
    watcher._scan_recent_backlog()
    assert len(received) == 1
    assert not path.exists()
