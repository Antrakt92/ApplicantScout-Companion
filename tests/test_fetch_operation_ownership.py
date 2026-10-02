"""Repeated equal fetch targets still represent different operation lifetimes."""

import pytest

from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Listing, WoWPlayer
from applicant_scout.wcl import CharacterRanks, WCL_ERROR_SERVER
from test_overlay_fetch_identity import (
    _app,
    _member,
    _QueuedPool,
    _ranks_with,
    _ShutdownPool,
    _window,
)


_ON = MetricPreferences()
_OFF = MetricPreferences(
    mplus=False, raid_normal=False, raid_heroic=False, raid_mythic=False
)


def _setup(qtbot, tmp_path, *, party=False):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    member = _member(fetch_status="pending") if party else None
    if member is not None:
        state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _QueuedPool()
    return state, app, member, window, client


def _toggle(window):
    window.apply_metric_preferences(_OFF)
    window.apply_metric_preferences(_ON)


@pytest.mark.parametrize("result", ["success", "error", "not_found"])
@pytest.mark.parametrize("current_finished", [False, True])
def test_old_summary_callback_cannot_take_reenabled_target_waiters(
    qtbot,
    tmp_path,
    result,
    current_finished,
):
    _state, app, member, window, client = _setup(qtbot, tmp_path, party=True)
    try:
        window._launch_fetch(app)
        window._launch_fetch(member)
        old = window._pool.tasks[0]
        _toggle(window)
        current = window._pool.tasks[-1]
        assert current is not old
        if current_finished:
            window._on_fetch_done(
                current._identity, _ranks_with(raid_heroic=88, mplus_dps=89)
            )
        tracked = dict(window._fetches_in_flight)
        waiters = {
            key: dict(value) for key, value in window._fetch_waiters_by_target.items()
        }
        old_result = (
            _ranks_with(raid_heroic=11, mplus_dps=12)
            if result == "success"
            else CharacterRanks.empty(not_found=True)
            if result == "not_found"
            else CharacterRanks.empty(error="old failure", error_kind=WCL_ERROR_SERVER)
        )
        window._on_fetch_done(old._identity, old_result)
        assert window._fetches_in_flight == tracked
        assert window._fetch_waiters_by_target == waiters
        for row in (app, member):
            assert row.raid_heroic == (88 if current_finished else None)
            assert row.fetch_status == ("ready" if current_finished else "loading")
        if not current_finished:
            window._on_fetch_done(
                current._identity, _ranks_with(raid_heroic=88, mplus_dps=89)
            )
            assert app.raid_heroic == member.raid_heroic == 88
    finally:
        client.close()


def test_retired_queued_summary_worker_spends_no_network_request(
    qtbot, tmp_path, monkeypatch
):
    _state, app, _member_row, window, client = _setup(qtbot, tmp_path)
    calls = []
    monkeypatch.setattr(
        client,
        "fetch_character_ranks",
        lambda *_a, **_k: (
            calls.append("network") or _ranks_with(raid_heroic=88, mplus_dps=89)
        ),
    )
    try:
        window._launch_fetch(app)
        old = window._pool.tasks[0]
        _toggle(window)
        old.run()
        assert calls == []
        assert app.fetch_status == "loading"
    finally:
        client.close()


def _detail_setup(qtbot, tmp_path):
    state, app, member, window, client = _setup(qtbot, tmp_path, party=True)
    state.listing = Listing(999, "Raid", "Raid", "", 0, difficulty_id=16)
    app.fetch_status = member.fetch_status = "ready"
    window._panel._set_detail_mode("raid")
    return state, app, member, window, client


@pytest.mark.parametrize("result", ["success", "error"])
@pytest.mark.parametrize("current_finished", [False, True])
def test_old_manual_detail_callback_cannot_consume_new_waiters(
    qtbot,
    tmp_path,
    result,
    current_finished,
):
    _state, app, member, window, client = _detail_setup(qtbot, tmp_path)
    rows = {"M": [{"name": "Current boss", "best": 88}]}
    try:
        assert window._launch_raid_boss_fetch_if_needed(app)
        assert window._launch_raid_boss_fetch_if_needed(member)
        old = window._pool.tasks[0]
        _toggle(window)
        # Re-enabling queues summary work, never automatic boss detail. Complete
        # that real prerequisite before issuing the next explicit manual request.
        assert (
            sum(
                type(task).__name__ == "_RaidBossFetchTask"
                for task in window._pool.tasks
            )
            == 1
        )
        summary = window._pool.tasks[-1]
        assert type(summary).__name__ == "_FetchTask"
        window._on_fetch_done(
            summary._identity, _ranks_with(raid_heroic=88, mplus_dps=89)
        )
        assert window._launch_raid_boss_fetch_if_needed(app)
        assert window._launch_raid_boss_fetch_if_needed(member)
        current = window._pool.tasks[-1]
        assert current is not old
        if current_finished:
            window._on_raid_boss_fetch_done(current._identity, rows, "")
        tracked = dict(window._raid_boss_fetches_in_flight)
        waiters = {
            key: dict(value)
            for key, value in window._raid_boss_fetch_waiters_by_target.items()
        }
        failures = dict(window._raid_boss_fetch_failures)
        window._on_raid_boss_fetch_done(
            old._identity,
            {"M": [{"name": "Old boss", "best": 11}]} if result == "success" else {},
            "old failure" if result == "error" else "",
            WCL_ERROR_SERVER if result == "error" else "",
        )
        assert window._raid_boss_fetches_in_flight == tracked
        assert window._raid_boss_fetch_waiters_by_target == waiters
        assert window._raid_boss_fetch_failures == failures
        assert (
            app.raid_boss_parses
            == member.raid_boss_parses
            == ({"N": [], "H": [], **rows} if current_finished else {})
        )
    finally:
        client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_retirement_during_cache_lookup_prevents_subsequent_network(
    qtbot, tmp_path, monkeypatch, detail
):
    _state, app, _member_row, window, client = _detail_setup(qtbot, tmp_path)
    calls = []
    try:
        if detail:
            assert window._launch_raid_boss_fetch_if_needed(app)
        else:
            window._launch_fetch(app)
        task = window._pool.tasks[0]

        def retire_during_lookup(*_args, **_kwargs):
            window.apply_metric_preferences(_OFF)
            return None

        monkeypatch.setattr(
            window._cache,
            "get_raid_boss_details" if detail else "get",
            retire_during_lookup,
        )
        monkeypatch.setattr(
            client,
            "fetch_character_raid_boss_details" if detail else "fetch_character_ranks",
            lambda *_a, **_k: (
                calls.append("network")
                or ({} if detail else _ranks_with(raid_heroic=88, mplus_dps=89))
            ),
        )
        task.run()
        assert calls == []
    finally:
        client.close()


def test_removed_last_party_detail_waiter_retires_queued_worker(
    qtbot, tmp_path, monkeypatch
):
    state, _app_row, member, window, client = _detail_setup(qtbot, tmp_path)
    calls = []
    monkeypatch.setattr(
        client,
        "fetch_character_raid_boss_details",
        lambda *_a, **_k: calls.append("network") or {},
    )
    try:
        assert window._launch_raid_boss_fetch_if_needed(member)
        task = window._pool.tasks[0]
        state.remove_party_member(member.applicant_id)
        window.on_roster_changed()
        task.run()
        assert calls == []
        assert window._raid_boss_fetches_in_flight == {}
    finally:
        client.close()


@pytest.mark.parametrize("retirement", ["runtime", "shutdown"])
def test_runtime_or_shutdown_retires_previously_queued_summary(
    qtbot, tmp_path, monkeypatch, retirement
):
    _state, app, _member_row, window, client = _setup(qtbot, tmp_path)
    window._pool = _ShutdownPool()
    calls = []
    monkeypatch.setattr(
        client,
        "fetch_character_ranks",
        lambda *_a, **_k: (
            calls.append("network") or _ranks_with(raid_heroic=88, mplus_dps=89)
        ),
    )
    try:
        window._launch_fetch(app)
        task = window._pool.tasks[0]
        if retirement == "runtime":
            window.bump_wcl_runtime_generation()
        else:
            window.shutdown_fetches()
        task.run()
        assert calls == []
    finally:
        client.close()


def test_retired_queued_manual_detail_spends_no_network_request(
    qtbot, tmp_path, monkeypatch
):
    _state, app, _member_row, window, client = _detail_setup(qtbot, tmp_path)
    calls = []
    monkeypatch.setattr(
        client,
        "fetch_character_raid_boss_details",
        lambda *_a, **_k: calls.append("network") or {"M": []},
    )
    try:
        assert window._launch_raid_boss_fetch_if_needed(app)
        old = window._pool.tasks[0]
        _toggle(window)
        old.run()
        assert calls == []
    finally:
        client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_shared_party_worker_survives_applicant_removal_and_repeated_clears(
    qtbot, tmp_path, monkeypatch, detail
):
    state, app, member, window, client = (
        _detail_setup(qtbot, tmp_path)
        if detail
        else _setup(qtbot, tmp_path, party=True)
    )
    calls = []
    monkeypatch.setattr(
        client,
        "fetch_character_raid_boss_details" if detail else "fetch_character_ranks",
        lambda *_a, **_k: (
            calls.append("network")
            or (
                {"M": [{"name": "Current boss", "best": 88}]}
                if detail
                else _ranks_with(raid_heroic=88, mplus_dps=89)
            )
        ),
    )
    try:
        if detail:
            assert window._launch_raid_boss_fetch_if_needed(app)
            assert window._launch_raid_boss_fetch_if_needed(member)
        else:
            window._launch_fetch(app)
            window._launch_fetch(member)
        task = window._pool.tasks[0]
        state.remove(app.applicant_id)
        window.on_applicant_removed(app.applicant_id)
        for _index in range(3):
            window.on_cleared()
        assert len(window._pool.tasks) == 1
        task.run()
        assert calls == ["network"]
        if detail:
            assert member.raid_boss_parses["M"] == [
                {"name": "Current boss", "best": 88}
            ]
        else:
            assert member.fetch_status == "ready" and member.raid_heroic == 88
    finally:
        client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_retired_callback_cannot_overwrite_connection_status(
    qtbot, tmp_path, monkeypatch, detail
):
    _state, app, _member_row, window, client = _detail_setup(qtbot, tmp_path)
    recorded = []
    monkeypatch.setattr(
        client, "record_api_result", lambda **values: recorded.append(values)
    )
    try:
        if detail:
            assert window._launch_raid_boss_fetch_if_needed(app)
        else:
            window._launch_fetch(app)
        old = window._pool.tasks[0]
        _toggle(window)
        if detail:
            window._record_raid_boss_connection_status(
                old._identity, {}, "old failure", WCL_ERROR_SERVER
            )
        else:
            window._record_fetch_connection_status(
                old._identity,
                CharacterRanks.empty(error="old failure", error_kind=WCL_ERROR_SERVER),
            )
        assert recorded == []
    finally:
        client.close()


@pytest.mark.parametrize("party", [False, True])
def test_changed_spec_retires_queued_detail_before_network(
    qtbot, tmp_path, monkeypatch, party
):
    _state, app, member, window, client = _detail_setup(qtbot, tmp_path)
    row = member if party else app
    calls = []
    monkeypatch.setattr(
        client,
        "fetch_character_raid_boss_details",
        lambda *_a, **_k: calls.append("network") or {},
    )
    try:
        assert window._launch_raid_boss_fetch_if_needed(row)
        task = window._pool.tasks[0]
        row.spec_id = 72
        row.clear_wcl_data()
        if party:
            window.on_roster_changed()
        else:
            window.on_applicant_updated(row)
        task.run()
        assert calls == []
        assert window._raid_boss_fetches_in_flight == {}
        assert (
            sum(
                type(item).__name__ == "_RaidBossFetchTask"
                for item in window._pool.tasks
            )
            == 1
        )
    finally:
        client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_scope_narrowing_keeps_live_covering_worker(
    qtbot, tmp_path, monkeypatch, detail
):
    _state, app, _member_row, window, client = _detail_setup(qtbot, tmp_path)
    narrow = MetricPreferences(
        mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=False
    )
    calls = []
    rows = {
        "N": [{"name": "Normal"}],
        "H": [{"name": "Heroic"}],
        "M": [{"name": "Mythic"}],
    }
    monkeypatch.setattr(
        client,
        "fetch_character_raid_boss_details" if detail else "fetch_character_ranks",
        lambda *_a, **_k: (
            calls.append("network")
            or (rows if detail else _ranks_with(raid_heroic=88, mplus_dps=89))
        ),
    )
    try:
        if detail:
            assert window._launch_raid_boss_fetch_if_needed(app)
        else:
            window._launch_fetch(app)
        task = window._pool.tasks[0]
        window.apply_metric_preferences(narrow, refetch_missing=False)
        task.run()
        assert calls == ["network"]
        if detail:
            assert app.raid_boss_parses.get("H") == rows["H"]
            assert window._raid_boss_fetches_in_flight == {}
        else:
            assert app.raid_heroic == 88
            assert app.raid_normal is app.raid_mythic is app.mplus_dps is None
            assert window._fetches_in_flight == {}
    finally:
        client.close()


def test_scope_widening_retires_detail_without_automatic_detail_replacement(
    qtbot, tmp_path, monkeypatch
):
    _state, app, _member_row, window, client = _detail_setup(qtbot, tmp_path)
    narrow = MetricPreferences(
        mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=False
    )
    calls = []
    monkeypatch.setattr(
        client,
        "fetch_character_raid_boss_details",
        lambda *_a, **_k: calls.append("network") or {},
    )
    try:
        window.apply_metric_preferences(narrow, refetch_missing=False)
        assert window._launch_raid_boss_fetch_if_needed(app)
        old = window._pool.tasks[-1]
        window.apply_metric_preferences(_ON)
        assert (
            sum(
                type(task).__name__ == "_RaidBossFetchTask"
                for task in window._pool.tasks
            )
            == 1
        )
        old.run()
        assert calls == []
        summary = window._pool.tasks[-1]
        assert type(summary).__name__ == "_FetchTask"
        window._on_fetch_done(
            summary._identity, _ranks_with(raid_heroic=88, mplus_dps=89)
        )
        assert window._launch_raid_boss_fetch_if_needed(app)
        assert (
            sum(
                type(task).__name__ == "_RaidBossFetchTask"
                for task in window._pool.tasks
            )
            == 2
        )
    finally:
        client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_current_cache_hit_wakes_rebound_party_waiter_without_touching_other_target(
    qtbot,
    tmp_path,
    detail,
):
    state, app, member, window, client = (
        _detail_setup(qtbot, tmp_path)
        if detail
        else _setup(qtbot, tmp_path, party=True)
    )
    unrelated = _member(
        applicant_id="other-realma",
        name="Other-RealmA",
        fetch_status="ready" if detail else "pending",
    )
    state.add_or_update_party_member(unrelated)
    try:
        if detail:
            assert window._launch_raid_boss_fetch_if_needed(member)
            assert window._launch_raid_boss_fetch_if_needed(unrelated)
        else:
            window._launch_fetch(member)
            window._launch_fetch(unrelated)
        original = window._pool.tasks[0]
        unrelated_task = window._pool.tasks[1]
        state.remove(app.applicant_id)
        window.on_cleared()
        new_app = _app(fetch_status="ready" if detail else "pending")
        state.add_or_update(new_app)
        identity = original._identity
        if detail:
            rows = {
                "M": [
                    {
                        "encounter_id": 101,
                        "name": "Current boss",
                        "overall": 88,
                        "ilvl": None,
                    }
                ]
            }
            assert window._cache.put_raid_boss_details(
                "Scout",
                identity.server_slug,
                identity.region,
                identity.spec_id,
                rows,
                identity.metric_role,
                identity.metric_preferences,
            )
            assert window._launch_raid_boss_fetch_if_needed(new_app)
            assert new_app.raid_boss_parses == member.raid_boss_parses
            assert member.raid_boss_parses["M"] == rows["M"]
            tracking = window._raid_boss_fetches_in_flight
            waiters = window._raid_boss_fetch_waiters_by_target
            before = dict(tracking)
            window._on_raid_boss_fetch_done(identity, {}, "old error", WCL_ERROR_SERVER)
            assert window._raid_boss_fetches_in_flight == before
            assert member.raid_boss_parses["M"] == rows["M"]
        else:
            assert window._cache.put(
                "Scout",
                identity.server_slug,
                identity.region,
                identity.spec_id,
                _ranks_with(raid_heroic=88, mplus_dps=89),
                identity.metric_role,
                identity.metric_preferences,
            )
            window._launch_fetch(new_app)
            assert new_app.fetch_status == member.fetch_status == "ready"
            assert new_app.raid_heroic == member.raid_heroic == 88
            tracking = window._fetches_in_flight
            waiters = window._fetch_waiters_by_target
            before = dict(tracking)
            window._on_fetch_done(
                identity,
                CharacterRanks.empty(error="old error", error_kind=WCL_ERROR_SERVER),
            )
            assert window._fetches_in_flight == before
            assert member.fetch_status == "ready" and member.raid_heroic == 88
        assert len(window._pool.tasks) == 2
        assert set(tracking) == {"party:other-realma"}
        assert set(waiters) == {unrelated_task._identity.network_key}
    finally:
        client.close()
