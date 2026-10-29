"""Recovery and cancellation checks for screenshot cleanup."""
import os
import threading
from types import SimpleNamespace

from PySide6.QtCore import Qt

import applicant_scout.screenshot as mod
from applicant_scout.__main__ import _connect_screenshot_watcher
from test_background_screenshot_cleanup import _clock, _shot


def test_old_replacement_never_reaches_live_consumer(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    decoded = []
    live_retries = []
    rescans = []
    received = []
    watcher.snapshotReceived.connect(received.append)
    monkeypatch.setattr(watcher, "_schedule_generation_retry", live_retries.append)
    monkeypatch.setattr(watcher, "_request_backlog_rescan_after_deferred_resolution", lambda: rescans.append(True))
    monkeypatch.setattr(mod, "_wait_for_stable_size", lambda _path: True)
    clock = _clock(monkeypatch)

    def decode(candidate):
        if not decoded:
            replacement = tmp_path / "replacement.jpg"
            replacement.write_bytes(b"a different historical owned capture")
            stamp = mod.time.time() - 1000
            os.utime(replacement, (stamp, stamp))
            os.replace(replacement, candidate)
        decoded.append(candidate)
        return mod.DecodeResult(mod.Snapshot(None, None), True)

    monkeypatch.setattr(mod, "_decode_screenshot_result", decode)
    watcher._cleanup_tick()
    # Exercise the old route too, so the regression proves stale publication,
    # rather than only asserting which private scheduling method was chosen.
    for candidate in live_retries:
        watcher._on_new_file(candidate)
    assert received == []
    assert rescans
    clock.now += 5
    watcher._scan_recent_backlog()
    assert received == []
    assert not path.exists()


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback, *_args):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


def test_cleanup_notification_recovers_after_scheduler_failure(monkeypatch, tmp_path):
    watcher = mod.ScreenshotWatcher(tmp_path)
    monkeypatch.setattr(watcher, "cleanupStatus", _Signal())
    messages = []
    attempts = []

    def schedule(callback):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("temporary GUI scheduler failure")
        callback()

    _connect_screenshot_watcher(
        watcher, SimpleNamespace(), SimpleNamespace(note_screenshot_cleanup_status=messages.append),
        lambda *_args: None,
        signal_gate=SimpleNamespace(is_current=lambda _generation: True),
        source_gate=SimpleNamespace(accept=lambda *_args, **_kwargs: True),
        generation=1, scheduler=schedule,
    )
    path = _shot(tmp_path, 1)
    watcher._cleanup_failed[mod._normalized_work_path(path)] = watcher._source_from_stat(path, path.stat())
    watcher._cleanup_error = "access denied"
    watcher._publish_cleanup_status()
    assert messages == []
    watcher._publish_cleanup_status()
    assert len(messages) == 1
    assert "access denied" in messages[0]


def test_request_stop_does_not_wait_for_notification_callback(tmp_path):
    watcher = mod.ScreenshotWatcher(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def callback(_message):
        entered.set()
        assert release.wait(2)

    watcher.cleanupStatus.connect(callback, Qt.ConnectionType.DirectConnection)
    worker = threading.Thread(target=watcher._publish_cleanup_status)
    worker.start()
    stop_worker = threading.Thread(target=lambda: (watcher.request_stop(), stopped.set()))
    try:
        assert entered.wait(2)
        stop_worker.start()
        assert stopped.wait(0.2)
    finally:
        release.set()
        worker.join(2)
        if stop_worker.ident is not None:
            stop_worker.join(2)
    assert not worker.is_alive()
    assert not stop_worker.is_alive()


def test_gui_apply_failure_is_retried_after_queued_delivery(monkeypatch, tmp_path):
    watcher = mod.ScreenshotWatcher(tmp_path)
    monkeypatch.setattr(watcher, "cleanupStatus", _Signal())
    scheduled = []
    messages = []
    attempts = []

    def apply(message):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("temporary widget update failure")
        messages.append(message)

    _connect_screenshot_watcher(
        watcher, SimpleNamespace(), SimpleNamespace(note_screenshot_cleanup_status=apply),
        lambda *_args: None,
        signal_gate=SimpleNamespace(is_current=lambda _generation: True),
        source_gate=SimpleNamespace(accept=lambda *_args, **_kwargs: True),
        generation=1, scheduler=scheduled.append,
    )
    watcher._publish_cleanup_status()
    scheduled.pop(0)()
    assert messages == []
    watcher._publish_cleanup_status()
    scheduled.pop(0)()
    assert messages == [""]
    assert len(attempts) == 2


def test_replacement_aged_before_rescan_is_cleanup_only(monkeypatch, tmp_path):
    path = _shot(tmp_path, 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    requests = []
    received = []
    watcher.snapshotReceived.connect(received.append)
    monkeypatch.setattr(watcher, "_request_backlog_rescan_after_deferred_resolution", lambda: requests.append(True))
    monkeypatch.setattr(mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(mod, "_decode_screenshot_result",
                        lambda _path: mod.DecodeResult(mod.Snapshot(None, None), True))
    original_claim = watcher._work_claims.try_claim
    replaced = []

    def claim(candidate):
        if not replaced:
            replaced.append(True)
            replacement = tmp_path / "replacement.jpg"
            replacement.write_bytes(b"fresh capture")
            os.replace(replacement, candidate)
        return original_claim(candidate)

    monkeypatch.setattr(watcher._work_claims, "try_claim", claim)
    watcher._cleanup_tick()
    assert requests
    assert received == []
    stamp = mod.time.time() - 1000
    os.utime(path, (stamp, stamp))
    watcher._scan_recent_backlog()
    assert received == []
    assert not path.exists()
