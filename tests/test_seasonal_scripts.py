from __future__ import annotations

import pytest

from scripts.seasonal import get_mplus_encounter_ids


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
