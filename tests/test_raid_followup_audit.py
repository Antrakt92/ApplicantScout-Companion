"""Source contradictions and stale scope values must not become player evidence."""

from dataclasses import replace
import json

import pytest
import applicant_scout.wcl as wcl

from applicant_scout.__main__ import StateMachine
from applicant_scout.constants import CURRENT_RAID_ENCOUNTERS
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.scoring import candidate_fit
from applicant_scout.screenshot import DecodedApplicant, DecodedListing, DecodedRosterMember, Snapshot
from applicant_scout.state import Applicant, AppState, Listing
from applicant_scout.wcl import (
    CharacterCache,
    CharacterRanks,
    DungeonPerf,
    _project_ranks_to_metric_preferences,
    _ranks_from_graphql,
)
from applicant_scout.wcl_availability import METRIC_SCOPES, RaidBossDetails


HEROIC = MetricPreferences(mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=False)
HEROIC_MYTHIC = replace(HEROIC, raid_mythic=True)
BOSS_ID = CURRENT_RAID_ENCOUNTERS[0][1]


def _row(*, spec="Arms", percentile=90):
    return {"encounter": {"id": BOSS_ID}, "spec": spec, "rankPercent": percentile, "totalKills": 3}


def _parse(heroic, mythic=None):
    character = {"raidHeroic": heroic}
    prefs = HEROIC
    if mythic is not None:
        character["raidMythic"] = mythic
        prefs = HEROIC_MYTHIC
    return _ranks_from_graphql(
        {"data": {"characterData": {"character": character}}},
        metric_preferences=prefs, spec_name="Arms",
    )


def _applicant(ranks, *, prefs=HEROIC):
    return Applicant(
        "1:1", "Scout-Ravencrest", "WARRIOR", 71, 700, 3000, "DAMAGER",
        raid_heroic=ranks.raid_heroic, raid_heroic_median=ranks.raid_heroic_median,
        raid_mythic=ranks.raid_mythic, raid_mythic_median=ranks.raid_mythic_median,
        raid_evidence=ranks.raid_evidence, wcl_availability=ranks.availability,
        wcl_metric_preferences=prefs, fetch_status="ready",
    )


@pytest.mark.parametrize("rows", [[_row(spec="Fury")], [_row(), _row(spec="Fury")]])
def test_explicit_foreign_spec_rows_cannot_certify_zone_averages(rows):
    ranks = _parse({"bestPerformanceAverage": 99, "medianPerformanceAverage": 99, "rankings": rows})
    assert ranks.raid_heroic is None
    assert ranks.raid_heroic_median is None
    assert ranks.availability["raid_heroic"] == "unavailable"
    assert "H" not in ranks.raid_evidence


@pytest.mark.parametrize("value", [0, 99])
def test_explicit_empty_rows_cannot_be_presented_as_observed_average(value):
    ranks = _parse({"bestPerformanceAverage": value, "medianPerformanceAverage": value, "rankings": []})
    assert ranks.raid_heroic is None
    assert ranks.raid_heroic_median is None
    assert not candidate_fit(_applicant(ranks), Listing(1, "Raid", "Raid", "", category_id=3, difficulty_id=15)).is_available


def test_conflicting_scope_does_not_discard_healthy_higher_difficulty():
    ranks = _parse(
        {"bestPerformanceAverage": 99, "medianPerformanceAverage": 99, "rankings": [_row(spec="Fury")]},
        {"bestPerformanceAverage": 90, "medianPerformanceAverage": 90, "rankings": [_row()]},
    )
    fit = candidate_fit(
        _applicant(ranks, prefs=HEROIC_MYTHIC),
        Listing(1, "Raid", "Raid", "", category_id=3, difficulty_id=15),
    )
    assert fit.is_available
    assert fit.evidence_source == "M"
    assert fit.score == pytest.approx(79.2)
    assert fit.raid_boss_count == 1


@pytest.mark.parametrize("rankings", [None, "unknown", 1])
def test_optional_unrecognized_rows_do_not_erase_valid_query_scoped_summary(rankings):
    ranks = _parse({"bestPerformanceAverage": 90, "medianPerformanceAverage": 80, "rankings": rankings})
    assert ranks.raid_heroic == 90
    assert ranks.raid_heroic_median == 80
    assert ranks.raid_evidence == {}


def test_absent_optional_rows_and_zero_percentile_are_valid_summary_evidence():
    ranks = _parse({"bestPerformanceAverage": 0, "medianPerformanceAverage": 0})
    assert ranks.raid_heroic == 0
    assert ranks.raid_evidence == {}


@pytest.mark.parametrize("status", ["restricted", "unavailable", "error", "empty", "disabled"])
def test_scope_projection_clears_stale_scalars_breakdown_and_counts(status):
    ranks = replace(CharacterRanks.empty(),
        raid_heroic=99, raid_heroic_median=98, raid_mythic=90, raid_mythic_median=85,
        mplus_dps=99, mplus_dps_median=98,
        mplus_dps_breakdown=[DungeonPerf("Pit of Saron", 99, 98, 15, 3)],
        raid_evidence={"H": {"boss_count": 1, "total_bosses": len(CURRENT_RAID_ENCOUNTERS), "sample_known": True, "kill_count": 3}},
        availability={"raid_heroic": status, "raid_mythic": "available", "mplus": status},
    )
    projected = _project_ranks_to_metric_preferences(ranks, replace(HEROIC_MYTHIC, mplus=True))
    assert projected.raid_heroic is None
    assert projected.raid_heroic_median is None
    assert projected.mplus_dps is None
    assert projected.mplus_dps_median is None
    assert projected.mplus_dps_breakdown == []
    assert projected.raid_evidence == {}
    assert projected.raid_mythic == 90
    assert projected.raid_mythic_median == 85
    assert projected.availability["raid_heroic"] == status
    assert ranks.raid_heroic == 99  # Projection is detached from its input.


@pytest.mark.parametrize("status", ["restricted", "unavailable", "error", "empty", "disabled"])
def test_cache_reload_does_not_resurrect_unavailable_scalar_scope(tmp_path, status):
    ranks = replace(CharacterRanks.empty(),
        raid_heroic=99, raid_heroic_median=99, raid_mythic=90,
        availability={"raid_heroic": status, "raid_mythic": "available"},
    )
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, ranks, metric_preferences=HEROIC_MYTHIC)
    cache.close()
    loaded = CharacterCache(tmp_path)
    try:
        result = loaded.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_MYTHIC)
        assert result is not None
        assert result.raid_heroic is None
        assert result.raid_heroic_median is None
        assert result.availability["raid_heroic"] == status
        assert result.raid_mythic == 90
    finally:
        loaded.close()


@pytest.mark.parametrize("status", ["restricted", "unavailable", "error", "empty", "disabled"])
def test_cache_detail_scope_cannot_resurrect_stale_boss_percentiles(tmp_path, status):
    row = {"encounter_id": BOSS_ID, "name": "Current boss", "overall": 99, "ilvl": 98}
    rows = RaidBossDetails(
        {"H": [row], "M": [{**row, "overall": 90}]},
        availability={"H": status, "M": "available"},
    )
    cache = CharacterCache(tmp_path)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC_MYTHIC)
    cache.close()
    loaded = CharacterCache(tmp_path)
    try:
        result = loaded.get_raid_boss_details("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_MYTHIC)
        assert result is not None
        assert not result.get("H")
        assert result["M"][0]["overall"] == 90
        assert result.availability["H"] == status
    finally:
        loaded.close()


def test_cache_write_omits_explicitly_unusable_values_from_persisted_record(tmp_path):
    cache = CharacterCache(tmp_path)
    ranks = replace(CharacterRanks.empty(), raid_heroic=99, raid_mythic=90,
                    availability={"raid_heroic": "restricted", "raid_mythic": "available"})
    cache.put("Scout", "ravencrest", "EU", 71, ranks, metric_preferences=HEROIC_MYTHIC)
    cache.close()
    payload = json.loads(cache._path.read_text(encoding="utf-8"))
    records = [entry["ranks"] for entry in payload["entries"].values() if entry.get("ranks")]
    assert len(records) == 1
    assert records[0]["raid_heroic"] is None
    assert records[0]["raid_mythic"] == 90
    assert ranks.raid_heroic == 99


def test_new_private_summary_retracts_older_manual_scope_without_losing_public_scope(tmp_path):
    row = {"encounter_id": BOSS_ID, "name": "Current boss", "overall": 99, "ilvl": 98}
    cache = CharacterCache(tmp_path)
    public_details = RaidBossDetails(
        {"H": [row], "M": [{**row, "overall": 90}]},
        availability={"H": "available", "M": "available"},
    )
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, public_details,
                               metric_preferences=HEROIC_MYTHIC)
    summary = replace(CharacterRanks.empty(), raid_mythic=90,
                      availability={"raid_heroic": "restricted", "raid_mythic": "available"})
    cache.put("Scout", "ravencrest", "EU", 71, summary, metric_preferences=HEROIC_MYTHIC)
    details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                         metric_preferences=HEROIC_MYTHIC)
    try:
        assert details is not None
        assert not details.get("H")
        assert details.availability["H"] == "restricted"
        assert details["M"][0]["overall"] == 90
    finally:
        cache.close()


def test_manual_query_started_before_private_summary_cannot_publish_even_in_new_scope(tmp_path):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
    rows = RaidBossDetails(
        {"H": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 99}], "M": []},
        availability={"H": "available", "M": "available"},
    )
    try:
        assert cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC_MYTHIC,
            expected_publication_epoch=started,
        )
        projected = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                               metric_preferences=HEROIC_MYTHIC)
        assert projected is not None and projected.availability["H"] == "restricted"
        assert not projected.get("H")
        assert cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC_MYTHIC,
            expected_publication_epoch=cache.publication_epoch,
        )
        restored = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                              metric_preferences=HEROIC_MYTHIC)
        assert restored is not None and restored["H"][0]["overall"] == 99
    finally:
        cache.close()


def test_privacy_retraction_keeps_original_detail_expiry_and_blocks_older_narrow_publish(tmp_path):
    cache = CharacterCache(tmp_path)
    rows = RaidBossDetails(
        {"H": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 99}], "M": []},
        availability={"H": "available", "M": "available"},
    )
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, rows,
                               metric_preferences=HEROIC_MYTHIC)
    detail_key = cache._raid_boss_key("Scout", "ravencrest", "EU", 71, "DAMAGER", HEROIC_MYTHIC)
    fetched_at = cache._data[detail_key].fetched_at
    started = cache.publication_epoch
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC,
              expected_publication_epoch=started)
    try:
        assert cache._data[detail_key].fetched_at == fetched_at
        assert not cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC,
            expected_publication_epoch=started,
        )
    finally:
        cache.close()


def test_older_summary_does_not_retract_manual_publication_started_after_it(tmp_path):
    cache = CharacterCache(tmp_path)
    summary_started = cache.publication_epoch
    rows = RaidBossDetails(
        {"H": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 99}]},
        availability={"H": "available"},
    )
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC)
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC,
              expected_publication_epoch=summary_started)
    try:
        details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                             metric_preferences=HEROIC)
        assert details is not None and details["H"][0]["overall"] == 99
    finally:
        cache.close()


def test_private_publication_barrier_survives_cache_entry_eviction(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.MAX_ENTRIES = 1
    started = cache.publication_epoch
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
    cache.put("Other", "ravencrest", "EU", 71, CharacterRanks.empty(), metric_preferences=HEROIC)
    rows = RaidBossDetails(
        {"H": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 99}]},
        availability={"H": "available"},
    )
    try:
        assert not cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC,
            expected_publication_epoch=started,
        )
        assert cache.put_raid_boss_details(
            "Unrelated", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC,
            expected_publication_epoch=started,
        )
    finally:
        cache.close()


@pytest.mark.parametrize("evict", [False, True])
def test_fresh_manual_private_scope_retracts_broad_history_and_blocks_older_publish(tmp_path, evict):
    cache = CharacterCache(tmp_path)
    rows = RaidBossDetails(
        {"H": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 99}],
         "M": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 90}]},
        availability={"H": "available", "M": "available"},
    )
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, rows,
                               metric_preferences=HEROIC_MYTHIC)
    started = cache.publication_epoch
    cache.put_raid_boss_details(
        "Scout", "ravencrest", "EU", 71,
        RaidBossDetails({"H": []}, availability={"H": "restricted"}),
        metric_preferences=HEROIC, expected_publication_epoch=started,
    )
    try:
        if evict:
            cache.MAX_ENTRIES = 1
            cache.put("Other", "ravencrest", "EU", 71, CharacterRanks.empty(), metric_preferences=HEROIC)
        else:
            details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                                 metric_preferences=HEROIC_MYTHIC)
            assert details is not None and not details.get("H")
            assert details.availability["H"] == "restricted"
            assert details["M"][0]["overall"] == 90
        assert cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC_MYTHIC,
            expected_publication_epoch=started,
        )
        projected = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                               metric_preferences=HEROIC_MYTHIC)
        assert projected is None or not projected.get("H")
        assert cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC_MYTHIC,
            expected_publication_epoch=cache.publication_epoch,
        )
    finally:
        cache.close()


def test_old_narrow_private_manual_response_cannot_mask_newer_broad_public_evidence(tmp_path):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    rows = RaidBossDetails(
        {"H": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 99}], "M": []},
        availability={"H": "available", "M": "available"},
    )
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, rows,
                               metric_preferences=HEROIC_MYTHIC)
    try:
        assert not cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71,
            RaidBossDetails({"H": []}, availability={"H": "restricted"}),
            metric_preferences=HEROIC, expected_publication_epoch=started,
        )
        details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                             metric_preferences=HEROIC)
        assert details is not None and details["H"][0]["overall"] == 99
    finally:
        cache.close()


def test_bounded_privacy_barrier_retirement_and_clear_preserve_publication_order(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.MAX_ENTRIES = 1
    started, generation = cache.publication_epoch, cache.generation
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    for name in ("Scout", "Other1", "Other2", "Other3"):
        cache.put(name, "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
    rows = RaidBossDetails(
        {"H": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 99}]},
        availability={"H": "available"},
    )
    try:
        assert len(cache._raid_detail_privacy_epochs) <= 3
        assert not cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC,
            expected_publication_epoch=started, expected_generation=generation,
        )
        fresh_epoch = cache.publication_epoch
        assert cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC,
            expected_publication_epoch=fresh_epoch, expected_generation=generation,
        )
        assert cache.clear()
        assert not cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC,
            expected_publication_epoch=fresh_epoch, expected_generation=generation,
        )
        assert cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, rows, metric_preferences=HEROIC,
            expected_publication_epoch=cache.publication_epoch, expected_generation=cache.generation,
        )
    finally:
        cache.close()


def _public_details(heroic=99, mythic=90):
    row = {"encounter_id": BOSS_ID, "name": "Boss", "overall": heroic}
    return RaidBossDetails(
        {"H": [row], "M": [{**row, "overall": mythic}]},
        availability={"H": "available", "M": "available"},
    )


def test_new_narrow_public_detail_restores_broader_read_without_losing_other_difficulty(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(),
                               metric_preferences=HEROIC_MYTHIC)
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(heroic=88),
                               metric_preferences=HEROIC,
                               expected_publication_epoch=cache.publication_epoch)
    try:
        details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                             metric_preferences=HEROIC_MYTHIC)
        assert details is not None and details.availability["H"] == "available"
        assert details["H"][0]["overall"] == 88
        assert details["M"][0]["overall"] == 90
    finally:
        cache.close()


def test_h_only_privacy_retraction_does_not_make_old_m_boss_rows_newer(tmp_path):
    mythic = replace(HEROIC_MYTHIC, raid_heroic=False)
    cache = CharacterCache(tmp_path)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(mythic=10),
                               metric_preferences=HEROIC_MYTHIC)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(mythic=95),
                               metric_preferences=mythic)
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
    try:
        details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                             metric_preferences=HEROIC_MYTHIC)
        assert details is not None and details.availability["H"] == "restricted"
        assert details["M"][0]["overall"] == 95
    finally:
        cache.close()


def test_delayed_mixed_private_summary_retracts_old_m_while_preserving_newer_h(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(),
                               metric_preferences=HEROIC_MYTHIC)
    started = cache.publication_epoch
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(heroic=88),
                               metric_preferences=HEROIC)
    private = replace(CharacterRanks.empty(),
                      availability={"raid_heroic": "restricted", "raid_mythic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC_MYTHIC,
              expected_publication_epoch=started)
    try:
        details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                             metric_preferences=HEROIC_MYTHIC)
        assert details is not None and details.availability["H"] == "available"
        assert details["H"][0]["overall"] == 88
        assert details.availability["M"] == "restricted"
        assert not details.get("M")
    finally:
        cache.close()


def test_delayed_mixed_manual_private_response_only_retracts_eligible_m_scope(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(),
                               metric_preferences=HEROIC_MYTHIC)
    started = cache.publication_epoch
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(heroic=88),
                               metric_preferences=HEROIC)
    accepted = cache.put_raid_boss_details(
        "Scout", "ravencrest", "EU", 71,
        RaidBossDetails({"H": [], "M": []}, availability={"H": "restricted", "M": "restricted"}),
        metric_preferences=HEROIC_MYTHIC, expected_publication_epoch=started,
    )
    try:
        assert accepted
        details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                             metric_preferences=HEROIC_MYTHIC)
        assert details is not None and details.availability["H"] == "available"
        assert details["H"][0]["overall"] == 88
        assert details.availability["M"] == "restricted"
    finally:
        cache.close()


def test_fresh_manual_positive_clears_older_not_found_but_older_positive_cannot(tmp_path):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
              metric_preferences=HEROIC)
    try:
        assert not cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, _public_details(), metric_preferences=HEROIC,
            expected_publication_epoch=started,
        )
        assert cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71, _public_details(), metric_preferences=HEROIC,
            expected_publication_epoch=cache.publication_epoch,
        )
        details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                             metric_preferences=HEROIC)
        assert details is not None and not details.not_found
        assert details["H"][0]["overall"] == 99
    finally:
        cache.close()


def test_difficulty_proof_timestamps_and_reset_receipts_survive_cache_reload(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(wcl.time, "time", lambda: clock["now"])
    mythic = replace(HEROIC_MYTHIC, raid_heroic=False)
    cache = CharacterCache(tmp_path)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(mythic=10),
                               metric_preferences=HEROIC_MYTHIC)
    clock["now"] = 1100.0
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(mythic=95),
                               metric_preferences=mythic)
    m_epoch = cache.publication_epoch
    clock["now"] = 1200.0
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
    details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                         metric_preferences=HEROIC_MYTHIC)
    assert details is not None and details.publication_epochs == {"H": cache.publication_epoch, "M": m_epoch}
    cache.close()
    loaded = CharacterCache(tmp_path)
    try:
        details = loaded.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                              metric_preferences=HEROIC_MYTHIC)
        assert details is not None and details.availability["H"] == "restricted"
        assert details["M"][0]["overall"] == 95
        assert details.publication_epochs == {"H": 0, "M": 0}
    finally:
        loaded.close()


def test_composed_detail_scopes_expire_independently(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(wcl.time, "time", lambda: clock["now"])
    cache = CharacterCache(tmp_path, ttl_seconds=300)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(),
                               metric_preferences=HEROIC_MYTHIC)
    clock["now"] = 1100.0
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
    clock["now"] = 1200.0
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(heroic=88),
                               metric_preferences=HEROIC)
    clock["now"] = 1290.0
    details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                         metric_preferences=HEROIC_MYTHIC)
    assert details is not None and details["M"][0]["overall"] == 90
    clock["now"] = 1301.0
    try:
        assert cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                           metric_preferences=HEROIC_MYTHIC) is None
        heroic = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                            metric_preferences=HEROIC)
        assert heroic is not None and heroic["H"][0]["overall"] == 88
    finally:
        cache.close()


def test_composed_getter_retries_when_publication_changes_without_generation(tmp_path, monkeypatch):
    cache = CharacterCache(tmp_path)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(),
                               metric_preferences=HEROIC)
    original = cache._raid_boss_details_from_entry
    calls = []
    def convert(entry, preferences):
        calls.append(entry)
        if len(calls) == 1:
            private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
            cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
        return original(entry, preferences)
    monkeypatch.setattr(cache, "_raid_boss_details_from_entry", convert)
    try:
        details = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                             metric_preferences=HEROIC)
        assert len(calls) >= 2
        assert details is not None and details.availability["H"] == "restricted"
        assert not details.get("H")
    finally:
        cache.close()


@pytest.mark.parametrize("raid_details,not_found", [(False, False), (False, True), (True, False), (True, True)])
def test_generation_only_clear_does_not_supersede_fresh_active_source(tmp_path, raid_details, not_found):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    cache.clear()
    try:
        assert cache.can_deliver_response(
            "Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
            expected_publication_epoch=started, raid_details=raid_details, not_found=not_found,
        )
    finally:
        cache.close()


@pytest.mark.parametrize("raid_details", [False, True])
def test_postclear_private_proof_blocks_old_source_delivery(tmp_path, raid_details):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    cache.clear()
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
    try:
        assert not cache.can_deliver_response(
            "Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
            expected_publication_epoch=started, raid_details=raid_details,
        )
    finally:
        cache.close()


@pytest.mark.parametrize("raid_details", [False, True])
def test_unrelated_scope_identity_or_role_does_not_supersede_source_delivery(tmp_path, raid_details):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    mythic = replace(HEROIC_MYTHIC, raid_heroic=False)
    cache.put("Other", "ravencrest", "EU", 71, CharacterRanks.empty(), metric_preferences=HEROIC)
    cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(), role="HEALER", metric_preferences=HEROIC)
    cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(), metric_preferences=mythic)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(), metric_preferences=mythic)
    try:
        assert cache.can_deliver_response(
            "Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
            expected_publication_epoch=started, raid_details=raid_details,
        )
    finally:
        cache.close()


@pytest.mark.parametrize("manual", [False, True])
def test_newer_same_scope_source_blocks_old_fallback_delivery(tmp_path, manual):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    if manual:
        cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _public_details(), metric_preferences=HEROIC)
    else:
        cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(), metric_preferences=HEROIC)
    try:
        assert not cache.can_deliver_response(
            "Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
            expected_publication_epoch=started, raid_details=manual,
        )
    finally:
        cache.close()


def test_manual_positive_on_another_spec_blocks_queued_character_not_found(tmp_path):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 72, _public_details(), metric_preferences=HEROIC)
    try:
        assert not cache.can_deliver_response(
            "Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
            expected_publication_epoch=started, not_found=True,
        )
    finally:
        cache.close()


def test_scalar_getter_retries_private_publication_without_generation_change(tmp_path, monkeypatch):
    cache = CharacterCache(tmp_path)
    public = replace(CharacterRanks.empty(), raid_heroic=99, availability={"raid_heroic": "available"})
    cache.put("Scout", "ravencrest", "EU", 71, public, metric_preferences=HEROIC)
    original = cache._ranks_from_entry
    calls = []
    def convert(entry):
        calls.append(entry)
        if len(calls) == 1:
            private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
            cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
        return original(entry)
    monkeypatch.setattr(cache, "_ranks_from_entry", convert)
    try:
        ranks = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC)
        assert len(calls) >= 2
        assert ranks is not None and ranks.availability["raid_heroic"] == "restricted"
        assert ranks.raid_heroic is None
    finally:
        cache.close()


def test_narrow_summary_privacy_retracts_old_wide_scalar_without_extending_other_scope_ttl(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(wcl.time, "time", lambda: clock["now"])
    cache = CharacterCache(tmp_path, ttl_seconds=300)
    public = replace(CharacterRanks.empty(), raid_heroic=99, raid_mythic=90,
                     availability={"raid_heroic": "available", "raid_mythic": "available"})
    cache.put("Scout", "ravencrest", "EU", 71, public, metric_preferences=HEROIC_MYTHIC)
    clock["now"] = 1100.0
    private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
    cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=HEROIC)
    try:
        wide = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_MYTHIC)
        assert wide is not None and wide.raid_heroic is None
        assert wide.availability["raid_heroic"] == "restricted"
        assert wide.raid_mythic == 90
        clock["now"] = 1301.0
        assert cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_MYTHIC) is None
    finally:
        cache.close()


def test_narrow_public_summary_cannot_revive_old_wide_scalar_but_new_broad_public_can(tmp_path):
    cache = CharacterCache(tmp_path)
    broad = replace(CharacterRanks.empty(), raid_heroic=99, raid_mythic=90,
                    availability={"raid_heroic": "available", "raid_mythic": "available"})
    cache.put("Scout", "ravencrest", "EU", 71, broad, metric_preferences=HEROIC_MYTHIC)
    cache.put("Scout", "ravencrest", "EU", 71,
              replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"}),
              metric_preferences=HEROIC)
    narrow = replace(CharacterRanks.empty(), raid_heroic=88, availability={"raid_heroic": "available"})
    cache.put("Scout", "ravencrest", "EU", 71, narrow, metric_preferences=HEROIC)
    try:
        wide = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_MYTHIC)
        assert wide is not None and wide.raid_heroic is None and wide.raid_mythic == 90
        fresh = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC)
        assert fresh is not None and fresh.raid_heroic == 88
        cache.put("Scout", "ravencrest", "EU", 71, replace(broad, raid_heroic=77),
                  metric_preferences=HEROIC_MYTHIC)
        restored = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_MYTHIC)
        assert restored is not None and restored.raid_heroic == 77 and restored.raid_mythic == 90
    finally:
        cache.close()


def test_manual_only_privacy_does_not_erase_independent_scalar_summary(tmp_path):
    cache = CharacterCache(tmp_path)
    broad = replace(CharacterRanks.empty(), raid_heroic=99, raid_mythic=90,
                    availability={"raid_heroic": "available", "raid_mythic": "available"})
    cache.put("Scout", "ravencrest", "EU", 71, broad, metric_preferences=HEROIC_MYTHIC)
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71,
                               RaidBossDetails({"H": []}, availability={"H": "restricted"}),
                               metric_preferences=HEROIC)
    try:
        wide = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_MYTHIC)
        assert wide is not None and wide.raid_heroic == 99 and wide.raid_mythic == 90
    finally:
        cache.close()


def test_manual_only_private_proof_does_not_supersede_uncached_scalar_response(tmp_path):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71,
                               RaidBossDetails({"H": []}, availability={"H": "restricted"}),
                               metric_preferences=HEROIC)
    try:
        assert cache.can_deliver_response(
            "Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
            expected_publication_epoch=started,
        )
    finally:
        cache.close()


@pytest.mark.parametrize("retire_marker", [False, True])
def test_scalar_privacy_barrier_survives_eviction_but_new_current_aggregate_is_eligible(tmp_path, retire_marker):
    cache = CharacterCache(tmp_path)
    cache.MAX_ENTRIES = 1
    started = cache.publication_epoch
    cache.put("Scout", "ravencrest", "EU", 71,
              replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"}),
              metric_preferences=HEROIC)
    cache.put("Other", "ravencrest", "EU", 71, CharacterRanks.empty(), metric_preferences=HEROIC)
    if retire_marker:
        for name in ("Private1", "Private2", "Private3", "Private4"):
            cache.put(name, "ravencrest", "EU", 71,
                      replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"}),
                      metric_preferences=HEROIC)
    try:
        assert len(cache._raid_summary_privacy_epochs) <= len(METRIC_SCOPES)
        assert cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC) is None
        assert not cache.can_deliver_response(
            "Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
            expected_publication_epoch=started,
        )
        assert not cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(),
                             metric_preferences=HEROIC, expected_publication_epoch=started)
        assert not cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
                             metric_preferences=HEROIC, expected_publication_epoch=started)
        fresh_started = cache.publication_epoch
        assert cache.can_deliver_response(
            "Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
            expected_publication_epoch=fresh_started,
        )
        public = replace(CharacterRanks.empty(), raid_heroic=88, availability={"raid_heroic": "available"})
        assert cache.put("Scout", "ravencrest", "EU", 71, public, metric_preferences=HEROIC,
                         expected_publication_epoch=fresh_started)
        ranks = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC)
        assert ranks is not None and ranks.raid_heroic == 88
        cache.clear()
        assert not cache._raid_summary_privacy_epochs and cache._raid_summary_privacy_floor == 0
        assert cache.can_deliver_response("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
                                          expected_publication_epoch=started)
    finally:
        cache.close()


def test_older_narrow_private_summary_cannot_override_newer_broad_public_summary(tmp_path):
    cache = CharacterCache(tmp_path)
    started = cache.publication_epoch
    broad = replace(CharacterRanks.empty(), raid_heroic=99, raid_mythic=90,
                    availability={"raid_heroic": "available", "raid_mythic": "available"})
    cache.put("Scout", "ravencrest", "EU", 71, broad, metric_preferences=HEROIC_MYTHIC)
    try:
        assert not cache.put(
            "Scout", "ravencrest", "EU", 71,
            replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"}),
            metric_preferences=HEROIC, expected_publication_epoch=started,
        )
        heroic = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC)
        assert heroic is not None and heroic.raid_heroic == 99
        wide = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC_MYTHIC)
        assert wide is not None and wide.raid_heroic == 99 and wide.raid_mythic == 90
    finally:
        cache.close()


def test_mplus_privacy_retracts_old_wide_summary_and_breakdowns_without_renewing_raid(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(wcl.time, "time", lambda: clock["now"])
    cache = CharacterCache(tmp_path, ttl_seconds=300)
    wide_preferences = replace(HEROIC, mplus=True)
    mplus_only = replace(wide_preferences, raid_heroic=False)
    public = replace(
        CharacterRanks.empty(), raid_heroic=90, mplus_dps=99, mplus_dps_median=98,
        mplus_hps=97, mplus_hps_median=96,
        mplus_dps_breakdown=[DungeonPerf("Pit of Saron", 99, 98, 15, 3)],
        mplus_hps_breakdown=[DungeonPerf("Pit of Saron", 97, 96, 15, 3)],
        availability={"mplus": "available", "raid_heroic": "available"},
    )
    cache.put("Scout", "ravencrest", "EU", 71, public, metric_preferences=wide_preferences)
    clock["now"] = 1100.0
    cache.put("Scout", "ravencrest", "EU", 71,
              replace(CharacterRanks.empty(), availability={"mplus": "restricted"}),
              metric_preferences=mplus_only)
    try:
        wide = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=wide_preferences)
        assert wide is not None and wide.availability["mplus"] == "restricted"
        assert wide.mplus_dps is None and wide.mplus_dps_median is None
        assert wide.mplus_hps is None and wide.mplus_hps_median is None
        assert not wide.mplus_dps_breakdown and not wide.mplus_hps_breakdown
        assert wide.raid_heroic == 90
        cache.put("Scout", "ravencrest", "EU", 71,
                  replace(CharacterRanks.empty(), mplus_dps=88, availability={"mplus": "available"}),
                  metric_preferences=mplus_only)
        wide = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=wide_preferences)
        assert wide is not None and wide.mplus_dps is None and wide.raid_heroic == 90
        fresh = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=mplus_only)
        assert fresh is not None and fresh.mplus_dps == 88
        clock["now"] = 1301.0
        assert cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=wide_preferences) is None
        cache.put("Scout", "ravencrest", "EU", 71, replace(public, mplus_dps=77),
                  metric_preferences=wide_preferences)
        fresh = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=wide_preferences)
        assert fresh is not None and fresh.mplus_dps == 77 and fresh.raid_heroic == 90
    finally:
        cache.close()


def test_mplus_privacy_barrier_blocks_old_scalar_and_not_found_after_eviction(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.MAX_ENTRIES = 1
    mplus_only = MetricPreferences(mplus=True, raid_normal=False, raid_heroic=False, raid_mythic=False)
    started = cache.publication_epoch
    cache.put("Scout", "ravencrest", "EU", 71,
              replace(CharacterRanks.empty(), availability={"mplus": "restricted"}),
              metric_preferences=mplus_only)
    cache.put("Other", "ravencrest", "EU", 71, CharacterRanks.empty(), metric_preferences=mplus_only)
    try:
        assert cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=mplus_only) is None
        for not_found in (False, True):
            assert not cache.can_deliver_response(
                "Scout", "ravencrest", "EU", 71, metric_preferences=mplus_only,
                expected_publication_epoch=started, not_found=not_found,
            )
            assert not cache.put("Scout", "ravencrest", "EU", 71,
                                 CharacterRanks.empty(not_found=not_found), metric_preferences=mplus_only,
                                 expected_publication_epoch=started)
        fresh_started = cache.publication_epoch
        assert cache.can_deliver_response("Scout", "ravencrest", "EU", 71, metric_preferences=mplus_only,
                                          expected_publication_epoch=fresh_started)
    finally:
        cache.close()


def test_explicit_class_change_does_not_preserve_foreign_spec_or_wcl_for_same_name():
    state = AppState()
    machine = StateMachine(state)
    first = DecodedApplicant(1, 1, 71, 700, 3000, 2, "Scout-Ravencrest")
    listing = DecodedListing(401, 14, "Pit of Saron", "+14", "")
    machine.apply_snapshot(Snapshot(listing=listing, version=None, applicants=[first]))
    before = state.applicants["1:1"]
    before.fetch_status = "ready"
    before.raid_heroic = 99
    second = replace(first, class_id=5, spec_id=0)
    machine.apply_snapshot(Snapshot(listing=listing, version=None, applicants=[second]))
    after = state.applicants["1:1"]
    assert after.cls == "PRIEST"
    assert after.spec_id == 0
    assert after.raid_heroic is None
    assert after.fetch_status == "pending"


def test_roster_explicit_class_change_does_not_copy_foreign_spec_wcl():
    state = AppState()
    machine = StateMachine(state)
    first = DecodedRosterMember(1, 0, 1, 1, 71, 700, 3000, 0, role=2, name="Scout-Ravencrest")
    machine._apply_roster_snapshot([first])
    before = next(iter(state.party_members.values()))
    before.fetch_status = "ready"
    before.raid_heroic = 99
    machine._apply_roster_snapshot([replace(first, class_id=5, spec_id=0)])
    after = next(iter(state.party_members.values()))
    assert after.cls == "PRIEST"
    assert after.spec_id == 0
    assert after.raid_heroic is None
    assert after.fetch_status == "pending"


@pytest.mark.parametrize("surface", ["applicant", "roster"])
def test_transient_unknown_class_cannot_erase_provenance_before_different_known_class(surface):
    state = AppState()
    machine = StateMachine(state)
    if surface == "applicant":
        listing = DecodedListing(401, 14, "Pit of Saron", "+14", "")
        row = DecodedApplicant(1, 1, 71, 700, 3000, 2, "Scout-Ravencrest")
        def apply(value):
            machine.apply_snapshot(Snapshot(listing=listing, version=None, applicants=[value]))
        def current():
            return state.applicants["1:1"]
    else:
        row = DecodedRosterMember(1, 0, 1, 1, 71, 700, 3000, 0, role=2, name="Scout-Ravencrest")
        def apply(value):
            machine._apply_roster_snapshot([value])
        def current():
            return next(iter(state.party_members.values()))
    apply(row)
    current().fetch_status = "ready"
    current().raid_heroic = 99
    apply(replace(row, class_id=0, spec_id=0))
    assert current().cls == "WARRIOR" and current().spec_id == 71
    assert current().raid_heroic == 99
    apply(replace(row, class_id=5, spec_id=0))
    assert current().cls == "PRIEST" and current().spec_id == 0
    assert current().raid_heroic is None and current().fetch_status == "pending"
