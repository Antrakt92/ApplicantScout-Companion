from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from applicant_scout.constants import (
    CURRENT_MPLUS_ZONE_ID,
    CURRENT_RAID_ENCOUNTERS,
    CURRENT_RAID_ENCOUNTER_ZONE_IDS,
    CURRENT_RAID_ZONE_ID,
    MPLUS_ENCOUNTERS,
)
from scripts.seasonal import (
    get_mplus_activity_ids,
    get_mplus_challenge_map_ids,
    get_mplus_encounter_ids,
    verify_wcl_season,
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


def _wcl_zone(
    zone_id: int,
    encounters: list[tuple[int, str]],
) -> dict[str, object]:
    return {
        "id": zone_id,
        "name": f"Test zone {zone_id}",
        "encounters": [
            {"id": encounter_id, "name": name}
            for encounter_id, name in encounters
        ],
    }


def _valid_wcl_season_payload() -> dict[str, Any]:
    # This deliberate season-shaped fixture must be updated when WCL moves the
    # shipped boss-detail encounters to different zones.
    assert CURRENT_RAID_ENCOUNTER_ZONE_IDS == (CURRENT_RAID_ZONE_ID, 50)
    raid_rows = [
        (encounter_id, name)
        for _alias, encounter_id, name in CURRENT_RAID_ENCOUNTERS
    ]
    assert raid_rows[-1][0] == 3159
    return {
        "data": {
            "rateLimitData": {
                "limitPerHour": 3600.0,
                "pointsSpentThisHour": 25.5,
                "pointsResetIn": 1200,
            },
            "worldData": {
                f"zone_{CURRENT_MPLUS_ZONE_ID}": _wcl_zone(
                    CURRENT_MPLUS_ZONE_ID,
                    [
                        (encounter_id, name)
                        for _alias, encounter_id, name in MPLUS_ENCOUNTERS
                    ],
                ),
                f"zone_{CURRENT_RAID_ZONE_ID}": _wcl_zone(
                    CURRENT_RAID_ZONE_ID,
                    raid_rows[:-1],
                ),
                "zone_50": _wcl_zone(50, raid_rows[-1:]),
            },
        }
    }


@pytest.mark.parametrize("zone_ids", [(), (47, 47), (True,), (0,), ("47",)])
def test_wcl_season_query_rejects_unsafe_zone_ids(zone_ids):
    with pytest.raises(verify_wcl_season.SeasonalWCLVerificationError):
        verify_wcl_season.build_query(zone_ids)


def test_wcl_season_query_requests_quota_and_each_zone_once():
    zone_ids = verify_wcl_season.seasonal_zone_ids()

    query = verify_wcl_season.build_query(zone_ids)

    assert "rateLimitData" in query
    assert "pointsSpentThisHour" in query
    for zone_id in zone_ids:
        assert query.count(f"zone_{zone_id}: zone(id: {zone_id})") == 1


def test_wcl_season_payload_matches_current_constants():
    zone_ids = verify_wcl_season.seasonal_zone_ids()

    zones, quota = verify_wcl_season.extract_payload(
        _valid_wcl_season_payload(), zone_ids
    )
    verify_wcl_season.validate_current_constants(zones)
    verify_wcl_season.require_quota_floor(quota, 50.0)

    assert quota.remaining_points == pytest.approx(3574.5)


def test_wcl_season_payload_rejects_stale_encounter_name():
    payload = _valid_wcl_season_payload()
    world_data = payload["data"]["worldData"]
    world_data[f"zone_{CURRENT_MPLUS_ZONE_ID}"]["encounters"][0]["name"] = "Stale"

    zones, _quota = verify_wcl_season.extract_payload(
        payload, verify_wcl_season.seasonal_zone_ids()
    )
    with pytest.raises(
        verify_wcl_season.SeasonalWCLVerificationError,
        match="M\\+ encounter constants are stale",
    ):
        verify_wcl_season.validate_current_constants(zones)


def test_wcl_season_payload_rejects_duplicate_encounter_ids():
    payload = _valid_wcl_season_payload()
    world_data = payload["data"]["worldData"]
    encounters = world_data[f"zone_{CURRENT_MPLUS_ZONE_ID}"]["encounters"]
    encounters.append({"id": encounters[0]["id"], "name": "Other name"})

    with pytest.raises(
        verify_wcl_season.SeasonalWCLVerificationError,
        match="duplicate encounter IDs",
    ):
        verify_wcl_season.extract_payload(
            payload, verify_wcl_season.seasonal_zone_ids()
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("limitPerHour", float("nan")),
        ("pointsSpentThisHour", True),
        ("pointsResetIn", 1.5),
    ],
)
def test_wcl_season_payload_rejects_invalid_quota(field, value):
    payload = _valid_wcl_season_payload()
    payload["data"]["rateLimitData"][field] = value

    with pytest.raises(verify_wcl_season.SeasonalWCLVerificationError):
        verify_wcl_season.extract_payload(
            payload, verify_wcl_season.seasonal_zone_ids()
        )


def test_wcl_season_quota_floor_fails_closed():
    quota = verify_wcl_season.QuotaSnapshot(100.0, 60.1, 300)

    with pytest.raises(
        verify_wcl_season.SeasonalWCLVerificationError,
        match="below required floor",
    ):
        verify_wcl_season.require_quota_floor(quota, 40.0)


def test_wcl_season_main_refuses_before_loading_config(monkeypatch):
    def unexpected_load():
        pytest.fail("load_config must not run without explicit quota confirmation")

    monkeypatch.setattr(verify_wcl_season, "load_config", unexpected_load)

    with pytest.raises(
        verify_wcl_season.SeasonalWCLVerificationError,
        match="Refusing live WCL query",
    ):
        verify_wcl_season.main([])


def test_wcl_season_main_executes_one_confirmed_query(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        verify_wcl_season,
        "load_config",
        lambda: SimpleNamespace(
            wcl_client_id="client",
            wcl_client_secret="secret",
            cache_dir=tmp_path,
        ),
    )

    class FakeAuth:
        def __init__(self, client_id, client_secret, cache_dir):
            assert (client_id, client_secret, cache_dir) == (
                "client",
                "secret",
                tmp_path,
            )

        def get_token(self):
            return "token"

    calls: list[tuple[str, str]] = []

    def fake_fetch(token, query):
        calls.append((token, query))
        return _valid_wcl_season_payload()

    monkeypatch.setattr(verify_wcl_season, "WCLAuth", FakeAuth)
    monkeypatch.setattr(verify_wcl_season, "fetch_live_payload", fake_fetch)

    assert verify_wcl_season.main(["--confirm-spend-wcl-quota"]) == 0
    assert len(calls) == 1
    assert calls[0][0] == "token"
    assert "WCL quota after check" in capsys.readouterr().out
