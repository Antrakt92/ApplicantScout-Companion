"""Unit tests for StateMachine.apply_snapshot diff semantics.

Covers:
- Composite-id construction: f"{applicant_id}:{member_idx}" — multi-member
  group apps create one state entry per member.
- Name-change detection in diff (B-5: leader-leaves stale-WCL bug):
  when leader leaves a group and member 2 becomes new leader at idx=1,
  same composite slot has a DIFFERENT character — must wipe WCL data
  even if specs happen to match.

PyQtSignal emit-to-nothing is no-op (no slots connected) so these tests
run without QApplication. State inspection via state.applicants dict.
"""

from __future__ import annotations

from applicant_scout.__main__ import StateMachine
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedListing,
    DecodedVersion,
    Snapshot,
)
from applicant_scout.state import AppState, WoWPlayer


def _decoded(
    aid: int,
    member_idx: int,
    name: str,
    spec_id: int = 71,
    cls: int = 1,
    ilvl: int = 480,
    score: int = 2000,
    main_score: int = 0,
    role: int = 2,
) -> DecodedApplicant:
    return DecodedApplicant(
        applicant_id=aid,
        member_idx=member_idx,
        name=name,
        spec_id=spec_id,
        class_id=cls,
        ilvl=ilvl,
        score=score,
        main_score=main_score,
        role=role,
    )


def _listing(activity_id: int = 401, key_level: int = 14) -> DecodedListing:
    return DecodedListing(
        activity_id=activity_id,
        key_level=key_level,
        dungeon_name="Pit of Saron",
        listing_name="+14",
        comment="",
    )


def _version(player_name: str, region_id: int = 3) -> DecodedVersion:
    return DecodedVersion(
        addon_version="0.1.0",
        game_version="12.0.5",
        region_id=region_id,
        player_name=player_name,
    )


# ─── Composite-id construction ──────────────────────────────────────────────


def test_two_member_group_creates_two_state_entries_with_composite_keys():
    state = AppState()
    sm = StateMachine(state)
    snap = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=42, member_idx=1, name="Voodooghost-TN"),
            _decoded(aid=42, member_idx=2, name="Umbranology-TN", cls=9, spec_id=265),
        ],
    )
    sm.apply_snapshot(snap)
    assert "42:1" in state.applicants
    assert "42:2" in state.applicants
    assert state.applicants["42:1"].name == "Voodooghost-TN"
    assert state.applicants["42:2"].name == "Umbranology-TN"
    # Both pull from same applicant_id — group identity preserved at composite-key level.
    assert state.applicants["42:1"].applicant_id == "42:1"
    assert state.applicants["42:2"].applicant_id == "42:2"


def test_solo_app_creates_single_entry_with_member_idx_one():
    state = AppState()
    sm = StateMachine(state)
    snap = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=99, member_idx=1, name="Solo-Realm")],
    )
    sm.apply_snapshot(snap)
    assert list(state.applicants.keys()) == ["99:1"]


def test_new_applicant_maps_main_score():
    state = AppState()
    sm = StateMachine(state)
    snap = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=99,
                member_idx=1,
                name="Altmain-Realm",
                score=2443,
                main_score=3468,
            )
        ],
    )

    sm.apply_snapshot(snap)

    applicant = state.applicants["99:1"]
    assert applicant.score == 2443
    assert applicant.main_score == 3468


# ─── Name-change detection (B-5) ────────────────────────────────────────────


def test_name_change_at_same_composite_key_wipes_wcl_data():
    """Leader-leaves edge case: leader Voodooghost (Frost) leaves group,
    member 2 (also Frost spec but DIFFERENT character) becomes new leader at
    member_idx=1. Same composite key 42:1, same spec — but different person.
    Must wipe WCL data and re-trigger fetch (fetch_status='pending')."""
    state = AppState()
    sm = StateMachine(state)
    # Initial: leader is Voodooghost, has WCL data populated (simulate fetch
    # already landed by mutating state directly post-snapshot).
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=42, member_idx=1, name="Voodooghost-TN", spec_id=64),
        ],
    )
    sm.apply_snapshot(snap1)
    state.applicants["42:1"].fetch_status = "ready"
    state.applicants["42:1"].mplus_dps = 88.5
    state.applicants["42:1"].raid_heroic = 92.0
    state.applicants["42:1"].mplus_dps_breakdown = [
        {
            "name": "X",
            "parse_percent": 80.0,
            "median_percent": 70.0,
            "key_level": 14,
            "run_count": 3,
        }
    ]

    # Snap 2: leader gone. The OTHER Frost Mage (was member 2) is now the new
    # leader at member_idx=1. Same composite, same spec, DIFFERENT name.
    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=42, member_idx=1, name="OtherFrost-TN", spec_id=64),
        ],
    )
    sm.apply_snapshot(snap2)
    a = state.applicants["42:1"]
    assert a.name == "OtherFrost-TN"
    assert a.fetch_status == "pending"  # name-change triggered refetch
    assert a.mplus_dps is None  # WCL wiped
    assert a.raid_heroic is None  # WCL wiped
    assert a.mplus_dps_breakdown == []  # WCL wiped


def test_spec_change_wipes_wcl_data_unchanged_behavior():
    """Pin existing spec_changed wipe behavior — regression guard."""
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=99, member_idx=1, name="Same-Realm", spec_id=64)],
    )
    sm.apply_snapshot(snap1)
    state.applicants["99:1"].fetch_status = "ready"
    state.applicants["99:1"].raid_mythic = 75.0

    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=99, member_idx=1, name="Same-Realm", spec_id=63),  # respec
        ],
    )
    sm.apply_snapshot(snap2)
    a = state.applicants["99:1"]
    assert a.spec_id == 63
    assert a.fetch_status == "pending"
    assert a.raid_mythic is None


def test_metric_role_change_wipes_wcl_data():
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=99, member_idx=1, name="Same-Realm", spec_id=70, role=2),
        ],
    )
    sm.apply_snapshot(snap1)
    state.applicants["99:1"].fetch_status = "ready"
    state.applicants["99:1"].mplus_dps = 80.0
    state.applicants["99:1"].raid_heroic = 88.0

    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=99, member_idx=1, name="Same-Realm", spec_id=70, role=1),
        ],
    )
    sm.apply_snapshot(snap2)
    a = state.applicants["99:1"]
    assert a.role == "HEALER"
    assert a.fetch_status == "pending"
    assert a.mplus_dps is None
    assert a.raid_heroic is None


def test_tank_to_damager_preserves_dps_shaped_wcl_data():
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=99, member_idx=1, name="Same-Realm", spec_id=70, role=0),
        ],
    )
    sm.apply_snapshot(snap1)
    state.applicants["99:1"].fetch_status = "ready"
    state.applicants["99:1"].mplus_dps = 80.0
    state.applicants["99:1"].raid_heroic = 88.0

    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=99, member_idx=1, name="Same-Realm", spec_id=70, role=2),
        ],
    )
    sm.apply_snapshot(snap2)
    a = state.applicants["99:1"]
    assert a.role == "DAMAGER"
    assert a.fetch_status == "ready"
    assert a.mplus_dps == 80.0
    assert a.raid_heroic == 88.0


def test_no_change_preserves_wcl_data():
    """No spec change AND no name change → preserve cached WCL (gear swap,
    score change, etc. don't invalidate WCL data)."""
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=99, member_idx=1, name="Same-Realm", spec_id=64, ilvl=480),
        ],
    )
    sm.apply_snapshot(snap1)
    state.applicants["99:1"].fetch_status = "ready"
    state.applicants["99:1"].mplus_dps = 80.0

    # Snap 2: only ilvl/score changed (gear swap, not respec, not different char).
    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=99, member_idx=1, name="Same-Realm", spec_id=64, ilvl=485),
        ],
    )
    sm.apply_snapshot(snap2)
    a = state.applicants["99:1"]
    assert a.ilvl == 485
    assert a.fetch_status == "ready"  # preserved
    assert a.mplus_dps == 80.0  # preserved


def test_main_score_change_preserves_wcl_data():
    """RaiderIO main score drift is not WCL identity; preserve fetched parses."""
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=99,
                member_idx=1,
                name="Same-Realm",
                spec_id=64,
                score=2400,
                main_score=0,
            ),
        ],
    )
    sm.apply_snapshot(snap1)
    state.applicants["99:1"].fetch_status = "ready"
    state.applicants["99:1"].mplus_dps = 80.0
    state.applicants["99:1"].raid_heroic = 92.0

    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=99,
                member_idx=1,
                name="Same-Realm",
                spec_id=64,
                score=2400,
                main_score=3468,
            ),
        ],
    )
    sm.apply_snapshot(snap2)

    applicant = state.applicants["99:1"]
    assert applicant.main_score == 3468
    assert applicant.fetch_status == "ready"
    assert applicant.mplus_dps == 80.0
    assert applicant.raid_heroic == 92.0


def test_late_player_full_name_clears_same_realm_missing_realm():
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=42, member_idx=1, name="Scout", spec_id=71)],
    )
    sm.apply_snapshot(snap1)
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "error"
    applicant.error_message = "missing realm"
    applicant.mplus_dps = 77.0

    snap2 = Snapshot(
        listing=_listing(),
        version=_version("Host-RealmA"),
        applicants=[_decoded(aid=42, member_idx=1, name="Scout", spec_id=71)],
    )
    sm.apply_snapshot(snap2)

    applicant = state.applicants["42:1"]
    assert applicant.fetch_status == "pending"
    assert applicant.error_message == ""
    assert applicant.mplus_dps is None


def test_player_full_name_change_invalidates_same_realm_ready_data():
    state = AppState()
    state.player = WoWPlayer(region_id=3, full_name="Host-RealmA")
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=42, member_idx=1, name="Scout", spec_id=71)],
    )
    sm.apply_snapshot(snap1)
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "ready"
    applicant.raid_heroic = 92.0
    applicant.mplus_dps_breakdown = [
        {
            "name": "X",
            "parse_percent": 80.0,
            "median_percent": 70.0,
            "key_level": 14,
            "run_count": 3,
        }
    ]

    snap2 = Snapshot(
        listing=_listing(),
        version=_version("Host-RealmB"),
        applicants=[_decoded(aid=42, member_idx=1, name="Scout", spec_id=71)],
    )
    sm.apply_snapshot(snap2)

    applicant = state.applicants["42:1"]
    assert applicant.fetch_status == "pending"
    assert applicant.raid_heroic is None
    assert applicant.mplus_dps_breakdown == []


def test_player_full_name_change_preserves_explicit_realm_data():
    state = AppState()
    state.player = WoWPlayer(region_id=3, full_name="Host-RealmA")
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=42, member_idx=1, name="Scout-RealmX", spec_id=71)],
    )
    sm.apply_snapshot(snap1)
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "ready"
    applicant.raid_heroic = 92.0

    snap2 = Snapshot(
        listing=_listing(),
        version=_version("Host-RealmB"),
        applicants=[_decoded(aid=42, member_idx=1, name="Scout-RealmX", spec_id=71)],
    )
    sm.apply_snapshot(snap2)

    applicant = state.applicants["42:1"]
    assert applicant.fetch_status == "ready"
    assert applicant.raid_heroic == 92.0


def test_region_change_invalidates_explicit_realm_data():
    state = AppState()
    state.player = WoWPlayer(region_id=3, full_name="Host-RealmA")
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=42, member_idx=1, name="Scout-RealmX", spec_id=71)],
    )
    sm.apply_snapshot(snap1)
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "ready"
    applicant.raid_heroic = 92.0

    snap2 = Snapshot(
        listing=_listing(),
        version=_version("Host-RealmA", region_id=1),
        applicants=[_decoded(aid=42, member_idx=1, name="Scout-RealmX", spec_id=71)],
    )
    sm.apply_snapshot(snap2)

    applicant = state.applicants["42:1"]
    assert applicant.fetch_status == "pending"
    assert applicant.raid_heroic is None


def test_unknown_region_change_preserves_wcl_data():
    state = AppState()
    state.player = WoWPlayer(region_id=0, full_name="Host-RealmA")
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=42, member_idx=1, name="Scout-RealmX", spec_id=71)],
    )
    sm.apply_snapshot(snap1)
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "ready"
    applicant.raid_heroic = 92.0

    snap2 = Snapshot(
        listing=_listing(),
        version=_version("Host-RealmA", region_id=99),
        applicants=[_decoded(aid=42, member_idx=1, name="Scout-RealmX", spec_id=71)],
    )
    sm.apply_snapshot(snap2)

    applicant = state.applicants["42:1"]
    assert applicant.fetch_status == "ready"
    assert applicant.raid_heroic == 92.0


def test_no_listing_version_snapshot_clears_without_pending_refetch_state():
    state = AppState()
    state.player = WoWPlayer(region_id=3, full_name="Host-RealmA")
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=42, member_idx=1, name="Scout", spec_id=71)],
    )
    sm.apply_snapshot(snap1)
    state.applicants["42:1"].fetch_status = "ready"

    snap2 = Snapshot(
        listing=None,
        version=_version("Host-RealmB", region_id=1),
        applicants=[],
    )
    sm.apply_snapshot(snap2)

    assert state.listing is None
    assert state.applicants == {}


def test_member_leaves_group_remove_emitted():
    """Group of 2 → solo. Composite '42:2' must be removed from state."""
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=42, member_idx=1, name="A-Realm"),
            _decoded(aid=42, member_idx=2, name="B-Realm"),
        ],
    )
    sm.apply_snapshot(snap1)
    assert "42:1" in state.applicants and "42:2" in state.applicants

    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=42, member_idx=1, name="A-Realm")],
    )
    sm.apply_snapshot(snap2)
    assert "42:1" in state.applicants
    assert "42:2" not in state.applicants


def test_member_idx_swap_with_different_specs_treated_as_two_changes():
    """Edge case: 2-member group, leader (Frost Mage spec=64) leaves; member 2
    (Fire Mage spec=63) is now leader at idx=1. Composite '42:1' undergoes
    BOTH spec change AND name change in one snapshot. Verify needs_refetch
    fires (defensive against future code where one of the two conditions is
    accidentally dropped)."""
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=42, member_idx=1, name="Frost-TN", spec_id=64),
            _decoded(aid=42, member_idx=2, name="Fire-TN", spec_id=63),
        ],
    )
    sm.apply_snapshot(snap1)
    state.applicants["42:1"].fetch_status = "ready"
    state.applicants["42:1"].mplus_dps = 90.0

    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[_decoded(aid=42, member_idx=1, name="Fire-TN", spec_id=63)],
    )
    sm.apply_snapshot(snap2)
    a = state.applicants["42:1"]
    assert a.name == "Fire-TN"
    assert a.spec_id == 63
    assert a.fetch_status == "pending"
    assert a.mplus_dps is None
