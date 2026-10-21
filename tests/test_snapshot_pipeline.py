from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace

import pytest

from applicant_scout.__main__ import StateMachine
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedListing,
    DecodedVersion,
    Snapshot,
    SnapshotSource,
)
from applicant_scout.snapshot_pipeline import SnapshotApplyQueue
from applicant_scout.state import AppState


class _CurrentGate:
    def is_current(self, generation: int) -> bool:
        return generation == 0


@pytest.mark.parametrize("terminal_name", ["?", "Host", None])
@pytest.mark.parametrize("followup", [False, True])
def test_terminal_clear_preserves_only_pending_producer_fallback(
    terminal_name, followup
):
    version = DecodedVersion("0.14.4", "12.1.5", 3, "Host-Realm")
    first = Snapshot(
        listing=DecodedListing(100, 12, "Dungeon", "Listing", ""),
        version=version,
        applicants=[DecodedApplicant(1, 8, 62, 200, 2000, 2, "Old-Realm")],
    )
    terminal = Snapshot(
        listing=None,
        version=replace(version, player_name=terminal_name, region_id=0)
        if terminal_name is not None else None,
        terminal_clear=True,
    )
    snapshots = [first, terminal]
    if followup:
        snapshots.append(Snapshot(
            listing=first.listing,
            version=replace(version, player_name="Host", region_id=0),
            applicants=[DecodedApplicant(2, 8, 62, 200, 2000, 2, "New")],
            roster_unavailable=True,
        ))
    sequential, coalesced = AppState(), AppState()
    expected, actual = StateMachine(sequential), StateMachine(coalesced)
    callbacks = []
    cached = []
    queue = SnapshotApplyQueue(
        actual, object(), lambda *_args: None,
        signal_gate=_CurrentGate(), generation=0, scheduler=callbacks.append,
        live_snapshot_cache_writer=SimpleNamespace(submit=cached.append),
    )
    for snap in snapshots:
        expected.apply_snapshot(snap)
        queue.enqueue_snapshot(snap)
    while callbacks:
        callbacks.pop(0)()
    assert coalesced.player == sequential.player
    assert coalesced.player.full_name == "Host-Realm"
    assert coalesced.player.region_id == 3
    assert coalesced.applicants == sequential.applicants
    assert coalesced.listing == sequential.listing
    assert all(row.name != "Old-Realm" for row in coalesced.applicants.values())
    assert len(cached) == len(snapshots) - 1
    assert all(left is right for left, right in zip(cached, snapshots[1:]))


def test_explicit_terminal_producer_reset_discards_pending_fallback():
    first = Snapshot(None, DecodedVersion("1", "12", 3, "Host-Realm"))
    assert first.version is not None
    reset = Snapshot(
        None, DecodedVersion("1", "12", 0, "?"), terminal_clear=True,
        reset_producer_identity=True,
        producer_version_history=(first.version,),
    )
    state = AppState()
    callbacks = []
    queue = SnapshotApplyQueue(
        StateMachine(state), object(), lambda *_args: None,
        signal_gate=_CurrentGate(), generation=0, scheduler=callbacks.append,
    )
    queue.enqueue_snapshot(first)
    queue.enqueue_snapshot(reset)
    callbacks.pop(0)()
    assert state.player.full_name == ""
    assert state.player.region_id == 0


@pytest.mark.parametrize("boundary", [
    "repeated-clear", "character-switch", "region-switch", "versionless-rows",
])
def test_terminal_producer_fallback_keeps_sequential_identity_boundaries(boundary):
    version = DecodedVersion("1", "12", 3, "Host-Realm")
    first = Snapshot(None, version)
    terminal_version = replace(version, player_name="?", region_id=0)
    if boundary == "character-switch":
        terminal_version = replace(terminal_version, player_name="Other")
    elif boundary == "region-switch":
        terminal_version = replace(version, region_id=1)
    snapshots = [first, Snapshot(None, terminal_version, terminal_clear=True)]
    if boundary == "repeated-clear":
        snapshots.append(Snapshot(None, None, terminal_clear=True))
    if boundary == "versionless-rows":
        snapshots.append(Snapshot(
            DecodedListing(100, 12, "Dungeon", "Listing", ""), None,
            applicants=[DecodedApplicant(1, 8, 62, 200, 2000, 2, "Mage")],
        ))
    snapshots.append(Snapshot(None, replace(version, player_name="Host", region_id=0)))
    expected_state, actual_state = AppState(), AppState()
    expected, actual = StateMachine(expected_state), StateMachine(actual_state)
    callbacks = []
    observed_players = []
    actual.applicantAdded.connect(lambda _row: observed_players.append(actual_state.player))
    expected_players = []
    expected.applicantAdded.connect(lambda _row: expected_players.append(expected_state.player))
    queue = SnapshotApplyQueue(
        actual, object(), lambda *_args: None,
        signal_gate=_CurrentGate(), generation=0, scheduler=callbacks.append,
    )
    for snap in snapshots:
        expected.apply_snapshot(snap)
        queue.enqueue_snapshot(snap)
    while callbacks:
        callbacks.pop(0)()
    assert vars(actual_state) == vars(expected_state)
    assert observed_players == expected_players


@pytest.mark.parametrize("already_applied", [False, True])
def test_listing_roundtrip_does_not_restore_departed_applicants(already_applied):
    full = Snapshot(
        listing=DecodedListing(100, 12, "Dungeon", "Listing", ""),
        version=DecodedVersion("0.9.13", "12.0.5", 3, "Host-Realm"),
        applicants=[DecodedApplicant(1, 8, 62, 200, 2000, 2, "Mage-Realm")],
    )
    assert full.listing is not None
    changed = replace(
        full,
        listing=replace(full.listing, key_level=13),
        applicants=[],
        applicants_unavailable=True,
    )
    returned = replace(changed, listing=full.listing)
    state = AppState()
    machine = StateMachine(state)
    expected_state = AppState()
    expected_machine = StateMachine(expected_state)
    callbacks: list[Callable[[], None]] = []
    queue = SnapshotApplyQueue(
        machine,
        object(),
        lambda *_args: None,
        signal_gate=_CurrentGate(),
        generation=0,
        scheduler=callbacks.append,
    )
    if already_applied:
        machine.apply_snapshot(full)
    else:
        queue.enqueue_snapshot(full)
    for snap in (full, changed, returned):
        expected_machine.apply_snapshot(snap)
    for snap in (changed, returned):
        queue.enqueue_snapshot(snap)
    assert len(callbacks) == 1
    callbacks.pop(0)()

    assert state.listing == expected_state.listing
    assert state.applicants == expected_state.applicants == {}


@pytest.mark.parametrize("already_applied", [False, True])
def test_producer_roundtrip_does_not_restore_previous_session(already_applied):
    full = Snapshot(
        listing=DecodedListing(100, 12, "Dungeon", "Listing", ""),
        version=DecodedVersion("0.9.13", "12.0.5", 3, "Host-Realm"),
        applicants=[DecodedApplicant(1, 8, 62, 200, 2000, 2, "Mage-Realm")],
    )
    assert full.version is not None
    changed = Snapshot(
        listing=None,
        version=replace(full.version, player_name="Other-Realm"),
        lfg_unavailable=True,
        roster_unavailable=True,
        applicants_unavailable=True,
    )
    returned = replace(changed, version=full.version)
    state = AppState()
    machine = StateMachine(state)
    expected_state = AppState()
    expected_machine = StateMachine(expected_state)
    callbacks: list[Callable[[], None]] = []
    queue = SnapshotApplyQueue(
        machine,
        object(),
        lambda *_args: None,
        signal_gate=_CurrentGate(),
        generation=0,
        scheduler=callbacks.append,
    )
    if already_applied:
        machine.apply_snapshot(full)
    else:
        queue.enqueue_snapshot(full)
    for snap in (full, changed, returned):
        expected_machine.apply_snapshot(snap)
    for snap in (changed, returned):
        queue.enqueue_snapshot(snap)
    callbacks.pop(0)()

    assert state.player == expected_state.player
    assert state.listing == expected_state.listing is None
    assert state.applicants == expected_state.applicants == {}


@pytest.mark.parametrize("change_producer", [False, True])
def test_snapshot_bursts_keep_bounded_state_and_cache_only_originals(change_producer):
    callbacks: list[Callable[[], None]] = []
    cached: list[Snapshot] = []
    queue = SnapshotApplyQueue(
        SimpleNamespace(apply_snapshot=lambda _snap: None),
        object(),
        lambda *_args: None,
        signal_gate=_CurrentGate(),
        generation=0,
        scheduler=callbacks.append,
        live_snapshot_cache_writer=SimpleNamespace(submit=cached.append),
    )
    originals = [
        Snapshot(
            listing=DecodedListing(100, 12, "Dungeon", "Listing", ""),
            version=DecodedVersion(
                "0.9.13",
                "12.0.5",
                3,
                "Other-Realm" if change_producer and index % 2 else "Host-Realm",
            ),
        )
        for index in range(100)
    ]
    for snap in originals:
        queue.enqueue_snapshot(snap)

    assert queue._pending is not None
    assert len(queue._pending[1]) == (2 if change_producer else 1)
    assert len(callbacks) == 1
    callbacks.pop(0)()
    assert len(cached) == len(originals)
    assert all(actual is original for actual, original in zip(cached, originals))


def _stamped_snapshot(mtime_ns: int, file_id: str = "shot.jpg") -> Snapshot:
    return Snapshot(
        listing=None,
        version=None,
        source=SnapshotSource(mtime_ns, file_id, 10),
    )


def test_flush_snapshot_reports_only_newer_retained_failure():
    applied: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    callbacks: list[Callable[[], None]] = []
    queue = SnapshotApplyQueue(
        SimpleNamespace(apply_snapshot=applied.append),
        SimpleNamespace(),
        lambda path, reason: failures.append((path, reason)),
        signal_gate=_CurrentGate(),
        generation=0,
        scheduler=callbacks.append,
    )
    new = _stamped_snapshot(200, "new.jpg")
    queue.enqueue_snapshot(new)
    callbacks.pop(0)()
    assert applied == [new]

    # A failure older than the applied snapshot is stale: reported never,
    # retained never.
    queue.enqueue_decode_failed("old.jpg", "stale", SnapshotSource(100, "old.jpg", 10))
    callbacks.pop(0)()
    assert failures == []
    assert queue._retained_decode_failure is None

    # A failure newer than the applied snapshot survives it and is reported.
    queue.enqueue_decode_failed(
        "newer.jpg", "fresh", SnapshotSource(300, "newer.jpg", 10)
    )
    callbacks.pop(0)()
    assert failures == [("newer.jpg", "fresh")]

    # An unstamped failure carries no order key and is always reported.
    queue.enqueue_decode_failed("plain.jpg", "no source", None)
    callbacks.pop(0)()
    assert failures == [("newer.jpg", "fresh"), ("plain.jpg", "no source")]


def test_apply_plan_step_failure_retains_and_retries_once():
    applied: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    callbacks: list[Callable[[], None]] = []

    def failing(snap: Snapshot) -> None:
        applied.append(snap)
        raise RuntimeError("gui down")

    queue = SnapshotApplyQueue(
        SimpleNamespace(apply_snapshot=failing),
        SimpleNamespace(),
        lambda path, reason: failures.append((path, reason)),
        signal_gate=_CurrentGate(),
        generation=0,
        scheduler=callbacks.append,
    )
    snap = Snapshot(listing=None, version=None)
    snapshots = (snap,)

    assert queue._apply_plan_step(snap, (snap,), snapshots, snapshots) is False
    assert applied == [snap]
    assert failures == [
        ("screenshot", "Snapshot apply failed; state was reset. Retrying once.")
    ]
    assert queue._pending == ("snapshot", snapshots)
    assert len(callbacks) == 1

    # The scheduled retry replays the full original sequence, not the tail.
    # The repeated failure is single-shot: it waits for a fresh capture.
    callbacks.pop(0)()
    assert applied == [snap, snap]
    assert failures == [
        ("screenshot", "Snapshot apply failed; state was reset. Retrying once."),
        (
            "screenshot",
            "Snapshot apply failed; state was reset. Waiting for a fresh capture.",
        ),
    ]
    assert callbacks == []
    assert queue._pending is None


class _RetiredGate:
    def is_current(self, _generation: int) -> bool:
        return False


def test_apply_plan_step_aborts_when_generation_retired():
    applied: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    callbacks: list[Callable[[], None]] = []
    queue = SnapshotApplyQueue(
        SimpleNamespace(apply_snapshot=applied.append),
        SimpleNamespace(),
        lambda path, reason: failures.append((path, reason)),
        signal_gate=_RetiredGate(),
        generation=0,
        scheduler=callbacks.append,
    )
    snap = Snapshot(listing=None, version=None)

    assert queue._apply_plan_step(snap, (snap,), (snap,), (snap,)) is False
    assert applied == []
    assert failures == []
    assert callbacks == []
    assert queue._pending is None
