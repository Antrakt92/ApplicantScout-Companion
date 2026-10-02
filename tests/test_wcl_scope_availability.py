"""Private aliases retain their status when broader responses are projected."""

import pytest

import applicant_scout.wcl as wcl
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Listing, WoWPlayer
from applicant_scout.wcl import CharacterCache, WCL_ERROR_RESTRICTED
from test_overlay_fetch_identity import _app, _QueuedPool, _window
from test_wcl import _client_for_payload, _wcl_payload


_H = MetricPreferences(
    mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=False
)
_M = MetricPreferences(
    mplus=False, raid_normal=False, raid_heroic=False, raid_mythic=True
)
_HM = MetricPreferences(
    mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=True
)
_PRIVATE = {"error": "You do not have permission to see this character's rankings."}


def _summary(*, private=True):
    return _wcl_payload(
        {
            "raidHeroic": dict(_PRIVATE) if private else {},
            "raidMythic": {
                "bestPerformanceAverage": 88.0,
                "medianPerformanceAverage": 77.0,
            },
        }
    )


def _parse(data, preferences):
    return wcl._ranks_from_graphql(
        data, metric_preferences=preferences, spec_name="Arms"
    )


def test_projected_private_scope_matches_direct_private_query():
    data = _summary()
    direct = _parse(data, _H)
    broad = _parse(data, _HM)
    assert broad.error_kind == "" and broad.raid_mythic == 88
    projected = wcl._project_ranks_to_metric_preferences(broad, _H)
    assert projected.error_kind == direct.error_kind == WCL_ERROR_RESTRICTED
    assert projected.error == direct.error
    public = wcl._project_ranks_to_metric_preferences(broad, _M)
    assert public.error_kind == "" and public.raid_mythic == 88


@pytest.mark.parametrize("reload", [False, True])
def test_cache_scope_projection_preserves_private_status_and_public_metrics(
    tmp_path, reload
):
    cache = CharacterCache(tmp_path)
    assert cache.put(
        "Scout", "realma", "EU", 71, _parse(_summary(), _HM), metric_preferences=_HM
    )
    if reload:
        cache = CharacterCache(tmp_path)
    private = cache.get("Scout", "realma", "EU", 71, metric_preferences=_H)
    public = cache.get("Scout", "realma", "EU", 71, metric_preferences=_M)
    assert private is not None and private.error_kind == WCL_ERROR_RESTRICTED
    assert public is not None and public.error_kind == "" and public.raid_mythic == 88


@pytest.mark.parametrize("private", [False, True])
def test_applicant_and_qt_narrowing_preserve_scope_status_without_refetch(
    qtbot, tmp_path, private
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=_HM)
    window._pool = _QueuedPool()
    try:
        window._launch_fetch(app)
        task = window._pool.tasks[0]
        window._on_fetch_done(task._identity, _parse(_summary(private=private), _HM))
        assert app.fetch_status == "ready" and app.raid_mythic == 88
        window.apply_metric_preferences(_H)
        assert app.fetch_status == ("restricted" if private else "ready")
        assert app.wcl_error_kind == (WCL_ERROR_RESTRICTED if private else "")
        assert app.raid_heroic is app.raid_mythic is None
        assert len(window._pool.tasks) == 1
    finally:
        client.close()


@pytest.mark.parametrize("private", [False, True])
def test_all_private_manual_boss_aliases_are_restricted_while_accessible_empty_is_valid(
    private,
):
    char = {}
    prefix = wcl._RAID_DETAIL_DIFFICULTIES["H"][0]
    for alias, _encounter, _name in wcl.CURRENT_RAID_ENCOUNTERS:
        for metric in ("overall", "ilvl"):
            char[f"{prefix}_{alias}_{metric}"] = (
                dict(_PRIVATE) if private else {"ranks": []}
            )
    client, _http = _client_for_payload(_wcl_payload(char))
    try:
        if private:
            rows = client.fetch_character_raid_boss_details(
                "Scout", "realma", 71, metric_preferences=_H
            )
            assert getattr(rows, "availability", {}) == {"H": "restricted"}
        else:
            rows = client.fetch_character_raid_boss_details(
                "Scout", "realma", 71, metric_preferences=_H
            )
            assert not any(rows.values())
            assert getattr(rows, "availability", {}) == {"H": "available"}
    finally:
        client.close()


def _boss_rows(*, partial_within_heroic=False):
    char = {}
    for difficulty in ("H", "M"):
        prefix = wcl._RAID_DETAIL_DIFFICULTIES[difficulty][0]
        for alias, _encounter, _name in wcl.CURRENT_RAID_ENCOUNTERS:
            for metric in ("overall", "ilvl"):
                char[f"{prefix}_{alias}_{metric}"] = (
                    dict(_PRIVATE) if difficulty == "H" else {"ranks": []}
                )
    first_alias = wcl.CURRENT_RAID_ENCOUNTERS[0][0]
    public_alias = f"{wcl._RAID_DETAIL_DIFFICULTIES['M'][0]}_{first_alias}_overall"
    char[public_alias] = {"ranks": [{"spec": "Arms", "rankPercent": 88.0}]}
    if partial_within_heroic:
        char[f"{wcl._RAID_DETAIL_DIFFICULTIES['H'][0]}_{first_alias}_overall"] = {
            "ranks": [{"spec": "Arms", "rankPercent": 77.0}]
        }
    client, _http = _client_for_payload(_wcl_payload(char))
    try:
        return client.fetch_character_raid_boss_details(
            "Scout", "realma", 71, metric_preferences=_HM
        )
    finally:
        client.close()


@pytest.mark.parametrize("partial_within_heroic", [False, True])
def test_manual_detail_private_aliases_preserve_public_rows_and_per_difficulty_status(
    partial_within_heroic,
):
    rows = _boss_rows(partial_within_heroic=partial_within_heroic)
    assert rows["M"][0]["overall"] == 88
    assert getattr(rows, "availability", {}) == {
        "H": "partial" if partial_within_heroic else "restricted",
        "M": "available",
    }
    if partial_within_heroic:
        assert rows["H"][0]["overall"] == 77


@pytest.mark.parametrize("reload", [False, True])
def test_detail_cache_preserves_restricted_scope_projection(tmp_path, reload):
    cache = CharacterCache(tmp_path)
    assert cache.put_raid_boss_details(
        "Scout", "realma", "EU", 71, _boss_rows(), metric_preferences=_HM
    )
    if reload:
        cache = CharacterCache(tmp_path)
    private = cache.get_raid_boss_details(
        "Scout", "realma", "EU", 71, metric_preferences=_H
    )
    public = cache.get_raid_boss_details(
        "Scout", "realma", "EU", 71, metric_preferences=_M
    )
    assert private is not None and getattr(private, "availability", {}) == {
        "H": "restricted"
    }
    assert public is not None and getattr(public, "availability", {}) == {
        "M": "available"
    }
    assert public["M"][0]["overall"] == 88


def test_qt_manual_partial_detail_request_preserves_status_and_does_not_refetch_automatically(
    qtbot, tmp_path, monkeypatch
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    state.listing = Listing(999, "Raid", "Raid", "", 0, difficulty_id=16)
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=_HM)
    window._pool = _QueuedPool()
    rows = _boss_rows()
    calls = []
    monkeypatch.setattr(
        client,
        "fetch_character_raid_boss_details",
        lambda *_a, **_k: calls.append("manual network") or rows,
    )
    try:
        window._launch_fetch(app)
        window._on_fetch_done(
            window._pool.tasks[0]._identity, _parse(_summary(private=False), _HM)
        )
        window._panel._set_detail_mode("raid")
        assert len(window._pool.tasks) == 1
        assert window._launch_raid_boss_fetch_if_needed(app)
        window._pool.tasks[-1].run()
        assert calls == ["manual network"]
        assert app.raid_boss_parses["M"][0]["overall"] == 88
        assert app.raid_boss_availability == {"H": "restricted", "M": "available"}
        window.apply_metric_preferences(_H)
        window._retry_ready_raid_boss_fetches()
        assert calls == ["manual network"]
        assert len(window._pool.tasks) == 2
    finally:
        client.close()


def test_qt_all_private_manual_details_are_terminal_restricted_not_loaded_empty(
    qtbot, tmp_path, monkeypatch
):
    char = {}
    prefix = wcl._RAID_DETAIL_DIFFICULTIES["H"][0]
    for alias, _encounter, _name in wcl.CURRENT_RAID_ENCOUNTERS:
        for metric in ("overall", "ilvl"):
            char[f"{prefix}_{alias}_{metric}"] = dict(_PRIVATE)
    parser_client, _http = _client_for_payload(_wcl_payload(char))
    try:
        rows = parser_client.fetch_character_raid_boss_details(
            "Scout", "realma", 71, metric_preferences=_H
        )
    finally:
        parser_client.close()
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    state.listing = Listing(999, "Raid", "Raid", "", 0, difficulty_id=15)
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=_H)
    window._pool = _QueuedPool()
    calls = []
    monkeypatch.setattr(
        client,
        "fetch_character_raid_boss_details",
        lambda *_a, **_k: calls.append("manual network") or rows,
    )
    try:
        window._launch_fetch(app)
        window._on_fetch_done(
            window._pool.tasks[0]._identity, _parse(_summary(private=False), _H)
        )
        window._panel._set_detail_mode("raid")
        assert window._launch_raid_boss_fetch_if_needed(app)
        window._pool.tasks[-1].run()
        assert calls == ["manual network"]
        assert app.raid_boss_availability == {"H": "restricted"}
        assert "H" not in app.raid_boss_parses
        assert window._raid_detail_status_for(app) == (
            "Raid boss rankings are private on Warcraft Logs",
            False,
            False,
        )
        assert not window._launch_raid_boss_fetch_if_needed(app)
        window._retry_ready_raid_boss_fetches()
        assert calls == ["manual network"] and len(window._pool.tasks) == 2
    finally:
        client.close()


def test_manual_detail_missing_character_is_distinct_from_accessible_empty():
    client, _http = _client_for_payload(_wcl_payload(None))
    try:
        rows = client.fetch_character_raid_boss_details(
            "Scout", "realma", 71, metric_preferences=_H
        )
        assert getattr(rows, "not_found", False)
        assert not any(rows.values())
    finally:
        client.close()


def test_private_boss_alias_with_numeric_ranks_does_not_leak_private_metric():
    char = {}
    prefix = wcl._RAID_DETAIL_DIFFICULTIES["H"][0]
    for alias, _encounter, _name in wcl.CURRENT_RAID_ENCOUNTERS:
        for metric in ("overall", "ilvl"):
            char[f"{prefix}_{alias}_{metric}"] = {"ranks": []}
    alias = wcl.CURRENT_RAID_ENCOUNTERS[0][0]
    char[f"{prefix}_{alias}_overall"] = {
        "ranks": [{"spec": "Arms", "rankPercent": 77.0}]
    }
    char[f"{prefix}_{alias}_ilvl"] = {
        **_PRIVATE,
        "ranks": [{"spec": "Arms", "rankPercent": 99.0}],
    }
    client, _http = _client_for_payload(_wcl_payload(char))
    try:
        rows = client.fetch_character_raid_boss_details(
            "Scout", "realma", 71, metric_preferences=_H
        )
        assert rows.availability == {"H": "partial"}
        assert rows["H"][0]["overall"] == 77
        assert rows["H"][0]["ilvl"] is None
    finally:
        client.close()


def test_qt_manual_missing_character_counts_as_successful_api_without_empty_cache(
    qtbot, tmp_path, monkeypatch
):
    parser_client, _http = _client_for_payload(_wcl_payload(None))
    try:
        rows = parser_client.fetch_character_raid_boss_details(
            "Scout", "realma", 71, metric_preferences=_H
        )
    finally:
        parser_client.close()
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    state.listing = Listing(999, "Raid", "Raid", "", 0, difficulty_id=15)
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=_H)
    window._pool = _QueuedPool()
    results = []
    monkeypatch.setattr(
        client, "record_api_result", lambda **kwargs: results.append(kwargs)
    )
    monkeypatch.setattr(
        client, "fetch_character_raid_boss_details", lambda *_a, **_k: rows
    )
    try:
        window._launch_fetch(app)
        window._on_fetch_done(
            window._pool.tasks[0]._identity, _parse(_summary(private=False), _H)
        )
        window._panel._set_detail_mode("raid")
        assert window._launch_raid_boss_fetch_if_needed(app)
        window._pool.tasks[-1].run()
        assert len(results) == 1 and results[0]["succeeded"] is True
        assert (
            window._cache.get_raid_boss_details(
                "Scout", "realma", "EU", 71, metric_preferences=_H
            )
            is None
        )
    finally:
        client.close()
