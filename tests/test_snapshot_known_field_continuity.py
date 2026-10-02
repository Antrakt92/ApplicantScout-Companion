"""Coalescing must retain row identity and known-value fallback transitions."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from applicant_scout.__main__ import StateMachine
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
)
from applicant_scout.snapshot_pipeline import (
    SnapshotApplyQueue,
    append_pending_snapshot,
    snapshot_application_plan,
)
from applicant_scout.state import AppState


def _known():
    return Snapshot(
        listing=DecodedListing(401, 12, "Dungeon", "Weekly", "", 2, 8),
        version=DecodedVersion("0.13.2", "12.0.5", 3, "Host-Realm"),
        applicants=[DecodedApplicant(42, 8, 62, 685, 2200, 2, "Mage-Realm")],
        roster=[
            DecodedRosterMember(1, 1, 1, 8, 62, 685, 2200, 0, role=2, name="Host-Realm")
        ],
    )


def _unknown_rows(snap):
    return replace(
        snap,
        applicants=[replace(row, spec_id=0, ilvl=0) for row in snap.applicants],
        roster=[replace(row, spec_id=0, ilvl=0) for row in snap.roster],
    )


def _ready(state):
    for row in (*state.applicants.values(), *state.party_members.values()):
        row.fetch_status = "ready"
        row.raid_heroic = 91.0


def _run(snapshots, *, mode, initial=None):
    state = AppState()
    machine = StateMachine(state)
    if initial is not None:
        machine.apply_snapshot(initial)
        _ready(state)
    lifecycle = []
    machine.applicantAdded.connect(lambda row: lifecycle.append(("added", row.name)))
    machine.applicantRemoved.connect(lambda aid: lifecycle.append(("removed", aid)))
    if mode == "sequential":
        for snap in snapshots:
            machine.apply_snapshot(snap)
    elif mode == "plan":
        pending = ()
        for snap in snapshots:
            pending = append_pending_snapshot(pending, snap)
        for snap, _cache in snapshot_application_plan(pending, snapshots):
            machine.apply_snapshot(snap)
    else:
        callbacks = []
        cached = []
        queue = SnapshotApplyQueue(
            machine,
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
        assert len(cached) == len(snapshots)
        assert all(actual is original for actual, original in zip(cached, snapshots))
    return state, machine, lifecycle


def _values(state):
    def rows(mapping):
        return [
            (key, row.name, row.spec_id, row.ilvl, row.fetch_status, row.raid_heroic)
            for key, row in sorted(mapping.items())
        ]

    return rows(state.applicants), rows(state.party_members), state.player


@pytest.mark.parametrize("mode", ["plan", "queue"])
@pytest.mark.parametrize("already_applied", [False, True])
def test_unknown_rows_keep_known_spec_and_gear_through_coalescing(
    mode, already_applied
):
    first = _known()
    snapshots = (
        replace(
            first,
            applicants=[replace(first.applicants[0], ilvl=690)],
            roster=[replace(first.roster[0], ilvl=690)],
        ),
        _unknown_rows(first),
    )
    initial = first if already_applied else None
    expected, _, events = _run(snapshots, mode="sequential", initial=initial)
    actual, _, actual_events = _run(snapshots, mode=mode, initial=initial)
    assert _values(actual) == _values(expected)
    assert actual_events == events
    assert [row.ilvl for row in actual.applicants.values()] == [690]
    assert [row.spec_id for row in actual.party_members.values()] == [62]


@pytest.mark.parametrize("mode", ["plan", "queue"])
@pytest.mark.parametrize("incomplete", ["unknown", "bare", "region"])
def test_incomplete_producer_frame_keeps_prior_host_identity(mode, incomplete):
    first = _known()
    assert first.version is not None
    version = replace(
        first.version,
        player_name="?"
        if incomplete == "unknown"
        else "Host"
        if incomplete == "bare"
        else "Host-Realm",
        region_id=0 if incomplete in ("unknown", "region") else 3,
    )
    snapshots = (first, replace(first, version=version))
    expected, expected_machine, _ = _run(snapshots, mode="sequential")
    actual, actual_machine, _ = _run(snapshots, mode=mode)
    assert _values(actual) == _values(expected)
    assert (
        actual_machine._producer_player_name == expected_machine._producer_player_name
    )
    assert (
        actual_machine._producer_player_realm == expected_machine._producer_player_realm
    )
    assert actual_machine._producer_region == expected_machine._producer_region


@pytest.mark.parametrize("mode", ["plan", "queue"])
@pytest.mark.parametrize("surface", ["applicants", "roster"])
def test_removed_and_reappearing_row_does_not_keep_old_wcl(mode, surface):
    first = _known()
    absent = replace(first, **{surface: []})
    snapshots = (absent, first)
    expected, _, expected_events = _run(snapshots, mode="sequential", initial=first)
    actual, _, events = _run(snapshots, mode=mode, initial=first)
    assert _values(actual) == _values(expected)
    assert events == expected_events
    mapping = actual.applicants if surface == "applicants" else actual.party_members
    assert [row.raid_heroic for row in mapping.values()] == [None]


@pytest.mark.parametrize("mode", ["plan", "queue"])
@pytest.mark.parametrize("transition", ["character", "spec"])
def test_row_identity_roundtrip_retires_previous_wcl(mode, transition):
    first = _known()
    if transition == "character":
        changed = replace(
            first,
            applicants=[replace(first.applicants[0], name="Other-Realm")],
            roster=[replace(first.roster[0], name="Other-Realm")],
        )
    else:
        changed = replace(
            first,
            applicants=[replace(first.applicants[0], spec_id=63)],
            roster=[replace(first.roster[0], spec_id=63)],
        )
    snapshots = (changed, first)
    expected, _, expected_events = _run(snapshots, mode="sequential", initial=first)
    actual, _, events = _run(snapshots, mode=mode, initial=first)
    assert _values(actual) == _values(expected)
    assert events == expected_events
    assert all(
        row.raid_heroic is None
        for row in (*actual.applicants.values(), *actual.party_members.values())
    )


def test_stable_identical_burst_remains_one_compacted_snapshot():
    first = _known()
    pending = ()
    for _index in range(100):
        pending = append_pending_snapshot(pending, replace(first))
    assert len(pending) == 1
    assert len(snapshot_application_plan(pending, (first,) * 100)) == 1
    expected, _, events = _run((first,), mode="sequential")
    actual, _, actual_events = _run((first,) * 100, mode="queue")
    assert _values(actual) == _values(expected)
    assert actual_events == events


@pytest.mark.parametrize("mode", ["plan", "queue"])
@pytest.mark.parametrize("surface", ["applicants", "roster"])
def test_unavailable_surface_does_not_forget_previous_authoritative_known_rows(
    mode, surface
):
    first = _known()
    unavailable = replace(first, **{surface: [], surface + "_unavailable": True})
    snapshots = (first, unavailable, _unknown_rows(first))
    expected, _, expected_events = _run(snapshots, mode="sequential")
    actual, _, events = _run(snapshots, mode=mode)
    assert _values(actual) == _values(expected)
    assert events == expected_events
    assert all(
        row.spec_id == 62 and row.ilvl == 685
        for row in (*actual.applicants.values(), *actual.party_members.values())
    )


@pytest.mark.parametrize("mode", ["plan", "queue"])
def test_restricted_listing_seed_is_first_available_listing(mode):
    first = replace(
        _known(),
        lfg_unavailable=True,
        applicants_unavailable=True,
        roster_unavailable=True,
        applicants=[],
        roster=[],
    )
    assert first.listing is not None
    second = replace(
        first,
        listing=replace(
            first.listing, activity_id=402, dungeon_name="Other dungeon", key_level=14
        ),
    )
    snapshots = (first, second)
    expected, _, _ = _run(snapshots, mode="sequential")
    actual, _, _ = _run(snapshots, mode=mode)
    assert actual.listing == expected.listing
    assert actual.listing is not None and actual.listing.activity_id == 401


@pytest.mark.parametrize("mode", ["plan", "queue"])
def test_roster_name_case_roundtrip_does_not_borrow_known_spec_or_gear(mode):
    first = _known()
    unknown = _unknown_rows(first)
    changed_case = replace(
        unknown, roster=[replace(unknown.roster[0], name="host-realm")]
    )
    snapshots = (changed_case, unknown)
    expected, _, _ = _run(snapshots, mode="sequential", initial=first)
    actual, _, _ = _run(snapshots, mode=mode, initial=first)
    assert _values(actual) == _values(expected)
    assert [(row.spec_id, row.ilvl) for row in actual.party_members.values()] == [
        (0, 0)
    ]


@pytest.mark.parametrize("mode", ["plan", "queue"])
def test_filtered_placeholder_applicant_is_an_authoritative_removal(mode):
    first = _known()
    placeholder = replace(first, applicants=[replace(first.applicants[0], name="?")])
    snapshots = (placeholder, first)
    expected, _, expected_events = _run(snapshots, mode="sequential", initial=first)
    actual, _, events = _run(snapshots, mode=mode, initial=first)
    assert _values(actual) == _values(expected)
    assert events == expected_events == [("removed", "42:1"), ("added", "Mage-Realm")]
    assert [row.raid_heroic for row in actual.applicants.values()] == [None]


@pytest.mark.parametrize("mode", ["plan", "queue"])
@pytest.mark.parametrize("already_applied", [False, True])
def test_authoritative_no_listing_precedes_restricted_new_listing_seed(
    mode, already_applied
):
    first = _known()
    clear_listing = replace(
        first, listing=None, applicants=[], roster=[], roster_unavailable=True
    )
    assert first.listing is not None
    new_seed = replace(
        first,
        listing=replace(first.listing, activity_id=402, dungeon_name="New dungeon"),
        applicants=[],
        roster=[],
        lfg_unavailable=True,
        applicants_unavailable=True,
        roster_unavailable=True,
    )
    snapshots = (clear_listing, new_seed)
    initial = first if already_applied else None
    expected, _, expected_events = _run(snapshots, mode="sequential", initial=initial)
    actual, _, events = _run(snapshots, mode=mode, initial=initial)
    assert _values(actual) == _values(expected)
    assert events == expected_events
    assert actual.listing == expected.listing
    assert actual.listing is not None and actual.listing.activity_id == 402
    assert actual.applicants == {}


@pytest.mark.parametrize("mode", ["plan", "queue"])
@pytest.mark.parametrize("incomplete", ["realm", "region"])
def test_producer_switch_before_incomplete_return_folds_identity_in_order(
    mode, incomplete
):
    first = _known()
    assert first.version is not None
    other = replace(
        _unknown_rows(first),
        listing=None,
        applicants=[],
        version=replace(
            first.version,
            player_name="Other-Realm",
            region_id=1 if incomplete == "region" else 3,
        ),
    )
    returned = replace(
        other,
        version=replace(
            first.version,
            player_name="Host" if incomplete == "realm" else "Host-Realm",
            region_id=0 if incomplete == "region" else 3,
        ),
        roster=[replace(first.roster[0], spec_id=63, ilvl=200)],
    )
    snapshots = (other, returned)
    expected, expected_machine, expected_events = _run(
        snapshots,
        mode="sequential",
        initial=first,
    )
    actual, actual_machine, events = _run(snapshots, mode=mode, initial=first)
    assert _values(actual) == _values(expected)
    assert events == expected_events
    assert (
        actual_machine._producer_player_name == expected_machine._producer_player_name
    )
    assert (
        actual_machine._producer_player_realm == expected_machine._producer_player_realm
    )
    assert actual_machine._producer_region == expected_machine._producer_region
    if incomplete == "realm":
        assert actual.player.full_name == "Host"
    else:
        assert actual.player.region_id == 1


@pytest.mark.parametrize("mode", ["plan", "queue"])
def test_bare_host_region_change_removes_obsolete_realm_conflict(mode):
    first = _known()
    assert first.version is not None
    roster = [replace(first.roster[0], spec_id=63, ilvl=200)]
    common = replace(first, applicants=[], roster=roster)
    other = replace(
        common,
        listing=None,
        lfg_unavailable=True,
        roster_unavailable=True,
        applicants_unavailable=True,
        version=replace(first.version, player_name="Other-Realm", region_id=1),
    )
    host_unknown_region = replace(common, version=replace(first.version, region_id=0))
    restricted = replace(
        host_unknown_region,
        listing=None,
        lfg_unavailable=True,
        applicants_unavailable=True,
        roster=[replace(roster[0], spec_id=0, ilvl=0)],
    )
    bare_host_eu = replace(
        common, version=replace(first.version, player_name="Host", region_id=3)
    )
    new_realm = replace(
        common,
        listing=None,
        roster=[],
        roster_unavailable=True,
        version=replace(first.version, player_name="Host-OtherRealm", region_id=3),
    )
    snapshots = (other, host_unknown_region, restricted, bare_host_eu, new_realm)
    expected, expected_machine, _ = _run(snapshots, mode="sequential", initial=first)
    actual, actual_machine, _ = _run(snapshots, mode=mode, initial=first)
    assert _values(actual) == _values(expected)
    assert actual.listing == expected.listing is None
    assert [(row.spec_id, row.ilvl) for row in actual.party_members.values()] == [
        (63, 200)
    ]
    assert (
        actual_machine._producer_player_realm == expected_machine._producer_player_realm
    )
    assert actual_machine._producer_region == expected_machine._producer_region


def test_alternating_fully_known_producer_burst_has_bounded_context_history():
    first = _known()
    assert first.version is not None
    snapshots = tuple(
        replace(
            first,
            version=replace(
                first.version,
                player_name="Host-Realm" if index % 2 else "Other-Realm",
                region_id=3 if index % 2 else 1,
            ),
        )
        for index in range(100)
    )
    pending = ()
    for snap in snapshots:
        pending = append_pending_snapshot(pending, snap)
        assert len(pending) <= 2
        assert sum(len(frame.producer_version_history) for frame in pending) <= 1
    expected, _, _ = _run(snapshots, mode="sequential")
    actual, _, _ = _run(snapshots, mode="queue")
    assert _values(actual) == _values(expected)
