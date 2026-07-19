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

from collections.abc import Callable
import json
from pathlib import Path

import pytest

from applicant_scout.__main__ import (
    RIO_PRELOAD_REFRESH_INTERVAL_SECONDS,
    StateMachine,
)
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedLeaderKey,
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
    _try_parse_appscout_payload,
)
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, WoWPlayer


FIXTURES = Path(__file__).parent / "fixtures"
LUA_GOLDEN_STEM = "aps1_v8_lua_golden"
LUA_LEADER_KEY_GOLDEN_STEM = "aps1_v8_lua_leader_key_golden"


def _load_lua_golden_snapshot(stem: str = LUA_GOLDEN_STEM) -> tuple[Snapshot, dict]:
    payload = bytes.fromhex((FIXTURES / f"{stem}.hex").read_text(encoding="ascii"))
    expected = json.loads((FIXTURES / f"{stem}.expected.json").read_text(encoding="utf-8"))
    snap, error = _try_parse_appscout_payload(payload)
    assert error is None
    assert snap is not None
    return snap, expected


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
                    ],
                    "raid_progress": {
                        "M": {
                            "killed": 4,
                            "total": 8,
                            "bosses": {"Plexus Sentinel": True},
                        }
                    },
                },
            )()
        if (name, realm, region) == ("Arthas", "Король-лич", "EU"):
            return type(
                "Profile",
                (),
                {
                    "dungeons": [{"name": "Skyreach", "key_level": 15}],
                    "raid_progress": {
                        "H": {
                            "killed": 8,
                            "total": 8,
                            "bosses": {"Plexus Sentinel": True},
                        }
                    },
                },
            )()
        return None


class _RaisingRioReader:
    def lookup_profile(self, *_args: object, **_kwargs: object) -> object:
        raise ValueError("decode drift")


class _OSErrorRioReader:
    def lookup_profile(self, *_args: object, **_kwargs: object) -> object:
        raise OSError("RaiderIO DB locked")


class _RaidOnlyRioReader:
    def lookup_profile(self, *_args: object, **_kwargs: object) -> object:
        return type(
            "Profile",
            (),
            {
                "current_score": 0,
                "dungeons": [],
                "has_mplus_profile": False,
                "raid_progress": {
                    "M": {
                        "killed": 2,
                        "total": 8,
                        "bosses": {"Plexus Sentinel": True},
                    }
                },
            },
        )()


class _ScoreOnlyRioReader:
    def __init__(self, current_score: int) -> None:
        self.current_score = current_score

    def lookup_profile(self, *_args: object, **_kwargs: object) -> object:
        return type(
            "Profile",
            (),
            {
                "current_score": self.current_score,
                "dungeons": [],
                "raid_progress": {},
                "has_mplus_profile": True,
            },
        )()


class _MutableRioReader:
    def __init__(self, profile: object | None) -> None:
        self.profile = profile

    def lookup_profile(self, *_args: object, **_kwargs: object) -> object | None:
        return self.profile


def _local_rio_profile(
    *,
    score: int,
    dungeons: list[dict],
    raid_progress: dict[str, dict],
    has_mplus_profile: bool = True,
) -> object:
    return type(
        "Profile",
        (),
        {
            "current_score": score,
            "dungeons": [dict(row) for row in dungeons],
            "raid_progress": {
                difficulty: dict(progress)
                for difficulty, progress in raid_progress.items()
            },
            "has_mplus_profile": has_mplus_profile,
        },
    )()


class _FullRioReader:
    def lookup_profile(self, *_args: object, **_kwargs: object) -> object:
        return type(
            "Profile",
            (),
            {
                "current_score": 2861,
                "dungeons": [{"name": "Pit of Saron", "key_level": 12}],
                "raid_progress": {
                    "M": {
                        "killed": 4,
                        "total": 8,
                        "bosses": {"Plexus Sentinel": True},
                    }
                },
                "has_mplus_profile": True,
            },
        )()


class _ColdRioReader:
    def __init__(self, rows: list[dict] | None = None, current_score: int = 0) -> None:
        self.loaded = False
        self.callbacks = []
        self.preload_calls: list[str | None] = []
        self.rows = rows or [{"name": "Pit of Saron", "key_level": 12}]
        self.current_score = current_score

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
        return type(
            "Profile",
            (),
            {
                "current_score": self.current_score,
                "dungeons": [dict(row) for row in self.rows],
                "raid_progress": {},
                "has_mplus_profile": True,
            },
        )()

    def preload_region_async(self, region: str | None, on_loaded=None) -> None:
        assert region == "EU"
        self.preload_calls.append(region)
        if on_loaded is not None:
            self.callbacks.append(on_loaded)

    def finish(self) -> None:
        self.loaded = True
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


class _ControlledRioReader:
    def __init__(self) -> None:
        self.preload_calls: list[str | None] = []
        self.callbacks: dict[str, list[Callable[[], None]]] = {}

    def lookup_profile(self, *_args: object, **_kwargs: object) -> None:
        return None

    def preload_region_async(self, region: str | None, on_loaded=None) -> None:
        self.preload_calls.append(region)
        if region is not None and on_loaded is not None:
            self.callbacks.setdefault(region, []).append(on_loaded)

    def finish(self, region: str) -> None:
        for callback in self.callbacks.pop(region, []):
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
    rio_dungeons: list[dict] | None = None,
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
        rio_dungeons=rio_dungeons or [],
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


def test_identical_full_snapshot_does_not_emit_duplicate_applicant_update():
    state = AppState()
    sm = StateMachine(state)
    updates: list[str] = []
    sm.applicantUpdated.connect(
        lambda applicant: updates.append(applicant.applicant_id)
    )
    snapshot = Snapshot(
        listing=_listing(),
        version=_version("Host-Realm"),
        applicants=[_decoded(aid=99, member_idx=1, name="Solo-Realm")],
        roster=[_roster_decoded("Host-Realm", flags=1)],
    )

    sm.apply_snapshot(snapshot)
    applicant = state.applicants["99:1"]
    applicant.fetch_status = "ready"
    applicant.mplus_dps = 88.0
    sm.apply_snapshot(snapshot)

    assert state.applicants["99:1"] is applicant
    assert applicant.fetch_status == "ready"
    assert applicant.mplus_dps == 88.0
    assert updates == []


def test_region_identity_change_emits_update_for_already_pending_applicant():
    state = AppState()
    sm = StateMachine(state)
    updates: list[str] = []
    sm.applicantUpdated.connect(
        lambda applicant: updates.append(applicant.applicant_id)
    )
    applicant_row = _decoded(aid=99, member_idx=1, name="Solo-Realm")

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm", region_id=3),
            applicants=[applicant_row],
        )
    )
    updates.clear()
    assert state.applicants["99:1"].fetch_status == "pending"

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm", region_id=1),
            applicants=[applicant_row],
        )
    )

    assert updates == ["99:1"]


def test_placeholder_applicant_name_is_skipped():
    state = AppState()
    sm = StateMachine(state)
    snap = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(aid=42, member_idx=1, name="?"),
            _decoded(aid=77, member_idx=1, name="Unknown-Realm"),
            _decoded(aid=88, member_idx=1, name="UNKNOWNOBJECT"),
            _decoded(aid=99, member_idx=1, name="Solo-Realm"),
        ],
    )

    sm.apply_snapshot(snap)

    assert list(state.applicants.keys()) == ["99:1"]
    assert state.applicants["99:1"].name == "Solo-Realm"


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


@pytest.mark.parametrize(
    "fixture_stem",
    [LUA_GOLDEN_STEM, LUA_LEADER_KEY_GOLDEN_STEM],
    ids=["base", "leader-key"],
)
def test_state_machine_applies_lua_generated_aps1_v8_golden_snapshot(fixture_stem: str):
    snap, expected = _load_lua_golden_snapshot(fixture_stem)
    state = AppState()
    sm = StateMachine(state)

    sm.apply_snapshot(snap)

    assert state.player.addon_version == expected["version"]["addon_version"]
    assert state.player.game_version == expected["version"]["game_version"]
    assert state.player.region_id == expected["version"]["region_id"]
    assert state.player.full_name == expected["version"]["player_name"]
    assert state.listing is not None
    assert state.listing.activity_id == expected["listing"]["activity_id"]
    assert state.listing.dungeon_name == expected["listing"]["dungeon_name"]
    assert state.listing.listing_name == expected["listing"]["listing_name"]
    assert state.listing.comment == expected["listing"]["comment"]
    assert state.listing.key_level == expected["listing"]["key_level"]
    assert state.listing.category_id == expected["listing"]["category_id"]
    assert state.listing.difficulty_id == expected["listing"]["difficulty_id"]
    if expected.get("leader_key") is None:
        assert state.leader_key is None
    else:
        assert state.leader_key is not None
        assert state.leader_key.key_level == expected["leader_key"]["key_level"]
        assert (
            state.leader_key.challenge_map_id
            == expected["leader_key"]["challenge_map_id"]
        )
        assert state.leader_key.player_name == expected["leader_key"]["player_name"]

    assert set(state.applicants) == {
        f'{a["applicant_id"]}:{a["member_idx"]}' for a in expected["applicants"]
    }
    applicant_expected = expected["applicants"][0]
    applicant = state.applicants[
        f'{applicant_expected["applicant_id"]}:{applicant_expected["member_idx"]}'
    ]
    assert applicant.name == applicant_expected["name"]
    assert applicant.cls == "WARRIOR"
    assert applicant.role == "TANK"
    assert applicant.spec_id == applicant_expected["spec_id"]
    assert applicant.ilvl == applicant_expected["ilvl"]
    assert applicant.score == applicant_expected["score"]
    assert applicant.main_score == applicant_expected["main_score"]
    assert applicant.rio_profile == applicant_expected["rio_profile"]
    assert applicant.rio_best_key == applicant_expected["rio_best_key"]
    assert applicant.rio_best_dungeon_key == applicant_expected["rio_best_dungeon_key"]
    assert applicant.rio_timed_at_or_above == applicant_expected[
        "rio_timed_at_or_above"
    ]
    assert applicant.rio_completed_at_or_above_minus1 == applicant_expected[
        "rio_completed_at_or_above_minus1"
    ]
    assert applicant.rio_dungeon_count == applicant_expected["rio_dungeon_count"]
    expected_target_key = (
        expected["leader_key"]["key_level"]
        if expected.get("leader_key") is not None
        else expected["listing"]["key_level"]
    )
    assert applicant.rio_summary_target_key == expected_target_key

    assert set(state.party_members) == {
        m["name"].strip().lower() for m in expected["roster"]
    }
    roster_expected = expected["roster"][0]
    member = state.party_members[roster_expected["name"].lower()]
    assert member.name == roster_expected["name"]
    assert member.is_self is True
    assert member.is_raid_member == bool(roster_expected["flags"] & 0x02)
    assert member.unit_index == roster_expected["unit_index"]
    assert member.subgroup == roster_expected["subgroup"]
    assert member.cls == "WARRIOR"
    assert member.role == "TANK"
    assert member.score == roster_expected["score"]
    assert member.main_score == roster_expected["main_score"]
    assert member.rio_profile == roster_expected["rio_profile"]
    assert member.rio_best_key == roster_expected["rio_best_key"]
    assert member.rio_best_dungeon_key == roster_expected["rio_best_dungeon_key"]
    assert member.rio_summary_target_key == expected_target_key


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


def test_new_applicant_enriches_rio_raid_progress_from_local_reader():
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

    assert state.applicants["42:1"].rio_raid_progress == {
        "M": {
            "killed": 4,
            "total": 8,
            "bosses": {"Plexus Sentinel": True},
        }
    }


def test_raid_only_local_rio_profile_preserves_decoded_mplus_rows():
    state = AppState()
    sm = StateMachine(state, rio_reader=_RaidOnlyRioReader())
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

    applicant = state.applicants["42:1"]
    assert applicant.rio_dungeons == decoded_rows
    assert applicant.rio_raid_progress["M"]["killed"] == 2


def test_local_rio_profile_fills_missing_applicant_score():
    state = AppState()
    sm = StateMachine(state, rio_reader=_ScoreOnlyRioReader(2861))

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="Chinie",
                    score=0,
                    rio_profile=False,
                )
            ],
        )
    )

    applicant = state.applicants["42:1"]
    assert applicant.score == 2861
    assert applicant.rio_profile is True


def test_local_rio_profile_does_not_downgrade_higher_transport_score():
    state = AppState()
    sm = StateMachine(state, rio_reader=_ScoreOnlyRioReader(2861))

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="Chinie",
                    score=3100,
                    rio_profile=True,
                )
            ],
        )
    )

    applicant = state.applicants["42:1"]
    assert applicant.score == 3100
    assert applicant.rio_profile is True


def test_local_rio_profile_fills_missing_roster_score():
    state = AppState()
    sm = StateMachine(state, rio_reader=_ScoreOnlyRioReader(2861))

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Dmss-Ragnaros"),
            roster=[
                _roster_decoded(
                    "Chinie",
                    score=0,
                    rio_profile=False,
                )
            ],
        )
    )

    member = state.party_members["chinie"]
    assert member.score == 2861
    assert member.rio_profile is True


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


def test_local_rio_lookup_oserror_does_not_abort_snapshot():
    state = AppState()
    sm = StateMachine(state, rio_reader=_OSErrorRioReader())
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


def test_existing_applicant_preserves_local_rio_fields_when_lookup_fails():
    state = AppState()
    sm = StateMachine(state, rio_reader=_FullRioReader())
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="Chinie",
                    score=0,
                    rio_profile=False,
                    rio_dungeons=[],
                )
            ],
        )
    )
    applicant = state.applicants["42:1"]
    assert applicant.score == 2861
    assert applicant.rio_profile is True
    assert applicant.rio_dungeons == [{"name": "Pit of Saron", "key_level": 12}]
    assert applicant.rio_raid_progress == {
        "M": {
            "killed": 4,
            "total": 8,
            "bosses": {"Plexus Sentinel": True},
        }
    }

    sm._rio_reader = _OSErrorRioReader()
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="Chinie",
                    score=0,
                    rio_profile=False,
                    rio_dungeons=[],
                )
            ],
        )
    )

    assert applicant.score == 2861
    assert applicant.rio_profile is True
    assert applicant.rio_dungeons == [{"name": "Pit of Saron", "key_level": 12}]
    assert applicant.rio_raid_progress == {
        "M": {
            "killed": 4,
            "total": 8,
            "bosses": {"Plexus Sentinel": True},
        }
    }


def test_existing_roster_member_preserves_local_rio_fields_when_lookup_fails():
    state = AppState()
    sm = StateMachine(state, rio_reader=_FullRioReader())
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            roster=[_roster_decoded("Chinie", score=0, rio_profile=False)],
        )
    )
    member = state.party_members["chinie"]
    assert member.score == 2861
    assert member.rio_profile is True
    assert member.rio_dungeons == [{"name": "Pit of Saron", "key_level": 12}]
    assert member.rio_raid_progress == {
        "M": {
            "killed": 4,
            "total": 8,
            "bosses": {"Plexus Sentinel": True},
        }
    }

    sm._rio_reader = _OSErrorRioReader()
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            roster=[_roster_decoded("Chinie", score=0, rio_profile=False)],
        )
    )

    assert state.party_members["chinie"].score == 2861
    assert state.party_members["chinie"].rio_profile is True
    assert state.party_members["chinie"].rio_dungeons == [
        {"name": "Pit of Saron", "key_level": 12}
    ]
    assert state.party_members["chinie"].rio_raid_progress == {
        "M": {
            "killed": 4,
            "total": 8,
            "bosses": {"Plexus Sentinel": True},
        }
    }


def test_existing_applicant_does_not_preserve_local_rio_after_name_change_lookup_fails():
    state = AppState()
    sm = StateMachine(state, rio_reader=_FullRioReader())
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="Old-Realm",
                    score=0,
                    rio_profile=False,
                    rio_dungeons=[],
                )
            ],
        )
    )
    assert state.applicants["42:1"].rio_dungeons == [
        {"name": "Pit of Saron", "key_level": 12}
    ]

    sm._rio_reader = _OSErrorRioReader()
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="New-Realm",
                    score=0,
                    rio_profile=False,
                    rio_dungeons=[],
                )
            ],
        )
    )

    applicant = state.applicants["42:1"]
    assert applicant.name == "New-Realm"
    assert applicant.score == 0
    assert applicant.rio_profile is False
    assert applicant.rio_dungeons == []
    assert applicant.rio_raid_progress == {}


def test_existing_roster_member_does_not_preserve_local_rio_after_region_change_lookup_fails():
    state = AppState()
    sm = StateMachine(state, rio_reader=_FullRioReader())
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros", region_id=3),
            roster=[_roster_decoded("Chinie", score=0, rio_profile=False)],
        )
    )
    assert state.party_members["chinie"].rio_dungeons == [
        {"name": "Pit of Saron", "key_level": 12}
    ]

    sm._rio_reader = _OSErrorRioReader()
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros", region_id=1),
            roster=[_roster_decoded("Chinie", score=0, rio_profile=False)],
        )
    )

    member = state.party_members["chinie"]
    assert member.score == 0
    assert member.rio_profile is False
    assert member.rio_dungeons == []
    assert member.rio_raid_progress == {}


def test_local_rio_reenrich_error_preserves_existing_raid_progress():
    state = AppState()
    sm = StateMachine(state, rio_reader=_FakeRioReader())
    updated: list[str] = []
    sm.applicantUpdated.connect(lambda applicant: updated.append(applicant.applicant_id))
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[_decoded(aid=42, member_idx=1, name="Chinie")],
        )
    )
    applicant = state.applicants["42:1"]
    assert applicant.rio_raid_progress
    updated.clear()

    sm._rio_reader = _OSErrorRioReader()
    sm._reenrich_local_rio_rows()

    assert applicant.rio_raid_progress == {
        "M": {
            "killed": 4,
            "total": 8,
            "bosses": {"Plexus Sentinel": True},
        }
    }
    assert updated == []


@pytest.mark.parametrize("surface", ["applicant", "roster"])
@pytest.mark.parametrize("refresh_kind", ["lower", "missing", "raid_only"])
def test_local_rio_refresh_rebuilds_from_transport_provenance(
    surface: str,
    refresh_kind: str,
):
    initial_rows = [{"name": "Local Current", "key_level": 15}]
    initial_raid = {"M": {"killed": 4, "total": 8}}
    reader = _MutableRioReader(
        _local_rio_profile(
            score=3000,
            dungeons=initial_rows,
            raid_progress=initial_raid,
        )
    )
    state = AppState()
    sm = StateMachine(state, rio_reader=reader)
    applicant_updates: list[str] = []
    roster_updates: list[bool] = []
    sm.applicantUpdated.connect(
        lambda applicant: applicant_updates.append(applicant.applicant_id)
    )
    sm.rosterChanged.connect(lambda: roster_updates.append(True))

    if refresh_kind == "raid_only":
        raw_score = 3100
    elif refresh_kind == "lower":
        raw_score = 2700
    else:
        raw_score = 2500
    raw_profile = False
    raw_rows = [{"name": "Transport", "key_level": 12}]
    if surface == "applicant":
        snapshot = Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            applicants=[
                _decoded(
                    aid=42,
                    member_idx=1,
                    name="Chinie",
                    score=raw_score,
                    rio_profile=raw_profile,
                    rio_dungeons=raw_rows,
                )
            ],
        )
    else:
        snapshot = Snapshot(
            listing=_listing(),
            version=_version("Dmss-Ragnaros"),
            roster=[
                _roster_decoded(
                    "Chinie",
                    score=raw_score,
                    rio_profile=raw_profile,
                    rio_dungeons=raw_rows,
                )
            ],
        )
    sm.apply_snapshot(snapshot)
    applicant_updates.clear()
    roster_updates.clear()

    if refresh_kind == "lower":
        expected_rows = [{"name": "Local Lower", "key_level": 10}]
        expected_raid = {"H": {"killed": 2, "total": 8}}
        reader.profile = _local_rio_profile(
            score=2600,
            dungeons=expected_rows,
            raid_progress=expected_raid,
        )
        expected_score = raw_score
        expected_profile = True
    elif refresh_kind == "missing":
        reader.profile = None
        expected_rows = raw_rows
        expected_raid = {}
        expected_score = raw_score
        expected_profile = raw_profile
    else:
        expected_raid = {"H": {"killed": 6, "total": 8}}
        reader.profile = _local_rio_profile(
            score=0,
            dungeons=[{"name": "Ignored Local", "key_level": 20}],
            raid_progress=expected_raid,
            has_mplus_profile=False,
        )
        expected_rows = raw_rows
        expected_score = raw_score
        expected_profile = raw_profile

    sm._reenrich_local_rio_rows()

    if surface == "applicant":
        row = state.applicants["42:1"]
        assert applicant_updates == ["42:1"]
        assert roster_updates == []
    else:
        row = state.party_members["chinie"]
        assert applicant_updates == []
        assert roster_updates == [True]
    assert row.score == expected_score
    assert row.rio_profile is expected_profile
    assert row.rio_dungeons == expected_rows
    assert row.rio_raid_progress == expected_raid


@pytest.mark.parametrize("surface", ["applicant", "roster"])
def test_fresh_snapshot_updates_transport_provenance_behind_local_enrichment(
    surface: str,
):
    local_rows = [{"name": "Local Current", "key_level": 15}]
    reader = _MutableRioReader(
        _local_rio_profile(
            score=3000,
            dungeons=local_rows,
            raid_progress={},
        )
    )
    state = AppState()
    sm = StateMachine(state, rio_reader=reader)

    def apply_transport(score: int, key_level: int) -> None:
        rows = [{"name": "Transport", "key_level": key_level}]
        if surface == "applicant":
            sm.apply_snapshot(
                Snapshot(
                    listing=_listing(),
                    version=_version("Dmss-Ragnaros"),
                    applicants=[
                        _decoded(
                            aid=42,
                            member_idx=1,
                            name="Chinie",
                            score=score,
                            rio_dungeons=rows,
                        )
                    ],
                )
            )
        else:
            sm.apply_snapshot(
                Snapshot(
                    listing=_listing(),
                    version=_version("Dmss-Ragnaros"),
                    roster=[
                        _roster_decoded(
                            "Chinie",
                            score=score,
                            rio_dungeons=rows,
                        )
                    ],
                )
            )

    apply_transport(2500, 12)
    apply_transport(2400, 11)
    row = (
        state.applicants["42:1"]
        if surface == "applicant"
        else state.party_members["chinie"]
    )
    assert row.score == 3000
    assert row.rio_dungeons == local_rows

    reader.profile = None
    sm._reenrich_local_rio_rows()

    assert row.score == 2400
    assert row.rio_dungeons == [{"name": "Transport", "key_level": 11}]


def test_first_snapshot_reenriches_applicant_after_async_rio_preload_completes():
    state = AppState()
    reader = _ColdRioReader(current_score=2861)
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
    assert state.applicants["42:1"].score == 2000

    reader.finish()

    assert state.applicants["42:1"].rio_dungeons == [
        {"name": "Pit of Saron", "key_level": 12}
    ]
    assert state.applicants["42:1"].score == 2861
    assert state.applicants["42:1"].rio_profile is True
    assert state.applicants["42:1"].fetch_status == "ready"
    assert state.applicants["42:1"].mplus_dps == 91.0
    assert updated == ["42:1"]


def test_first_snapshot_reenriches_party_member_after_async_rio_preload_completes():
    state = AppState()
    reader = _ColdRioReader(current_score=2861)
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
    assert state.party_members["chinie"].score == 3000
    assert roster_updates == 1

    reader.finish()

    assert state.party_members["chinie"].rio_dungeons == [
        {"name": "Pit of Saron", "key_level": 12}
    ]
    assert state.party_members["chinie"].score == 3000
    assert state.party_members["chinie"].rio_profile is True
    assert roster_updates == 2


def test_identical_rio_preloads_coalesce_until_bounded_refresh(
    monkeypatch: pytest.MonkeyPatch,
):
    state = AppState()
    reader = _ColdRioReader()
    now = [100.0]
    sm = StateMachine(
        state,
        rio_reader=reader,
        rio_preload_monotonic=lambda: now[0],
    )
    reenrichments: list[bool] = []
    monkeypatch.setattr(
        sm, "_reenrich_local_rio_rows", lambda: reenrichments.append(True)
    )
    snapshot = Snapshot(listing=None, version=_version("Dmss-Ragnaros"))

    for _ in range(20):
        sm.apply_snapshot(snapshot)

    assert reader.preload_calls == ["EU"]
    assert len(reader.callbacks) == 1
    reader.finish()
    assert reenrichments == [True]

    now[0] += RIO_PRELOAD_REFRESH_INTERVAL_SECONDS - 0.01
    sm.apply_snapshot(snapshot)
    assert reader.preload_calls == ["EU"]

    now[0] += 0.01
    sm.apply_snapshot(snapshot)
    assert reader.preload_calls == ["EU", "EU"]
    assert len(reader.callbacks) == 1


def test_rio_preload_region_and_reader_changes_start_immediately_and_ignore_stale(
    monkeypatch: pytest.MonkeyPatch,
):
    state = AppState()
    first_reader = _ControlledRioReader()
    second_reader = _ControlledRioReader()
    sm = StateMachine(state, rio_reader=first_reader)
    reenrichments: list[bool] = []
    monkeypatch.setattr(
        sm, "_reenrich_local_rio_rows", lambda: reenrichments.append(True)
    )

    sm.apply_snapshot(Snapshot(listing=None, version=_version("Dmss-Ragnaros")))
    first_reader.finish("EU")
    assert reenrichments == [True]
    reenrichments.clear()

    sm.apply_snapshot(
        Snapshot(listing=None, version=_version("Dmss-Illidan", region_id=1))
    )
    sm.apply_snapshot(Snapshot(listing=None, version=_version("Dmss-Ragnaros")))

    assert first_reader.preload_calls == ["EU", "US", "EU"]
    first_reader.finish("US")
    assert reenrichments == []

    sm.set_rio_reader(second_reader)
    sm.apply_snapshot(Snapshot(listing=None, version=_version("Dmss-Ragnaros")))
    assert second_reader.preload_calls == ["EU"]

    first_reader.finish("EU")
    assert reenrichments == []
    second_reader.finish("EU")
    assert reenrichments == [True]


def test_rio_preload_failure_can_retry_and_synchronous_completion_clears_active(
    monkeypatch: pytest.MonkeyPatch,
):
    class RetryReader:
        def __init__(self) -> None:
            self.calls = 0

        def lookup_profile(self, *_args: object, **_kwargs: object) -> None:
            return None

        def preload_region_async(self, region: str | None, on_loaded=None) -> None:
            assert region == "EU"
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("load failed")
            assert on_loaded is not None
            on_loaded()

    state = AppState()
    reader = RetryReader()
    sm = StateMachine(state, rio_reader=reader)
    reenrichments: list[bool] = []
    monkeypatch.setattr(
        sm, "_reenrich_local_rio_rows", lambda: reenrichments.append(True)
    )
    snapshot = Snapshot(listing=None, version=_version("Dmss-Ragnaros"))

    with pytest.raises(RuntimeError, match="load failed"):
        sm.apply_snapshot(snapshot)
    sm.apply_snapshot(snapshot)

    assert reader.calls == 2
    assert reenrichments == [True]


def test_region_signal_precedes_synchronous_rio_preload_row_updates():
    class SynchronousReader:
        def lookup_profile(self, *_args: object, **_kwargs: object) -> None:
            return None

        def preload_region_async(self, region: str | None, on_loaded=None) -> None:
            assert region == "US"
            assert on_loaded is not None
            on_loaded()

    state = AppState()
    state.player = WoWPlayer(region_id=3, full_name="Host-RealmA")
    sm = StateMachine(state, rio_reader=_FullRioReader())
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-RealmA", region_id=3),
            applicants=[_decoded(42, 1, "Scout-RealmX")],
            roster=[_roster_decoded("Friend-RealmX")],
        )
    )
    events: list[str] = []
    sm.versionUpdated.connect(lambda _region_id: events.append("version"))
    sm.applicantUpdated.connect(lambda _applicant: events.append("applicant"))
    sm.rosterChanged.connect(lambda: events.append("roster"))
    sm.set_rio_reader(SynchronousReader())

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Host-RealmA", region_id=1),
            applicants=[],
            roster=[],
            lfg_unavailable=True,
            roster_unavailable=True,
        )
    )

    assert events[0] == "version"
    assert "applicant" in events
    assert "roster" in events


def test_legacy_rio_preload_cooldown_starts_after_slow_fallback_returns():
    now = [100.0]

    class SlowLegacyReader:
        def __init__(self) -> None:
            self.calls = 0

        def lookup_profile(self, *_args: object, **_kwargs: object) -> None:
            return None

        def preload_region_async(self, region: str | None) -> None:
            assert region == "EU"
            self.calls += 1
            now[0] += RIO_PRELOAD_REFRESH_INTERVAL_SECONDS + 1.0

    state = AppState()
    reader = SlowLegacyReader()
    sm = StateMachine(
        state,
        rio_reader=reader,
        rio_preload_monotonic=lambda: now[0],
    )
    snapshot = Snapshot(listing=None, version=_version("Dmss-Ragnaros"))

    sm.apply_snapshot(snapshot)
    sm.apply_snapshot(snapshot)
    assert reader.calls == 1

    now[0] += RIO_PRELOAD_REFRESH_INTERVAL_SECONDS
    sm.apply_snapshot(snapshot)
    assert reader.calls == 2


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


def test_clear_wcl_data_removes_raid_boss_parses_but_preserves_rio_progress():
    state = AppState()
    sm = StateMachine(state)
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=None,
            applicants=[_decoded(aid=42, member_idx=1, name="Scout-Realm", spec_id=71)],
        )
    )
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "ready"
    applicant.raid_boss_parses = {
        "M": [
            {
                "name": "Plexus Sentinel",
                "encounter_id": 3001,
                "overall": 46.0,
                "ilvl": 68.0,
            }
        ]
    }
    applicant.rio_raid_progress = {
        "M": {
            "killed": 1,
            "total": 8,
            "bosses": {"Plexus Sentinel": True},
        }
    }

    applicant.clear_wcl_data()

    assert applicant.raid_boss_parses == {}
    assert applicant.rio_raid_progress == {
        "M": {
            "killed": 1,
            "total": 8,
            "bosses": {"Plexus Sentinel": True},
        }
    }


def test_project_wcl_data_to_preferences_removes_disabled_raid_boss_difficulties():
    state = AppState()
    sm = StateMachine(state)
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=None,
            applicants=[_decoded(aid=42, member_idx=1, name="Scout-Realm", spec_id=71)],
        )
    )
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "ready"
    applicant.wcl_metric_preferences = MetricPreferences(
        mplus=True,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=True,
    )
    applicant.raid_boss_parses = {
        "N": [{"name": "Boss N", "overall": 70.0}],
        "H": [{"name": "Boss H", "overall": 80.0}],
        "M": [{"name": "Boss M", "overall": 90.0}],
    }

    applicant.project_wcl_data_to_preferences(
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        )
    )

    assert applicant.raid_boss_parses == {"H": [{"name": "Boss H", "overall": 80.0}]}


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


def test_transient_zero_spec_snapshot_preserves_existing_applicant_identity():
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
                spec_id=65,
                ilvl=268,
                role=1,
            ),
        ],
    )
    sm.apply_snapshot(snap1)
    state.applicants["99:1"].fetch_status = "ready"
    state.applicants["99:1"].mplus_hps = 72.0

    snap2 = Snapshot(
        listing=_listing(),
        version=None,
        applicants=[
            _decoded(
                aid=99,
                member_idx=1,
                name="Same-Realm",
                spec_id=0,
                ilvl=0,
                role=1,
            ),
        ],
    )
    sm.apply_snapshot(snap2)

    a = state.applicants["99:1"]
    assert a.spec_id == 65
    assert a.ilvl == 268
    assert a.fetch_status == "ready"
    assert a.mplus_hps == 72.0


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


def test_lfg_unavailable_snapshot_preserves_listing_applicants_and_applies_roster():
    state = AppState()
    sm = StateMachine(state)
    cleared: list[bool] = []
    removed: list[str] = []
    roster_updates: list[bool] = []
    listing_updates: list[bool] = []
    sm.cleared.connect(lambda: cleared.append(True))
    sm.applicantRemoved.connect(removed.append)
    sm.rosterChanged.connect(lambda: roster_updates.append(True))
    sm.listingChanged.connect(lambda: listing_updates.append(True))

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(key_level=14),
            version=_version("Host-RealmA"),
            applicants=[_decoded(aid=42, member_idx=1, name="Scout-RealmA")],
        )
    )
    state.applicants["42:1"].fetch_status = "ready"
    state.applicants["42:1"].mplus_dps = 88.0
    listing_updates.clear()

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Host-RealmA"),
            applicants=[],
            roster=[_roster_decoded("Host-RealmA", flags=1)],
            lfg_unavailable=True,
        )
    )

    assert state.listing is not None
    assert state.listing.key_level == 14
    assert set(state.applicants) == {"42:1"}
    assert state.applicants["42:1"].fetch_status == "ready"
    assert state.applicants["42:1"].mplus_dps == 88.0
    assert set(state.party_members) == {"host-realma"}
    assert cleared == []
    assert removed == []
    assert listing_updates == []
    assert roster_updates == [True]


def test_combined_unavailable_snapshot_preserves_applicants_and_roster():
    state = AppState()
    sm = StateMachine(state)
    cleared: list[bool] = []
    removed: list[str] = []
    roster_updates: list[bool] = []
    sm.cleared.connect(lambda: cleared.append(True))
    sm.applicantRemoved.connect(removed.append)
    sm.rosterChanged.connect(lambda: roster_updates.append(True))
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            applicants=[_decoded(7, 1, "Applicant-Realm")],
            roster=[
                _roster_decoded("Host-Realm", flags=1),
                _roster_decoded("Friend-Realm", unit_index=2),
            ],
        )
    )
    roster_updates.clear()

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Host-Realm"),
            applicants=[],
            roster=[],
            lfg_unavailable=True,
            roster_unavailable=True,
        )
    )

    assert state.listing is not None
    assert set(state.applicants) == {"7:1"}
    assert set(state.party_members) == {"host-realm", "friend-realm"}
    assert cleared == []
    assert removed == []
    assert roster_updates == []


@pytest.mark.parametrize(
    ("change_kind", "lfg_unavailable", "explicit_realm", "should_refresh"),
    [
        ("region", False, True, True),
        ("unknown-region", True, True, False),
        ("default-realm", True, False, True),
        ("default-realm", True, True, False),
    ],
)
def test_roster_unavailable_identity_change_revalidates_preserved_enrichment(
    change_kind: str,
    lfg_unavailable: bool,
    explicit_realm: bool,
    should_refresh: bool,
):
    state = AppState()
    state.player = WoWPlayer(region_id=3, full_name="Host-RealmA")
    sm = StateMachine(state, rio_reader=_FullRioReader())
    applicant_name = "Scout-RealmX" if explicit_realm else "Scout"
    member_name = "Friend-RealmX" if explicit_realm else "Friend"
    transport_rows = [{"name": "Transport", "key_level": 9}]
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-RealmA", region_id=3),
            applicants=[
                _decoded(
                    42,
                    1,
                    applicant_name,
                    score=2100,
                    rio_profile=False,
                    rio_dungeons=transport_rows,
                )
            ],
            roster=[
                _roster_decoded(
                    member_name,
                    score=2200,
                    rio_profile=False,
                    rio_dungeons=transport_rows,
                )
            ],
        )
    )
    applicant = state.applicants["42:1"]
    member_id = member_name.lower()
    member = state.party_members[member_id]
    for row in (applicant, member):
        row.fetch_status = "ready"
        row.raid_heroic = 92.0
        assert row.rio_profile is True
        assert row.rio_dungeons == [{"name": "Pit of Saron", "key_level": 12}]
        assert row.rio_raid_progress

    applicant_updates: list[str] = []
    roster_updates: list[bool] = []
    signal_order: list[str] = []
    sm.applicantUpdated.connect(
        lambda updated: applicant_updates.append(updated.applicant_id)
    )
    sm.rosterChanged.connect(lambda: roster_updates.append(True))
    sm.versionUpdated.connect(lambda _region_id: signal_order.append("version"))
    sm.applicantUpdated.connect(lambda _updated: signal_order.append("applicant"))
    sm.rosterChanged.connect(lambda: signal_order.append("roster"))
    sm._rio_reader = _OSErrorRioReader()
    if change_kind == "region":
        new_version = _version("Host-RealmA", region_id=1)
    elif change_kind == "unknown-region":
        new_version = _version("Host-RealmA", region_id=99)
    else:
        new_version = _version("Host-RealmB", region_id=3)
    sm.apply_snapshot(
        Snapshot(
            listing=None if lfg_unavailable else _listing(),
            version=new_version,
            applicants=(
                []
                if lfg_unavailable
                else [
                    _decoded(
                        42,
                        1,
                        applicant_name,
                        score=2100,
                        rio_profile=False,
                        rio_dungeons=transport_rows,
                    )
                ]
            ),
            roster=[],
            lfg_unavailable=lfg_unavailable,
            roster_unavailable=True,
        )
    )

    assert set(state.applicants) == {"42:1"}
    assert set(state.party_members) == {member_id}
    assert state.applicants["42:1"] is applicant
    assert state.party_members[member_id] is member
    if change_kind == "region":
        assert signal_order[0] == "version"
    if should_refresh:
        assert applicant.fetch_status == "pending"
        assert applicant.raid_heroic is None
        assert applicant.score == 2100
        assert applicant.rio_profile is False
        assert applicant.rio_dungeons == transport_rows
        assert applicant.rio_raid_progress == {}
        assert member.fetch_status == "pending"
        assert member.raid_heroic is None
        assert member.score == 2200
        assert member.rio_profile is False
        assert member.rio_dungeons == transport_rows
        assert member.rio_raid_progress == {}
        assert applicant_updates == ["42:1"]
        assert roster_updates == [True]
    else:
        assert applicant.fetch_status == "ready"
        assert applicant.raid_heroic == 92.0
        assert applicant.score == 2861
        assert applicant.rio_profile is True
        assert applicant.rio_dungeons == [
            {"name": "Pit of Saron", "key_level": 12}
        ]
        assert applicant.rio_raid_progress
        assert member.fetch_status == "ready"
        assert member.raid_heroic == 92.0
        assert member.score == 2861
        assert member.rio_profile is True
        assert member.rio_dungeons == [
            {"name": "Pit of Saron", "key_level": 12}
        ]
        assert member.rio_raid_progress
        assert applicant_updates == []
        assert roster_updates == []


def test_lfg_unavailable_region_change_invalidates_preserved_applicant_wcl():
    state = AppState()
    state.player = WoWPlayer(region_id=3, full_name="Host-RealmA")
    sm = StateMachine(state)
    updated: list[str] = []
    sm.applicantUpdated.connect(
        lambda applicant: updated.append(applicant.applicant_id)
    )

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(key_level=14),
            version=_version("Host-RealmA", region_id=3),
            applicants=[
                _decoded(aid=42, member_idx=1, name="Scout-RealmX", spec_id=71),
            ],
        )
    )
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "ready"
    applicant.raid_heroic = 92.0
    applicant.mplus_dps = 88.0
    updated.clear()

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Host-RealmA", region_id=1),
            applicants=[],
            roster=[_roster_decoded("Host-RealmA", flags=1)],
            lfg_unavailable=True,
        )
    )

    applicant = state.applicants["42:1"]
    assert applicant.fetch_status == "pending"
    assert applicant.raid_heroic is None
    assert applicant.mplus_dps is None
    assert updated == ["42:1"]


def test_lfg_unavailable_default_realm_change_invalidates_same_realm_applicant_wcl():
    state = AppState()
    state.player = WoWPlayer(region_id=3, full_name="Host-RealmA")
    sm = StateMachine(state)
    updated: list[str] = []
    sm.applicantUpdated.connect(
        lambda applicant: updated.append(applicant.applicant_id)
    )

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(key_level=14),
            version=_version("Host-RealmA", region_id=3),
            applicants=[_decoded(aid=42, member_idx=1, name="Scout", spec_id=71)],
        )
    )
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "ready"
    applicant.raid_heroic = 92.0
    applicant.mplus_dps_breakdown = [
        {
            "name": "Pit of Saron",
            "parse_percent": 88.0,
            "median_percent": 80.0,
            "key_level": 14,
            "run_count": 4,
        }
    ]
    updated.clear()

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Host-RealmB", region_id=3),
            applicants=[],
            roster=[_roster_decoded("Host-RealmB", flags=1)],
            lfg_unavailable=True,
        )
    )

    applicant = state.applicants["42:1"]
    assert applicant.fetch_status == "pending"
    assert applicant.raid_heroic is None
    assert applicant.mplus_dps_breakdown == []
    assert updated == ["42:1"]


def test_lfg_unavailable_default_realm_change_preserves_explicit_realm_wcl():
    state = AppState()
    state.player = WoWPlayer(region_id=3, full_name="Host-RealmA")
    sm = StateMachine(state)
    updated: list[str] = []
    sm.applicantUpdated.connect(
        lambda applicant: updated.append(applicant.applicant_id)
    )

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(key_level=14),
            version=_version("Host-RealmA", region_id=3),
            applicants=[
                _decoded(aid=42, member_idx=1, name="Scout-RealmX", spec_id=71),
            ],
        )
    )
    applicant = state.applicants["42:1"]
    applicant.fetch_status = "ready"
    applicant.raid_heroic = 92.0
    updated.clear()

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Host-RealmB", region_id=3),
            applicants=[],
            roster=[_roster_decoded("Host-RealmB", flags=1)],
            lfg_unavailable=True,
        )
    )

    applicant = state.applicants["42:1"]
    assert applicant.fetch_status == "ready"
    assert applicant.raid_heroic == 92.0
    assert updated == []


def test_explicit_terminal_clear_snapshot_clears_listing_applicants_and_roster():
    state = AppState()
    sm = StateMachine(state)
    cleared: list[bool] = []
    removed: list[str] = []
    roster_updates: list[bool] = []
    listing_updates: list[bool] = []
    sm.cleared.connect(lambda: cleared.append(True))
    sm.applicantRemoved.connect(removed.append)
    sm.rosterChanged.connect(lambda: roster_updates.append(True))
    sm.listingChanged.connect(lambda: listing_updates.append(True))

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(key_level=14),
            version=_version("Host-RealmA"),
            applicants=[_decoded(aid=42, member_idx=1, name="Scout-RealmA")],
            roster=[_roster_decoded("Host-RealmA", flags=1)],
        )
    )
    listing_updates.clear()
    roster_updates.clear()

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(key_level=17),
            version=_version("Host-RealmA"),
            applicants=[_decoded(aid=99, member_idx=1, name="Late-RealmA")],
            roster=[_roster_decoded("Late-RealmA", flags=0)],
            terminal_clear=True,
        )
    )

    assert state.listing is None
    assert state.applicants == {}
    assert state.party_members == {}
    assert cleared == [True]
    assert removed == []
    assert listing_updates == [True]
    assert roster_updates == [True]


def test_explicit_terminal_clear_snapshot_clears_leader_key():
    state = AppState()
    sm = StateMachine(state)
    listing_updates: list[bool] = []
    sm.listingChanged.connect(lambda: listing_updates.append(True))

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Host-RealmA"),
            leader_key=DecodedLeaderKey(
                key_level=17,
                challenge_map_id=503,
                player_name="Host-RealmA",
            ),
            roster=[_roster_decoded("Host-RealmA", flags=1)],
        )
    )
    assert state.leader_key is not None
    listing_updates.clear()

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Host-RealmA"),
            leader_key=DecodedLeaderKey(
                key_level=17,
                challenge_map_id=503,
                player_name="Host-RealmA",
            ),
            applicants=[],
            roster=[],
            terminal_clear=True,
        )
    )

    assert state.leader_key is None
    assert state.listing is None
    assert state.party_members == {}
    assert listing_updates == [True]


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


def test_state_machine_tracks_leader_key_without_listing():
    state = AppState()
    sm = StateMachine(state)

    sm.apply_snapshot(
        Snapshot(
            listing=None,
            version=_version("Player-Realm"),
            leader_key=DecodedLeaderKey(
                key_level=17,
                challenge_map_id=503,
                player_name="Leader-Realm",
            ),
            roster=[_roster_decoded("Leader-Realm", unit_index=2)],
        )
    )

    assert state.listing is None
    assert state.leader_key is not None
    assert state.leader_key.key_level == 17
    assert state.leader_key.challenge_map_id == 503
    assert state.leader_key.player_name == "Leader-Realm"


def test_state_machine_uses_leader_key_for_compact_rio_target_key():
    state = AppState()
    sm = StateMachine(state)

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(key_level=15),
            version=_version("Player-Realm"),
            leader_key=DecodedLeaderKey(
                key_level=17,
                challenge_map_id=503,
                player_name="Leader-Realm",
            ),
            applicants=[_decoded(7, 1, "Applicant-Realm")],
            roster=[_roster_decoded("Leader-Realm", unit_index=2)],
        )
    )

    assert state.listing is not None
    assert state.listing.key_level == 15
    assert state.leader_key is not None
    assert state.applicants["7:1"].rio_summary_target_key == 17
    assert state.party_members["leader-realm"].rio_summary_target_key == 17


def test_roster_snapshot_maps_current_and_main_scores_separately():
    state = AppState()
    sm = StateMachine(state)

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            roster=[
                _roster_decoded(
                    "Alt-Realm",
                    score=2443,
                    main_score=3468,
                    rio_profile=True,
                ),
            ],
        )
    )

    member = state.party_members["alt-realm"]
    assert member.score == 2443
    assert member.main_score == 3468
    assert member.rio_profile is True


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


def test_transient_zero_spec_snapshot_preserves_existing_roster_identity():
    state = AppState()
    sm = StateMachine(state)

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            roster=[
                _roster_decoded(
                    "Friend-Realm",
                    unit_index=2,
                    spec_id=65,
                    ilvl=268,
                    role=1,
                ),
            ],
        )
    )
    member = state.party_members["friend-realm"]
    member.fetch_status = "ready"
    member.mplus_hps = 72.0

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            roster=[
                _roster_decoded(
                    "Friend-Realm",
                    unit_index=2,
                    spec_id=0,
                    ilvl=0,
                    role=1,
                ),
            ],
        )
    )

    member = state.party_members["friend-realm"]
    assert member.spec_id == 65
    assert member.ilvl == 268
    assert member.fetch_status == "ready"
    assert member.mplus_hps == 72.0


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


def test_roster_unavailable_snapshot_updates_applicants_without_clearing_party():
    state = AppState()
    sm = StateMachine(state)
    removed: list[str] = []
    added: list[str] = []
    roster_updates: list[bool] = []
    sm.applicantRemoved.connect(removed.append)
    sm.applicantAdded.connect(lambda applicant: added.append(applicant.applicant_id))
    sm.rosterChanged.connect(lambda: roster_updates.append(True))

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            applicants=[_decoded(7, 1, "Stale-Realm")],
            roster=[
                _roster_decoded("Host-Realm", unit_index=0, flags=1, score=3000),
                _roster_decoded("Friend-Realm", unit_index=1, score=2500),
            ],
        )
    )
    roster_updates.clear()
    removed.clear()
    added.clear()

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            applicants=[_decoded(8, 1, "Fresh-Realm")],
            roster=[],
            roster_unavailable=True,
        )
    )

    assert set(state.applicants) == {"8:1"}
    assert state.applicants["8:1"].name == "Fresh-Realm"
    assert set(state.party_members) == {"host-realm", "friend-realm"}
    assert state.party_members["friend-realm"].score == 2500
    assert removed == ["7:1"]
    assert added == ["8:1"]
    assert roster_updates == []


def test_restored_applicant_expiry_preserves_fresh_roster_domain():
    state = AppState()
    sm = StateMachine(state)
    listing_updates: list[bool] = []
    clears: list[bool] = []
    roster_updates: list[bool] = []
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            leader_key=DecodedLeaderKey(
                key_level=17,
                challenge_map_id=503,
                player_name="Host-Realm",
            ),
            applicants=[_decoded(7, 1, "Applicant-Realm")],
            roster=[_roster_decoded("Host-Realm", flags=1)],
        )
    )
    sm.listingChanged.connect(lambda: listing_updates.append(True))
    sm.cleared.connect(lambda: clears.append(True))
    sm.rosterChanged.connect(lambda: roster_updates.append(True))

    sm.expire_restored_snapshot_surfaces(applicants=True, roster=False)

    assert state.listing is None
    assert state.leader_key is None
    assert state.applicants == {}
    assert set(state.party_members) == {"host-realm"}
    assert listing_updates == [True]
    assert clears == [True]
    assert roster_updates == []


def test_restored_roster_expiry_preserves_fresh_applicant_domain():
    state = AppState()
    sm = StateMachine(state)
    listing_updates: list[bool] = []
    clears: list[bool] = []
    roster_updates: list[bool] = []
    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version("Host-Realm"),
            leader_key=DecodedLeaderKey(
                key_level=17,
                challenge_map_id=503,
                player_name="Host-Realm",
            ),
            applicants=[_decoded(7, 1, "Applicant-Realm")],
            roster=[_roster_decoded("Host-Realm", flags=1)],
        )
    )
    sm.listingChanged.connect(lambda: listing_updates.append(True))
    sm.cleared.connect(lambda: clears.append(True))
    sm.rosterChanged.connect(lambda: roster_updates.append(True))

    sm.expire_restored_snapshot_surfaces(applicants=False, roster=True)

    assert state.listing is not None
    assert state.leader_key is not None
    assert set(state.applicants) == {"7:1"}
    assert state.party_members == {}
    assert listing_updates == []
    assert clears == []
    assert roster_updates == [True]
