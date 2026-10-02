import os

import pytest

import applicant_scout.screenshot as mod


def _files(tmp_path, count):
    paths = []
    for index in range(count):
        path = tmp_path / f"WoWScrnShot_{index:04}.jpg"
        path.write_bytes(b"manual")
        os.utime(path, (1000 - index * 0.001, 1000 - index * 0.001))
        paths.append(path)
    return paths


@pytest.mark.parametrize("manual_count", [40, 600])
def test_known_manual_generations_do_not_wait_before_older_restore(
    monkeypatch, tmp_path, manual_count
):
    paths = _files(tmp_path, manual_count + 1)
    watcher = mod.ScreenshotWatcher(tmp_path)
    for path in paths[:-1]:
        watcher._manual_index.note_manual(
            mod._work_key_from_stat(path, path.stat()), flush=False
        )
    waits = []
    monkeypatch.setattr(mod.time, "time", lambda: 1000)
    monkeypatch.setattr(
        mod, "_wait_for_stable_size", lambda path: waits.append(path) or True
    )
    snapshot = mod.Snapshot(listing=None, version=None)
    monkeypatch.setattr(
        mod, "_decode_screenshot_result", lambda _path: mod.DecodeResult(snapshot, True)
    )
    received = []
    watcher.snapshotReceived.connect(received.append)
    watcher._scan_recent_backlog()
    assert waits == [paths[-1]]
    assert received == [snapshot]


def test_changed_manual_generation_is_still_checked_and_decoded(monkeypatch, tmp_path):
    path = _files(tmp_path, 1)[0]
    watcher = mod.ScreenshotWatcher(tmp_path)
    watcher._manual_index.note_manual(
        mod._work_key_from_stat(path, path.stat()), flush=False
    )
    path.write_bytes(b"new transport bytes")
    os.utime(path, (1000, 1000))
    waits = []
    monkeypatch.setattr(mod.time, "time", lambda: 1000)
    monkeypatch.setattr(
        mod, "_wait_for_stable_size", lambda candidate: waits.append(candidate) or True
    )
    monkeypatch.setattr(
        mod, "_decode_screenshot_result", lambda _candidate: mod.DecodeResult(None, True)
    )
    watcher._scan_recent_backlog()
    assert waits == [path]
    assert not path.exists()


@pytest.mark.parametrize("budget", ["count", "time"])
def test_unstable_candidates_cannot_bypass_startup_work_budget(
    monkeypatch, tmp_path, budget
):
    paths = _files(tmp_path, 10)
    watcher = mod.ScreenshotWatcher(tmp_path)
    waits = []
    monkeypatch.setattr(mod.time, "time", lambda: 1000)
    monkeypatch.setattr(
        mod, "_wait_for_stable_size", lambda path: waits.append(path) or False
    )
    monkeypatch.setattr(mod, "_BACKLOG_EXAMINED_LIMIT", 3 if budget == "count" else 500)
    monkeypatch.setattr(mod, "_BACKLOG_PHASE_SECONDS", 1.0)
    monkeypatch.setattr(
        mod.time, "monotonic", lambda: len(waits) * (0.6 if budget == "time" else 0)
    )
    watcher._scan_recent_backlog()
    assert len(waits) == (3 if budget == "count" else 2)
    assert all(path.exists() for path in paths)
