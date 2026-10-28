from __future__ import annotations

import pytest

from applicant_scout import scoring


@pytest.mark.parametrize("name", ["Kings' Rest", "Kings Rest", "KINGS' REST"])
def test_matching_activity_dungeon_does_not_scan_unrelated_encounters(monkeypatch, name):
    normalized = scoring.normalise_dungeon_name
    names = []

    def record_normalization(value):
        names.append(value)
        return normalized(value)

    monkeypatch.setattr(scoring, "normalise_dungeon_name", record_normalization)
    assert scoring._activity_dungeon_keys(512, name) == {"kingsrest"}
    assert set(names) <= {"Kings' Rest", name}


@pytest.mark.parametrize(
    ("activity", "name", "expected"),
    [
        (512, "Крепость Королей", {"kingsrest", "крепостькоролей"}),
        (512, "Ruby Life Pools", {"kingsrest"}),
        (512, "Mythic+", {"kingsrest"}),
        (512, None, {"kingsrest"}),
        (0, "Custom Dungeon", {"customdungeon"}),
        (0, None, set()),
    ],
)
def test_distinct_aliases_keep_activity_identity_rules(activity, name, expected):
    assert scoring._activity_dungeon_keys(activity, name) == expected
