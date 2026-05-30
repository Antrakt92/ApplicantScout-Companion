"""Unit tests for wcl.py pure functions (no I/O, no network, no Qt)."""

from __future__ import annotations

import json
import stat
import threading
import time

import httpx
import pytest

from applicant_scout import atomic_io
import applicant_scout.wcl as wcl_mod
from applicant_scout.constants import (
    CURRENT_RAID_ENCOUNTERS,
    MPLUS_ENCOUNTERS,
    REGION_ID_TO_WCL,
)
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.wcl import (
    CharacterCache,
    CharacterRanks,
    DungeonPerf,
    KeyBracketPerf,
    RateLimitInfo,
    _RU_REALM_MAP_LOWER,
    WCL_ERROR_AUTH,
    WCL_ERROR_GRAPHQL,
    WCL_ERROR_QUOTA_GUARD,
    WCL_ERROR_MALFORMED,
    WCL_ERROR_NETWORK,
    WCL_ERROR_RATE_LIMITED,
    WCL_ERROR_SERVER,
    WCLAuth,
    WCLAuthError,
    WCL_API_URL,
    WCLApiError,
    WCLClient,
    _CACHE_VERSION,
    _build_character_ranks_query,
    _build_raid_boss_detail_query,
    _compute_mplus_headline,
    _dict_to_dungeon_perf,
    _process_encounter_ranks,
    _raid_boss_rows_from_character,
    _spec_norm,
    _zone_avg,
    derive_server_slug,
)


def _dp(name="X", best=80.0, median=70.0, key=12, runs=3):
    """DungeonPerf factory with sensible defaults — keeps test cases self-doc."""
    return DungeonPerf(
        name=name,
        parse_percent=best,
        median_percent=median,
        key_level=key,
        run_count=runs,
    )


# ─── region mapping ───────────────────────────────────────────────────────────


def test_wow_region_ids_map_to_wcl_server_region_tokens():
    assert REGION_ID_TO_WCL[1] == "US"
    assert REGION_ID_TO_WCL[3] == "EU"


# ─── _spec_norm ───────────────────────────────────────────────────────────────


def test_spec_norm_multi_word():
    assert _spec_norm("Beast Mastery") == "beastmastery"


def test_spec_norm_single_word():
    assert _spec_norm("Brewmaster") == "brewmaster"


def test_spec_norm_non_str_returns_empty():
    assert _spec_norm(None) == ""  # type: ignore[arg-type]
    assert _spec_norm(123) == ""  # type: ignore[arg-type]


def test_spec_norm_strips_all_spaces_and_lowers():
    # replace(" ", "") strips ALL spaces including leading/trailing.
    assert _spec_norm(" FROST ") == "frost"


# ─── derive_server_slug ───────────────────────────────────────────────────────


def test_derive_server_slug_empty():
    assert derive_server_slug("") == ""
    assert derive_server_slug("   ") == ""


def test_derive_server_slug_simple_latin():
    assert derive_server_slug("Ravencrest") == "ravencrest"


def test_derive_server_slug_multi_word_latin():
    assert derive_server_slug("Twisting Nether") == "twisting-nether"


def test_derive_server_slug_strips_apostrophes():
    # Both straight and curly apostrophes removed before alnum split.
    assert derive_server_slug("Kil'jaeden") == "kiljaeden"


def test_derive_server_slug_ru_map_hit():
    # Pin against the canonical RU realm "Гордунни" — if RU_REALM_MAP loses
    # this entry the test fails loudly (desired — that's the canary purpose).
    assert "гордунни" in _RU_REALM_MAP_LOWER  # safety guard
    assert derive_server_slug("Гордунни") == "gordunni"
    # Case-insensitive lookup: lower() applied before map lookup.
    assert derive_server_slug("ГОРДУННИ") == "gordunni"


def test_derive_server_slug_ru_map_accepts_wow_normalized_realm_names():
    assert derive_server_slug("Ревущийфьорд") == "howling-fjord"
    assert derive_server_slug("Корольлич") == "lich-king"
    assert derive_server_slug("СвежевательДуш") == "soulflayer"


# ─── _zone_avg ────────────────────────────────────────────────────────────────


def test_zone_avg_none_data():
    assert _zone_avg(None) is None


def test_zone_avg_missing_key():
    assert _zone_avg({"someOtherKey": 50.0}) is None


def test_zone_avg_valid_float():
    assert _zone_avg({"bestPerformanceAverage": 85.5}) == pytest.approx(85.5)


def test_zone_avg_numeric_string():
    # Impl tolerates strings via float(val).
    assert _zone_avg({"bestPerformanceAverage": "95.5"}) == pytest.approx(95.5)


def test_zone_avg_non_numeric_string():
    assert _zone_avg({"bestPerformanceAverage": "bad"}) is None


def test_zone_avg_non_dict_data_returns_none():
    assert _zone_avg("bad") is None  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -1.0, 100.1])
def test_zone_avg_rejects_malformed_percentiles(bad_value):
    assert _zone_avg({"bestPerformanceAverage": bad_value}) is None


def test_zone_avg_custom_key():
    assert _zone_avg(
        {"medianPerformanceAverage": 50.0}, key="medianPerformanceAverage"
    ) == pytest.approx(50.0)


# ─── _process_encounter_ranks ─────────────────────────────────────────────────


def _rank(spec: str = "Brewmaster", bracket: int = 12, percent: float | None = 80.0):
    """Inline rank-dict factory matching WCL's encounterRankings shape."""
    return {"spec": spec, "bracketData": bracket, "rankPercent": percent}


class _FakeAuth:
    def __init__(self):
        self.invalidations = 0

    def get_token(self) -> str:
        return "test-token"

    def invalidate(self) -> None:
        self.invalidations += 1


class _FakeResponse:
    def __init__(
        self,
        body: object,
        status_code: int = 200,
        *,
        json_error: Exception | None = None,
    ):
        self._body = body
        self.status_code = status_code
        self.text = "fake response"
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._body


class _FakeHTTP:
    def __init__(
        self,
        body: object,
        status_code: int = 200,
        *,
        json_error: Exception | None = None,
    ):
        self._body = body
        self._status_code = status_code
        self._json_error = json_error
        self.calls: list[dict] = []

    def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(
            self._body,
            self._status_code,
            json_error=self._json_error,
        )

    def close(self) -> None:
        pass


class _TimeoutHTTP:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        raise httpx.ReadTimeout("read timed out")

    def close(self) -> None:
        pass


class _SequenceHTTP:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self._responses:
            raise AssertionError("unexpected HTTP call")
        return self._responses.pop(0)

    def close(self) -> None:
        pass


class _OAuthHTTP:
    def __init__(self, *responses: _FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, *, data: dict, auth: tuple[str, str]) -> _FakeResponse:
        self.calls.append({"url": url, "data": data, "auth": auth})
        if not self._responses:
            raise AssertionError("unexpected OAuth HTTP call")
        return self._responses.pop(0)


class _ReentrantHTTP:
    def __init__(self, body: object, callback):
        self._body = body
        self._callback = callback
        self.calls: list[dict] = []

    def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if len(self.calls) == 1:
            self._callback()
        return _FakeResponse(self._body)

    def close(self) -> None:
        pass


def _wcl_payload(character: dict | None, *, errors: object = None) -> dict:
    return {
        "data": {
            "rateLimitData": {
                "limitPerHour": 3600,
                "pointsSpentThisHour": 10,
                "pointsResetIn": 300,
            },
            "characterData": {"character": character},
        },
        "errors": errors or [],
    }


def _client_for_payload(payload: dict) -> tuple[WCLClient, _FakeHTTP]:
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    fake_http = _FakeHTTP(payload)
    client._http = fake_http  # type: ignore[assignment]
    return client, fake_http


def _ranks() -> CharacterRanks:
    return CharacterRanks(
        raid_normal=11.0,
        raid_heroic=22.0,
        raid_mythic=33.0,
        raid_normal_median=10.0,
        raid_heroic_median=20.0,
        raid_mythic_median=30.0,
        mplus_dps=77.0,
        mplus_hps=None,
        mplus_dps_median=70.0,
        mplus_hps_median=None,
    )


def _character(**encounters: object) -> dict:
    return {
        "raidNormal": {"bestPerformanceAverage": 71.0},
        "raidHeroic": {"bestPerformanceAverage": 81.0},
        "raidMythic": {"bestPerformanceAverage": 91.0},
        **encounters,
    }


def _character_with_empty_mplus(**encounters: object) -> dict:
    payload: dict[str, object] = {
        alias: {"ranks": []} for alias, _eid, _name in MPLUS_ENCOUNTERS
    }
    payload.update(encounters)
    return _character(**payload)


def _character_with_empty_raid_boss_details(
    difficulty: str = "M", **aliases: dict
) -> dict:
    prefix = {"N": "raid_n", "H": "raid_h", "M": "raid_m"}[difficulty]
    payload: dict[str, dict] = {}
    for encounter_alias, _eid, _name in CURRENT_RAID_ENCOUNTERS:
        base = f"{prefix}_{encounter_alias}"
        payload[f"{base}_overall"] = {"ranks": []}
        payload[f"{base}_ilvl"] = {"ranks": []}
    payload.update(aliases)
    return _character(**payload)


def test_process_ranks_none_data():
    assert _process_encounter_ranks(None, "Brewmaster", "X") is None


def test_process_ranks_empty_ranks():
    assert _process_encounter_ranks({"ranks": []}, "Brewmaster", "X") is None


def test_process_ranks_empty_spec_name():
    # Caller couldn't resolve spec_id (unmapped retail spec) → fail loud.
    enc = {"ranks": [_rank()]}
    assert _process_encounter_ranks(enc, "", "X") is None


def test_process_ranks_no_matching_spec():
    enc = {"ranks": [_rank(spec="Mistweaver")]}
    assert _process_encounter_ranks(enc, "Brewmaster", "X") is None


def test_process_ranks_single_run():
    enc = {"ranks": [_rank(spec="Brewmaster", bracket=15, percent=85.0)]}
    result = _process_encounter_ranks(enc, "Brewmaster", "Pit of Saron")
    assert result is not None
    assert result.name == "Pit of Saron"
    assert result.parse_percent == pytest.approx(85.0)
    assert result.median_percent == pytest.approx(85.0)  # N=1 → median == best
    assert result.key_level == 15
    assert result.run_count == 1


def test_process_ranks_multiple_runs_at_top_key():
    enc = {
        "ranks": [
            _rank(bracket=12, percent=60.0),
            _rank(bracket=12, percent=80.0),
            _rank(bracket=12, percent=100.0),
        ]
    }
    result = _process_encounter_ranks(enc, "Brewmaster", "X")
    assert result is not None
    assert result.parse_percent == pytest.approx(100.0)  # max
    assert result.median_percent == pytest.approx(80.0)  # middle of 3
    assert result.run_count == 3
    assert result.key_level == 12


def test_process_ranks_lower_brackets_filtered_out():
    # Top key still drives legacy headline fields, while lower bracket remains
    # available for context-fit scoring.
    enc = {
        "ranks": [
            _rank(bracket=8, percent=50.0),  # lower key, ignored
            _rank(bracket=12, percent=70.0),
            _rank(bracket=12, percent=90.0),
        ]
    }
    result = _process_encounter_ranks(enc, "Brewmaster", "X")
    assert result is not None
    assert result.parse_percent == pytest.approx(90.0)
    assert result.median_percent == pytest.approx(80.0)  # avg of 70 + 90
    assert result.run_count == 2
    assert result.key_level == 12
    assert [b.key_level for b in result.brackets] == [8, 12]
    assert result.brackets[0].parse_percent == pytest.approx(50.0)


def test_process_ranks_matches_multi_word_spec_without_spaces():
    enc = {
        "ranks": [
            _rank(spec="Marksmanship", bracket=14, percent=99.0),
            _rank(spec="BeastMastery", bracket=14, percent=82.0),
            _rank(spec="Beast Mastery", bracket=14, percent=62.0),
            _rank(spec="BeastMastery", bracket=12, percent=100.0),
        ]
    }

    result = _process_encounter_ranks(enc, "Beast Mastery", "Algeth'ar Academy")

    assert result is not None
    assert result.name == "Algeth'ar Academy"
    assert result.parse_percent == pytest.approx(82.0)
    assert result.median_percent == pytest.approx(72.0)
    assert result.key_level == 14
    assert result.run_count == 2


def test_process_ranks_retains_relevant_lower_key_bracket():
    enc = {
        "ranks": [
            _rank(spec="Beast Mastery", bracket=20, percent=31.0),
            _rank(spec="Beast Mastery", bracket=16, percent=88.0),
            _rank(spec="Beast Mastery", bracket=16, percent=78.0),
            _rank(spec="Beast Mastery", bracket=10, percent=99.0),
        ]
    }

    result = _process_encounter_ranks(enc, "Beast Mastery", "Skyreach")

    assert result is not None
    assert result.key_level == 20
    assert result.parse_percent == pytest.approx(31.0)
    assert [(b.key_level, b.run_count) for b in result.brackets] == [
        (10, 1),
        (16, 2),
        (20, 1),
    ]
    assert result.brackets[1].parse_percent == pytest.approx(88.0)
    assert result.brackets[1].median_percent == pytest.approx(83.0)


def test_process_ranks_death_knight_single_word_spec_filters_other_specs():
    enc = {
        "ranks": [
            _rank(spec="Unholy", bracket=15, percent=95.0),
            _rank(spec="Frost", bracket=15, percent=73.0),
            _rank(spec="Frost", bracket=15, percent=83.0),
        ]
    }

    result = _process_encounter_ranks(enc, "Frost", "Magisters' Terrace")

    assert result is not None
    assert result.parse_percent == pytest.approx(83.0)
    assert result.median_percent == pytest.approx(78.0)
    assert result.key_level == 15
    assert result.run_count == 2


def test_process_ranks_top_key_with_all_none_percentiles():
    # Spec matches AND bracketData is positive int (so max_key > 0 path runs)
    # AND rankPercent is None. Exercises the "percentiles list empty" branch
    # specifically — NOT the earlier max_key=0 branch.
    enc = {
        "ranks": [
            _rank(spec="Brewmaster", bracket=12, percent=None),
            _rank(spec="Brewmaster", bracket=12, percent=None),
        ]
    }
    assert _process_encounter_ranks(enc, "Brewmaster", "X") is None


# ─── _compute_mplus_headline ──────────────────────────────────────────────────


def test_headline_empty_list():
    assert _compute_mplus_headline([]) == (None, None)


def _encounter_query_lines(role: str) -> list[str]:
    return [
        line.strip()
        for line in _build_character_ranks_query(role).splitlines()
        if "encounterRankings" in line
    ]


def test_character_ranks_query_healer_uses_hps_for_mplus_encounters():
    lines = _encounter_query_lines("HEALER")

    assert len(lines) == len(MPLUS_ENCOUNTERS)
    assert all("metric: hps" in line for line in lines)
    assert not any("metric: dps" in line for line in lines)


@pytest.mark.parametrize("role", ["DAMAGER", "TANK", "DPS"])
def test_character_ranks_query_dps_roles_use_dps_for_mplus_encounters(role):
    lines = _encounter_query_lines(role)

    assert len(lines) == len(MPLUS_ENCOUNTERS)
    assert all("metric: dps" in line for line in lines)
    assert not any("metric: hps" in line for line in lines)


def test_character_ranks_query_omits_disabled_mplus():
    query = _build_character_ranks_query(
        "DAMAGER",
        MetricPreferences(
            mplus=False,
            raid_normal=True,
            raid_heroic=True,
            raid_mythic=True,
        ),
    )

    assert "encounterRankings" not in query
    assert "raidNormal: zoneRankings" in query
    assert "raidHeroic: zoneRankings" in query
    assert "raidMythic: zoneRankings" in query


def test_character_ranks_query_omits_disabled_raid_variables():
    query = _build_character_ranks_query(
        "HEALER",
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=False,
        ),
    )

    assert "$raidZoneID" not in query
    assert "$raidMetric" not in query
    assert "zoneRankings" not in query
    assert "encounterRankings" in query
    assert "metric: hps" in query


def test_raid_boss_detail_query_uses_two_aliases_per_enabled_boss():
    query = _build_raid_boss_detail_query(
        "DAMAGER",
        MetricPreferences(
            mplus=False,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=True,
        ),
    )

    lines = [line for line in query.splitlines() if "encounterRankings" in line]
    assert len(lines) == len(CURRENT_RAID_ENCOUNTERS) * 2
    assert all("difficulty: 5" in line for line in lines)
    assert all("metric: dps" in line for line in lines)
    assert all("compare: Parses" in line for line in lines)
    assert any("byBracket: true" in line for line in lines)
    assert "difficulty: 3" not in query
    assert "difficulty: 4" not in query


def test_raid_boss_rows_parse_overall_and_ilvl_percentiles_by_spec():
    char = {
        "raid_m_ia_overall": {
            "ranks": [
                {"spec": "Fury", "rankPercent": 99.0},
                {"spec": "Arms", "rankPercent": 46.2},
            ]
        },
        "raid_m_ia_ilvl": {
            "ranks": [
                {"spec": "Arms", "rankPercent": 68.4},
                {"spec": "Arms", "rankPercent": 66.0},
            ]
        },
        "raid_m_vo_overall": {"ranks": []},
    }

    rows = _raid_boss_rows_from_character(char, "M", spec_name="Arms")

    assert rows == [
        {
            "encounter_id": 3176,
            "name": "Imperator Averzian",
            "overall": 46.2,
            "ilvl": 68.4,
        }
    ]


def test_fetch_character_raid_boss_details_returns_enabled_difficulty_rows():
    client, http = _client_for_payload(
        _wcl_payload(
            _character_with_empty_raid_boss_details(
                "M",
                raid_m_ia_overall={"ranks": [{"spec": "Arms", "rankPercent": 46.2}]},
                raid_m_ia_ilvl={"ranks": [{"spec": "Arms", "rankPercent": 68.4}]},
            )
        )
    )

    rows = client.fetch_character_raid_boss_details(
        "Scout",
        "ravencrest",
        spec_id=71,
        role="DAMAGER",
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=True,
        ),
    )

    assert http.calls[0]["json"]["variables"]["specName"] == "Arms"
    assert rows == {
        "M": [
            {
                "encounter_id": 3176,
                "name": "Imperator Averzian",
                "overall": 46.2,
                "ilvl": 68.4,
            }
        ]
    }


def test_fetch_character_raid_boss_details_503_sets_server_retry_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP({"error": "unavailable"}, status_code=503)
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLApiError, match="Server error"):
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=False,
                raid_mythic=True,
            ),
        )

    assert len(http.calls) == 1
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)
    current[0] += 1.0

    with pytest.raises(WCLApiError) as excinfo:
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=False,
                raid_mythic=True,
            ),
        )

    assert excinfo.value.error_kind == WCL_ERROR_SERVER
    assert len(http.calls) == 1


def test_fetch_character_raid_boss_details_network_timeout_sets_short_retry_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _TimeoutHTTP()
    client._http = http  # type: ignore[assignment]

    with pytest.raises(httpx.TimeoutException):
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=False,
                raid_mythic=True,
            ),
        )

    assert len(http.calls) == 1
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)
    current[0] += 1.0

    with pytest.raises(WCLApiError) as excinfo:
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=False,
                raid_mythic=True,
            ),
        )

    assert excinfo.value.error_kind == WCL_ERROR_NETWORK
    assert len(http.calls) == 1


def test_fetch_character_raid_boss_details_oauth_timeout_sets_short_retry_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class FlakyAuth(_FakeAuth):
        calls = 0

        def get_token(self) -> str:
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("oauth timed out")
            return "fresh-token"

    auth = FlakyAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP(_wcl_payload(_character()))
    client._http = http  # type: ignore[assignment]

    with pytest.raises(httpx.TimeoutException):
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(mplus=False, raid_mythic=True),
        )

    assert auth.calls == 1
    assert len(http.calls) == 0
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)
    current[0] += 1.0

    with pytest.raises(WCLApiError) as excinfo:
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(mplus=False, raid_mythic=True),
        )

    assert excinfo.value.error_kind == WCL_ERROR_NETWORK
    assert auth.calls == 1
    assert len(http.calls) == 0


def test_fetch_character_raid_boss_details_oauth_503_uses_retryable_server_kind(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class ServerFailingAuth(_FakeAuth):
        calls = 0

        def get_token(self) -> str:
            self.calls += 1
            raise WCLAuthError(
                "OAuth failed (HTTP 503): unavailable",
                error_kind=WCL_ERROR_SERVER,
            )

    auth = ServerFailingAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP(_wcl_payload(_character()))
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLAuthError):
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(mplus=False, raid_mythic=True),
        )

    assert auth.calls == 1
    assert len(http.calls) == 0
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)
    current[0] += 1.0

    with pytest.raises(WCLApiError) as excinfo:
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(mplus=False, raid_mythic=True),
        )

    assert excinfo.value.error_kind == WCL_ERROR_SERVER
    assert auth.calls == 1
    assert len(http.calls) == 0


def test_fetch_character_raid_boss_details_stale_oauth_timeout_does_not_set_retry_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class ReconfiguredTimeoutAuth(_FakeAuth):
        def get_token(self) -> str:
            client.reconfigure_auth(_FakeAuth())
            raise httpx.ReadTimeout("old oauth timed out")

    client = WCLClient(ReconfiguredTimeoutAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    client._http = _FakeHTTP(_wcl_payload(_character()))  # type: ignore[assignment]

    with pytest.raises(httpx.TimeoutException):
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(mplus=False, raid_mythic=True),
        )

    assert client.retry_block_remaining_seconds(now=current[0]) == 0.0


def test_fetch_character_raid_boss_details_stale_401_does_not_invalidate_old_auth():
    class ReconfigureThenAuthErrorHTTP:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
            self.calls.append({"url": url, "json": json, "headers": headers})
            if len(self.calls) == 1:
                client.reconfigure_auth(_FakeAuth())
                return _FakeResponse({"error": "unauthorized"}, status_code=401)
            return _FakeResponse({"error": "forbidden"}, status_code=403)

        def close(self) -> None:
            pass

    auth = _FakeAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    client._http = ReconfigureThenAuthErrorHTTP()  # type: ignore[assignment]

    with pytest.raises(WCLApiError) as excinfo:
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(mplus=False, raid_mythic=True),
        )

    assert excinfo.value.error_kind == WCL_ERROR_AUTH
    assert auth.invalidations == 0


def test_fetch_character_ranks_healer_routes_mplus_to_hps_breakdown():
    client, http = _client_for_payload(
        _wcl_payload(
            _character_with_empty_mplus(
                aa={
                    "ranks": [
                        _rank(spec="Windwalker", bracket=14, percent=99.0),
                        _rank(spec="Mistweaver", bracket=14, percent=82.0),
                        _rank(spec="Mistweaver", bracket=14, percent=62.0),
                        _rank(spec="Mistweaver", bracket=12, percent=100.0),
                    ]
                }
            )
        )
    )

    result = client.fetch_character_ranks(
        "Healz",
        "ravencrest",
        spec_id=270,
        role="HEALER",
        region="US",
        metric_preferences=MetricPreferences(),
    )

    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"] == WCL_API_URL
    assert call["headers"] == {"Authorization": "Bearer test-token"}
    assert call["json"]["variables"]["serverRegion"] == "US"
    assert call["json"]["variables"]["serverSlug"] == "ravencrest"
    assert call["json"]["variables"]["raidMetric"] == "hps"

    assert result.mplus_dps is None
    assert result.mplus_dps_median is None
    assert result.mplus_dps_breakdown == []
    assert result.mplus_hps == pytest.approx(82.0)
    assert result.mplus_hps_median == pytest.approx(72.0)
    assert len(result.mplus_hps_breakdown) == 1
    perf = result.mplus_hps_breakdown[0]
    assert perf.name == "Algeth'ar Academy"
    assert perf.parse_percent == pytest.approx(82.0)
    assert perf.median_percent == pytest.approx(72.0)
    assert perf.key_level == 14
    assert perf.run_count == 2
    assert client.last_quota is not None
    assert client.last_quota.limit_per_hour == pytest.approx(3600)
    assert client.last_quota.points_spent == pytest.approx(10)
    assert client.last_quota.reset_in_seconds == pytest.approx(300)


@pytest.mark.parametrize("role", ["DAMAGER", "DPS"])
def test_fetch_character_ranks_dps_roles_route_mplus_to_dps_breakdown(role):
    client, http = _client_for_payload(
        _wcl_payload(
            _character_with_empty_mplus(
                mt={
                    "ranks": [
                        _rank(spec="Marksmanship", bracket=13, percent=97.0),
                        _rank(spec="BeastMastery", bracket=13, percent=80.0),
                        _rank(spec="Beast Mastery", bracket=13, percent=60.0),
                    ]
                }
            )
        )
    )

    result = client.fetch_character_ranks(
        "Shots",
        "twisting-nether",
        spec_id=253,
        role=role,
        metric_preferences=MetricPreferences(),
    )

    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["headers"] == {"Authorization": "Bearer test-token"}
    assert call["json"]["variables"]["serverRegion"] == "EU"
    assert call["json"]["variables"]["serverSlug"] == "twisting-nether"
    assert call["json"]["variables"]["raidMetric"] == "dps"

    assert result.mplus_hps is None
    assert result.mplus_hps_median is None
    assert result.mplus_hps_breakdown == []
    assert result.mplus_dps == pytest.approx(80.0)
    assert result.mplus_dps_median == pytest.approx(70.0)
    assert len(result.mplus_dps_breakdown) == 1
    perf = result.mplus_dps_breakdown[0]
    assert perf.name == "Magisters' Terrace"
    assert perf.parse_percent == pytest.approx(80.0)
    assert perf.median_percent == pytest.approx(70.0)
    assert perf.key_level == 13
    assert perf.run_count == 2


def test_fetch_character_ranks_devourer_filters_other_dh_specs():
    client, http = _client_for_payload(
        _wcl_payload(
            _character_with_empty_mplus(
                mt={
                    "ranks": [
                        _rank(spec="Havoc", bracket=13, percent=97.0),
                        _rank(spec="Vengeance", bracket=13, percent=88.0),
                        _rank(spec="Devourer", bracket=13, percent=80.0),
                        _rank(spec="Devourer", bracket=13, percent=60.0),
                    ]
                }
            )
        )
    )

    result = client.fetch_character_ranks(
        "Bites",
        "ravencrest",
        spec_id=1480,
        role="DAMAGER",
        metric_preferences=MetricPreferences(),
    )

    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["json"]["variables"]["raidMetric"] == "dps"
    assert result.mplus_hps is None
    assert result.mplus_hps_breakdown == []
    assert result.mplus_dps == pytest.approx(80.0)
    assert result.mplus_dps_median == pytest.approx(70.0)
    assert len(result.mplus_dps_breakdown) == 1
    perf = result.mplus_dps_breakdown[0]
    assert perf.name == "Magisters' Terrace"
    assert perf.parse_percent == pytest.approx(80.0)
    assert perf.median_percent == pytest.approx(70.0)
    assert perf.key_level == 13
    assert perf.run_count == 2


def test_fetch_character_ranks_respects_metric_preferences():
    client, http = _client_for_payload(_wcl_payload(_character()))
    prefs = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )

    result = client.fetch_character_ranks(
        "Raidonly",
        "ravencrest",
        spec_id=71,
        role="DAMAGER",
        metric_preferences=prefs,
    )

    assert len(http.calls) == 1
    query = http.calls[0]["json"]["query"]
    assert "encounterRankings" not in query
    assert "raidNormal: zoneRankings" not in query
    assert "raidHeroic: zoneRankings" in query
    assert "raidMythic: zoneRankings" not in query
    assert result.raid_normal is None
    assert result.raid_heroic == pytest.approx(81.0)
    assert result.raid_mythic is None
    assert result.mplus_dps is None
    assert result.mplus_dps_breakdown == []


def test_fetch_character_ranks_spec_zero_mplus_only_returns_empty_without_http():
    client, http = _client_for_payload(_wcl_payload(_character_with_empty_mplus()))
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )

    result = client.fetch_character_ranks(
        "Unknown",
        "ravencrest",
        spec_id=0,
        role="DAMAGER",
        metric_preferences=prefs,
    )

    assert len(http.calls) == 0
    assert client.last_quota is None
    assert result == CharacterRanks.empty()


def test_fetch_character_ranks_spec_zero_omits_mplus_but_keeps_raid_query():
    client, http = _client_for_payload(_wcl_payload(_character()))
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )

    result = client.fetch_character_ranks(
        "Unknown",
        "ravencrest",
        spec_id=0,
        role="DAMAGER",
        metric_preferences=prefs,
    )

    assert len(http.calls) == 1
    query = http.calls[0]["json"]["query"]
    assert "encounterRankings" not in query
    assert "raidNormal: zoneRankings" not in query
    assert "raidHeroic: zoneRankings" in query
    assert "raidMythic: zoneRankings" not in query
    assert result.raid_heroic == pytest.approx(81.0)
    assert result.mplus_dps is None
    assert result.mplus_hps is None
    assert result.mplus_dps_breakdown == []
    assert result.mplus_hps_breakdown == []


def test_fetch_character_ranks_second_401_is_auth_error_kind():
    auth = _FakeAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _SequenceHTTP(
        [
            _FakeResponse({"error": "expired"}, status_code=401),
            _FakeResponse({"error": "still bad"}, status_code=401),
        ]
    )
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLApiError) as excinfo:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert excinfo.value.error_kind == WCL_ERROR_AUTH
    assert auth.invalidations == 1
    assert len(http.calls) == 2


def test_fetch_character_ranks_401_retry_success_preserves_token_refresh():
    auth = _FakeAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _SequenceHTTP(
        [
            _FakeResponse({"error": "expired"}, status_code=401),
            _FakeResponse(_wcl_payload(_character_with_empty_mplus())),
        ]
    )
    client._http = http  # type: ignore[assignment]

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error == ""
    assert auth.invalidations == 1
    assert len(http.calls) == 2
    assert client.last_quota is not None
    assert client.last_quota.points_spent == pytest.approx(10.0)


def test_fetch_character_ranks_403_is_auth_error_kind():
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP({"error": "forbidden"}, status_code=403)
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLApiError) as excinfo:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert excinfo.value.error_kind == WCL_ERROR_AUTH
    assert len(http.calls) == 1


def test_quota_reservation_blocks_second_cache_miss_before_http(
    monkeypatch: pytest.MonkeyPatch,
):
    now = 1_000.0
    second_result: list[CharacterRanks] = []
    monkeypatch.setattr(wcl_mod.time, "time", lambda: now)
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._record_quota_snapshot(
        RateLimitInfo(limit_per_hour=100.0, points_spent=84.0, reset_in_seconds=60.0),
        now=now,
    )

    def fetch_second() -> None:
        second_result.append(
            client.fetch_character_ranks("Second", "ravencrest", spec_id=71)
        )

    client._http.close()
    http = _ReentrantHTTP(_wcl_payload(_character_with_empty_mplus()), fetch_second)
    client._http = http  # type: ignore[assignment]

    first = client.fetch_character_ranks("First", "ravencrest", spec_id=71)

    assert first.error == ""
    assert len(second_result) == 1
    assert second_result[0].error_kind == WCL_ERROR_QUOTA_GUARD
    assert len(http.calls) == 1


def test_quota_reservation_releases_after_network_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    now = 1_000.0
    monkeypatch.setattr(wcl_mod.time, "time", lambda: now)
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._record_quota_snapshot(
        RateLimitInfo(limit_per_hour=100.0, points_spent=84.0, reset_in_seconds=60.0),
        now=now,
    )

    class RaisingHTTP:
        calls = 0

        def post(self, *_args, **_kwargs):
            self.calls += 1
            raise httpx.RequestError("network down")

        def close(self) -> None:
            pass

    failing_http = RaisingHTTP()
    client._http.close()
    client._http = failing_http  # type: ignore[assignment]

    with pytest.raises(httpx.RequestError):
        client.fetch_character_ranks("First", "ravencrest", spec_id=71)

    http = _FakeHTTP(_wcl_payload(_character_with_empty_mplus()))
    client._http = http  # type: ignore[assignment]

    blocked = client.fetch_character_ranks("Second", "ravencrest", spec_id=71)
    assert blocked.error_kind == WCL_ERROR_NETWORK
    assert len(http.calls) == 0

    now += 31.0
    result = client.fetch_character_ranks("Second", "ravencrest", spec_id=71)

    assert result.error == ""
    assert len(http.calls) == 1


def test_reconfigure_auth_clears_quota_reservation_state(
    monkeypatch: pytest.MonkeyPatch,
):
    now = 1_000.0
    monkeypatch.setattr(wcl_mod.time, "time", lambda: now)
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._record_quota_snapshot(
        RateLimitInfo(limit_per_hour=100.0, points_spent=84.0, reset_in_seconds=60.0),
        now=now,
    )
    reservation = client._reserve_quota_for_fetch(12.0, now=now)
    assert not isinstance(reservation, CharacterRanks)
    assert client.quota_guard_retry_remaining_seconds(now=now) == pytest.approx(60.0)

    client.reconfigure_auth(_FakeAuth())  # type: ignore[arg-type]

    assert client.last_quota is None
    assert client.quota_guard_retry_remaining_seconds(now=now) == 0.0


def test_stale_quota_reservation_release_after_reconfigure_does_not_clear_new_reservation():
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]

    old_reservation = client._reserve_quota_for_fetch(12.0)
    assert not isinstance(old_reservation, CharacterRanks)
    assert client._reserved_quota_points == pytest.approx(12.0)

    client.reconfigure_auth(_FakeAuth())  # type: ignore[arg-type]
    new_reservation = client._reserve_quota_for_fetch(12.0)
    assert not isinstance(new_reservation, CharacterRanks)
    assert client._reserved_quota_points == pytest.approx(12.0)

    client._release_quota_reservation(old_reservation)
    assert client._reserved_quota_points == pytest.approx(12.0)

    client._release_quota_reservation(new_reservation)
    assert client._reserved_quota_points == pytest.approx(0.0)


def test_reconfigure_auth_ignores_stale_quota_snapshot_from_in_flight_fetch():
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]

    def reconfigure_before_response() -> None:
        client.reconfigure_auth(_FakeAuth())  # type: ignore[arg-type]

    client._http.close()
    client._http = _ReentrantHTTP(  # type: ignore[assignment]
        _wcl_payload(_character_with_empty_mplus()),
        reconfigure_before_response,
    )

    result = client.fetch_character_ranks("First", "ravencrest", spec_id=71)

    assert result.error == ""
    assert client.last_quota is None
    assert client.quota_guard_retry_remaining_seconds() == 0.0


def test_reconfigure_auth_ignores_stale_429_from_in_flight_fetch(
    monkeypatch: pytest.MonkeyPatch,
):
    now = 1_000.0
    monkeypatch.setattr(wcl_mod.time, "time", lambda: now)
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]

    class ReconfigureThenRateLimitHTTP:
        calls = 0

        def post(self, *_args, **_kwargs):
            self.calls += 1
            client.reconfigure_auth(_FakeAuth())  # type: ignore[arg-type]
            return _FakeResponse({"error": "rate limited"}, status_code=429)

        def close(self) -> None:
            pass

    client._http.close()
    client._http = ReconfigureThenRateLimitHTTP()  # type: ignore[assignment]

    with pytest.raises(WCLApiError) as excinfo:
        client.fetch_character_ranks("First", "ravencrest", spec_id=71)

    assert excinfo.value.error_kind == WCL_ERROR_RATE_LIMITED
    assert client.rate_limit_retry_remaining_seconds(now=now) == 0.0


def test_reconfigure_auth_ignores_stale_401_invalidation():
    old_auth = _FakeAuth()
    client = WCLClient(old_auth, region="EU")  # type: ignore[arg-type]

    class ReconfigureThenUnauthorizedHTTP:
        calls = 0

        def post(self, *_args, **_kwargs):
            self.calls += 1
            client.reconfigure_auth(_FakeAuth())  # type: ignore[arg-type]
            return _FakeResponse({"error": "unauthorized"}, status_code=401)

        def close(self) -> None:
            pass

    client._http.close()
    client._http = ReconfigureThenUnauthorizedHTTP()  # type: ignore[assignment]

    with pytest.raises(WCLApiError) as excinfo:
        client.fetch_character_ranks("First", "ravencrest", spec_id=71)

    assert excinfo.value.error_kind == WCL_ERROR_AUTH
    assert old_auth.invalidations == 0


def test_quota_guard_blocks_before_reset_without_spending_http_call(
    monkeypatch: pytest.MonkeyPatch,
):
    now = 1_000.0
    monkeypatch.setattr(wcl_mod.time, "time", lambda: now + 10.0)
    client, http = _client_for_payload(_wcl_payload(_character_with_empty_mplus()))
    client._record_quota_snapshot(
        RateLimitInfo(limit_per_hour=100.0, points_spent=90.0, reset_in_seconds=60.0),
        now=now,
    )

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error.startswith("WCL quota guard 90% used")
    assert http.calls == []


def test_quota_guard_lifts_after_recorded_reset_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])
    client, http = _client_for_payload(_wcl_payload(_character_with_empty_mplus()))
    client._record_quota_snapshot(
        RateLimitInfo(limit_per_hour=100.0, points_spent=90.0, reset_in_seconds=60.0),
        now=current[0],
    )
    current[0] += 61.0

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error == ""
    assert len(http.calls) == 1
    assert client.last_quota is not None
    assert client.last_quota.points_spent == pytest.approx(10.0)


def test_429_sets_cooldown_and_short_circuits_until_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP(_wcl_payload(_character()), status_code=429)
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLApiError, match="Rate limited"):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert len(http.calls) == 1
    current[0] += 1.0
    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert "rate-limited" in result.error
    assert result.error_kind == WCL_ERROR_RATE_LIMITED
    assert len(http.calls) == 1


def test_503_sets_short_retry_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP({"error": "unavailable"}, status_code=503)
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLApiError, match="Server error"):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert len(http.calls) == 1
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)
    current[0] += 1.0
    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error == "WCL server error; retrying in 29s"
    assert result.error_kind == WCL_ERROR_SERVER
    assert len(http.calls) == 1


def test_network_timeout_sets_short_retry_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _TimeoutHTTP()
    client._http = http  # type: ignore[assignment]

    with pytest.raises(httpx.TimeoutException):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert len(http.calls) == 1
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)
    current[0] += 1.0
    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error == "WCL network error; retrying in 29s"
    assert result.error_kind == WCL_ERROR_NETWORK
    assert len(http.calls) == 1


def test_oauth_refresh_network_error_sets_short_retry_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class FlakyAuth(_FakeAuth):
        calls = 0

        def get_token(self) -> str:
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("oauth timed out")
            return "fresh-token"

    auth = FlakyAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP(_wcl_payload(_character_with_empty_mplus()))
    client._http = http  # type: ignore[assignment]

    with pytest.raises(httpx.TimeoutException):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert auth.calls == 1
    assert len(http.calls) == 0
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)
    current[0] += 1.0
    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error == "WCL network error; retrying in 29s"
    assert result.error_kind == WCL_ERROR_NETWORK
    assert auth.calls == 1
    assert len(http.calls) == 0
    current[0] += 30.0
    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error == ""
    assert auth.calls == 2
    assert len(http.calls) == 1


def test_second_oauth_refresh_network_error_sets_short_retry_after_401(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class TimeoutAfterInvalidateAuth(_FakeAuth):
        calls = 0
        invalidated = False

        def get_token(self) -> str:
            self.calls += 1
            if self.invalidated:
                raise httpx.ReadTimeout("oauth refresh timed out")
            return "stale-token"

        def invalidate(self) -> None:
            self.invalidated = True

    auth = TimeoutAfterInvalidateAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP({"error": "unauthorized"}, status_code=401)
    client._http = http  # type: ignore[assignment]

    with pytest.raises(httpx.TimeoutException):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert auth.calls == 2
    assert len(http.calls) == 1
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)


def test_fetch_character_ranks_oauth_429_sets_rate_limit_block_and_short_circuits_until_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class RateLimitedAuth(_FakeAuth):
        calls = 0

        def get_token(self) -> str:
            self.calls += 1
            raise WCLAuthError(
                "OAuth failed (HTTP 429): slow down",
                error_kind=WCL_ERROR_RATE_LIMITED,
            )

    auth = RateLimitedAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP(_wcl_payload(_character_with_empty_mplus()))
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLAuthError):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert auth.calls == 1
    assert len(http.calls) == 0
    assert client.rate_limit_retry_remaining_seconds(now=current[0]) == pytest.approx(
        300.0
    )
    current[0] += 1.0

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error == "WCL rate-limited; retrying in 299s"
    assert result.error_kind == WCL_ERROR_RATE_LIMITED
    assert auth.calls == 1
    assert len(http.calls) == 0


def test_fetch_character_ranks_oauth_503_sets_server_retry_block_and_short_circuits_until_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class ServerFailingAuth(_FakeAuth):
        calls = 0

        def get_token(self) -> str:
            self.calls += 1
            raise WCLAuthError(
                "OAuth failed (HTTP 503): unavailable",
                error_kind=WCL_ERROR_SERVER,
            )

    auth = ServerFailingAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP(_wcl_payload(_character_with_empty_mplus()))
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLAuthError):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert auth.calls == 1
    assert len(http.calls) == 0
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)
    current[0] += 1.0

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error == "WCL server error; retrying in 29s"
    assert result.error_kind == WCL_ERROR_SERVER
    assert auth.calls == 1
    assert len(http.calls) == 0


def test_second_oauth_refresh_503_sets_short_retry_after_401(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class ServerErrorAfterInvalidateAuth(_FakeAuth):
        calls = 0
        invalidated = False

        def get_token(self) -> str:
            self.calls += 1
            if self.invalidated:
                raise WCLAuthError(
                    "OAuth failed (HTTP 503): unavailable",
                    error_kind=WCL_ERROR_SERVER,
                )
            return "stale-token"

        def invalidate(self) -> None:
            self.invalidated = True

    auth = ServerErrorAfterInvalidateAuth()
    client = WCLClient(auth, region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP({"error": "unauthorized"}, status_code=401)
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLAuthError):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert auth.calls == 2
    assert len(http.calls) == 1
    assert client.retry_block_remaining_seconds(now=current[0]) == pytest.approx(30.0)


def test_stale_oauth_refresh_network_error_does_not_set_retry_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class ReconfiguredTimeoutAuth(_FakeAuth):
        def get_token(self) -> str:
            client.reconfigure_auth(_FakeAuth())
            raise httpx.ReadTimeout("old oauth timed out")

    client = WCLClient(ReconfiguredTimeoutAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    client._http = _FakeHTTP(_wcl_payload(_character()))  # type: ignore[assignment]

    with pytest.raises(httpx.TimeoutException):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert client.retry_block_remaining_seconds(now=current[0]) == 0.0


def test_stale_oauth_503_after_reconfigure_does_not_set_retry_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])

    class ReconfiguredServerErrorAuth(_FakeAuth):
        def get_token(self) -> str:
            client.reconfigure_auth(_FakeAuth())
            raise WCLAuthError(
                "old oauth failed",
                error_kind=WCL_ERROR_SERVER,
            )

    client = WCLClient(ReconfiguredServerErrorAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    client._http = _FakeHTTP(_wcl_payload(_character()))  # type: ignore[assignment]

    with pytest.raises(WCLAuthError):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert client.retry_block_remaining_seconds(now=current[0]) == 0.0


def test_fetch_character_ranks_unexpected_400_is_non_retryable_http_error():
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    http = _FakeHTTP({"error": "bad request"}, status_code=400)
    client._http = http  # type: ignore[assignment]

    with pytest.raises(WCLApiError, match="Unexpected HTTP 400") as excinfo:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert excinfo.value.error_kind == wcl_mod.WCL_ERROR_HTTP
    assert excinfo.value.error_kind != WCL_ERROR_SERVER
    assert client.retry_block_remaining_seconds() == 0.0
    assert len(http.calls) == 1


def test_character_ranks_empty_preserves_error_kind():
    result = CharacterRanks.empty(error="paused", error_kind=WCL_ERROR_QUOTA_GUARD)

    assert result.error == "paused"
    assert result.error_kind == WCL_ERROR_QUOTA_GUARD


def test_wcl_api_error_preserves_message_and_kind():
    err = WCLApiError("boom", error_kind=WCL_ERROR_RATE_LIMITED)

    assert str(err) == "boom"
    assert err.error_kind == WCL_ERROR_RATE_LIMITED


def test_quota_guard_sets_retryable_error_kind(monkeypatch: pytest.MonkeyPatch):
    now = 1_000.0
    monkeypatch.setattr(wcl_mod.time, "time", lambda: now)
    client, _http = _client_for_payload(_wcl_payload(_character()))
    client._record_quota_snapshot(
        RateLimitInfo(limit_per_hour=100.0, points_spent=90.0, reset_in_seconds=60.0),
        now=now,
    )

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error_kind == WCL_ERROR_QUOTA_GUARD


def test_retry_block_remaining_seconds_uses_max_remaining_block(
    monkeypatch: pytest.MonkeyPatch,
):
    current = [1_000.0]
    monkeypatch.setattr(wcl_mod.time, "time", lambda: current[0])
    client, _http = _client_for_payload(_wcl_payload(_character()))
    client._record_quota_snapshot(
        RateLimitInfo(limit_per_hour=100.0, points_spent=90.0, reset_in_seconds=60.0),
        now=current[0],
    )
    client._rate_limited_until = current[0] + 120.0
    client._network_retry_until = current[0] + 90.0

    assert client.rate_limit_retry_remaining_seconds() == pytest.approx(120.0)
    assert client.network_retry_remaining_seconds() == pytest.approx(90.0)
    assert client.quota_guard_retry_remaining_seconds() == pytest.approx(60.0)
    assert client.retry_block_remaining_seconds() == pytest.approx(120.0)


def test_character_cache_reads_current_entry_without_error_kind(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    raw = json.loads(cache._path.read_text(encoding="utf-8"))
    key = CharacterCache._key("Scout", "ravencrest", "EU", 71, "DAMAGER")
    del raw["entries"][key]["ranks"]["error_kind"]
    cache._path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = CharacterCache(tmp_path)
    result = loaded.get("Scout", "ravencrest", "EU", 71, "DAMAGER")

    assert result is not None
    assert result.error_kind == ""


def test_character_cache_reuses_broader_metric_scope_for_narrow_request(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        _ranks(),
        role="DAMAGER",
        metric_preferences=MetricPreferences(),
    )
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )

    loaded = CharacterCache(tmp_path)
    result = loaded.get(
        "Scout",
        "ravencrest",
        "EU",
        71,
        "DAMAGER",
        metric_preferences=narrow,
    )

    assert result is not None
    assert result.raid_normal is None
    assert result.raid_heroic == pytest.approx(22.0)
    assert result.raid_mythic is None
    assert result.mplus_dps is None
    assert result.mplus_dps_breakdown == []


def test_character_cache_does_not_reuse_narrow_scope_for_broad_request(tmp_path):
    cache = CharacterCache(tmp_path)
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        _ranks(),
        role="DAMAGER",
        metric_preferences=narrow,
    )

    loaded = CharacterCache(tmp_path)
    result = loaded.get(
        "Scout",
        "ravencrest",
        "EU",
        71,
        "DAMAGER",
        metric_preferences=MetricPreferences(),
    )

    assert result is None


def test_character_cache_not_found_is_scoped_to_character_identity_only(tmp_path):
    cache = CharacterCache(tmp_path)
    stored = cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        CharacterRanks.empty(not_found=True, error="Could not find character"),
        role="DAMAGER",
        metric_preferences=MetricPreferences(),
    )

    assert stored is True
    for spec_id, role, prefs in (
        (71, "DAMAGER", MetricPreferences()),
        (72, "DAMAGER", MetricPreferences()),
        (71, "HEALER", MetricPreferences()),
        (
            71,
            "DAMAGER",
            MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=True,
                raid_mythic=False,
            ),
        ),
    ):
        result = cache.get(
            "Scout",
            "ravencrest",
            "EU",
            spec_id,
            role,
            metric_preferences=prefs,
        )
        assert result is not None
        assert result.not_found is True
        assert result.error == "Could not find character"

    assert cache.get("Other", "ravencrest", "EU", 71, "DAMAGER") is None
    assert cache.get("Scout", "argent-dawn", "EU", 71, "DAMAGER") is None
    assert cache.get("Scout", "ravencrest", "US", 71, "DAMAGER") is None


def test_character_cache_not_found_expires_with_negative_ttl(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        CharacterRanks.empty(not_found=True),
        role="DAMAGER",
    )
    key = CharacterCache._not_found_key("Scout", "ravencrest", "EU")
    cache._data[key].fetched_at = time.time() - CharacterCache.NOT_FOUND_TTL_SECONDS - 1

    assert cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None


def test_character_cache_not_found_evicts_stale_positive_identity_entries(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        _ranks(),
        role="DAMAGER",
        metric_preferences=MetricPreferences(),
    )
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        72,
        _ranks(),
        role="HEALER",
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        ),
    )

    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        CharacterRanks.empty(not_found=True),
        role="DAMAGER",
        metric_preferences=MetricPreferences(),
    )
    not_found_key = CharacterCache._not_found_key("Scout", "ravencrest", "EU")
    cache._data[not_found_key].fetched_at = (
        time.time() - CharacterCache.NOT_FOUND_TTL_SECONDS - 1
    )

    assert cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None
    assert cache.get("Scout", "ravencrest", "EU", 72, "HEALER") is None


def test_character_cache_not_found_respects_ttl_override(tmp_path):
    cache = CharacterCache(tmp_path, ttl_seconds=1)
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        CharacterRanks.empty(not_found=True),
        role="DAMAGER",
    )
    key = CharacterCache._not_found_key("Scout", "ravencrest", "EU")
    cache._data[key].fetched_at = time.time() - 2

    assert cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None


def test_character_cache_positive_put_clears_prior_not_found(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        CharacterRanks.empty(not_found=True),
        role="DAMAGER",
    )

    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        _ranks(),
        role="DAMAGER",
        metric_preferences=MetricPreferences(),
    )

    key = CharacterCache._not_found_key("Scout", "ravencrest", "EU")
    assert key not in cache._data
    result = cache.get(
        "Scout",
        "ravencrest",
        "EU",
        71,
        "DAMAGER",
        metric_preferences=MetricPreferences(),
    )
    assert result is not None
    assert result.not_found is False
    assert result.raid_heroic == pytest.approx(22.0)


def test_character_cache_stale_generation_rejects_not_found_put(tmp_path):
    cache = CharacterCache(tmp_path)
    old_generation = cache.generation

    cache.clear()
    stored = cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        CharacterRanks.empty(not_found=True),
        role="DAMAGER",
        expected_generation=old_generation,
    )

    assert stored is False
    assert cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None
    assert not cache._path.exists()


def test_character_cache_prefers_exact_scope_over_broader_scope(tmp_path):
    cache = CharacterCache(tmp_path)
    broad = _ranks()
    exact = _ranks()
    exact.raid_heroic = 44.0
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        broad,
        role="DAMAGER",
        metric_preferences=MetricPreferences(),
    )
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        exact,
        role="DAMAGER",
        metric_preferences=narrow,
    )

    loaded = CharacterCache(tmp_path)
    result = loaded.get(
        "Scout",
        "ravencrest",
        "EU",
        71,
        "DAMAGER",
        metric_preferences=narrow,
    )

    assert result is not None
    assert result.raid_heroic == pytest.approx(44.0)


def test_character_cache_tie_breaks_same_timestamp_broader_scopes(tmp_path):
    cache = CharacterCache(tmp_path)
    normal_heroic = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=False,
    )
    heroic_mythic = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=True,
    )
    requested = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    first = _ranks()
    first.raid_heroic = 31.0
    second = _ranks()
    second.raid_heroic = 32.0
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        first,
        role="DAMAGER",
        metric_preferences=normal_heroic,
    )
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        second,
        role="DAMAGER",
        metric_preferences=heroic_mythic,
    )
    same_time = time.time()
    for entry in cache._data.values():
        entry.fetched_at = same_time

    result = cache.get(
        "Scout",
        "ravencrest",
        "EU",
        71,
        "DAMAGER",
        metric_preferences=requested,
    )

    assert result is not None
    assert result.raid_heroic in {31.0, 32.0}


def test_character_cache_prefers_newest_covering_scope_when_exact_missing(tmp_path):
    cache = CharacterCache(tmp_path)
    normal_heroic = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=False,
    )
    all_metrics = MetricPreferences()
    requested = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    older = _ranks()
    older.raid_heroic = 31.0
    newer = _ranks()
    newer.raid_heroic = 64.0
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        older,
        role="DAMAGER",
        metric_preferences=normal_heroic,
    )
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        newer,
        role="DAMAGER",
        metric_preferences=all_metrics,
    )
    now = time.time()
    for key, entry in cache._data.items():
        preferences_key = key.rsplit(":", 1)[-1]
        if preferences_key == normal_heroic.cache_key():
            entry.fetched_at = now - 60 * 60
        elif preferences_key == all_metrics.cache_key():
            entry.fetched_at = now

    result = cache.get(
        "Scout",
        "ravencrest",
        "EU",
        71,
        "DAMAGER",
        metric_preferences=requested,
    )

    assert result is not None
    assert result.raid_heroic == pytest.approx(64.0)


def test_character_cache_ignores_malformed_scope_key_entries(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    raw = json.loads(cache._path.read_text(encoding="utf-8"))
    valid_key = CharacterCache._key("Scout", "ravencrest", "EU", 71, "DAMAGER")
    raw["entries"][
        "EU:ravencrest:scout:71:DPS:not-a-preference-key"
    ] = raw["entries"].pop(valid_key)
    cache._path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = CharacterCache(tmp_path)
    result = loaded.get(
        "Scout",
        "ravencrest",
        "EU",
        71,
        "DAMAGER",
        metric_preferences=MetricPreferences(mplus=False),
    )

    assert result is None


def test_character_cache_discards_previous_version_entries(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    raw = json.loads(cache._path.read_text(encoding="utf-8"))
    assert raw["__version__"] == _CACHE_VERSION
    raw["__version__"] = _CACHE_VERSION - 1
    cache._path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = CharacterCache(tmp_path)

    assert loaded.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None


def test_character_cache_failed_replace_preserves_previous_disk_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    before = cache._path.read_text(encoding="utf-8")

    def fail_replace(_src: object, _dst: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)

    cache.put("Other", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")

    assert cache._path.read_text(encoding="utf-8") == before
    assert cache.get("Other", "ravencrest", "EU", 71, "DAMAGER") is not None
    loaded = CharacterCache(tmp_path)
    assert loaded.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is not None
    assert loaded.get("Other", "ravencrest", "EU", 71, "DAMAGER") is None
    assert list(tmp_path.glob(".character-cache.json.*.tmp")) == []


def test_character_cache_saves_private_file_mode(monkeypatch: pytest.MonkeyPatch, tmp_path):
    cache = CharacterCache(tmp_path)
    calls: list[tuple[object, int]] = []
    path_type = type(cache._path)
    original_chmod = path_type.chmod

    def record_chmod(self, mode: int) -> None:
        calls.append((self, mode))
        original_chmod(self, mode)

    monkeypatch.setattr(path_type, "chmod", record_chmod)

    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")

    assert (cache._path, stat.S_IRUSR | stat.S_IWUSR) in calls
    assert (cache._path.parent, stat.S_IRWXU) in calls
    assert {mode for _path, mode in calls} <= {
        stat.S_IRUSR | stat.S_IWUSR,
        stat.S_IRWXU,
    }


def test_character_cache_applies_private_mode_to_existing_cache_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    cache_file = tmp_path / "character-cache.json"
    cache_file.write_text(
        json.dumps({"__version__": _CACHE_VERSION, "entries": {}}),
        encoding="utf-8",
    )
    calls: list[tuple[object, int]] = []
    path_type = type(cache_file)
    original_chmod = path_type.chmod

    def record_chmod(self, mode: int) -> None:
        calls.append((self, mode))
        original_chmod(self, mode)

    monkeypatch.setattr(path_type, "chmod", record_chmod)

    CharacterCache(tmp_path)

    assert (cache_file, stat.S_IRUSR | stat.S_IWUSR) in calls


def test_character_cache_private_mode_failure_does_not_break_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    cache = CharacterCache(tmp_path)
    calls: list[object] = []

    def fail_chmod(self, _mode: int) -> None:
        calls.append(self)
        raise PermissionError("policy")

    monkeypatch.setattr(type(cache._path), "chmod", fail_chmod)

    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")

    assert cache._path.exists()
    assert any(path == cache._path for path in calls)


def test_character_cache_load_prunes_expired_positive_and_not_found_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    now = 1_000_000.0
    monkeypatch.setattr(wcl_mod.time, "time", lambda: now)
    fresh_key = CharacterCache._key("Fresh", "ravencrest", "EU", 71, "DAMAGER")
    expired_key = CharacterCache._key("Expired", "ravencrest", "EU", 71, "DAMAGER")
    fresh_not_found_key = CharacterCache._not_found_key("Missing", "ravencrest", "EU")
    expired_not_found_key = CharacterCache._not_found_key("Gone", "ravencrest", "EU")
    cache_file = tmp_path / "character-cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "__version__": _CACHE_VERSION,
                "entries": {
                    fresh_key: {
                        "fetched_at": now - 60,
                        "ranks": _ranks().__dict__,
                    },
                    expired_key: {
                        "fetched_at": now - CharacterCache.TTL_SECONDS - 1,
                        "ranks": _ranks().__dict__,
                    },
                    fresh_not_found_key: {
                        "fetched_at": now - 60,
                        "ranks": CharacterRanks.empty(not_found=True).__dict__,
                    },
                    expired_not_found_key: {
                        "fetched_at": now - CharacterCache.NOT_FOUND_TTL_SECONDS - 1,
                        "ranks": CharacterRanks.empty(not_found=True).__dict__,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = CharacterCache(tmp_path)

    assert fresh_key in loaded._data
    assert fresh_not_found_key in loaded._data
    assert expired_key not in loaded._data
    assert expired_not_found_key not in loaded._data


def test_character_cache_save_prunes_expired_entries_before_persisting(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    now = 1_000_000.0
    monkeypatch.setattr(wcl_mod.time, "time", lambda: now)
    cache = CharacterCache(tmp_path)
    cache.put("Old", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    old_key = CharacterCache._key("Old", "ravencrest", "EU", 71, "DAMAGER")
    cache._data[old_key].fetched_at = now - CharacterCache.TTL_SECONDS - 1

    cache.put("Fresh", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")

    raw = json.loads(cache._path.read_text(encoding="utf-8"))
    fresh_key = CharacterCache._key("Fresh", "ravencrest", "EU", 71, "DAMAGER")
    assert old_key not in raw["entries"]
    assert fresh_key in raw["entries"]


def test_character_cache_deferred_save_batches_puts_until_flush(tmp_path):
    cache = CharacterCache(tmp_path, defer_saves=True, save_debounce_seconds=60.0)

    cache.put("One", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    cache.put("Two", "ravencrest", "EU", 72, _ranks(), role="DAMAGER")

    assert not cache._path.exists()
    assert cache.get("One", "ravencrest", "EU", 71, "DAMAGER") is not None
    assert cache.get("Two", "ravencrest", "EU", 72, "DAMAGER") is not None

    cache.flush()

    loaded = CharacterCache(tmp_path)
    assert loaded.get("One", "ravencrest", "EU", 71, "DAMAGER") is not None
    assert loaded.get("Two", "ravencrest", "EU", 72, "DAMAGER") is not None


def test_character_cache_clear_cancels_deferred_save(tmp_path):
    cache = CharacterCache(tmp_path, defer_saves=True, save_debounce_seconds=60.0)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")

    cache.clear()
    cache.flush()

    assert not cache._path.exists()
    assert cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None


def test_character_cache_ttl_override_is_instance_local(tmp_path):
    short_cache = CharacterCache(tmp_path / "short", ttl_seconds=1)
    default_cache = CharacterCache(tmp_path / "default")

    for cache in (short_cache, default_cache):
        cache._path.parent.mkdir(parents=True, exist_ok=True)
        cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")

    key = CharacterCache._key("Scout", "ravencrest", "EU", 71, "DAMAGER")
    stale_time = time.time() - 2
    short_cache._data[key].fetched_at = stale_time
    default_cache._data[key].fetched_at = stale_time

    assert short_cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None
    assert default_cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is not None
    assert CharacterCache.TTL_SECONDS == 12 * 60 * 60


def test_character_cache_get_sanitizes_scalar_percentiles(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        _ranks(),
        role="DAMAGER",
        metric_preferences=MetricPreferences(),
    )
    raw = json.loads(cache._path.read_text(encoding="utf-8"))
    key = CharacterCache._key(
        "Scout",
        "ravencrest",
        "EU",
        71,
        "DAMAGER",
        MetricPreferences(),
    )
    ranks = raw["entries"][key]["ranks"]
    ranks.update(
        {
            "raid_normal": "88.5",
            "raid_heroic": True,
            "raid_mythic": "101",
            "raid_normal_median": float("nan"),
            "raid_heroic_median": "bad",
            "raid_mythic_median": "-1",
            "mplus_dps": "62",
            "mplus_hps": float("inf"),
            "mplus_dps_median": None,
            "mplus_hps_median": "0",
        }
    )
    cache._path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = CharacterCache(tmp_path)
    result = loaded.get(
        "Scout",
        "ravencrest",
        "EU",
        71,
        "DAMAGER",
        metric_preferences=MetricPreferences(),
    )

    assert result is not None
    assert result.raid_normal == pytest.approx(88.5)
    assert result.raid_heroic is None
    assert result.raid_mythic is None
    assert result.raid_normal_median is None
    assert result.raid_heroic_median is None
    assert result.raid_mythic_median is None
    assert result.mplus_dps == pytest.approx(62.0)
    assert result.mplus_hps is None
    assert result.mplus_dps_median is None
    assert result.mplus_hps_median == pytest.approx(0.0)
    assert raw["__version__"] == _CACHE_VERSION


def test_character_cache_ignores_non_utf8_cache_file(tmp_path):
    cache_file = tmp_path / "character-cache.json"
    cache_file.write_bytes(b"\xff\xfe\xfa")

    loaded = CharacterCache(tmp_path)

    assert loaded.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None


def test_character_cache_clear_drops_memory_and_disk_entries(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")

    cache.clear()

    assert cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None
    assert not cache._path.exists()


def test_character_cache_clear_rejects_stale_generation_put(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    old_generation = cache.generation

    cache.clear()
    stored = cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        _ranks(),
        role="DAMAGER",
        expected_generation=old_generation,
    )

    assert stored is False
    assert cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None
    assert not cache._path.exists()


def test_character_cache_clear_during_get_rejects_selected_stale_entry(
    monkeypatch, tmp_path
):
    cache = CharacterCache(tmp_path)
    cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        _ranks(),
        role="DAMAGER",
    )
    original_ranks_from_entry = cache._ranks_from_entry

    def clear_before_rebuild(entry):
        cache.clear()
        return original_ranks_from_entry(entry)

    monkeypatch.setattr(cache, "_ranks_from_entry", clear_before_rebuild)

    assert cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None


def test_character_cache_put_accepts_current_generation_after_clear(tmp_path):
    cache = CharacterCache(tmp_path)
    old_generation = cache.generation

    cache.clear()
    current_generation = cache.generation
    stored = cache.put(
        "Scout",
        "ravencrest",
        "EU",
        71,
        _ranks(),
        role="DAMAGER",
        expected_generation=current_generation,
    )

    assert current_generation > old_generation
    assert stored is True
    assert cache.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is not None


def test_character_cache_get_discards_entries_missing_required_scalars(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    raw = json.loads(cache._path.read_text(encoding="utf-8"))
    key = CharacterCache._key("Scout", "ravencrest", "EU", 71, "DAMAGER")
    del raw["entries"][key]["ranks"]["raid_heroic"]
    cache._path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = CharacterCache(tmp_path)

    assert loaded.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None


def test_character_cache_get_discards_entries_with_malformed_fetched_at(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    raw = json.loads(cache._path.read_text(encoding="utf-8"))
    key = CharacterCache._key("Scout", "ravencrest", "EU", 71, "DAMAGER")
    raw["entries"][key]["fetched_at"] = "bad"
    cache._path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = CharacterCache(tmp_path)

    assert loaded.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None


def test_character_cache_get_discards_entries_with_future_fetched_at(tmp_path):
    cache = CharacterCache(tmp_path)
    cache.put("Scout", "ravencrest", "EU", 71, _ranks(), role="DAMAGER")
    raw = json.loads(cache._path.read_text(encoding="utf-8"))
    key = CharacterCache._key("Scout", "ravencrest", "EU", 71, "DAMAGER")
    raw["entries"][key]["fetched_at"] = time.time() + 365 * 24 * 60 * 60
    cache._path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = CharacterCache(tmp_path, ttl_seconds=1)

    assert loaded.get("Scout", "ravencrest", "EU", 71, "DAMAGER") is None


def test_fetch_character_ranks_rejects_non_dict_response():
    client = WCLClient(_FakeAuth(), region="EU")  # type: ignore[arg-type]
    client._http.close()
    client._http = _FakeHTTP(["not", "an", "object"])  # type: ignore[assignment]

    with pytest.raises(WCLApiError, match="Malformed WCL response"):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": {"characterData": "oops"}},
        {},
        {"data": {"rateLimitData": {}}},
        {"data": {"characterData": {}}},
    ],
)
def test_fetch_character_ranks_rejects_malformed_nested_graphql_data(payload):
    client, _http = _client_for_payload(payload)

    with pytest.raises(WCLApiError, match="Malformed WCL response") as exc:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)
    assert exc.value.error_kind == WCL_ERROR_MALFORMED


def test_fetch_character_ranks_prioritizes_not_found_error_over_malformed_nested_data():
    payload = _wcl_payload(_character())
    payload["data"]["characterData"] = "oops"
    payload["errors"] = [{"message": "Could not find character"}]
    client, _http = _client_for_payload(payload)

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.not_found is True
    assert result.error == "Could not find character"
    assert client.last_quota is not None
    assert client.last_quota.points_spent == pytest.approx(10)


def test_fetch_character_ranks_prioritizes_graphql_error_over_malformed_nested_data():
    payload = _wcl_payload(_character())
    payload["data"]["characterData"] = "oops"
    payload["errors"] = [{"message": "proxy exploded"}]
    client, _http = _client_for_payload(payload)

    with pytest.raises(WCLApiError, match="GraphQL error: proxy exploded") as exc:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert exc.value.error_kind == WCL_ERROR_GRAPHQL
    assert client.last_quota is not None
    assert client.last_quota.points_spent == pytest.approx(10)


def test_fetch_character_ranks_allows_explicit_character_null_as_not_found():
    client, _http = _client_for_payload(_wcl_payload(None))

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.not_found is True
    assert result.error == ""


def test_fetch_character_ranks_handles_graphql_error_without_data_as_not_found():
    client, _http = _client_for_payload(
        {"errors": [{"message": "Could not find character"}]}
    )

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.not_found is True
    assert result.error == "Could not find character"


def test_fetch_character_ranks_handles_graphql_error_without_data_as_graphql_error():
    client, _http = _client_for_payload({"data": None, "errors": [{"message": "boom"}]})

    with pytest.raises(WCLApiError, match="GraphQL error: boom") as exc:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert exc.value.error_kind == WCL_ERROR_GRAPHQL


def test_fetch_character_ranks_graphql_path_character_not_found_returns_not_found():
    client, _http = _client_for_payload(
        {
            "errors": [
                {
                    "message": "Not found",
                    "path": ["characterData", "character"],
                }
            ]
        }
    )

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.not_found is True
    assert result.error == "Not found"


@pytest.mark.parametrize(
    "message",
    [
        "Encounter not found",
        "Zone not found",
        "Server not found",
        "Could not find zone",
    ],
)
def test_fetch_character_ranks_graphql_non_character_not_found_raises_graphql(message):
    payload = _wcl_payload(_character())
    payload["errors"] = [{"message": message}]
    client, _http = _client_for_payload(payload)

    with pytest.raises(WCLApiError, match=f"GraphQL error: {message}") as exc:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert exc.value.error_kind == WCL_ERROR_GRAPHQL
    assert client.last_quota is not None
    assert client.last_quota.points_spent == pytest.approx(10)


def test_fetch_character_ranks_graphql_non_character_not_found_without_data_raises_graphql():
    client, _http = _client_for_payload(
        {"data": None, "errors": [{"message": "Server not found"}]}
    )

    with pytest.raises(WCLApiError, match="GraphQL error: Server not found") as exc:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert exc.value.error_kind == WCL_ERROR_GRAPHQL


def test_fetch_character_ranks_mixed_graphql_errors_do_not_mask_config_error():
    payload = _wcl_payload(_character())
    payload["errors"] = [
        {"message": "Could not find character"},
        {"message": "Encounter not found"},
    ]
    client, _http = _client_for_payload(payload)

    with pytest.raises(WCLApiError, match="GraphQL error: Encounter not found") as exc:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert exc.value.error_kind == WCL_ERROR_GRAPHQL


@pytest.mark.parametrize(
    ("errors", "expected_error", "not_found"),
    [
        (["boom"], "GraphQL error: boom", False),
        ([{"message": 123}], "GraphQL error: unknown error", False),
        ({"message": "proxy exploded"}, "GraphQL error: proxy exploded", False),
        ({"message": "could not find character"}, None, True),
    ],
)
def test_fetch_character_ranks_normalizes_malformed_graphql_errors(
    errors,
    expected_error,
    not_found,
):
    client, _http = _client_for_payload(_wcl_payload(None, errors=errors))

    if not_found:
        result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)
        assert result.not_found is True
        assert result.error == "could not find character"
        return

    with pytest.raises(WCLApiError, match=expected_error):
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)


def test_fetch_character_ranks_ignores_malformed_quota():
    payload = _wcl_payload(_character_with_empty_mplus())
    payload["data"]["rateLimitData"] = {
        "limitPerHour": "NaN",
        "pointsSpentThisHour": 10,
        "pointsResetIn": 300,
    }
    client, _http = _client_for_payload(payload)

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.not_found is False
    assert client.last_quota is None


def test_fetch_character_ranks_rejects_missing_enabled_raid_alias():
    character = _character_with_empty_mplus()
    del character["raidHeroic"]
    client, _http = _client_for_payload(_wcl_payload(character))

    with pytest.raises(WCLApiError, match="raidHeroic is missing") as exc:
        client.fetch_character_ranks(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=True,
                raid_mythic=False,
            ),
        )

    assert exc.value.error_kind == WCL_ERROR_MALFORMED


@pytest.mark.parametrize(
    ("alias_value", "expected_message"),
    [
        (None, "raidHeroic is null"),
        ("bad", "raidHeroic is not an object"),
        ([], "raidHeroic is not an object"),
    ],
)
def test_fetch_character_ranks_rejects_malformed_enabled_raid_alias(
    alias_value,
    expected_message,
):
    character = _character_with_empty_mplus()
    character["raidHeroic"] = alias_value
    client, _http = _client_for_payload(_wcl_payload(character))

    with pytest.raises(WCLApiError, match=expected_message) as exc:
        client.fetch_character_ranks(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=True,
                raid_mythic=False,
            ),
        )

    assert exc.value.error_kind == WCL_ERROR_MALFORMED


def test_fetch_character_ranks_allows_empty_enabled_raid_alias_object():
    character = _character_with_empty_mplus()
    character["raidHeroic"] = {}
    client, _http = _client_for_payload(_wcl_payload(character))

    result = client.fetch_character_ranks(
        "Scout",
        "ravencrest",
        spec_id=71,
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        ),
    )

    assert result.error == ""
    assert result.raid_heroic is None
    assert result.raid_heroic_median is None


def test_fetch_character_ranks_allows_missing_disabled_raid_alias():
    character = _character_with_empty_mplus()
    del character["raidNormal"]
    client, _http = _client_for_payload(_wcl_payload(character))

    result = client.fetch_character_ranks(
        "Scout",
        "ravencrest",
        spec_id=71,
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        ),
    )

    assert result.error == ""
    assert result.raid_normal is None
    assert result.raid_heroic == pytest.approx(81.0)


def test_fetch_character_ranks_graphql_error_precedes_malformed_raid_alias():
    payload = _wcl_payload(
        _character_with_empty_mplus(raidHeroic=None),
        errors=[{"message": "Encounter not found"}],
    )
    client, _http = _client_for_payload(payload)

    with pytest.raises(WCLApiError, match="GraphQL error: Encounter not found") as exc:
        client.fetch_character_ranks(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=True,
                raid_mythic=False,
            ),
        )

    assert exc.value.error_kind == WCL_ERROR_GRAPHQL


def test_fetch_character_ranks_character_not_found_precedes_malformed_raid_alias():
    payload = _wcl_payload(
        _character_with_empty_mplus(raidHeroic=None),
        errors=[{"message": "Could not find character"}],
    )
    client, _http = _client_for_payload(payload)

    result = client.fetch_character_ranks(
        "Scout",
        "ravencrest",
        spec_id=71,
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        ),
    )

    assert result.not_found is True
    assert result.error == "Could not find character"


def test_fetch_character_raid_boss_details_graphql_error_without_data_raises_graphql():
    client, _http = _client_for_payload(
        {"data": None, "errors": [{"message": "Encounter not found"}]}
    )

    with pytest.raises(WCLApiError, match="GraphQL error: Encounter not found") as exc:
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(mplus=False, raid_mythic=True),
        )

    assert exc.value.error_kind == WCL_ERROR_GRAPHQL


def test_fetch_character_raid_boss_details_character_not_found_without_data_returns_empty():
    client, _http = _client_for_payload(
        {"errors": [{"message": "Could not find character"}]}
    )

    result = client.fetch_character_raid_boss_details(
        "Scout",
        "ravencrest",
        spec_id=71,
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=True,
        ),
    )

    assert result == {}


def test_fetch_character_raid_boss_details_rejects_missing_enabled_alias():
    character = _character_with_empty_raid_boss_details("M")
    del character["raid_m_ia_overall"]
    client, _http = _client_for_payload(_wcl_payload(character))

    with pytest.raises(WCLApiError, match="raid_m_ia_overall is missing") as exc:
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=False,
                raid_mythic=True,
            ),
        )

    assert exc.value.error_kind == WCL_ERROR_MALFORMED


@pytest.mark.parametrize(
    ("alias_value", "expected_message"),
    [
        (None, "raid_m_ia_ilvl is null"),
        ("bad", "raid_m_ia_ilvl is not an object"),
        ({"ranks": None}, "raid_m_ia_ilvl.ranks is not a list"),
        ({"ranks": "bad"}, "raid_m_ia_ilvl.ranks is not a list"),
    ],
)
def test_fetch_character_raid_boss_details_rejects_malformed_enabled_alias(
    alias_value,
    expected_message,
):
    character = _character_with_empty_raid_boss_details(
        "M", raid_m_ia_ilvl=alias_value
    )
    client, _http = _client_for_payload(_wcl_payload(character))

    with pytest.raises(WCLApiError, match=expected_message) as exc:
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=False,
                raid_mythic=True,
            ),
        )

    assert exc.value.error_kind == WCL_ERROR_MALFORMED


def test_fetch_character_raid_boss_details_allows_empty_enabled_boss_detail_ranks():
    client, _http = _client_for_payload(
        _wcl_payload(_character_with_empty_raid_boss_details("M"))
    )

    rows = client.fetch_character_raid_boss_details(
        "Scout",
        "ravencrest",
        spec_id=71,
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=True,
        ),
    )

    assert rows == {}


def test_fetch_character_raid_boss_details_graphql_error_precedes_missing_detail_alias():
    character = _character_with_empty_raid_boss_details("M")
    del character["raid_m_ia_overall"]
    payload = _wcl_payload(character, errors=[{"message": "Encounter not found"}])
    client, _http = _client_for_payload(payload)

    with pytest.raises(WCLApiError, match="GraphQL error: Encounter not found") as exc:
        client.fetch_character_raid_boss_details(
            "Scout",
            "ravencrest",
            spec_id=71,
            metric_preferences=MetricPreferences(
                mplus=False,
                raid_normal=False,
                raid_heroic=False,
                raid_mythic=True,
            ),
        )

    assert exc.value.error_kind == WCL_ERROR_GRAPHQL


@pytest.mark.parametrize(
    ("alias_value", "expected_message"),
    [
        (None, "aa is null"),
        ({}, "aa.ranks is missing"),
        ({"ranks": None}, "aa.ranks is not a list"),
        ({"ranks": "bad"}, "aa.ranks is not a list"),
    ],
)
def test_fetch_character_ranks_rejects_malformed_mplus_alias_payload(
    alias_value,
    expected_message,
):
    character = _character_with_empty_mplus()
    character["aa"] = alias_value
    client, _http = _client_for_payload(_wcl_payload(character))

    with pytest.raises(WCLApiError, match=expected_message) as exc:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert exc.value.error_kind == WCL_ERROR_MALFORMED


def test_fetch_character_ranks_rejects_missing_mplus_alias():
    character = _character_with_empty_mplus()
    del character["aa"]
    client, _http = _client_for_payload(_wcl_payload(character))

    with pytest.raises(WCLApiError, match="aa is missing") as exc:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert exc.value.error_kind == WCL_ERROR_MALFORMED


def test_fetch_character_ranks_allows_empty_mplus_ranks_lists():
    client, _http = _client_for_payload(_wcl_payload(_character_with_empty_mplus()))

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.error == ""
    assert result.mplus_dps is None
    assert result.mplus_dps_breakdown == []


def test_fetch_character_ranks_graphql_error_precedes_malformed_mplus_alias():
    payload = _wcl_payload(
        _character_with_empty_mplus(aa={"ranks": "bad"}),
        errors=[{"message": "Encounter not found"}],
    )
    client, _http = _client_for_payload(payload)

    with pytest.raises(WCLApiError, match="GraphQL error: Encounter not found") as exc:
        client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert exc.value.error_kind == WCL_ERROR_GRAPHQL


def test_fetch_character_ranks_character_not_found_precedes_malformed_mplus_alias():
    payload = _wcl_payload(
        _character_with_empty_mplus(aa={"ranks": "bad"}),
        errors=[{"message": "Could not find character"}],
    )
    client, _http = _client_for_payload(payload)

    result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)

    assert result.not_found is True
    assert result.error == "Could not find character"


def test_oauth_refresh_malformed_json_raises_wcl_auth_error(monkeypatch, tmp_path):
    class _OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse({}, json_error=ValueError("bad json"))

    monkeypatch.setattr(wcl_mod.httpx, "Client", _OAuthClient)
    auth = WCLAuth("client", "secret", tmp_path)

    with pytest.raises(WCLAuthError, match="Malformed OAuth response"):
        auth.get_token()


def test_oauth_refresh_429_raises_retryable_rate_limited_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    oauth_http = _OAuthHTTP(_FakeResponse({"error": "slow down"}, status_code=429))
    monkeypatch.setattr(wcl_mod.httpx, "Client", lambda *args, **kwargs: oauth_http)
    auth = WCLAuth("client", "secret", tmp_path)

    with pytest.raises(WCLAuthError, match="OAuth failed \\(HTTP 429\\)") as excinfo:
        auth.get_token()

    assert excinfo.value.error_kind == WCL_ERROR_RATE_LIMITED
    assert len(oauth_http.calls) == 1


def test_oauth_refresh_503_raises_retryable_server_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    oauth_http = _OAuthHTTP(_FakeResponse({"error": "unavailable"}, status_code=503))
    monkeypatch.setattr(wcl_mod.httpx, "Client", lambda *args, **kwargs: oauth_http)
    auth = WCLAuth("client", "secret", tmp_path)

    with pytest.raises(WCLAuthError, match="OAuth failed \\(HTTP 503\\)") as excinfo:
        auth.get_token()

    assert excinfo.value.error_kind == WCL_ERROR_SERVER
    assert len(oauth_http.calls) == 1


def test_oauth_http_400_remains_non_retryable_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    oauth_http = _OAuthHTTP(_FakeResponse({"error": "bad client"}, status_code=400))
    monkeypatch.setattr(wcl_mod.httpx, "Client", lambda *args, **kwargs: oauth_http)
    auth = WCLAuth("client", "secret", tmp_path)

    with pytest.raises(WCLAuthError, match="OAuth failed \\(HTTP 400\\)") as excinfo:
        auth.get_token()

    assert excinfo.value.error_kind == WCL_ERROR_AUTH


def test_oauth_malformed_200_remains_non_retryable_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    oauth_http = _OAuthHTTP(_FakeResponse({}, json_error=ValueError("bad json")))
    monkeypatch.setattr(wcl_mod.httpx, "Client", lambda *args, **kwargs: oauth_http)
    auth = WCLAuth("client", "secret", tmp_path)

    with pytest.raises(WCLAuthError, match="Malformed OAuth response") as excinfo:
        auth.get_token()

    assert excinfo.value.error_kind == ""


def test_oauth_http_503_preserves_existing_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    token_path = tmp_path / "token.json"
    token_path.write_text("old-token", encoding="utf-8")
    oauth_http = _OAuthHTTP(_FakeResponse({"error": "unavailable"}, status_code=503))
    monkeypatch.setattr(wcl_mod.httpx, "Client", lambda *args, **kwargs: oauth_http)
    auth = WCLAuth("client", "secret", tmp_path)

    with pytest.raises(WCLAuthError):
        auth.get_token()

    assert token_path.read_text(encoding="utf-8") == "old-token"


def test_oauth_http_503_status_wins_over_malformed_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    oauth_http = _OAuthHTTP(
        _FakeResponse({}, status_code=503, json_error=ValueError("bad json"))
    )
    monkeypatch.setattr(wcl_mod.httpx, "Client", lambda *args, **kwargs: oauth_http)
    auth = WCLAuth("client", "secret", tmp_path)

    with pytest.raises(WCLAuthError, match="OAuth failed \\(HTTP 503\\)") as excinfo:
        auth.get_token()

    assert excinfo.value.error_kind == WCL_ERROR_SERVER


def test_oauth_token_save_failure_preserves_previous_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    token_path = tmp_path / "token.json"
    token_path.write_text("old-token", encoding="utf-8")

    class _OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse({"access_token": "fresh-token", "expires_in": 3600})

    def fail_replace(_src: object, _dst: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(wcl_mod.httpx, "Client", _OAuthClient)
    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)
    auth = WCLAuth("client", "secret", tmp_path)

    assert auth.get_token() == "fresh-token"
    assert token_path.read_text(encoding="utf-8") == "old-token"
    assert list(tmp_path.glob(".token.json.*.tmp")) == []


def test_oauth_invalidate_after_parallel_refresh_forces_next_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    started = threading.Event()
    release = threading.Event()
    posts: list[int] = []
    results: list[str] = []

    class _OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            call_index = len(posts)
            posts.append(call_index)
            if call_index == 0:
                started.set()
                assert release.wait(timeout=2.0)
                return _FakeResponse({"access_token": "refresh-token", "expires_in": 3600})
            return _FakeResponse({"access_token": "after-reset-token", "expires_in": 3600})

    monkeypatch.setattr(wcl_mod.httpx, "Client", _OAuthClient)
    auth = WCLAuth("client", "secret", tmp_path)

    refresh_thread = threading.Thread(target=lambda: results.append(auth.get_token()))
    refresh_thread.start()
    assert started.wait(timeout=2.0)

    try:
        invalidate_thread = threading.Thread(target=auth.invalidate)
        invalidate_thread.start()
        invalidate_thread.join(timeout=0.2)
        assert not invalidate_thread.is_alive()
    finally:
        release.set()
        refresh_thread.join(timeout=2.0)

    assert not refresh_thread.is_alive()
    assert results == ["refresh-token"]
    assert auth.get_token() == "after-reset-token"
    assert posts == [0, 1]


def test_oauth_auth_applies_private_mode_to_existing_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    token_path = tmp_path / "token.json"
    token_path.write_text(
        json.dumps(
            {
                "access_token": "cached-token",
                "expires_at": time.time() + 3600,
                "client_fingerprint": wcl_mod._client_fingerprint("client", "secret"),
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[object, int]] = []
    path_type = type(token_path)
    original_chmod = path_type.chmod

    def record_chmod(self, mode: int) -> None:
        calls.append((self, mode))
        original_chmod(self, mode)

    monkeypatch.setattr(path_type, "chmod", record_chmod)

    auth = WCLAuth("client", "secret", tmp_path)

    assert auth.get_token() == "cached-token"
    assert (token_path, stat.S_IRUSR | stat.S_IWUSR) in calls


def test_oauth_auth_private_mode_failure_does_not_break_cached_token_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    token_path = tmp_path / "token.json"
    token_path.write_text(
        json.dumps(
            {
                "access_token": "cached-token",
                "expires_at": time.time() + 3600,
                "client_fingerprint": wcl_mod._client_fingerprint("client", "secret"),
            }
        ),
        encoding="utf-8",
    )
    calls: list[object] = []

    def fail_chmod(self, _mode: int) -> None:
        calls.append(self)
        raise PermissionError("policy")

    monkeypatch.setattr(type(token_path), "chmod", fail_chmod)

    auth = WCLAuth("client", "secret", tmp_path)

    assert auth.get_token() == "cached-token"
    assert token_path in calls


def test_oauth_cached_token_ignored_for_different_client_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    token_path = tmp_path / "token.json"
    old_fingerprint = wcl_mod._client_fingerprint("old-client", "secret")
    token_path.write_text(
        json.dumps(
            {
                "access_token": "old-token",
                "expires_at": time.time() + 3600,
                "client_fingerprint": old_fingerprint,
            }
        ),
        encoding="utf-8",
    )

    class _OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse({"access_token": "fresh-token", "expires_in": 3600})

    monkeypatch.setattr(wcl_mod.httpx, "Client", _OAuthClient)
    auth = WCLAuth("new-client", "secret", tmp_path)

    assert auth.get_token() == "fresh-token"


def test_oauth_cached_token_ignored_for_same_client_different_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    token_path = tmp_path / "token.json"
    old_fingerprint = wcl_mod._client_fingerprint("client", "old-secret")
    token_path.write_text(
        json.dumps(
            {
                "access_token": "old-token",
                "expires_at": time.time() + 3600,
                "client_fingerprint": old_fingerprint,
            }
        ),
        encoding="utf-8",
    )

    class _OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse({"access_token": "fresh-token", "expires_in": 3600})

    monkeypatch.setattr(wcl_mod.httpx, "Client", _OAuthClient)
    auth = WCLAuth("client", "new-secret", tmp_path)

    assert auth.get_token() == "fresh-token"


def test_oauth_cached_token_without_fingerprint_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    token_path = tmp_path / "token.json"
    token_path.write_text(
        json.dumps({"access_token": "legacy-token", "expires_at": time.time() + 3600}),
        encoding="utf-8",
    )

    class _OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse({"access_token": "fresh-token", "expires_in": 3600})

    monkeypatch.setattr(wcl_mod.httpx, "Client", _OAuthClient)
    auth = WCLAuth("client", "secret", tmp_path)

    assert auth.get_token() == "fresh-token"


@pytest.mark.parametrize(
    "access_token",
    [123, "", "   "],
)
def test_oauth_cached_token_invalid_access_token_is_ignored_and_refreshed(
    monkeypatch: pytest.MonkeyPatch, tmp_path, access_token
):
    token_path = tmp_path / "token.json"
    token_path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "expires_at": time.time() + 3600,
                "client_fingerprint": wcl_mod._client_fingerprint(
                    "client", "secret"
                ),
            }
        ),
        encoding="utf-8",
    )

    class _OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse({"access_token": "fresh-token", "expires_in": 3600})

    monkeypatch.setattr(wcl_mod.httpx, "Client", _OAuthClient)
    auth = WCLAuth("client", "secret", tmp_path)

    assert auth.get_token() == "fresh-token"


@pytest.mark.parametrize(
    "expires_at",
    ["123", True, float("nan"), float("inf"), -1],
)
def test_oauth_cached_token_invalid_expires_at_is_ignored_and_refreshed(
    monkeypatch: pytest.MonkeyPatch, tmp_path, expires_at
):
    token_path = tmp_path / "token.json"
    token_path.write_text(
        json.dumps(
            {
                "access_token": "cached-token",
                "expires_at": expires_at,
                "client_fingerprint": wcl_mod._client_fingerprint(
                    "client", "secret"
                ),
            }
        ),
        encoding="utf-8",
    )

    class _OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse({"access_token": "fresh-token", "expires_in": 3600})

    monkeypatch.setattr(wcl_mod.httpx, "Client", _OAuthClient)
    auth = WCLAuth("client", "secret", tmp_path)

    assert auth.get_token() == "fresh-token"


def test_oauth_refresh_invalid_expires_in_raises_wcl_auth_error(
    monkeypatch, tmp_path
):
    class _OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse(
                {"access_token": "token", "expires_in": float("inf")}
            )

    monkeypatch.setattr(wcl_mod.httpx, "Client", _OAuthClient)
    auth = WCLAuth("client", "secret", tmp_path)

    with pytest.raises(WCLAuthError, match="invalid expires_in"):
        auth.get_token()


def test_dict_to_dungeon_perf_accepts_numeric_string_percentiles():
    result = _dict_to_dungeon_perf(
        {
            "name": "Pit of Saron",
            "parse_percent": "80.5",
            "median_percent": "62",
            "key_level": "14",
            "run_count": "3",
        }
    )

    assert result.name == "Pit of Saron"
    assert result.parse_percent == pytest.approx(80.5)
    assert result.median_percent == pytest.approx(62.0)
    assert result.key_level == 14
    assert result.run_count == 3


@pytest.mark.parametrize(
    ("bad_parse", "bad_median"),
    [
        (True, False),
        ("garbage", "101"),
        (float("nan"), float("inf")),
        (-1.0, 100.1),
    ],
)
def test_dict_to_dungeon_perf_rejects_malformed_percentiles(
    bad_parse,
    bad_median,
):
    result = _dict_to_dungeon_perf(
        {
            "name": "Skyreach",
            "parse_percent": bad_parse,
            "median_percent": bad_median,
            "key_level": 12,
            "run_count": 2,
        }
    )

    assert result.name == "Skyreach"
    assert result.parse_percent is None
    assert result.median_percent is None
    assert result.key_level == 12
    assert result.run_count == 2


def test_dict_to_dungeon_perf_malformed_key_run_fields_default_independently():
    result = _dict_to_dungeon_perf(
        {
            "name": "Bad Cache",
            "parse_percent": 75,
            "median_percent": 55,
            "key_level": "14.5",
            "run_count": True,
        }
    )

    assert result.parse_percent == pytest.approx(75.0)
    assert result.median_percent == pytest.approx(55.0)
    assert result.key_level == 0
    assert result.run_count == 0


def test_dict_to_dungeon_perf_rebuilds_nested_brackets_safely():
    result = _dict_to_dungeon_perf(
        {
            "name": "Skyreach",
            "parse_percent": 31,
            "median_percent": 31,
            "key_level": 20,
            "run_count": 1,
            "brackets": [
                {
                    "key_level": "16",
                    "parse_percent": "88",
                    "median_percent": "78",
                    "run_count": "2",
                },
                {
                    "key_level": "14.5",
                    "parse_percent": True,
                    "median_percent": 80,
                    "run_count": 1,
                },
            ],
        }
    )

    assert result.brackets == [
        KeyBracketPerf(
            key_level=16,
            parse_percent=88.0,
            median_percent=78.0,
            run_count=2,
        )
    ]


def test_headline_all_parse_none():
    breakdown = [
        DungeonPerf(
            name="A", parse_percent=None, median_percent=None, key_level=0, run_count=0
        )
    ]
    best, median = _compute_mplus_headline(breakdown)
    assert best is None
    assert median is None


def test_headline_all_runs_one_excludes_median():
    # All entries N=1 → median_avg None; best_avg still computed.
    breakdown = [
        _dp(best=80.0, median=80.0, runs=1),
        _dp(best=60.0, median=60.0, runs=1),
    ]
    best, median = _compute_mplus_headline(breakdown)
    assert best == pytest.approx(70.0)
    assert median is None


def test_headline_mixed_run_counts_median_uses_only_multi_run():
    # Explicit construction (no _dp defaults) — proves run_count >= 2 gate.
    # Two runs=1 entries with REAL median values; one runs=3 entry.
    # median_avg should equal ONLY the runs=3 entry's median (other two filtered).
    breakdown = [
        DungeonPerf(
            name="A", parse_percent=80.0, median_percent=75.0, key_level=10, run_count=1
        ),
        DungeonPerf(
            name="B", parse_percent=90.0, median_percent=85.0, key_level=12, run_count=1
        ),
        DungeonPerf(
            name="C", parse_percent=70.0, median_percent=65.0, key_level=11, run_count=3
        ),
    ]
    best, median = _compute_mplus_headline(breakdown)
    assert best == pytest.approx((80.0 + 90.0 + 70.0) / 3)
    assert median == pytest.approx(65.0)  # only C contributes


def test_headline_single_multi_run_entry():
    breakdown = [_dp(best=85.0, median=75.0, runs=2)]
    best, median = _compute_mplus_headline(breakdown)
    assert best == pytest.approx(85.0)
    assert median == pytest.approx(75.0)


def test_headline_default_constructed_dungeonperf_excluded():
    # Back-compat default DungeonPerf (run_count=0, median_percent=None) must
    # contribute to neither average. Pins behavior of the cache back-compat path.
    breakdown = [DungeonPerf(name="X", parse_percent=None)]
    best, median = _compute_mplus_headline(breakdown)
    assert best is None
    assert median is None
