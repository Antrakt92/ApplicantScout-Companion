"""Retired workers cannot start another GraphQL request after OAuth or 401."""

from threading import Event, Thread

import pytest
from PySide6.QtCore import Qt

from applicant_scout.fetch_operation import FetchOperation
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.wcl import WCLApiError, WCL_ERROR_RATE_LIMITED
from test_fetch_operation_ownership import _detail_setup
from test_wcl import (
    _FakeResponse, _character_with_empty_mplus,
    _character_with_empty_raid_boss_details, _client_for_payload, _wcl_payload,
)


_OFF = MetricPreferences(
    mplus=False, raid_normal=False, raid_heroic=False, raid_mythic=False,
)


def _task(qtbot, tmp_path, detail):
    state, applicant, member, window, client = _detail_setup(qtbot, tmp_path)
    if detail:
        assert window._launch_raid_boss_fetch_if_needed(applicant)
    else:
        window._launch_fetch(applicant)
    task = window._pool.tasks[0]
    signals = []
    task.signals.done.connect(
        lambda *_args: signals.append("done"), Qt.ConnectionType.DirectConnection,
    )
    task.signals.networkDone.connect(
        lambda *_args: signals.append("network"), Qt.ConnectionType.DirectConnection,
    )
    return state, applicant, member, window, client, task, signals


def _response(detail):
    if detail:
        character = {}
        for difficulty in ("N", "H", "M"):
            character.update(_character_with_empty_raid_boss_details(difficulty))
    else:
        character = _character_with_empty_mplus()
    return _FakeResponse(_wcl_payload(character))


def _cleanup(window, client):
    window._pool = None
    window.shutdown_fetches()
    client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_retirement_during_oauth_prevents_post_and_completion(
    qtbot, tmp_path, monkeypatch, detail,
):
    _state, applicant, _member, window, client, task, signals = _task(qtbot, tmp_path, detail)
    entered, release = Event(), Event()
    posts, errors = [], []

    def token():
        entered.set()
        assert release.wait(3)
        return "synthetic-token"

    def post(*_args, **_kwargs):
        posts.append(True)
        return _response(detail)

    def run():
        try:
            task.run()
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(client._auth, "get_token", token)
    monkeypatch.setattr(client._http, "post", post)
    worker = Thread(target=run, daemon=True)
    try:
        worker.start()
        assert entered.wait(1)
        window.apply_metric_preferences(_OFF)
        assert not task._identity.operation.is_active()
        assert not window._fetches_in_flight
        assert not window._raid_boss_fetches_in_flight
        release.set()
        worker.join(3)
        assert not worker.is_alive() and errors == []
        assert posts == []
        assert signals == []
        assert window._cache._data == {}
        assert applicant.fetch_status == "ready"
        assert applicant.error_message == ""
        assert client.retry_block_remaining_seconds() == 0
        assert window._raid_boss_fetch_failures == {}
    finally:
        release.set()
        worker.join(3)
        _cleanup(window, client)


@pytest.mark.parametrize("detail", [False, True])
def test_retirement_before_401_retry_preserves_invalidation_without_new_request(
    qtbot, tmp_path, monkeypatch, detail,
):
    _state, _applicant, _member, window, client, task, signals = _task(qtbot, tmp_path, detail)
    tokens, posts, invalidated = [], [], []

    def token():
        tokens.append(True)
        return "synthetic-token"

    def post(*_args, **_kwargs):
        posts.append(True)
        task._identity.operation.retire()
        return _FakeResponse({}, status_code=401)

    monkeypatch.setattr(client._auth, "get_token", token)
    monkeypatch.setattr(client._auth, "invalidate", invalidated.append)
    monkeypatch.setattr(client._http, "post", post)
    try:
        task.run()
        assert posts == [True]
        assert tokens == [True]
        assert invalidated == ["synthetic-token"]
        assert signals == []
        assert window._cache._data == {}
        assert client.retry_block_remaining_seconds() == 0
    finally:
        _cleanup(window, client)


@pytest.mark.parametrize("detail", [False, True])
def test_retirement_before_worker_start_skips_oauth(qtbot, tmp_path, monkeypatch, detail):
    _state, _applicant, _member, window, client, task, signals = _task(qtbot, tmp_path, detail)
    tokens = []
    monkeypatch.setattr(client._auth, "get_token", lambda: tokens.append(True) or "synthetic-token")
    try:
        window.apply_metric_preferences(_OFF)
        task.run()
        assert tokens == [] and signals == []
        assert window._cache._data == {}
    finally:
        _cleanup(window, client)


@pytest.mark.parametrize("detail", [False, True])
def test_active_worker_retains_normal_oauth_and_completion(qtbot, tmp_path, monkeypatch, detail):
    _state, _applicant, _member, window, client, task, signals = _task(qtbot, tmp_path, detail)
    posts = []
    monkeypatch.setattr(client._auth, "get_token", lambda: "synthetic-token")
    monkeypatch.setattr(client._http, "post", lambda *_args, **_kwargs: posts.append(True) or _response(detail))
    try:
        task.run()
        assert posts == [True]
        assert signals == ["network", "done"]
        assert window._cache._data
    finally:
        _cleanup(window, client)


@pytest.mark.parametrize("detail", [False, True])
def test_one_remaining_coalesced_waiter_keeps_request_active(qtbot, tmp_path, monkeypatch, detail):
    _state, applicant, member, window, client, task, signals = _task(qtbot, tmp_path, detail)
    if detail:
        assert window._launch_raid_boss_fetch_if_needed(member)
        window._discard_raid_boss_fetch_by_storage_key(applicant.applicant_id)
    else:
        window._launch_fetch(member)
        window._discard_fetch_by_storage_key(applicant.applicant_id)
    posts = []
    monkeypatch.setattr(client._auth, "get_token", lambda: "synthetic-token")
    monkeypatch.setattr(client._http, "post", lambda *_args, **_kwargs: posts.append(True) or _response(detail))
    try:
        assert task._identity.operation.is_active()
        task.run()
        assert posts == [True] and signals == ["network", "done"]
        assert member.fetch_status == "ready"
        assert window._cache._data
    finally:
        _cleanup(window, client)


@pytest.mark.parametrize("detail", [False, True])
def test_already_started_retired_request_preserves_shared_429_backoff(monkeypatch, detail):
    client, http = _client_for_payload(_wcl_payload(_character_with_empty_mplus()))
    operation = FetchOperation()

    def post(*_args, **_kwargs):
        operation.retire()
        return _FakeResponse({}, status_code=429)

    monkeypatch.setattr(http, "post", post)
    try:
        fetch = client.fetch_character_raid_boss_details if detail else client.fetch_character_ranks
        with pytest.raises(WCLApiError) as error:
            fetch("Scout", "realma", 71, expected_operation=operation)
        assert error.value.error_kind == WCL_ERROR_RATE_LIMITED
        assert client.retry_block_remaining_seconds() > 0
    finally:
        client.close()
