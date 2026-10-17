"""Seasonal lairs retain their boss evidence after provider roster transitions."""

import pytest

from applicant_scout.overlay_presenters import raid_boss_rows_for_display
from applicant_scout.raiderio_local import _RaidInfo, _decode_raid_progress
from applicant_scout.state import Applicant


def _pack(rows):
    bits = offset = 0
    for difficulty, counts in rows:
        bits |= difficulty << offset
        offset += 2
        for count in counts:
            bits |= count << offset
            offset += 5
    return bits.to_bytes((offset + 7) // 8, "little")


@pytest.mark.parametrize("difficulty,label", [(0, "N"), (1, "H"), (2, "M")])
def test_previous_seasonal_lair_survives_without_current_progress(difficulty, label):
    progress = _decode_raid_progress(
        _pack([(difficulty, [5])]), (2,), [],
        [_RaidInfo("The Tidebound Grotto", 1, 16671)],
    )
    assert progress[label]["boss_encounter_ids"] == [3379]
    assert progress[label]["boss_kills"] == [5]
    assert progress[label]["killed"] == progress[label]["total"] == 1
    app = Applicant("1:1", "Scout-Realm", "MAGE", 62, 322, 3304, "DAMAGER",
                    rio_raid_progress=progress)
    rows = {row["name"]: row for row in raid_boss_rows_for_display(app, ["N", "H", "M"])}
    assert rows["Nymrissa Wavecaller"]["rio_text"] == f"{label}×5"
    assert rows["Kith'ix"]["rio_text"] == ""


@pytest.mark.parametrize("order", [(1, 2, 3, 4), (3, 2, 4, 1)])
def test_previous_lair_is_mapped_without_old_tier_or_summary_contamination(order):
    current = [_RaidInfo("Main", 8, 16915), _RaidInfo("Kith'ix", 1, 17059)]
    previous = [_RaidInfo("Old tier", 9, 16340), _RaidInfo("Nymrissa", 1, 16671)]
    # Summary records are six bits each and must never become character kills.
    blocks = {
        1: [(1, [5, 5, 4, 4, 4, 4, 2, 2]), (2, [2] + [0] * 7),
            (0, [0]), (1, [3])],
        2: [(2, [12] * 9), (1, [5])],
    }
    bits = offset = 0
    for field in order:
        if field in blocks:
            for difficulty, counts in blocks[field]:
                bits |= difficulty << offset
                offset += 2
                for count in counts:
                    bits |= count << offset
                    offset += 5
        else:
            width = 24
            bits |= ((1 << width) - 1) << offset
            offset += width
    progress = _decode_raid_progress(bits.to_bytes((offset + 7) // 8, "little"),
                                     order, current, previous)
    assert progress["H"]["boss_kills"] == [5, 5, 4, 4, 4, 4, 2, 2, 3, 5]
    assert progress["H"]["boss_encounter_ids"] == [3470, 3445, 3497, 3455, 3420, 3421,
                                                   3429, 3492, 3513, 3379]
    assert progress["H"]["total"] == 10
    assert progress["M"]["killed"] == 1
    assert "Old tier" not in progress["M"]["raid_name"]
    assert "N" not in progress


@pytest.mark.parametrize("raid", [_RaidInfo("Unknown", 1, 99999),
                                  _RaidInfo("Changed roster", 2, 16671)])
def test_unmapped_previous_raids_cannot_supply_seasonal_progress(raid):
    assert _decode_raid_progress(_pack([(1, [5] * raid.boss_count)]), (2,), [], [raid]) == {}
