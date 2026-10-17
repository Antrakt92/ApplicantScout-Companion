"""Provider integrity and raid identity boundaries for local kill evidence."""

import pytest

from applicant_scout.raiderio_local import (
    _ProviderMeta,
    _RaidInfo,
    _decode_raid_progress,
    _validate_raid_encoding_plan,
)


def _pack_fields(fields: list[tuple[int, int]]) -> bytes:
    value = offset = 0
    for number, width in fields:
        value |= number << offset
        offset += width
    return value.to_bytes((offset + 7) // 8, "little")


@pytest.mark.parametrize("duplicate_group", ["current", "previous"])
def test_duplicate_known_raid_ids_fail_closed(duplicate_group: str):
    # Duplicate provider entries otherwise report the same boss twice and
    # can overwrite the real count when the presenter keys by encounter ID.
    raid = _RaidInfo("The Tidebound Grotto", 1, 16671)
    current = [raid, raid] if duplicate_group == "current" else []
    previous = [raid, raid] if duplicate_group == "previous" else []
    with pytest.raises(ValueError, match="duplicate.*raid|raid.*duplicate"):
        _validate_raid_encoding_plan(_ProviderMeta(16, (1, 2)), current, previous)


def test_main_summary_is_never_reported_as_character_progress():
    raid = _RaidInfo("The Tidebound Grotto", 1, 16671)
    # The main has normal and heroic completion; the character has no kills.
    record = _pack_fields([(0, 2), (1, 4), (1, 2), (1, 4)])
    assert _decode_raid_progress(record, (4,), [raid], []) == {}


def test_unknown_previous_raid_consumes_full_width_before_known_lair():
    unknown = _RaidInfo("Older unrelated tier", 9, 99999)
    lair = _RaidInfo("The Tidebound Grotto", 1, 16671)
    fields = [(2, 2), *[(12, 5)] * 9, (1, 2), (5, 5)]
    progress = _decode_raid_progress(_pack_fields(fields), (2,), [], [unknown, lair])
    assert set(progress) == {"H"}
    assert progress["H"]["boss_encounter_ids"] == [3379]
    assert progress["H"]["boss_kills"] == [5]
    assert progress["H"]["killed"] == progress["H"]["total"] == 1


def test_current_identity_wins_over_previous_copy_without_double_counting():
    raid = _RaidInfo("The Tidebound Grotto", 1, 16671)
    # A current heroic count and empty alternate slot precede a stale previous
    # copy with a larger count. It must neither replace nor duplicate the row.
    fields = [(1, 2), (5, 5), (0, 2), (0, 5), (1, 2), (12, 5)]
    progress = _decode_raid_progress(_pack_fields(fields), (1, 2), [raid], [raid])
    assert progress["H"]["boss_encounter_ids"] == [3379]
    assert progress["H"]["boss_kills"] == [5]
    assert progress["H"]["killed"] == progress["H"]["total"] == 1
