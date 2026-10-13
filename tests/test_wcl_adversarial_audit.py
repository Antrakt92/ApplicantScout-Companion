"""Source order must survive loss of cache rows without mixing capabilities."""

from dataclasses import replace

import pytest

from applicant_scout import wcl
from applicant_scout.constants import CURRENT_RAID_ENCOUNTERS
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.wcl import CharacterCache, CharacterRanks
from applicant_scout.wcl_availability import RaidBossDetails


HEROIC = MetricPreferences(mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=False)
MYTHIC = replace(HEROIC, raid_heroic=False, raid_mythic=True)
BOSS_ID = CURRENT_RAID_ENCOUNTERS[0][1]


@pytest.fixture
def cache(tmp_path):
    result = CharacterCache(tmp_path)
    yield result
    result.close()


def _publish(cache, kind, *, name="Scout", preferences=HEROIC, spec=71, expected=None, value=88):
    if kind == "manual":
        difficulty = "H" if preferences.raid_heroic else "M"
        rows = RaidBossDetails(
            {difficulty: [{"encounter_id": BOSS_ID, "name": CURRENT_RAID_ENCOUNTERS[0][2], "overall": value}]},
            availability={difficulty: "available"},
        )
        return cache.put_raid_boss_details(
            name, "ravencrest", "EU", spec, rows, metric_preferences=preferences,
            expected_publication_epoch=expected,
        )
    ranks = replace(CharacterRanks.empty(), raid_heroic=value, raid_mythic=value,
                    availability={"raid_heroic": "available", "raid_mythic": "available"})
    return cache.put(name, "ravencrest", "EU", spec, ranks, metric_preferences=preferences,
                     expected_publication_epoch=expected)


def _eligible(cache, kind, *, expected, not_found=False, preferences=HEROIC):
    return cache.can_deliver_response(
        "Scout", "ravencrest", "EU", 71, metric_preferences=preferences,
        expected_publication_epoch=expected, raid_details=kind == "manual", not_found=not_found,
    )


@pytest.mark.parametrize("kind", ["scalar", "manual"])
def test_newer_public_character_proof_blocks_old_404_after_eviction(cache, kind):
    cache.MAX_ENTRIES = 1
    started = cache.publication_epoch
    assert _publish(cache, kind)
    assert _publish(cache, "scalar", name="Other")
    assert cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC) is None
    assert cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC) is None
    assert not _eligible(cache, "scalar", expected=started, not_found=True)
    assert not cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
                         metric_preferences=HEROIC, expected_publication_epoch=started)


@pytest.mark.parametrize("kind", ["scalar", "manual"])
def test_newer_character_404_blocks_old_public_after_eviction(cache, kind):
    cache.MAX_ENTRIES = 1
    started = cache.publication_epoch
    assert cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
                     metric_preferences=HEROIC)
    assert _publish(cache, "scalar", name="Other")
    assert cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC) is None
    assert not _eligible(cache, kind, expected=started)
    assert not _publish(cache, kind, expected=started)


@pytest.mark.parametrize("kind", ["scalar", "manual"])
def test_newer_same_scope_public_blocks_old_public_after_eviction(cache, kind):
    cache.MAX_ENTRIES = 1
    started = cache.publication_epoch
    assert _publish(cache, kind, value=88)
    assert _publish(cache, "scalar", name="Other")
    assert not _eligible(cache, kind, expected=started)
    assert not _publish(cache, kind, expected=started, value=99)


@pytest.mark.parametrize("kind", ["scalar", "manual"])
def test_later_started_public_response_can_restore_after_evicted_404(cache, kind):
    cache.MAX_ENTRIES = 1
    assert cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
                     metric_preferences=HEROIC)
    assert _publish(cache, "scalar", name="Other")
    current = cache.publication_epoch
    assert _eligible(cache, kind, expected=current)
    assert _publish(cache, kind, expected=current)
    if kind == "manual":
        rows = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC)
        assert rows is not None and rows["H"][0]["overall"] == 88
    else:
        ranks = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC)
        assert ranks is not None and not ranks.not_found and ranks.raid_heroic == 88


@pytest.mark.parametrize("kind", ["scalar", "manual"])
def test_unrelated_character_scope_and_capability_do_not_block_active_source(cache, kind):
    started = cache.publication_epoch
    assert _publish(cache, "scalar", name="Other")
    assert _publish(cache, kind, preferences=MYTHIC)
    assert _publish(cache, "manual" if kind == "scalar" else "scalar")
    assert _eligible(cache, kind, expected=started)
    assert _publish(cache, kind, expected=started)


@pytest.mark.parametrize("kind", ["scalar", "manual"])
def test_generation_only_clear_allows_active_response_and_removes_old_proofs(cache, kind):
    started = cache.publication_epoch
    assert _publish(cache, kind)
    assert cache.clear()
    assert _eligible(cache, kind, expected=started)
    assert _eligible(cache, "scalar", expected=started, not_found=True)


@pytest.mark.parametrize("kind", ["scalar", "manual"])
def test_positive_on_other_spec_is_character_proof_without_becoming_same_spec_performance(cache, kind):
    started = cache.publication_epoch
    assert _publish(cache, kind, spec=72)
    assert not _eligible(cache, "scalar", expected=started, not_found=True)
    assert _eligible(cache, kind, expected=started)


def test_expiring_and_pruning_negative_does_not_erase_session_source_order(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(wcl.time, "time", lambda: clock["now"])
    cache = CharacterCache(tmp_path, ttl_seconds=1)
    started = cache.publication_epoch
    try:
        assert cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
                         metric_preferences=HEROIC)
        clock["now"] = 1002.0
        assert _publish(cache, "scalar", name="Other")
        assert cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC) is None
        assert not _eligible(cache, "scalar", expected=started)
        assert not _publish(cache, "scalar", expected=started)
    finally:
        cache.close()


def test_fresh_manual_character_404_retracts_all_wcl_and_blocks_old_publishers(cache):
    assert _publish(cache, "scalar")
    assert _publish(cache, "manual")
    started = cache.publication_epoch
    assert cache.put_raid_boss_details(
        "Scout", "ravencrest", "EU", 71, RaidBossDetails(not_found=True),
        metric_preferences=HEROIC, expected_publication_epoch=started,
    )
    missing = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC)
    assert missing is not None and missing.not_found
    assert cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC) is None
    assert not _publish(cache, "scalar", expected=started)
    assert not _publish(cache, "manual", expected=started)


def test_exact_negative_receipt_survives_unrelated_publication_and_cache_eviction(cache):
    cache.MAX_ENTRIES = 1
    assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                     RaidBossDetails(not_found=True), metric_preferences=HEROIC,
                                     expected_publication_epoch=cache.publication_epoch)
    receipt = cache.current_not_found_publication_epoch("Scout", "ravencrest", "EU")
    assert receipt == 1
    assert _publish(cache, "scalar", name="Other")
    assert cache.publication_epoch == 2
    assert cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC) is None
    assert cache.current_not_found_publication_epoch("Scout", "ravencrest", "EU") == receipt
    assert _eligible(cache, "manual", expected=receipt, not_found=True)


@pytest.mark.parametrize("kind", ["scalar", "manual"])
def test_later_positive_revokes_old_negative_receipt_even_after_both_cache_rows_leave(cache, kind):
    cache.MAX_ENTRIES = 1
    assert cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
                     metric_preferences=HEROIC)
    receipt = cache.current_not_found_publication_epoch("Scout", "ravencrest", "EU")
    assert receipt == 1
    assert _publish(cache, kind, expected=receipt)
    assert _publish(cache, "scalar", name="Other")
    assert cache.current_not_found_publication_epoch("Scout", "ravencrest", "EU") is None
    assert not _eligible(cache, "manual", expected=receipt, not_found=True)


def test_reload_resets_session_receipts_but_preserves_retained_negative_source(tmp_path):
    cache = CharacterCache(tmp_path)
    assert cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
                     metric_preferences=HEROIC)
    assert cache.current_not_found_publication_epoch("Scout", "ravencrest", "EU") == 1
    cache.close()
    loaded = CharacterCache(tmp_path)
    try:
        assert loaded.publication_epoch == 0
        assert loaded.current_not_found_publication_epoch("Scout", "ravencrest", "EU") == 0
        assert _publish(loaded, "manual", expected=0)
        assert loaded.current_not_found_publication_epoch("Scout", "ravencrest", "EU") is None
    finally:
        loaded.close()


@pytest.mark.parametrize("kind", ["scalar", "manual", "not_found"])
def test_source_proof_retirement_is_bounded_and_rejects_older_requests(cache, kind):
    cache.MAX_ENTRIES = 1
    started = cache.publication_epoch
    for index in range(6):
        if kind == "not_found":
            assert cache.put(f"Other{index}", "ravencrest", "EU", 71,
                             CharacterRanks.empty(not_found=True), metric_preferences=HEROIC)
        else:
            assert _publish(cache, kind, name=f"Other{index}")
    assert len(cache._character_positive_epochs) <= 1
    assert len(cache._character_negative_epochs) <= 1
    assert len(cache._raid_summary_privacy_epochs) <= 4
    assert len(cache._raid_detail_privacy_epochs) <= 3
    if kind == "not_found":
        assert not _eligible(cache, "manual", expected=started)
    else:
        assert not _eligible(cache, "scalar", expected=started, not_found=True)
        assert not _eligible(cache, kind, expected=started)
    assert _eligible(cache, "manual", expected=cache.publication_epoch)
    assert cache.clear()
    assert _eligible(cache, "manual", expected=started)
    assert _eligible(cache, "scalar", expected=started, not_found=True)


def test_old_aggregate_privacy_cannot_revoke_a_newer_manual_public_proof_after_eviction(cache):
    cache.MAX_ENTRIES = 1
    started = cache.publication_epoch
    assert _publish(cache, "manual")
    receipt = cache.publication_epoch
    assert _publish(cache, "scalar", name="Other")
    assert cache.put("Scout", "ravencrest", "EU", 71,
                     replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"}),
                     metric_preferences=HEROIC, expected_publication_epoch=started)
    assert cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC) is None
    assert _eligible(cache, "manual", expected=receipt)


@pytest.mark.parametrize("intervening", ["Other", "Scout"])
def test_scalar_receipt_is_exact_before_intervening_publication_and_eviction(cache, monkeypatch, intervening):
    cache.MAX_ENTRIES = 1
    original_flush = cache.flush
    triggered = []
    def flush(*args, **kwargs):
        if not triggered:
            triggered.append(True)
            assert _publish(cache, "scalar", name=intervening, value=99)
        return original_flush(*args, **kwargs)
    monkeypatch.setattr(cache, "flush", flush)
    receipt = []
    ranks = replace(CharacterRanks.empty(), raid_heroic=88, availability={"raid_heroic": "available"})
    assert cache.put("Scout", "ravencrest", "EU", 71, ranks, metric_preferences=HEROIC,
                     expected_publication_epoch=0, publication_receipt=receipt)
    assert receipt == [1] and cache.publication_epoch == 2
    assert _eligible(cache, "scalar", expected=receipt[0]) is (intervening == "Other")


@pytest.mark.parametrize("manual", [False, True])
def test_rejected_publication_does_not_emit_a_receipt(cache, manual):
    assert cache.clear()
    if manual:
        receipts = {}
        assert not cache.put_raid_boss_details(
            "Scout", "ravencrest", "EU", 71,
            RaidBossDetails({"H": []}, availability={"H": "available"}),
            metric_preferences=HEROIC, expected_generation=0, publication_receipts=receipts,
        )
        assert receipts == {}
    else:
        receipt = []
        assert not cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(),
                             metric_preferences=HEROIC, expected_generation=0,
                             publication_receipt=receipt)
        assert receipt == []


def test_manual_partial_publication_receipt_stamps_only_accepted_scope(cache):
    started = cache.publication_epoch
    assert _publish(cache, "manual", value=99)
    rows = RaidBossDetails(
        {"H": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 10}],
         "M": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 88}]},
        availability={"H": "available", "M": "available"},
    )
    receipts = {}
    assert cache.put_raid_boss_details(
        "Scout", "ravencrest", "EU", 71, rows, metric_preferences=replace(HEROIC, raid_mythic=True),
        expected_publication_epoch=started, publication_receipts=receipts,
    )
    assert receipts == {"M": 2}
    assert rows["H"][0]["overall"] == 10
    current = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                         metric_preferences=replace(HEROIC, raid_mythic=True))
    assert current is not None and current["H"][0]["overall"] == 99
    assert current["M"][0]["overall"] == 88


def test_manual_negative_receipt_is_its_own_accepted_clock_before_another_negative(cache, monkeypatch):
    original_flush = cache.flush
    triggered = []
    def flush(*args, **kwargs):
        if not triggered:
            triggered.append(True)
            assert cache.put("Other", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
                             metric_preferences=HEROIC)
        return original_flush(*args, **kwargs)
    monkeypatch.setattr(cache, "flush", flush)
    receipts = {}
    assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                     RaidBossDetails(not_found=True), metric_preferences=HEROIC,
                                     expected_publication_epoch=0, publication_receipts=receipts)
    assert receipts == {"not_found": 1}
    assert cache.publication_epoch == 2


@pytest.mark.parametrize("intervening", ["Other", "Scout"])
def test_manual_receipt_is_exact_before_intervening_publication_and_eviction(cache, monkeypatch, intervening):
    cache.MAX_ENTRIES = 1
    original_flush = cache.flush
    triggered = []
    def flush(*args, **kwargs):
        if not triggered:
            triggered.append(True)
            assert _publish(cache, "manual", name=intervening, value=99)
        return original_flush(*args, **kwargs)
    monkeypatch.setattr(cache, "flush", flush)
    receipts = {}
    rows = RaidBossDetails({"H": [{"encounter_id": BOSS_ID, "name": "Boss", "overall": 88}]},
                          availability={"H": "available"})
    assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, rows,
                                     metric_preferences=HEROIC, expected_publication_epoch=0,
                                     publication_receipts=receipts)
    assert receipts == {"H": 1} and cache.publication_epoch == 2
    assert _eligible(cache, "manual", expected=receipts["H"]) is (intervening == "Other")


def test_cached_scalar_read_receipt_remains_valid_after_unrelated_eviction(cache):
    cache.MAX_ENTRIES = 1
    assert _publish(cache, "scalar", value=88)
    receipt = []
    ranks = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
                      publication_receipt=receipt)
    assert ranks is not None and ranks.raid_heroic == 88 and receipt == [1]
    assert _publish(cache, "scalar", name="Other")
    assert _eligible(cache, "scalar", expected=receipt[0])


def test_cached_scalar_read_receipt_comes_from_final_stable_snapshot(cache, monkeypatch):
    assert _publish(cache, "scalar", value=88)
    original = cache._ranks_from_entry
    changed = []
    def convert(entry):
        if not changed:
            changed.append(True)
            assert _publish(cache, "scalar", value=22)
        return original(entry)
    monkeypatch.setattr(cache, "_ranks_from_entry", convert)
    receipt = []
    ranks = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
                      publication_receipt=receipt)
    assert ranks is not None and ranks.raid_heroic == 22 and receipt == [2]


def test_cached_scalar_privacy_amendment_uses_read_watermark_without_renewing_other_numbers(cache):
    wide = replace(HEROIC, raid_mythic=True)
    assert _publish(cache, "scalar", preferences=wide, value=88)
    assert cache.put("Scout", "ravencrest", "EU", 71,
                     replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"}),
                     metric_preferences=HEROIC)
    receipt = []
    ranks = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=wide,
                      publication_receipt=receipt)
    assert ranks is not None and ranks.raid_heroic is None and ranks.raid_mythic == 88
    assert receipt == [2]


def test_cached_negative_read_receipt_is_exact_not_unrelated_read_watermark(cache):
    assert cache.put("Scout", "ravencrest", "EU", 71, CharacterRanks.empty(not_found=True),
                     metric_preferences=HEROIC)
    assert _publish(cache, "scalar", name="Other")
    receipt = []
    ranks = cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
                      publication_receipt=receipt)
    assert ranks is not None and ranks.not_found and cache.publication_epoch == 2
    assert receipt == [1]


def test_failed_cached_scalar_read_does_not_emit_receipt_after_generation_change(cache, monkeypatch):
    assert _publish(cache, "scalar")
    original = cache._ranks_from_entry
    def convert(entry):
        assert cache.clear()
        return original(entry)
    monkeypatch.setattr(cache, "_ranks_from_entry", convert)
    receipt = []
    assert cache.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
                     publication_receipt=receipt) is None
    assert receipt == []


def test_loaded_positive_cache_read_has_valid_zero_watermark(tmp_path):
    cache = CharacterCache(tmp_path)
    assert _publish(cache, "scalar")
    cache.close()
    loaded = CharacterCache(tmp_path)
    try:
        receipt = []
        ranks = loaded.get("Scout", "ravencrest", "EU", 71, metric_preferences=HEROIC,
                           publication_receipt=receipt)
        assert ranks is not None and ranks.raid_heroic == 88 and receipt == [0]
    finally:
        loaded.close()
