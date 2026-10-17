"""Local raid evidence survives provider retirement and WCL state changes."""

from dataclasses import replace

import pytest

from applicant_scout import overlay_presenters
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.raiderio_local import _decode_raid_progress, _parse_provider_raids
from applicant_scout.state import Applicant


def _retired_lair_applicant(difficulty: str, raid_id: int) -> Applicant:
    other_lair_id = 17059 if raid_id == 16671 else 16671
    current = _parse_provider_raids(
        f"currentRaids={{{{id=16915,bossCount=8}},{{id={other_lair_id},bossCount=1}}}}",
        "currentRaids",
    )
    previous = _parse_provider_raids(
        f"previousRaids={{{{id={raid_id},bossCount=1}}}}", "previousRaids",
    )
    # Current records are empty; the previous raid carries five kills on the
    # supplied difficulty. All provider records are packed LSB first.
    offset = sum(2 * (2 + 5 * raid.boss_count) for raid in current)
    raw_difficulty = {"N": 0, "H": 1, "M": 2}[difficulty]
    packed = (raw_difficulty | (5 << 2)) << offset
    record = packed.to_bytes((offset + 7 + 7) // 8, "little")
    progress = _decode_raid_progress(record, (1, 2), current, previous)
    return Applicant(
        "1:1", "Scout-Ravencrest", "MAGE", 62, 322, 3304, "DAMAGER",
        rio_raid_progress=progress,
    )


@pytest.mark.parametrize("difficulty", ["N", "H", "M"])
@pytest.mark.parametrize("raid_id,name", [
    (16671, "Nymrissa Wavecaller"),
    (17059, "Kith'ix"),
])
def test_retired_lair_kills_reach_the_right_display_row_and_survive_wcl_reset(
    difficulty, raid_id, name,
):
    applicant = _retired_lair_applicant(difficulty, raid_id)
    applicant.clear_wcl_data(fetch_status="restricted")
    applicant.retract_private_raid_details({
        "raid_normal": "restricted", "raid_heroic": "restricted",
        "raid_mythic": "restricted",
    })
    applicant.project_wcl_data_to_preferences(MetricPreferences())

    rows = overlay_presenters.raid_boss_rows_for_display(applicant, ["N", "H", "M"])
    assert {row["name"]: row["rio_text"] for row in rows if row["rio_text"]} == {
        name: f"{difficulty}×5",
    }
    assert all(row["value"] == "" for row in rows)


@pytest.mark.parametrize("difficulty", ["N", "H", "M"])
def test_disabling_and_reenabling_wcl_difficulty_keeps_independent_local_kills(difficulty):
    applicant = _retired_lair_applicant(difficulty, 16671)
    preferences = MetricPreferences()
    field = {"N": "raid_normal", "H": "raid_heroic", "M": "raid_mythic"}[difficulty]
    applicant.project_wcl_data_to_preferences(replace(preferences, **{field: False}))
    assert all(row["rio_text"] == "" for row in overlay_presenters.raid_boss_rows_for_display(
        applicant, [key for key in ("N", "H", "M") if key != difficulty],
    ))
    applicant.project_wcl_data_to_preferences(preferences)
    rows = overlay_presenters.raid_boss_rows_for_display(applicant, [difficulty])
    assert next(row for row in rows if row["name"] == "Nymrissa Wavecaller")["rio_text"] == (
        f"{difficulty}×5"
    )
