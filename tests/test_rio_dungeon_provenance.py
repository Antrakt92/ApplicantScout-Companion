"""Compact same-dungeon evidence keeps its captured dungeon identity."""
from __future__ import annotations

import pytest

from applicant_scout import scoring
from applicant_scout.state import Applicant, Listing


def _row(*, activity_id=0, dungeon="", target=12, raw_key=16, named=None):
    row = Applicant("1:1", "Mage-Realm", "MAGE", 62, 200, 0, "DAMAGER",
                    rio_profile=True, rio_summary_target_key=target,
                    rio_best_dungeon_key=raw_key, rio_dungeons=named or [])
    row.rio_summary_activity_id = activity_id
    row.rio_summary_dungeon_name = dungeon
    return row


def _listing(*, activity_id=0, dungeon="Dungeon B", target=12):
    return Listing(activity_id, dungeon, "Listing", "", key_level=target, category_id=2)


@pytest.mark.parametrize("capture", ["", "Dungeon A", "Mythic+"])
def test_unknown_or_different_capture_cannot_rebind_raw_dungeon_key(capture):
    row = _row(dungeon=capture)
    target = _listing()
    assert scoring._rio_same_dungeon_key(row, target) == 0
    assert scoring._mplus_rio_dungeon_key_levels(row, target) == {}
    assert scoring.candidate_fit(row, target).same_dungeon_rio_key == 0


def test_matching_capture_name_accepts_raw_evidence():
    row = _row(dungeon="Dungeon B")
    target = _listing(dungeon="DUNGEON-B")
    assert scoring._rio_same_dungeon_key(row, target) == 16
    assert scoring._mplus_rio_dungeon_key_levels(row, target) == {"dungeonb": 16}


def test_generic_capture_name_does_not_authorize_generic_target():
    assert scoring._rio_same_dungeon_key(_row(dungeon="Mythic+"), _listing(dungeon="Mythic+")) == 0


def test_same_positive_activity_retains_identity_without_known_mapping():
    row = _row(activity_id=999999, dungeon="Dungeon B")
    assert scoring._rio_same_dungeon_key(row, _listing(activity_id=999999)) == 16


@pytest.mark.parametrize("capture_name,target_name", [
    ("Mythic+", "Mythic+"), ("", "Dungeon B"),
    ("Dungeon B", "?"), ("Unknown", "Dungeon B"),
])
def test_same_unmapped_activity_cannot_authorize_unknown_dungeon(capture_name, target_name):
    row = _row(activity_id=999999, dungeon=capture_name)
    target = _listing(activity_id=999999, dungeon=target_name)
    assert scoring._rio_same_dungeon_key(row, target) == 0
    assert scoring._mplus_rio_dungeon_key_levels(row, target) == {}


def test_canonical_activity_aliases_match_localized_capture():
    row = _row(activity_id=503, dungeon="Localized capture")
    target = _listing(activity_id=504, dungeon="Other localized text")
    assert scoring._rio_same_dungeon_key(row, target) == 16
    assert scoring._mplus_rio_dungeon_key_levels(row, target) == {"templeofsethraliss": 16}


def test_conflicting_known_activity_cannot_match_incidental_raw_name():
    row = _row(activity_id=512, dungeon="Temple of Sethraliss")
    target = _listing(activity_id=503, dungeon="Temple of Sethraliss")
    assert scoring._rio_same_dungeon_key(row, target) == 0
    assert scoring._mplus_rio_dungeon_key_levels(row, target) == {}


def test_matching_dungeon_still_requires_matching_capture_key():
    row = _row(dungeon="Dungeon B", target=12)
    assert scoring._rio_same_dungeon_key(row, _listing(target=14)) == 0


def test_named_dungeon_records_remain_independent_of_raw_capture():
    row = _row(dungeon="Dungeon A", named=[{"name": "Dungeon B", "key_level": 13}])
    target = _listing()
    assert scoring._rio_same_dungeon_key(row, target) == 13
    assert scoring._mplus_rio_dungeon_key_levels(row, target) == {"dungeonb": 13}


def test_synthetic_counts_do_not_fallback_to_unverified_raw_dungeon_best():
    row = _row(raw_key=20)
    assert scoring._mplus_synthetic_rio_key_levels(row, 12, same_dungeon_key=0) == []


def test_generic_overall_best_and_key_relative_counts_remain_usable():
    row = _row(dungeon="Dungeon A", raw_key=20)
    row.rio_best_key = 15
    row.rio_timed_at_or_above = 2
    levels = scoring._mplus_rio_key_levels(row, 12, 0, rio_row_key_levels=[])
    assert levels == [15, 12]
