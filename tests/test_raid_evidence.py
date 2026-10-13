"""Raid summary coverage must come from observed, matching-spec boss rows."""

from dataclasses import asdict, replace
import json

import pytest

from applicant_scout.constants import CURRENT_RAID_ENCOUNTERS
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import Applicant
from applicant_scout.wcl import (
    CharacterCache,
    CharacterRanks,
    DungeonPerf,
    _compute_mplus_headline,
    _ranks_from_graphql,
)


HEROIC_ONLY = MetricPreferences(
    mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=False,
)
BOSS_IDS = [encounter_id for _alias, encounter_id, _name in CURRENT_RAID_ENCOUNTERS]


def _summary(zone, *, spec_name="Arms", prefs=HEROIC_ONLY):
    character = {"raidHeroic": zone}
    if prefs.raid_normal:
        character["raidNormal"] = zone
    if prefs.raid_mythic:
        character["raidMythic"] = zone
    return _ranks_from_graphql(
        {"data": {"characterData": {"character": character}}},
        metric_preferences=prefs,
        spec_name=spec_name,
    )


def _row(encounter_id, *, spec="Arms", percentile=80, **extra):
    return {
        "encounter": {"id": encounter_id}, "spec": spec,
        "rankPercent": percentile, **extra,
    }


def test_raid_summary_average_does_not_invent_boss_or_kill_counts():
    ranks = _summary({"bestPerformanceAverage": 95, "medianPerformanceAverage": 95})
    assert ranks.raid_heroic == 95
    assert ranks.raid_evidence == {}


def test_raid_coverage_counts_distinct_current_applying_spec_bosses():
    ranks = _summary({"rankings": [
        _row(BOSS_IDS[0], totalKills=3),
        _row(BOSS_IDS[0], totalKills=2),
        _row(BOSS_IDS[1], totalKills=4),
        _row(BOSS_IDS[2], spec="Fury", totalKills=20),
        _row(999999, totalKills=20),
        _row(BOSS_IDS[3], percentile=None),
    ]})
    assert ranks.raid_evidence == {"H": {
        "boss_count": 2, "total_bosses": len(BOSS_IDS),
        "kill_count": 7, "sample_known": True,
    }}


def test_raid_coverage_can_be_known_while_repeat_sample_is_unknown():
    ranks = _summary({"rankings": [
        _row(BOSS_IDS[0], totalKills=4), _row(BOSS_IDS[1]),
    ]})
    assert ranks.raid_evidence["H"] == {
        "boss_count": 2, "total_bosses": len(BOSS_IDS), "sample_known": False,
    }


@pytest.mark.parametrize("bad", [
    None, True, 0, -1, 3.5, "bad", "1e3",
    pytest.param(10**400, id="oversized-integer"),
])
def test_malformed_raid_kill_counts_do_not_become_known_samples(bad):
    ranks = _summary({"rankings": [_row(BOSS_IDS[0], totalKills=bad)]})
    assert ranks.raid_evidence["H"] == {
        "boss_count": 1, "total_bosses": len(BOSS_IDS), "sample_known": False,
    }


def test_raid_evidence_accepts_zero_percentile_and_explicit_kills_without_parse():
    ranks = _summary({"rankings": [
        _row(BOSS_IDS[0], percentile=0, totalKills=2),
        _row(BOSS_IDS[1], percentile=None, totalKills="3"),
    ]})
    assert ranks.raid_evidence["H"]["boss_count"] == 2
    assert ranks.raid_evidence["H"]["kill_count"] == 5


def test_raid_evidence_missing_row_spec_relies_on_explicit_query_spec_filter():
    row = _row(BOSS_IDS[0], totalKills=2)
    del row["spec"]
    ranks = _summary({"rankings": [row]})
    assert ranks.raid_evidence["H"]["kill_count"] == 2
    assert _summary({"rankings": [row]}, spec_name="").raid_evidence == {}


def test_empty_rankings_prove_zero_logged_coverage_not_zero_actual_kills():
    assert _summary({"rankings": []}).raid_evidence == {"H": {
        "boss_count": 0, "total_bosses": len(BOSS_IDS), "sample_known": False,
    }}


@pytest.mark.parametrize("spec", [None, "Fury", "", 71, True])
def test_explicit_contradictory_or_invalid_row_spec_never_supplies_evidence(spec):
    assert _summary({"rankings": [_row(BOSS_IDS[0], spec=spec, totalKills=4)]}).raid_evidence == {}


def test_overflowing_total_sample_stays_unknown_instead_of_bypassing_cache_bounds():
    assert _summary({"rankings": [
        _row(BOSS_IDS[0], totalKills=2**63-1),
        _row(BOSS_IDS[1], totalKills=2**63-1),
    ]}).raid_evidence["H"] == {
        "boss_count": 2, "total_bosses": len(BOSS_IDS), "sample_known": False,
    }


@pytest.mark.parametrize("payload", [None, "invalid", {}, {"rankings": None}])
def test_unusable_rankings_metadata_stays_unknown(payload):
    zone = payload if isinstance(payload, dict) else {"rankings": payload}
    assert _summary(zone).raid_evidence == {}


def test_private_scope_never_retains_visible_looking_coverage():
    ranks = _summary({
        "error": "You do not have permission to see this character's rankings.",
        "rankings": [_row(BOSS_IDS[0], totalKills=9)],
    })
    assert ranks.raid_evidence == {}


def test_raid_evidence_roundtrips_through_cache_and_scope_projection(tmp_path):
    broad = MetricPreferences(mplus=False)
    ranks = _summary({"rankings": [_row(BOSS_IDS[0], totalKills=4)]}, prefs=broad)
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, ranks, metric_preferences=broad)
    cache.close()
    loaded = CharacterCache(tmp_path)
    result = loaded.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_ONLY)
    assert result is not None
    assert result.raid_evidence == {"H": ranks.raid_evidence["H"]}
    result.raid_evidence["H"]["boss_count"] = 0
    fresh = loaded.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_ONLY)
    assert fresh is not None
    assert fresh.raid_evidence["H"]["boss_count"] == 1
    loaded.close()


def test_corrupt_cache_raid_counts_cannot_invent_coverage_or_repeats(tmp_path):
    ranks = _summary({"rankings": [_row(BOSS_IDS[0], totalKills=4)]})
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, ranks, metric_preferences=HEROIC_ONLY)
    cache.close()
    raw = json.loads(cache._path.read_text(encoding="utf-8"))
    key = CharacterCache._key("Scout", "ravencrest", "EU", 71, "DAMAGER", HEROIC_ONLY)
    raw["entries"][key]["ranks"]["raid_evidence"] = {
        "N": {"boss_count": True, "total_bosses": len(BOSS_IDS), "sample_known": True},
        "H": {"boss_count": 1, "total_bosses": len(BOSS_IDS), "kill_count": True,
              "sample_known": True, "ignored": "value"},
        "M": {"boss_count": len(BOSS_IDS)+1, "total_bosses": len(BOSS_IDS)},
    }
    cache._path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = CharacterCache(tmp_path)
    result = loaded.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_ONLY)
    assert result is not None
    assert result.raid_evidence == {"H": {
        "boss_count": 1, "total_bosses": len(BOSS_IDS), "sample_known": False,
    }}
    loaded.close()


def test_cached_scope_privacy_removes_even_well_formed_summary_evidence(tmp_path):
    cache = CharacterCache(tmp_path)
    ranks = _summary({"rankings": [_row(BOSS_IDS[0], totalKills=4)]})
    ranks.availability["raid_heroic"] = "restricted"
    cache.put("Scout", "ravencrest", "EU", 71, ranks, metric_preferences=HEROIC_ONLY)
    result = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_ONLY)
    assert result is not None
    assert result.raid_evidence == {}
    cache.close()


def test_applicant_raid_evidence_clears_and_projects_without_touching_rio_progress():
    evidence = {key: {"boss_count": 1, "total_bosses": len(BOSS_IDS), "sample_known": False}
                for key in ("N", "H", "M")}
    applicant = Applicant("1:1", "Scout", "WARRIOR", 71, 300, 3000, "DAMAGER",
                          raid_evidence=evidence, rio_raid_progress={"H": {"killed": 9}})
    applicant.project_wcl_data_to_preferences(HEROIC_ONLY)
    assert applicant.raid_evidence == {"H": evidence["H"]}
    assert asdict(applicant)["raid_evidence"] == applicant.raid_evidence
    applicant.clear_wcl_data()
    assert applicant.raid_evidence == {}
    assert applicant.rio_raid_progress == {"H": {"killed": 9}}


def test_old_in_memory_rank_payload_defaults_raid_metadata_to_unknown(tmp_path):
    cache = CharacterCache(tmp_path)
    ranks = replace(CharacterRanks.empty(), raid_heroic=80)
    cache.put("Scout", "ravencrest", "EU", 71, ranks, metric_preferences=HEROIC_ONLY)
    key = CharacterCache._key("Scout", "ravencrest", "EU", 71, "DAMAGER", HEROIC_ONLY)
    del cache._data[key].ranks["raid_evidence"]
    result = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_ONLY)
    assert result is not None
    assert result.raid_evidence == {}
    cache.close()


def test_mplus_headline_median_requires_the_same_logged_dungeon_population():
    breakdown = [
        DungeonPerf("Repeated", 10, median_percent=10, key_level=12, run_count=3),
        DungeonPerf("Single", 99, median_percent=99, key_level=12, run_count=1),
    ]
    best, median = _compute_mplus_headline(breakdown)
    assert best == pytest.approx(54.5)
    assert median is None


def test_mplus_missing_repeat_median_does_not_change_the_denominator():
    breakdown = [
        DungeonPerf("Known", 80, median_percent=70, key_level=12, run_count=3),
        DungeonPerf("Unknown", 90, median_percent=None, key_level=12, run_count=3),
    ]
    assert _compute_mplus_headline(breakdown) == (85, None)


def test_mplus_unknown_best_dungeon_cannot_supply_an_unpaired_median():
    breakdown = [
        DungeonPerf("Known", 80, median_percent=70, key_level=12, run_count=3),
        DungeonPerf("Unknown", None, median_percent=99, key_level=12, run_count=3),
    ]
    assert _compute_mplus_headline(breakdown) == (80, 70)
