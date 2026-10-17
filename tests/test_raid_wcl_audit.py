"""Seasonal lairs survive WCL parsing and persistent difficulty projection."""

import pytest

from applicant_scout.constants import CURRENT_RAID_ENCOUNTERS
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.wcl import (
    CharacterCache,
    _raid_boss_rows_from_character,
    _ranks_from_graphql,
)
from applicant_scout.wcl_availability import RaidBossDetails


@pytest.mark.parametrize("encounter_id", [3379, 3513])
@pytest.mark.parametrize("difficulty,alias,scope,prefix", [
    ("N", "raidNormal", "raid_normal", "raid_n"),
    ("H", "raidHeroic", "raid_heroic", "raid_h"),
    ("M", "raidMythic", "raid_mythic", "raid_m"),
])
def test_lair_only_wcl_evidence_survives_cache_reload_and_projection(
    tmp_path, encounter_id, difficulty, alias, scope, prefix,
):
    broad = MetricPreferences(mplus=False)
    narrow = MetricPreferences(
        mplus=False, raid_normal=difficulty == "N",
        raid_heroic=difficulty == "H", raid_mythic=difficulty == "M",
    )
    encounter_alias, _id, name = next(
        encounter for encounter in CURRENT_RAID_ENCOUNTERS if encounter[1] == encounter_id
    )
    character = {key: {"rankings": []} for key in ("raidNormal", "raidHeroic", "raidMythic")}
    character[alias] = {
        "bestPerformanceAverage": 81, "medianPerformanceAverage": 70,
        "rankings": [{
            "encounter": {"id": encounter_id}, "spec": "Arcane",
            "rankPercent": 81, "totalKills": 5,
        }],
    }
    ranks = _ranks_from_graphql(
        {"data": {"characterData": {"character": character}}},
        metric_preferences=broad, spec_name="Arcane",
    )
    details = _raid_boss_rows_from_character({
        f"{prefix}_{encounter_alias}_overall": {
            "ranks": [{"spec": "Arcane", "rankPercent": 81}],
        },
        f"{prefix}_{encounter_alias}_ilvl": {
            "ranks": [{"spec": "Arcane", "rankPercent": 90}],
        },
    }, difficulty, spec_name="Arcane")
    with_cache = CharacterCache(tmp_path)
    try:
        with_cache.put("Scout", "ravencrest", "EU", 62, ranks, metric_preferences=broad)
        with_cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 62,
            RaidBossDetails({difficulty: details}, availability={difficulty: "available"}),
            metric_preferences=narrow,
        )
    finally:
        with_cache.close()

    loaded = CharacterCache(tmp_path)
    try:
        cached_ranks = loaded.get("Scout", "ravencrest", "EU", 62, metric_preferences=narrow)
        assert cached_ranks is not None
        assert getattr(cached_ranks, scope) == 81
        assert cached_ranks.raid_evidence == {difficulty: {
            "boss_count": 1, "total_bosses": len(CURRENT_RAID_ENCOUNTERS),
            "sample_known": True, "kill_count": 5,
        }}
        cached_details = loaded.get_raid_boss_details(
            "Scout", "ravencrest", "EU", 62, metric_preferences=narrow,
        )
        assert cached_details == {difficulty: [{
            "encounter_id": encounter_id, "name": name, "overall": 81, "ilvl": 90,
        }]}
    finally:
        loaded.close()
