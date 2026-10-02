import pytest

from applicant_scout import scoring, wcl
from applicant_scout.metric_preferences import MetricPreferences


@pytest.mark.parametrize("value", ["9" * 5000, 10**400, 2**63, str(2**63)],
                         ids=["oversized-string", "oversized-integer", "limit-int", "limit-string"])
def test_oversized_integer_evidence_is_rejected_locally(value):
    assert wcl._safe_nonnegative_cache_int(value) == 0
    assert scoring.nonnegative_int(value) == 0
    assert scoring.positive_int(value) == 0


@pytest.mark.parametrize("value, expected", [(0, 0), (42, 42), ("42", 42), (2**63 - 1, 2**63 - 1)])
def test_bounded_integer_evidence_keeps_normal_values(value, expected):
    assert wcl._safe_nonnegative_cache_int(value) == expected
    assert scoring.nonnegative_int(value) == expected


def test_oversized_percentage_does_not_abort_scoring():
    assert scoring.safe_percent(10**400) is None
    assert scoring.safe_percent("85.5") == 85.5


def test_malformed_boss_identifier_does_not_abort_other_cached_rows(tmp_path):
    cache = wcl.CharacterCache(tmp_path)
    prefs = MetricPreferences(mplus=False, raid_heroic=True)
    cache.put_raid_boss_details(
        "Scout", "ravencrest", "EU", 71,
        {"H": [
            {"encounter_id": "9" * 5000, "name": "Malformed", "overall": 99.0, "ilvl": None},
            {"encounter_id": 3445, "name": "Valid", "overall": 85.0, "ilvl": None},
        ]},
        role="DAMAGER", metric_preferences=prefs,
    )
    result = cache.get_raid_boss_details(
        "Scout", "ravencrest", "EU", 71, "DAMAGER", metric_preferences=prefs
    )
    assert result is not None
    assert result["H"] == [{"encounter_id": 3445, "name": "Valid", "overall": 85.0, "ilvl": None}]
