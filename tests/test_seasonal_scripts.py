from __future__ import annotations

import pytest

from scripts.seasonal import (
    get_mplus_activity_ids,
    get_mplus_challenge_map_ids,
    get_mplus_encounter_ids,
)


def test_format_mplus_tuples_outputs_copyable_constants():
    zone = get_mplus_encounter_ids.extract_zone_payload(
        {
            "data": {
                "worldData": {
                    "zone": {
                        "id": 47,
                        "name": "Midnight Season 1",
                        "encounters": [
                            {"id": 112526, "name": "Algeth'ar Academy"},
                            {"id": 10658, "name": "Pit of Saron"},
                        ],
                    }
                }
            }
        }
    )

    text = get_mplus_encounter_ids.format_mplus_tuples(zone)

    assert "# WCL zone 47: Midnight Season 1" in text
    assert '("aa", 112526, "Algeth\'ar Academy"),' in text
    assert '("ps", 10658, "Pit of Saron"),' in text


def test_format_mplus_tuples_escapes_double_quotes():
    zone = get_mplus_encounter_ids.extract_zone_payload(
        {
            "data": {
                "worldData": {
                    "zone": {
                        "id": 47,
                        "name": "Test",
                        "encounters": [{"id": 1, "name": 'Vault "Prime"'}],
                    }
                }
            }
        }
    )

    text = get_mplus_encounter_ids.format_mplus_tuples(zone)

    assert '("vp", 1, "Vault \\"Prime\\""),' in text


def test_extract_zone_payload_rejects_graphql_errors():
    with pytest.raises(get_mplus_encounter_ids.SeasonalScriptError, match="boom"):
        get_mplus_encounter_ids.extract_zone_payload({"errors": [{"message": "boom"}]})


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"worldData": {"zone": None}}},
        {"data": {"worldData": {"zone": {"encounters": []}}}},
        {"data": {"worldData": {"zone": {"encounters": [{"id": True, "name": "X"}]}}}},
        {"data": {"worldData": {"zone": {"encounters": [{"id": 1, "name": ""}]}}}},
    ],
)
def test_extract_zone_payload_rejects_malformed_zone_data(payload):
    with pytest.raises(get_mplus_encounter_ids.SeasonalScriptError):
        get_mplus_encounter_ids.extract_zone_payload(payload)


def test_json_object_response_rejects_non_object_json():
    class Response:
        text = "[]"

        def json(self) -> object:
            return []

    with pytest.raises(get_mplus_encounter_ids.SeasonalScriptError, match="object"):
        get_mplus_encounter_ids.json_object_response(Response())


def _activity_csv(*rows: str) -> str:
    header = (
        "ID,FullName_lang,ShortName_lang,GroupFinderCategoryID,"
        "GroupFinderActivityGrpID,MapID,DifficultyID,ExpansionID,"
        "MaxPlayers,MapChallengeModeID"
    )
    return "\n".join((header, *rows))


def _challenge_map_csv(*rows: str) -> str:
    header = "Name_lang,ID,MapID"
    return "\n".join((header, *rows))


def _season_tracked_map_csv(*rows: str) -> str:
    header = "ID,MapChallengeModeID,DisplaySeasonID"
    return "\n".join((header, *rows))


def test_extract_mplus_activity_mapping_selects_current_group_with_keystone_row():
    csv_text = _activity_csv(
        '99,Magisters\' Terrace (Heroic),Heroic,2,20,585,2,1,5,0',
        '100,Magisters\' Terrace (Mythic),Mythic,2,20,585,23,1,5,0',
        '1757,Magisters\' Terrace (Normal),Normal,2,399,2811,1,11,5,0',
        '1758,Magisters\' Terrace (Heroic),Heroic,2,399,2811,2,11,5,0',
        '1759,Magisters\' Terrace (Mythic),Mythic,2,399,2811,23,11,5,0',
        '1760,Magisters\' Terrace (Mythic Keystone),Mythic+,2,399,2811,8,11,5,0',
    )

    mapping = get_mplus_activity_ids.extract_mplus_activity_mapping(
        csv_text, ["Magisters' Terrace"]
    )

    assert mapping == {
        1757: "Magisters' Terrace",
        1758: "Magisters' Terrace",
        1759: "Magisters' Terrace",
        1760: "Magisters' Terrace",
    }


def test_extract_mplus_activity_mapping_keeps_duplicate_same_name_groups():
    csv_text = _activity_csv(
        "484,Seat of the Triumvirate (Heroic),Heroic,2,133,1753,2,6,5,0",
        "485,Seat of the Triumvirate (Mythic),Mythic,2,133,1753,23,6,5,0",
        "486,Seat of the Triumvirate (Mythic Keystone),Mythic+,2,133,1753,8,6,5,0",
        "1622,Seat of the Triumvirate (Heroic),Heroic,2,133,1753,2,6,5,0",
        "1644,Seat of the Triumvirate (Mythic),Mythic,2,133,1753,23,6,5,0",
    )

    mapping = get_mplus_activity_ids.extract_mplus_activity_mapping(
        csv_text, ["Seat of the Triumvirate"]
    )

    assert mapping == {
        484: "Seat of the Triumvirate",
        485: "Seat of the Triumvirate",
        486: "Seat of the Triumvirate",
        1622: "Seat of the Triumvirate",
        1644: "Seat of the Triumvirate",
    }


def test_extract_mplus_activity_mapping_rejects_missing_columns():
    csv_text = "ID,FullName_lang\n1,Skyreach (Mythic Keystone)"

    with pytest.raises(get_mplus_activity_ids.SeasonalScriptError, match="columns"):
        get_mplus_activity_ids.extract_mplus_activity_mapping(csv_text, ["Skyreach"])


def test_extract_mplus_activity_mapping_rejects_bad_numeric_fields():
    csv_text = _activity_csv(
        "bad,Skyreach (Mythic Keystone),Mythic+,2,9,1209,8,5,5,0"
    )

    with pytest.raises(get_mplus_activity_ids.SeasonalScriptError, match="ID"):
        get_mplus_activity_ids.extract_mplus_activity_mapping(csv_text, ["Skyreach"])


def test_extract_mplus_activity_mapping_rejects_missing_keystone_group():
    csv_text = _activity_csv(
        "24,Skyreach (Normal),Normal,2,9,1209,1,5,5,0",
        "32,Skyreach (Heroic),Heroic,2,9,1209,2,5,5,0",
    )

    with pytest.raises(get_mplus_activity_ids.SeasonalScriptError, match="Skyreach"):
        get_mplus_activity_ids.extract_mplus_activity_mapping(csv_text, ["Skyreach"])


def test_extract_mplus_activity_mapping_rejects_conflicting_duplicate_ids():
    csv_text = _activity_csv(
        "182,Skyreach (Mythic Keystone),Mythic+,2,9,1209,8,5,5,0",
        "182,Pit of Saron (Mythic Keystone),Mythic+,2,52,658,8,2,5,0",
    )

    with pytest.raises(get_mplus_activity_ids.SeasonalScriptError, match="Duplicate"):
        get_mplus_activity_ids.extract_mplus_activity_mapping(
            csv_text, ["Skyreach", "Pit of Saron"]
        )


def test_format_activity_mapping_outputs_copyable_constants():
    text = get_mplus_activity_ids.format_activity_mapping(
        {182: "Skyreach", 404: "Skyreach", 1770: "Pit of Saron"}
    )

    assert "MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME" in text
    assert '182: "Skyreach",' in text
    assert '404: "Skyreach",' in text
    assert '1770: "Pit of Saron",' in text


def test_extract_mplus_challenge_map_mapping_selects_latest_tracked_season():
    challenge_csv = _challenge_map_csv(
        "Skyreach,161,1209",
        "Pit of Saron,556,658",
        "Old Dungeon,999,1",
    )
    tracked_csv = _season_tracked_map_csv(
        "1,999,33",
        "2,161,34",
        "3,556,34",
    )

    mapping = get_mplus_challenge_map_ids.extract_mplus_challenge_map_mapping(
        challenge_csv, tracked_csv, ["Skyreach", "Pit of Saron"]
    )

    assert mapping == {
        161: "Skyreach",
        556: "Pit of Saron",
    }


def test_extract_mplus_challenge_map_mapping_rejects_missing_columns():
    challenge_csv = "ID,Name_lang\n161,Skyreach"
    tracked_csv = _season_tracked_map_csv("1,161,34")

    with pytest.raises(
        get_mplus_challenge_map_ids.SeasonalScriptError, match="columns"
    ):
        get_mplus_challenge_map_ids.extract_mplus_challenge_map_mapping(
            challenge_csv, tracked_csv, ["Skyreach"]
        )


def test_extract_mplus_challenge_map_mapping_rejects_missing_current_dungeon():
    challenge_csv = _challenge_map_csv("Skyreach,161,1209")
    tracked_csv = _season_tracked_map_csv("1,161,34")

    with pytest.raises(
        get_mplus_challenge_map_ids.SeasonalScriptError, match="Pit of Saron"
    ):
        get_mplus_challenge_map_ids.extract_mplus_challenge_map_mapping(
            challenge_csv, tracked_csv, ["Skyreach", "Pit of Saron"]
        )


def test_extract_mplus_challenge_map_mapping_rejects_unknown_tracked_dungeon():
    challenge_csv = _challenge_map_csv(
        "Skyreach,161,1209",
        "Old Dungeon,999,1",
    )
    tracked_csv = _season_tracked_map_csv(
        "1,161,34",
        "2,999,34",
    )

    with pytest.raises(
        get_mplus_challenge_map_ids.SeasonalScriptError, match="Old Dungeon"
    ):
        get_mplus_challenge_map_ids.extract_mplus_challenge_map_mapping(
            challenge_csv, tracked_csv, ["Skyreach"]
        )


def test_format_challenge_map_mapping_outputs_copyable_constants():
    text = get_mplus_challenge_map_ids.format_challenge_map_mapping(
        {161: "Skyreach", 556: "Pit of Saron"}
    )

    assert "MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME" in text
    assert '161: "Skyreach",' in text
    assert '556: "Pit of Saron",' in text
