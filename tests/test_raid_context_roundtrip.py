"""Raid context survives restart cache and deferred snapshot replacement."""

import pytest

from applicant_scout.__main__ import StateMachine
from applicant_scout.live_snapshot_cache import (
    LiveSnapshotCacheWriter,
    load_live_snapshot,
    save_live_snapshot,
)
from applicant_scout.screenshot import DecodedListing, DecodedRosterMember, Snapshot
from applicant_scout.state import AppState


def _snapshot(flags: int) -> Snapshot:
    return Snapshot(
        listing=DecodedListing(401, 0, "Raid", "Raid", "", 3, 15),
        version=None,
        roster=[DecodedRosterMember(
            unit_index=1,
            flags=flags,
            subgroup=1,
            class_id=8,
            spec_id=62,
            ilvl=320,
            score=3000,
            main_score=3000,
            name="Raider-Realm",
        )],
    )


@pytest.mark.parametrize("flags,difficulty", [
    (0x17, 14), (0x1B, 15), (0x1F, 16), (0x13, 0), (0x03, None),
])
def test_cached_raid_roster_restores_observed_or_legacy_context(tmp_path, flags, difficulty):
    assert save_live_snapshot(tmp_path, _snapshot(flags), now=100)
    restored = load_live_snapshot(tmp_path, now=101)
    assert restored is not None
    assert restored.snapshot.roster[0].flags == flags

    state = AppState()
    StateMachine(state).apply_snapshot(restored.snapshot)
    member = state.party_members["raider-realm"]
    assert member.is_self
    assert member.is_raid_member
    assert member.raid_difficulty_id == difficulty


@pytest.mark.parametrize("flags,difficulty", [(0x1F, 16), (0x13, 0)])
def test_deferred_cache_keeps_latest_difficulty_only_change(tmp_path, flags, difficulty):
    writer = LiveSnapshotCacheWriter(tmp_path, save_debounce_seconds=60)
    try:
        writer.submit(_snapshot(0x1B), now=100)
        writer.submit(_snapshot(flags), now=101)
    finally:
        assert writer.close()

    restored = load_live_snapshot(tmp_path, now=102)
    assert restored is not None
    assert restored.saved_at == 101
    state = AppState()
    StateMachine(state).apply_snapshot(restored.snapshot)
    assert state.party_members["raider-realm"].raid_difficulty_id == difficulty
