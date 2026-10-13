"""Character evidence must follow identity context, including incomplete roster rows."""

from dataclasses import replace
from collections.abc import Callable

import pytest

from applicant_scout.__main__ import StateMachine
from applicant_scout.screenshot import (
    DecodedApplicant, DecodedListing, DecodedRosterMember, DecodedVersion, Snapshot,
)
from applicant_scout.snapshot_pipeline import SnapshotApplyQueue
from applicant_scout.state import AppState, WoWPlayer


@pytest.mark.parametrize(
    ("name", "region_changed", "realm_changed", "stable"),
    [
        ("Scout", False, False, True),
        ("Scout-Ravencrest", False, False, True),
        ("Scout", True, False, False),
        ("Scout-Ravencrest", True, False, False),
        ("Scout", False, True, False),
        ("Scout-Ravencrest", False, True, True),
        ("Scout", True, True, False),
        ("Scout-Ravencrest", True, True, False),
    ],
)
def test_incomplete_roster_fields_survive_only_a_stable_character_context(
    name, region_changed, realm_changed, stable,
):
    state = AppState()
    state.player = WoWPlayer(full_name="Leader-Ravencrest", region_id=3)
    machine = StateMachine(state)
    observed = DecodedRosterMember(2, 0, 1, 1, 71, 700, 3000, 0, role=2, name=name)
    machine._apply_roster_snapshot([observed])
    previous = state.party_members[name.casefold()]
    previous.fetch_status = "ready"
    previous.raid_heroic = 88
    machine._apply_roster_snapshot(
        [replace(observed, class_id=0, spec_id=0, ilvl=0)],
        region_identity_changed=region_changed,
        default_realm_changed=realm_changed,
    )
    current = state.party_members[name.casefold()]
    if stable:
        assert (current.cls, current.spec_id, current.ilvl) == ("WARRIOR", 71, 700)
        assert current.raid_heroic == 88 and current.fetch_status == "ready"
    else:
        assert (current.cls, current.spec_id, current.ilvl) == ("?", 0, 0)
        assert current.raid_heroic is None and current.fetch_status == "pending"


def test_fresh_roster_fields_remain_usable_after_identity_context_change():
    state = AppState()
    machine = StateMachine(state)
    old = DecodedRosterMember(2, 0, 1, 1, 71, 700, 3000, 0, role=2, name="Scout")
    machine._apply_roster_snapshot([old])
    machine._apply_roster_snapshot(
        [replace(old, class_id=8, spec_id=63, ilvl=680)],
        region_identity_changed=True,
        default_realm_changed=True,
    )
    current = state.party_members["scout"]
    assert (current.cls, current.spec_id, current.ilvl) == ("MAGE", 63, 680)
    assert current.fetch_status == "pending"


class _CurrentGate:
    def is_current(self, generation: int) -> bool:
        return generation == 0


@pytest.mark.parametrize("roster", [False, True])
@pytest.mark.parametrize("return_to_known", [False, True])
def test_coalesced_class_transition_matches_sequential_evidence_retirement(
    roster: bool, return_to_known: bool,
):
    initial = Snapshot(
        listing=DecodedListing(100, 12, "Dungeon", "Listing", ""),
        version=DecodedVersion("0.9.13", "12.0.5", 3, "Host-Realm"),
        applicants=[DecodedApplicant(1, 1, 71, 700, 3000, 2, "Scout-Realm")],
        roster=[DecodedRosterMember(2, 0, 1, 1, 71, 700, 3000, 0, role=2, name="Scout-Realm")],
    )
    warrior = replace(initial, applicants=[replace(initial.applicants[0], spec_id=0, ilvl=0)],
                       roster=[replace(initial.roster[0], spec_id=0, ilvl=0)])
    changed = replace(warrior, applicants=[replace(warrior.applicants[0], class_id=8)],
                      roster=[replace(warrior.roster[0], class_id=8)])
    returned = warrior if return_to_known else replace(
        warrior, applicants=[replace(warrior.applicants[0], class_id=0)],
        roster=[replace(warrior.roster[0], class_id=0)],
    )
    state, expected = AppState(), AppState()
    machine, sequential = StateMachine(state), StateMachine(expected)
    callbacks: list[Callable[[], None]] = []
    queue = SnapshotApplyQueue(machine, object(), lambda *_args: None,
                               signal_gate=_CurrentGate(), generation=0, scheduler=callbacks.append)
    for target, owner in ((state, machine), (expected, sequential)):
        owner.apply_snapshot(initial)
        row = target.party_members["scout-realm"] if roster else target.applicants["1:1"]
        row.fetch_status = "ready"
        row.raid_heroic = 88
    for snapshot in (changed, returned):
        sequential.apply_snapshot(snapshot)
        queue.enqueue_snapshot(snapshot)
    assert len(callbacks) == 1
    callbacks.pop(0)()
    actual = state.party_members["scout-realm"] if roster else state.applicants["1:1"]
    reference = expected.party_members["scout-realm"] if roster else expected.applicants["1:1"]
    assert (actual.cls, actual.spec_id, actual.ilvl, actual.raid_heroic, actual.fetch_status) == (
        reference.cls, reference.spec_id, reference.ilvl, reference.raid_heroic, reference.fetch_status,
    )
    assert reference.spec_id == reference.ilvl == 0
    assert reference.raid_heroic is None and reference.fetch_status == "pending"


@pytest.mark.parametrize("known_producer", [False, True])
@pytest.mark.parametrize("region", [1, 3])
@pytest.mark.parametrize("name", ["Scout", "Scout-Realm"])
@pytest.mark.parametrize("repeat_version", [False, True])
def test_version_only_context_step_settles_before_unknown_roster_fallback(
    known_producer: bool, region: int, name: str, repeat_version: bool,
):
    version = DecodedVersion("0.9.13", "12.0.5", 3, "Host-Realm")
    row = DecodedRosterMember(2, 0, 1, 8, 63, 700, 3000, 0, role=2, name=name)
    initial = Snapshot(listing=None, version=version if known_producer else None,
                       lfg_unavailable=True, roster=[row])
    context = Snapshot(listing=None, version=replace(version, region_id=region, player_name="Host"),
                       roster_unavailable=True)
    partial = replace(context, version=context.version if repeat_version else None,
                      roster=[replace(row, spec_id=0, ilvl=0)],
                      roster_unavailable=False)
    actual, expected = AppState(), AppState()
    queued, sequential = StateMachine(actual), StateMachine(expected)
    callbacks: list[Callable[[], None]] = []
    queue = SnapshotApplyQueue(queued, object(), lambda *_args: None,
                               signal_gate=_CurrentGate(), generation=0, scheduler=callbacks.append)
    for state, machine in ((actual, queued), (expected, sequential)):
        machine.apply_snapshot(initial)
        state.party_members[name.casefold()].raid_heroic = 88
        state.party_members[name.casefold()].fetch_status = "ready"
    for snapshot in (context, partial):
        sequential.apply_snapshot(snapshot)
        queue.enqueue_snapshot(snapshot)
    assert len(callbacks) == 1
    callbacks.pop(0)()
    current, reference = actual.party_members[name.casefold()], expected.party_members[name.casefold()]
    assert (current.cls, current.spec_id, current.ilvl, current.raid_heroic, current.fetch_status) == (
        reference.cls, reference.spec_id, reference.ilvl, reference.raid_heroic, reference.fetch_status,
    )
    # Resolving the first producer differs from switching a known producer's region.
    assert reference.spec_id == (0 if known_producer and region == 1 else 63)
