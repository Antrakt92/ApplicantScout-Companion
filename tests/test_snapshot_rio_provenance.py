"""Capture-relative compact RIO evidence must survive independent surface updates."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from applicant_scout.__main__ import StateMachine
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedLeaderKey,
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
)
from applicant_scout.snapshot_pipeline import (
    SnapshotApplyQueue,
    merge_snapshot_segment,
    snapshot_application_plan,
)
from applicant_scout.state import AppState


def _full() -> Snapshot:
    return Snapshot(
        listing=DecodedListing(401, 12, "Capture dungeon", "Weekly", "", 2, 8),
        version=DecodedVersion("0.13.2", "12.0.5", 3, "Host-Realm"),
        leader_key=DecodedLeaderKey(12, 99, "Host-Realm"),
        applicants=[
            DecodedApplicant(
                42,
                8,
                62,
                200,
                2000,
                2,
                "Mage-Realm",
                rio_profile=True,
                rio_best_dungeon_key=12,
                rio_timed_at_or_above=4,
                rio_dungeon_count=8,
            )
        ],
        roster=[
            DecodedRosterMember(
                1,
                1,
                1,
                1,
                73,
                200,
                2000,
                0,
                rio_profile=True,
                rio_best_dungeon_key=12,
                rio_timed_at_or_above=3,
                rio_dungeon_count=8,
                role=0,
                name="Tank-Realm",
            )
        ],
    )


def _apply(snapshots) -> AppState:
    state = AppState()
    machine = StateMachine(state)
    for snap in snapshots:
        machine.apply_snapshot(snap)
    return state


def _row_context(row):
    return (
        row.rio_summary_target_key,
        row.rio_summary_activity_id,
        row.rio_summary_dungeon_name,
    )


def _assert_context(state, *, applicants, roster):
    assert [_row_context(row) for row in state.applicants.values()] == applicants
    assert [_row_context(row) for row in state.party_members.values()] == roster


@pytest.mark.parametrize("surface", ["applicants", "roster"])
def test_raw_listing_key_precedes_unrelated_owned_key(surface):
    snap = replace(_full(), leader_key=DecodedLeaderKey(14, 99, "Host-Realm"))
    state = _apply([snap])
    rows = state.applicants if surface == "applicants" else state.party_members
    assert [row.rio_summary_target_key for row in rows.values()] == [12]
    assert [_row_context(row) for row in rows.values()] == [
        (12, 401, "Capture dungeon")
    ]


@pytest.mark.parametrize("mode", ["sequential", "merged", "plan", "queue"])
def test_preserved_applicants_do_not_gain_later_owned_key(mode):
    first = _full()
    second = Snapshot(
        listing=None,
        version=first.version,
        leader_key=DecodedLeaderKey(14),
        lfg_unavailable=True,
        roster_unavailable=True,
        applicants_unavailable=True,
    )
    state = _apply_mode((first, second), mode)
    assert state.leader_key is not None and state.leader_key.key_level == 14
    _assert_context(
        state,
        applicants=[(12, 401, "Capture dungeon")],
        roster=[(12, 401, "Capture dungeon")],
    )


def _apply_mode(snapshots, mode):
    if mode == "sequential":
        return _apply(snapshots)
    if mode == "merged":
        return _apply([merge_snapshot_segment(snapshots)])
    if mode == "plan":
        return _apply(
            [
                step
                for step, _originals in snapshot_application_plan(snapshots, snapshots)
            ]
        )
    state = AppState()
    callbacks = []
    cached = []
    queue = SnapshotApplyQueue(
        StateMachine(state),
        object(),
        lambda *_args: None,
        signal_gate=SimpleNamespace(is_current=lambda generation: generation == 0),
        generation=0,
        scheduler=callbacks.append,
        live_snapshot_cache_writer=SimpleNamespace(submit=cached.append),
    )
    for snap in snapshots:
        queue.enqueue_snapshot(snap)
    assert len(callbacks) == 1
    callbacks.pop()()
    last_clear = next(
        (
            index
            for index in reversed(range(len(snapshots)))
            if snapshots[index].terminal_clear
        ),
        0,
    )
    cache_originals = snapshots[last_clear:]
    assert len(cached) == len(cache_originals)
    assert all(actual is original for actual, original in zip(cached, cache_originals))
    return state


@pytest.mark.parametrize("mode", ["sequential", "plan", "queue"])
def test_roster_survives_new_listing_with_original_capture_context(mode):
    first = _full()
    second = replace(
        first,
        listing=DecodedListing(402, 14, "New dungeon", "Higher", "", 2, 8),
        leader_key=DecodedLeaderKey(14),
        applicants=[],
        roster=[],
        roster_unavailable=True,
    )
    state = _apply_mode((first, second), mode)
    assert state.listing is not None and state.listing.activity_id == 402
    _assert_context(state, applicants=[], roster=[(12, 401, "Capture dungeon")])


@pytest.mark.parametrize("mode", ["sequential", "merged", "plan", "queue"])
def test_merged_applicant_and_roster_surfaces_keep_distinct_capture_contexts(mode):
    first = _full()
    second = replace(
        first,
        listing=None,
        leader_key=DecodedLeaderKey(14),
        applicants=[],
        lfg_unavailable=True,
        applicants_unavailable=True,
    )
    state = _apply_mode((first, second), mode)
    _assert_context(
        state, applicants=[(12, 401, "Capture dungeon")], roster=[(14, 0, "")]
    )


def test_no_listing_raw_roster_uses_only_its_raw_leader_context():
    first = _full()
    second = replace(
        first,
        listing=None,
        leader_key=None,
        applicants=[],
        lfg_unavailable=True,
        applicants_unavailable=True,
    )
    state = _apply([first, second])
    # Retained state still knows the old listing/leader; neither authorizes a
    # fresh row's compact summary when absent from the capture itself.
    _assert_context(
        state, applicants=[(12, 401, "Capture dungeon")], roster=[(0, 0, "")]
    )


def test_repeat_merge_preserves_selected_surface_provenance():
    first = _full()
    second = replace(
        first,
        listing=None,
        leader_key=DecodedLeaderKey(14),
        applicants=[],
        lfg_unavailable=True,
        applicants_unavailable=True,
    )
    merged = merge_snapshot_segment((first, second))
    repeated = merge_snapshot_segment((merged,))
    state = _apply([repeated])
    _assert_context(
        state, applicants=[(12, 401, "Capture dungeon")], roster=[(14, 0, "")]
    )
    assert first.listing is not None and first.listing.key_level == 12
    assert first.leader_key is not None and first.leader_key.key_level == 12


@pytest.mark.parametrize("barrier", ["producer", "listing", "terminal"])
def test_queue_authority_barriers_do_not_resurrect_compact_evidence(barrier):
    first = _full()
    if barrier == "producer":
        assert first.version is not None
        barrier_snap = Snapshot(
            listing=None,
            version=replace(first.version, player_name="Other-Realm"),
            lfg_unavailable=True,
            roster_unavailable=True,
            applicants_unavailable=True,
        )
        returned = replace(barrier_snap, version=first.version)
    elif barrier == "listing":
        assert first.listing is not None
        barrier_snap = replace(
            first,
            listing=replace(first.listing, key_level=14),
            applicants=[],
            applicants_unavailable=True,
            roster=[],
            roster_unavailable=True,
        )
        returned = replace(barrier_snap, listing=first.listing)
    else:
        barrier_snap = Snapshot(listing=None, version=None, terminal_clear=True)
        returned = Snapshot(
            listing=None,
            version=first.version,
            lfg_unavailable=True,
            roster_unavailable=True,
            applicants_unavailable=True,
        )
    snapshots = (first, barrier_snap, returned)
    sequential = _apply(snapshots)
    queued = _apply_mode(snapshots, "queue")
    assert queued.applicants == sequential.applicants == {}
    assert [_row_context(row) for row in queued.party_members.values()] == [
        _row_context(row) for row in sequential.party_members.values()
    ]
