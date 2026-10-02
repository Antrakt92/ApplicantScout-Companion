"""State equivalence over reproducible mixed partial transport sequences."""

from dataclasses import replace
import random

from applicant_scout.__main__ import StateMachine
from applicant_scout.screenshot import (
    DecodedApplicant, DecodedListing, DecodedRosterMember, DecodedVersion, Snapshot,
)
from applicant_scout.snapshot_pipeline import append_pending_snapshot, snapshot_application_plan
from applicant_scout.state import AppState


def _state_values(state, machine):
    def rows(mapping):
        return [
            (key, row.name, row.spec_id, row.ilvl, row.raid_heroic,
             row.rio_summary_target_key, row.rio_summary_activity_id)
            for key, row in sorted(mapping.items())
        ]

    return (
        state.player, state.listing, rows(state.applicants), rows(state.party_members),
        machine._producer_player_name, machine._producer_player_realm,
        machine._producer_region,
    )


def test_mixed_partial_sequences_match_sequential_state():
    rng = random.Random(174)
    base = Snapshot(
        listing=DecodedListing(401, 12, "Dungeon A", "Listing", "", 2, 8),
        version=DecodedVersion("1", "12", 3, "Host-Realm"),
        applicants=[DecodedApplicant(42, 8, 62, 200, 2000, 2, "Mage-Realm")],
        roster=[DecodedRosterMember(1, 1, 1, 8, 62, 200, 2000, 0,
                                    role=2, name="Tank-Realm")],
    )
    assert base.version is not None
    for case in range(1000):
        snapshots = []
        for _index in range(rng.randint(2, 5)):
            applicant_mode = rng.randrange(4)
            roster_mode = rng.randrange(4)
            version_mode = rng.randrange(8)
            listing_mode = rng.randrange(3)
            snapshots.append(replace(
                base,
                version=None if version_mode == 7 else replace(
                    base.version,
                    player_name=("Host-Realm", "Host", "?", "Other-Realm",
                                 "Host-Realm", "Other-Realm", "Host-OtherRealm")[version_mode],
                    region_id=(3, 3, 3, 3, 0, 1, 3)[version_mode],
                ),
                listing=base.listing if listing_mode != 1 else None,
                lfg_unavailable=listing_mode == 2,
                applicants=[] if applicant_mode == 0 else [replace(
                    base.applicants[0],
                    spec_id=0 if applicant_mode == 1 else 63 if applicant_mode == 2 else 62,
                    ilvl=0 if applicant_mode == 1 else 200,
                )],
                applicants_unavailable=applicant_mode == 3,
                roster=[] if roster_mode == 0 else [replace(
                    base.roster[0],
                    spec_id=0 if roster_mode == 1 else 63 if roster_mode == 2 else 62,
                    ilvl=0 if roster_mode == 1 else 200,
                )],
                roster_unavailable=roster_mode == 3,
            ))
        expected, actual = AppState(), AppState()
        sequential, coalesced = StateMachine(expected), StateMachine(actual)
        if case % 2:
            for state, machine in ((expected, sequential), (actual, coalesced)):
                machine.apply_snapshot(base)
                for row in (*state.applicants.values(), *state.party_members.values()):
                    row.raid_heroic = 91.0
                    row.fetch_status = "ready"
        for snap in snapshots:
            sequential.apply_snapshot(snap)
        pending = ()
        for snap in snapshots:
            pending = append_pending_snapshot(pending, snap)
        for snap, _cache in snapshot_application_plan(pending, tuple(snapshots)):
            coalesced.apply_snapshot(snap)
        assert _state_values(actual, coalesced) == _state_values(expected, sequential), (
            case, snapshots
        )
