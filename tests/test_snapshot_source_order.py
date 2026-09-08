"""Concurrent native Qt signal delivery retains source order at GUI handoff."""

from collections.abc import Callable
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from applicant_scout import __main__ as main_mod
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedListing,
    ScreenshotWatcher,
    Snapshot,
    SnapshotSource,
)
from applicant_scout.state import AppState


@pytest.mark.parametrize("kind", ["full", "restricted", "terminal", "unstamped"])
@pytest.mark.parametrize("retire_generation", [False, True])
@pytest.mark.usefixtures("qapp")
def test_direct_worker_signals_keep_source_acceptance_and_enqueue_order(
    tmp_path, monkeypatch, kind, retire_generation,
):
    first = Snapshot(
        listing=DecodedListing(100, 12, "Dungeon", "First listing", ""),
        version=None,
        applicants=[DecodedApplicant(1, 8, 62, 200, 2000, 2, "Mage-Realm")],
        source=SnapshotSource(100, "first.jpg", 10),
    )
    second = Snapshot(
        listing=DecodedListing(200, 13, "Other dungeon", "Second listing", ""),
        version=None,
        source=SnapshotSource(200, "second.jpg", 10),
    )
    if kind == "restricted":
        second = replace(
            second, listing=None, lfg_unavailable=True,
            applicants_unavailable=True, roster_unavailable=True,
        )
    elif kind == "terminal":
        second = replace(second, listing=None, terminal_clear=True)
    elif kind == "unstamped":
        second = replace(second, source=None)

    state = AppState()
    expected = AppState()
    expected_machine = main_mod.StateMachine(expected)
    if not retire_generation:
        expected_machine.apply_snapshot(first)
        expected_machine.apply_snapshot(second)
    watcher = ScreenshotWatcher(tmp_path)
    generation_gate = main_mod._WatcherSignalGate()
    scheduled: list[Callable[[], None]] = []
    cached: list[Snapshot] = []
    queue = main_mod._connect_screenshot_watcher(
        watcher, main_mod.StateMachine(state), object(), lambda *_args: None,
        signal_gate=generation_gate,
        source_gate=main_mod._SnapshotSourceGate(),
        generation=0,
        scheduler=scheduled.append,
        live_snapshot_cache_writer=SimpleNamespace(submit=cached.append),
    )
    first_accepted = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_enqueued = threading.Event()
    enqueued: list[Snapshot] = []
    errors: list[str] = []
    original_enqueue = queue.enqueue_snapshot

    def enqueue_after_preemption(snap):
        # The production DirectConnection callback already accepted this source.
        # Model a worker preemption before its separately locked queue mutation.
        if snap is first:
            first_accepted.set()
            if not release_first.wait(5):
                errors.append("first worker was not released")
        enqueued.append(snap)
        original_enqueue(snap)
        if snap is second:
            second_enqueued.set()

    monkeypatch.setattr(queue, "enqueue_snapshot", enqueue_after_preemption)

    def send_second():
        second_started.set()
        watcher.snapshotReceived.emit(second)

    first_worker = threading.Thread(target=watcher.snapshotReceived.emit, args=(first,))
    second_worker = threading.Thread(target=send_second)
    first_worker.start()
    try:
        assert first_accepted.wait(2), "Qt signal must be delivered directly on its worker"
        second_worker.start()
        assert second_started.wait(2)
        # Without an atomic handoff the second worker enqueues while the first
        # remains paused, reversing both original cache inputs and GUI state.
        second_enqueued.wait(0.2)
        if retire_generation:
            generation_gate.invalidate()
    finally:
        release_first.set()
        first_worker.join(2)
        if second_worker.ident is not None:
            second_worker.join(2)
    assert not first_worker.is_alive()
    assert not second_worker.is_alive()
    assert errors == []
    assert enqueued == ([first] if retire_generation else [first, second])
    for callback in scheduled:
        callback()
    assert state.listing == expected.listing
    assert state.applicants == expected.applicants
    assert state.party_members == expected.party_members
    expected_cache = [] if retire_generation else [second] if kind == "terminal" else [first, second]
    assert len(cached) == len(expected_cache)
    assert all(actual is original for actual, original in zip(cached, expected_cache, strict=True))
