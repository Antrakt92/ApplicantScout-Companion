"""Unit tests for group_id_colour helper."""

from __future__ import annotations

import re

from applicant_scout.constants import (
    MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME,
    MPLUS_ENCOUNTERS,
    SPEC_ID_TO_WCL_NAME,
    SPEC_SHORT_NAMES,
    group_id_colour,
    mplus_dungeon_name_for_activity_id,
)


def test_returns_lowercase_hex_color():
    color = group_id_colour("42")
    assert re.match(r"^#[0-9a-f]{6}$", color)


def test_deterministic_same_input_same_output():
    assert group_id_colour("42") == group_id_colour("42")
    assert group_id_colour("0") == group_id_colour("0")
    assert group_id_colour("999999999") == group_id_colour("999999999")


def test_distinct_aids_typically_distinct_colors():
    """Hash-spread sanity: 10 distinct aids should produce ≥7 distinct hues.
    Some collisions OK (Pr ~= n*(n-1)/(2*360) for small n); total uniformity
    not the point. Catastrophic collapse to 1-2 colours WOULD signal a hash
    bug (multiplicative-constant zero, mod-1 collapse, etc.)."""
    colors = {group_id_colour(str(i)) for i in range(10)}
    assert len(colors) >= 7


def test_non_numeric_aid_falls_back_gracefully():
    """Defensive: helper must not raise on unexpected non-numeric input."""
    color = group_id_colour("bogus")
    assert re.match(r"^#[0-9a-f]{6}$", color)


def test_empty_aid_falls_back_gracefully():
    """Empty string defensive path — exercises the `if raw_aid else 0` branch."""
    color = group_id_colour("")
    assert re.match(r"^#[0-9a-f]{6}$", color)


def test_large_int_aid_does_not_overflow():
    """[B-7] Knuth multiplicative hash mask keeps the value 32-bit before
    mod-360. Without the mask, very large aids could behave non-classically.
    Pin: large aid hashes to a valid 6-hex-char colour."""
    color = group_id_colour("999999999999")
    assert re.match(r"^#[0-9a-f]{6}$", color)


def test_duplicate_spec_names_stay_class_neutral():
    """Class colour and role icon disambiguate the row; the spec text stays
    focused on the specialization name the user should inspect."""
    assert SPEC_SHORT_NAMES[65] == "Holy"
    assert SPEC_SHORT_NAMES[257] == "Holy"
    assert SPEC_SHORT_NAMES[66] == "Prot"
    assert SPEC_SHORT_NAMES[73] == "Prot"
    assert SPEC_SHORT_NAMES[105] == "Resto"
    assert SPEC_SHORT_NAMES[264] == "Resto"
    assert SPEC_SHORT_NAMES[251] == "Frost"


def test_devourer_spec_mapping_is_known():
    assert SPEC_SHORT_NAMES[1480] == "Devour"
    assert SPEC_ID_TO_WCL_NAME[1480] == "Devourer"


def test_mplus_activity_id_mapping_covers_current_season_dungeons():
    mapped_names = {
        mplus_dungeon_name_for_activity_id(activity_id)
        for activity_id in (
            24,
            115,
            484,
            1157,
            1539,
            1757,
            1761,
            1765,
        )
    }
    encounter_names = {name for _alias, _encounter_id, name in MPLUS_ENCOUNTERS}

    assert mapped_names == encounter_names


def test_all_mplus_activity_id_mapping_names_are_current_encounters():
    encounter_names = {name for _alias, _encounter_id, name in MPLUS_ENCOUNTERS}

    assert set(MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME.values()) <= encounter_names


def test_each_current_mplus_encounter_has_activity_id_mapping():
    encounter_names = {name for _alias, _encounter_id, name in MPLUS_ENCOUNTERS}
    mapped_names = set(MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME.values())

    assert encounter_names <= mapped_names


def test_mplus_activity_id_mapping_rejects_non_numeric_values():
    assert mplus_dungeon_name_for_activity_id(True) == ""
    assert mplus_dungeon_name_for_activity_id("bogus") == ""
