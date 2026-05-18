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
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
)
from applicant_scout.state import AppState, WoWPlayer


class _FakeRioReader:
    def lookup_profile(
        self,
        name: str,
        realm: str,
        region: str,
        *,
        allow_load: bool = True,
    ):
        assert allow_load is False
        if (name, realm, region) == ("Chinie", "Ragnaros", "EU"):
            return type(
                "Profile",
                (),
                {
                    "dungeons": [
                        {"name": "Pit of Saron", "key_level": 12},
                        {"name": "Skyreach", "key_level": 14},
                    ]
                },
            )()
        if (name, realm, region) == ("Arthas", "Король-лич", "EU"):
            return type(
                "Profile",
                (),
                {"dungeons": [{"name": "Skyreach", "key_level": 15}]},
            )()
        return None


class _RaisingRioReader:
    def lookup_profile(self, *_args: object, **_kwargs: object) -> object:
        raise ValueError("decode drift")


class _ColdRioReader:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.loaded = False
        self.callbacks = []
        self.rows = rows or [{"name": "Pit of Saron", "key_level": 12}]

    def lookup_profile(
        self,
        name: str,
        realm: str,
        region: str,
        *,
        allow_load: bool = True,
    ):
        assert allow_load is False
        if not self.loaded or (name, realm, region) != ("Chinie", "Ragnaros", "EU"):
            return None
        return type("Profile", (), {"dungeons": [dict(row) for row in self.rows]})()

    def preload_region_async(self, region: str | None, on_loaded=None) -> None:
        assert region == "EU"
        if on_loaded is not None:
            self.callbacks.append(on_loaded)

    def finish(self) -> None:
        self.loaded = True
        for callback in list(self.callbacks):
            callback()


def _decoded(
    aid: int,
    member_idx: int,
    name: str,
    spec_id: int = 71,
    cls: int = 1,
    ilvl: int = 480,
    score: int = 2000,
    main_score: int = 0,
    rio_profile: bool = False,
    rio_best_key: int = 0,
    rio_best_dungeon_key: int = 0,
    rio_timed_at_or_above: int = 0,
    rio_timed_at_or_above_minus1: int = 0,
    rio_timed_at_or_above_minus2: int = 0,
    rio_completed_at_or_above_minus1: int = 0,
    rio_dungeon_count: int = 0,
    rio_dungeons: list[dict] | None = None,
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
        rio_profile=rio_profile,
        rio_best_key=rio_best_key,
        rio_best_dungeon_key=rio_best_dungeon_key,
        rio_timed_at_or_above=rio_timed_at_or_above,
        rio_timed_at_or_above_minus1=rio_timed_at_or_above_minus1,
        rio_timed_at_or_above_minus2=rio_timed_at_or_above_minus2,
        rio_completed_at_or_above_minus1=rio_completed_at_or_above_minus1,
        rio_dungeon_count=rio_dungeon_count,
        rio_dungeons=rio_dungeons or [],
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


def _roster_decoded(
    name: str,
    *,
    unit_index: int = 1,
    flags: int = 0,
    subgroup: int = 1,
    cls: int = 1,
    spec_id: int = 71,
    ilvl: int = 700,
    score: int = 3000,
    main_score: int = 0,
    rio_profile: bool = False,
    rio_best_key: int = 0,
    rio_best_dungeon_key: int = 0,
    rio_timed_at_or_above: int = 0,
    rio_timed_at_or_above_minus1: int = 0,
    rio_timed_at_or_above_minus2: int = 0,
    rio_completed_at_or_above_minus1: int = 0,
    rio_dungeon_count: int = 0,
    role: int = 2,
) -> DecodedRosterMember:
    return DecodedRosterMember(
        unit_index=unit_index,
        flags=flags,
        subgroup=subgroup,
        class_id=cls,
        spec_id=spec_id,
        ilvl=ilvl,
        score=score,
        main_score=main_score,
        rio_profile=rio_profile,
        rio_best_key=rio_best_key,
        rio_best_dungeon_key=rio_best_dungeon_key,
        rio_timed_at_or_above=rio_timed_at_or_above,
        rio_timed_at_or_above_minus1=rio_timed_at_or_above_minus1,
        rio_timed_at_or_above_minus2=rio_timed_at_or_above_minus2,
        rio_completed_at_or_above_minus1=rio_completed_at_or_above_minus1,
        rio_dungeon_count=rio_dungeon_count,
        role=role,
        name=name,
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


def test_new_applicant_maps_rio_completion_summary():
    state = AppState()
    sm = StateMachine(state)
    snap = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=42,
                member_idx=1,
                name="Rio-Realm",
                score=3321,
                main_score=3550,
                rio_profile=True,
                rio_best_key=17,
                rio_best_dungeon_key=15,
                rio_timed_at_or_above=1,
                rio_timed_at_or_above_minus1=8,
                rio_timed_at_or_above_minus2=8,
                rio_completed_at_or_above_minus1=8,
                rio_dungeon_count=8,
            )
        ],
    )

    sm.apply_snapshot(snap)

    applicant = state.applicants["42:1"]
    assert applicant.rio_profile is True
    assert applicant.rio_best_key == 17
    assert applicant.rio_best_dungeon_key == 15
    assert applicant.rio_timed_at_or_above == 1
    assert applicant.rio_timed_at_or_above_minus1 == 8
    assert applicant.rio_timed_at_or_above_minus2 == 8
    assert applicant.rio_completed_at_or_above_minus1 == 8
    assert applicant.rio_dungeon_count == 8
    assert applicant.rio_summary_target_key == 14


def test_new_applicant_maps_rio_dungeon_rows():
    state = AppState()
    sm = StateMachine(state)
    rows = [
        {"name": "Skyreach", "key_level": 15},
        {"name": "Pit of Saron", "key_level": 16},
    ]
    snap = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=42,
                member_idx=1,
                name="Rio-Realm",
                rio_profile=True,
                rio_dungeons=rows,
            )
        ],
    )

    sm.apply_snapshot(snap)

    assert state.applicants["42:1"].rio_dungeons == rows


def test_new_applicant_enriches_rio_dungeon_rows_from_local_reader():
    state = AppState()
    sm = StateMachine(state, rio_reader=_FakeRioReader())
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="Chinie",
                    rio_dungeons=[],
                )
            ],
        )
    )

    assert state.applicants["42:1"].rio_dungeons == [
        {"name": "Pit of Saron", "key_level": 12},
        {"name": "Skyreach", "key_level": 14},
    ]


def test_new_applicant_enriches_explicit_hyphenated_realm_from_local_reader():
    state = AppState()
    sm = StateMachine(state, rio_reader=_FakeRioReader())
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="Arthas-Король-лич",
                    rio_dungeons=[],
                )
            ],
        )
    )

    assert state.applicants["42:1"].rio_dungeons == [
        {"name": "Skyreach", "key_level": 15}
    ]


def test_local_rio_decode_error_falls_back_to_decoded_rows_without_crashing():
    state = AppState()
    sm = StateMachine(state, rio_reader=_RaisingRioReader())
    decoded_rows = [{"name": "Skyreach", "key_level": 15}]

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="Chinie",
                    rio_dungeons=decoded_rows,
                )
            ],
        )
    )

    assert state.applicants["42:1"].rio_dungeons == decoded_rows


def test_first_snapshot_reenriches_applicant_after_async_rio_preload_completes():
    state = AppState()
    reader = _ColdRioReader()
    sm = StateMachine(state, rio_reader=reader)
    updated: list[str] = []
    sm.applicantUpdated.connect(lambda applicant: updated.append(applicant.applicant_id))

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[_decoded(aid=42, member_idx=1, name="Chinie")],
        )
    )
    state.applicants["42:1"].fetch_status = "ready"
    state.applicants["42:1"].mplus_dps = 91.0

    assert state.applicants["42:1"].rio_dungeons == []

    reader.finish()

    assert state.applicants["42:1"].rio_dungeons == [
        {"name": "Pit of Saron", "key_level": 12}
    ]
    assert state.applicants["42:1"].fetch_status == "ready"
    assert state.applicants["42:1"].mplus_dps == 91.0
    assert updated == ["42:1"]


def test_first_snapshot_reenriches_party_member_after_async_rio_preload_completes():
    state = AppState()
    reader = _ColdRioReader()
    sm = StateMachine(state, rio_reader=reader)
    roster_updates = 0

    def note_roster_update() -> None:
        nonlocal roster_updates
        roster_updates += 1

    sm.rosterChanged.connect(note_roster_update)
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            roster=[_roster_decoded("Chinie")],
        )
    )

    assert state.party_members["chinie"].rio_dungeons == []
    assert roster_updates == 1

    reader.finish()

    assert state.party_members["chinie"].rio_dungeons == [
        {"name": "Pit of Saron", "key_level": 12}
    ]
    assert roster_updates == 2


def test_stale_rio_preload_completion_is_ignored_after_reader_swap():
    state = AppState()
    old_reader = _ColdRioReader([{"name": "Old", "key_level": 20}])
    new_reader = _ColdRioReader([{"name": "New", "key_level": 21}])
    sm = StateMachine(state, rio_reader=old_reader)
    updated: list[str] = []
    sm.applicantUpdated.connect(lambda applicant: updated.append(applicant.applicant_id))

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[_decoded(aid=42, member_idx=1, name="Chinie")],
        )
    )
    sm.set_rio_reader(new_reader)
    old_reader.finish()

    assert state.applicants["42:1"].rio_dungeons == []
    assert updated == []


def test_rio_preload_completion_after_clear_does_not_resurrect_removed_rows():
    state = AppState()
    reader = _ColdRioReader()
    sm = StateMachine(state, rio_reader=reader)
    updated: list[str] = []
    sm.applicantUpdated.connect(lambda applicant: updated.append(applicant.applicant_id))

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[_decoded(aid=42, member_idx=1, name="Chinie")],
        )
    )
    sm.apply_snapshot(Snapshot(listing=None, version=None, applicants=[]))
    reader.finish()

    assert state.applicants == {}
    assert updated == []


def test_existing_applicant_replaces_stale_rio_dungeon_rows():
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=42,
                member_idx=1,
                name="Rio-Realm",
                rio_profile=True,
                rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
            )
        ],
    )
    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=42,
                member_idx=1,
                name="Rio-Realm",
                rio_profile=True,
                rio_dungeons=[{"name": "Pit of Saron", "key_level": 16}],
            )
        ],
    )

    sm.apply_snapshot(snap1)
    sm.apply_snapshot(snap2)

    assert state.applicants["42:1"].rio_dungeons == [
        {"name": "Pit of Saron", "key_level": 16}
    ]


def test_existing_applicant_clears_rio_dungeon_rows_when_older_wire_has_none():
    state = AppState()
    sm = StateMachine(state)
    snap1 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=42,
                member_idx=1,
                name="Rio-Realm",
                rio_profile=True,
                rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
            )
        ],
    )
    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=42,
                member_idx=1,
                name="Rio-Realm",
                rio_profile=True,
                rio_dungeons=[],
            )
        ],
    )

    sm.apply_snapshot(snap1)
    sm.apply_snapshot(snap2)

    assert state.applicants["42:1"].rio_dungeons == []


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


# ─── Party / raid roster diff ───────────────────────────────────────────────


def test_roster_snapshot_adds_party_members_separately_from_applicants():
    state = AppState()
    sm = StateMachine(state)

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            applicants=[_decoded(7, 1, "Applicant-Realm")],
            roster=[
                _roster_decoded("Host-Realm", unit_index=0, flags=1, role=0),
                _roster_decoded("Friend-Realm", unit_index=1, role=1),
            ],
        )
    )

    assert set(state.applicants) == {"7:1"}
    assert set(state.party_members) == {"host-realm", "friend-realm"}
    assert state.party_members["host-realm"].is_self
    assert state.party_members["friend-realm"].role == "HEALER"


def test_roster_snapshot_updates_and_removes_members_by_identity():
    state = AppState()
    sm = StateMachine(state)

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            roster=[
                _roster_decoded("Host-Realm", unit_index=0, flags=1, score=3000),
                _roster_decoded("Friend-Realm", unit_index=1, score=2500),
            ],
        )
    )
    state.party_members["friend-realm"].fetch_status = "ready"
    state.party_members["friend-realm"].mplus_dps = 80.0

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            roster=[
                _roster_decoded("Host-Realm", unit_index=0, flags=1, score=3100),
                _roster_decoded("Newfriend-Realm", unit_index=2, score=2600),
            ],
        )
    )

    assert set(state.party_members) == {"host-realm", "newfriend-realm"}
    assert state.party_members["host-realm"].score == 3100
    assert state.party_members["newfriend-realm"].score == 2600


def test_empty_roster_snapshot_clears_party_without_clearing_applicants():
    state = AppState()
    sm = StateMachine(state)

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            applicants=[_decoded(7, 1, "Applicant-Realm")],
            roster=[_roster_decoded("Host-Realm", flags=1)],
        )
    )
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            applicants=[_decoded(7, 1, "Applicant-Realm")],
            roster=[],
        )
    )

    assert set(state.applicants) == {"7:1"}
    assert state.party_members == {}
