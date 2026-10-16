"""Live Kith'ix evidence must reach queries, coverage, and manual detail rows."""

import pytest

from applicant_scout import overlay_presenters
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import Applicant
from applicant_scout.raiderio_local import _decode_raid_progress, _parse_provider_raids
from applicant_scout.wcl import (
    _build_raid_boss_detail_query,
    _raid_boss_rows_from_character,
    _ranks_from_graphql,
)


HEROIC_ONLY = MetricPreferences(
    mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=False,
)


def test_live_kithix_is_queried_and_displayed_without_inventing_missing_progress():
    query = _build_raid_boss_detail_query("DAMAGER", HEROIC_ONLY)
    assert "raid_h_ki_overall: encounterRankings(encounterID: 3513, difficulty: 4" in query
    assert "raid_h_ki_ilvl: encounterRankings(encounterID: 3513, difficulty: 4" in query

    details = _raid_boss_rows_from_character({
        "raid_h_ki_overall": {"ranks": [{"rankPercent": 81, "spec": "Arms"}]},
        "raid_h_ki_ilvl": {"ranks": [{"rankPercent": 93, "spec": "Arms"}]},
    }, "H", spec_name="Arms")
    assert details == [{
        "encounter_id": 3513, "name": "Kith'ix", "overall": 81, "ilvl": 93,
    }]
    applicant = Applicant("1:1", "Scout-Realm", "WARRIOR", 71, 300, 3000, "DAMAGER",
                          raid_boss_parses={"H": details})
    row = overlay_presenters.raid_boss_rows_for_display(applicant, ["H"])[-1]
    assert row["name"] == "Kith'ix"
    assert "81" in row["value"] and "93" in row["value"]
    assert row["rio_text"] == ""


def test_live_kithix_counts_toward_logged_coverage_for_applying_spec_only():
    ranks = _ranks_from_graphql({"data": {"characterData": {"character": {
        "raidHeroic": {"rankings": [
            {"encounter": {"id": 3513}, "spec": "Arms", "rankPercent": 81, "totalKills": 2},
            {"encounter": {"id": 3513}, "spec": "Fury", "rankPercent": 99, "totalKills": 8},
        ]},
    }}}}, metric_preferences=HEROIC_ONLY, spec_name="Arms")
    assert ranks.raid_evidence == {"H": {
        "boss_count": 1, "total_bosses": 10, "sample_known": True, "kill_count": 2,
    }}


def test_current_raiderio_lair_progress_never_moves_to_retired_nymrissa():
    # Current RaiderIO metadata puts Kith'ix directly after the eight-boss raid;
    # Tidebound Grotto is now previousRaids, unlike WCL's combined zone roster.
    raids = _parse_provider_raids('currentRaids={'
        '{id=16915,name="The Venomous Abyss",bossCount=8},'
        '{id=17059,name="The Unbinding of Kith\'ix",bossCount=1}}', "currentRaids")
    # Two records per raid: Normal zero, Heroic zero for main raid; Normal zero,
    # Heroic three kills for Kith'ix. Integers are packed least-significant bit first.
    bits = 0
    offset = 0
    for difficulty, kills in [(0, [0] * 8), (1, [0] * 8), (0, [0]), (1, [3])]:
        bits |= difficulty << offset
        offset += 2
        for count in kills:
            bits |= count << offset
            offset += 5
    progress = _decode_raid_progress(bits.to_bytes((offset + 7) // 8, "little"), (1,), raids, [])
    assert progress["H"]["killed"] == 1
    assert progress["H"]["total"] == 9
    applicant = Applicant("1:1", "Scout-Realm", "WARRIOR", 71, 300, 3000, "DAMAGER",
                          rio_raid_progress=progress)
    rows = {row["name"]: row for row in overlay_presenters.raid_boss_rows_for_display(applicant, ["H"])}
    assert rows["Kith'ix"]["rio_text"] == "H×3"
    assert rows["Nymrissa Wavecaller"]["rio_text"] == ""


@pytest.mark.parametrize("raid_id,boss_count,expected", [
    (16671, 1, {"Nymrissa Wavecaller": "H×3"}),
    (17059, 1, {"Kith'ix": "H×3"}),
    (99999, 1, {}),
    (0, 1, {}),
    (17059, 2, {}),
])
def test_raiderio_lair_identity_controls_progress_even_before_main_raid(raid_id, boss_count, expected):
    raids = _parse_provider_raids(f'currentRaids={{{{id={raid_id},name="Lair",bossCount={boss_count}}},'
                                  '{id=16915,name="Main raid",bossCount=8}}', "currentRaids")
    bits = offset = 0
    for difficulty, kills in [(0, [0] * boss_count), (1, [3] * boss_count),
                              (0, [0] * 8), (1, [0] * 8)]:
        bits |= difficulty << offset
        offset += 2
        for count in kills:
            bits |= count << offset
            offset += 5
    progress = _decode_raid_progress(bits.to_bytes((offset + 7) // 8, "little"), (1,), raids, [])
    applicant = Applicant("1:1", "Scout-Realm", "MAGE", 62, 300, 3000, "DAMAGER",
                          rio_raid_progress=progress)
    actual = {row["name"]: row["rio_text"]
              for row in overlay_presenters.raid_boss_rows_for_display(applicant, ["H"])
              if row["rio_text"]}
    assert actual == expected


@pytest.mark.parametrize("progress", [
    {"boss_kills": [3]},
    {"boss_kills": [3], "boss_encounter_ids": []},
    {"boss_kills": [3, 5], "boss_encounter_ids": [3513, 3513]},
    {"boss_kills": [3], "boss_encounter_ids": [True]},
    {"boss_kills": [3], "boss_encounter_ids": [float("inf")]},
    {"boss_kills": [float("inf")], "boss_encounter_ids": [3513]},
    {"boss_kills": [True], "boss_encounter_ids": [3513]},
])
def test_missing_or_malformed_boss_identity_never_borrows_positional_progress(progress):
    applicant = Applicant("1:1", "Scout-Realm", "MAGE", 62, 300, 3000, "DAMAGER",
                          rio_raid_progress={"H": progress})
    assert all(row["rio_text"] == "" for row in
               overlay_presenters.raid_boss_rows_for_display(applicant, ["H"]))


@pytest.mark.parametrize("fields", [
    "id=17059.5", "invalid=17059", "id=17059junk", "id=17059,id=16671",
    'name="Lair,id=17059"', 'id="17059"', "id=true", "id=17059e0",
])
def test_provider_identity_requires_one_complete_integer_field(fields):
    raids = _parse_provider_raids(f"currentRaids={{{{{fields},bossCount=1}}}}", "currentRaids")
    assert raids[0].raid_id == 0


@pytest.mark.parametrize("fields", [
    "bossCount=1.5", "invalidbossCount=1", "bossCount=1junk", "bossCount=1,bossCount=8",
    'name="Lair,bossCount=1"', "bossCount=0", "bossCount=99999999999999999999999",
])
def test_invalid_boss_count_cannot_shift_following_provider_records(fields):
    with pytest.raises(ValueError, match="bossCount"):
        _parse_provider_raids("currentRaids={{id=17059," + fields + "}}", "currentRaids")


def test_raiderio_main_raid_order_keeps_lost_explorers_before_vashnik():
    raids = _parse_provider_raids('currentRaids={{id=16915,bossCount=8}}', "currentRaids")
    # Heroic third boss has three kills, fourth boss has five; second scope empty.
    bits = 1 | (3 << (2 + 2 * 5)) | (5 << (2 + 3 * 5))
    progress = _decode_raid_progress(bits.to_bytes(11, "little"), (1,), raids, [])
    applicant = Applicant("1:1", "Scout-Realm", "MAGE", 62, 300, 3000, "DAMAGER",
                          rio_raid_progress=progress)
    rows = {row["name"]: row for row in overlay_presenters.raid_boss_rows_for_display(applicant, ["H"])}
    assert rows["The Lost Explorers"]["rio_text"] == "H×3"
    assert rows["Vashnik the Malignant"]["rio_text"] == "H×5"
