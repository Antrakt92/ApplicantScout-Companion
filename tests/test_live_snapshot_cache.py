from __future__ import annotations

import json

from applicant_scout.live_snapshot_cache import (
    LIVE_SNAPSHOT_CACHE_FILENAME,
    LIVE_SNAPSHOT_CACHE_TTL_SECONDS,
    load_live_snapshot,
    save_live_snapshot,
)
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
    SnapshotSource,
)


def _live_snapshot() -> Snapshot:
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


def test_save_and_load_live_snapshot_round_trips_without_source(tmp_path):
    snap = _live_snapshot()

    save_live_snapshot(tmp_path, snap, now=100.0)
    restored = load_live_snapshot(tmp_path, now=120.0)

    assert restored is not None
    assert restored.saved_at == 100.0
    assert restored.snapshot.source is None
    assert restored.snapshot.listing == snap.listing
    assert restored.snapshot.version == snap.version
    assert restored.snapshot.applicants == snap.applicants
    assert restored.snapshot.roster == snap.roster
    assert not restored.snapshot.terminal_clear
    assert not restored.snapshot.lfg_unavailable


def test_load_live_snapshot_rejects_and_removes_expired_snapshot(tmp_path):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)

    restored = load_live_snapshot(
        tmp_path,
        now=100.0 + LIVE_SNAPSHOT_CACHE_TTL_SECONDS + 0.1,
    )

    assert restored is None
    assert not (tmp_path / LIVE_SNAPSHOT_CACHE_FILENAME).exists()


def test_save_live_snapshot_ignores_partial_snapshot_without_clearing_previous(tmp_path):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    partial = Snapshot(
        listing=None,
        version=DecodedVersion("0.4.3", "12.0.5", 3, "Host-Realm"),
        lfg_unavailable=True,
    )

    save_live_snapshot(tmp_path, partial, now=110.0)
    restored = load_live_snapshot(tmp_path, now=120.0)

    assert restored is not None
    assert restored.saved_at == 100.0


def test_save_live_snapshot_clears_on_terminal_clear_or_no_listing(tmp_path):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    save_live_snapshot(
        tmp_path,
        Snapshot(listing=None, version=None, terminal_clear=True),
        now=110.0,
    )

    assert load_live_snapshot(tmp_path, now=111.0) is None

    save_live_snapshot(tmp_path, _live_snapshot(), now=120.0)
    save_live_snapshot(tmp_path, Snapshot(listing=None, version=None), now=121.0)

    assert load_live_snapshot(tmp_path, now=122.0) is None


def test_load_live_snapshot_rejects_malformed_or_non_live_payload(tmp_path):
    path = tmp_path / LIVE_SNAPSHOT_CACHE_FILENAME
    path.write_text("{not json", encoding="utf-8")

    assert load_live_snapshot(tmp_path, now=100.0) is None
    assert not path.exists()

    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "saved_at": 100.0,
                "snapshot": {
                    "listing": None,
                    "version": None,
                    "leader_key": None,
                    "applicants": [],
                    "roster": [],
                    "terminal_clear": False,
                    "lfg_unavailable": False,
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_live_snapshot(tmp_path, now=101.0) is None
    assert not path.exists()
