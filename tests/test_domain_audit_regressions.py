"""Provider evidence must pass identical bounds with and without disk caching."""

from dataclasses import asdict

import pytest

from applicant_scout import raiderio_local, wcl
from test_raiderio_local import _record, _write_test_db


@pytest.mark.parametrize("invalid_key", [2**63, 10**400], ids=["int64-overflow", "float-overflow"])
def test_live_wcl_brackets_reject_integer_overflow_before_selecting_highest_key(invalid_key):
    perf = wcl._process_encounter_ranks(
        {"ranks": [
            {"spec": "Arms", "bracketData": 14, "rankPercent": 72.0},
            {"spec": "Arms", "bracketData": invalid_key, "rankPercent": 99.0},
        ]},
        "Arms", "Pit of Saron",
    )

    assert perf is not None
    assert perf.key_level == 14
    assert perf.parse_percent == 72.0
    assert [bracket.key_level for bracket in perf.brackets] == [14]
    assert wcl._dict_to_dungeon_perf(asdict(perf)) == perf


@pytest.mark.parametrize("disk_cache", [False, True])
@pytest.mark.parametrize("damage", ["truncated-lookup", "incomplete-characters"])
def test_incomplete_raiderio_source_is_unavailable_even_without_disk_cache(
    tmp_path, monkeypatch, disk_cache, damage,
):
    monkeypatch.setattr(
        raiderio_local, "_EXPECTED_RAIDERIO_DUNGEON_ORDER", ("Skyreach", "Pit of Saron"),
    )
    payload = _record(3200, 15, 14, 1, 0)
    if damage == "incomplete-characters":
        payload += _record(2800, 0, 12, 0, 2)
    _write_test_db(tmp_path, payload)
    if damage == "incomplete-characters":
        characters = tmp_path / "Interface/AddOns/RaiderIO/db/db_mythicplus_eu_characters.lua"
        with characters.open("a", encoding="utf-8") as source:
            source.write('provider.db["Incomplete"]={10,"Other"')
    reader = raiderio_local.RaiderIOLocalReader(
        tmp_path, cache_dir=tmp_path / "cache" if disk_cache else None,
    )

    assert reader.lookup_profile("Alphapack", "Ragnaros", "EU") is None
