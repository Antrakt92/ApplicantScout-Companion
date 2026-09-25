"""Canonical live-snapshot builders shared by snapshot/cache tests.

Kept in a uniquely named support module (not ``conftest``) because pytest
also loads the paired addon checkout's ``tests/conftest.py`` under the bare
``conftest`` module name whenever a directory-valued option (e.g.
``--native-addon-root``/``--companion-root``) points at that checkout, which
would shadow a ``from conftest import ...`` helper.
"""

from __future__ import annotations

from pathlib import Path

from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
    SnapshotSource,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
LUA_GOLDEN_STEM = "aps1_v9_lua_golden"
LUA_LEADER_KEY_GOLDEN_STEM = "aps1_v9_lua_leader_key_golden"


def _live_snapshot() -> Snapshot:
    """Canonical live snapshot: full RIO evidence, one roster row, a source."""
    return Snapshot(
        listing=DecodedListing(
            activity_id=401,
            key_level=14,
            dungeon_name="Theater of Pain",
            listing_name="+14 weekly",
            comment="chill",
            category_id=2,
            difficulty_id=8,
        ),
        version=DecodedVersion(
            addon_version="0.4.3",
            game_version="12.0.5",
            region_id=3,
            player_name="Host-Realm",
        ),
        applicants=[
            DecodedApplicant(
                applicant_id=42,
                member_idx=1,
                class_id=10,
                spec_id=270,
                ilvl=685,
                score=3100,
                role=1,
                name="Healer-Realm",
                main_score=3200,
                rio_profile=True,
                rio_best_key=14,
                rio_best_dungeon_key=13,
                rio_timed_at_or_above=2,
                rio_timed_at_or_above_minus1=4,
                rio_timed_at_or_above_minus2=6,
                rio_completed_at_or_above_minus1=5,
                rio_dungeon_count=8,
                rio_dungeons=[{"name": "Theater of Pain", "key_level": 14}],
            )
        ],
        roster=[
            DecodedRosterMember(
                unit_index=1,
                flags=1,
                subgroup=1,
                class_id=1,
                spec_id=73,
                ilvl=690,
                score=3000,
                main_score=3000,
                role=0,
                name="Tank-Realm",
            )
        ],
        source=SnapshotSource(mtime_ns=123, file_id="WoWScrnShot.jpg", size=456),
    )


def _live_roster_member(name: str, *, unit_index: int = 1) -> DecodedRosterMember:
    return DecodedRosterMember(
        unit_index=unit_index,
        flags=1 if unit_index == 1 else 0,
        subgroup=1,
        class_id=10,
        spec_id=270,
        ilvl=685,
        score=3100,
        main_score=0,
        role=1,
        name=name,
    )
