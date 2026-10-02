from collections.abc import Callable

import pytest

from applicant_scout.__main__ import StateMachine, _ReaderBoundMachine
from applicant_scout.screenshot import DecodedApplicant, DecodedListing, Snapshot
from applicant_scout.snapshot_pipeline import SnapshotApplyQueue
from applicant_scout.state import AppState


class _CurrentGate:
    def is_current(self, generation: int) -> bool:
        return generation == 0


@pytest.mark.parametrize("reader_changed", [False, True])
def test_bound_reader_queue_clears_partial_apply_and_retry(monkeypatch, reader_changed):
    state = AppState()
    machine = StateMachine(state)
    reader = object() if reader_changed else None
    bound = _ReaderBoundMachine(machine, reader)
    callbacks: list[Callable[[], None]] = []
    failures: list[str] = []
    events: list[str] = []
    machine.cleared.connect(lambda: events.append("cleared"))
    calls = 0
    snap = Snapshot(
        listing=DecodedListing(100, 12, "Dungeon", "Listing", ""),
        version=None,
        applicants=[DecodedApplicant(1, 8, 62, 200, 2000, 2, "Mage-Realm")],
    )
    original = machine.apply_snapshot

    def interrupted(snapshot):
        nonlocal calls
        calls += 1
        original(snapshot)
        assert state.applicants
        raise RuntimeError("interrupted after state mutation")

    monkeypatch.setattr(machine, "apply_snapshot", interrupted)
    queue = SnapshotApplyQueue(
        bound,
        object(),
        lambda _path, reason: failures.append(reason),
        signal_gate=_CurrentGate(),
        generation=0,
        scheduler=callbacks.append,
    )
    queue.enqueue_snapshot(snap)
    callbacks.pop(0)()
    assert state.applicants == {}
    assert state.listing is None
    assert events == ["cleared"]
    assert len(callbacks) == 1
    callbacks.pop(0)()
    assert calls == 2
    assert state.applicants == {}
    assert state.listing is None
    assert events == ["cleared", "cleared"]
    assert callbacks == []
    assert "Retrying once" in failures[0]
    assert "Waiting for a fresh capture" in failures[1]
    assert machine._rio_reader is None
