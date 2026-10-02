"""Retired WCL workers must not mutate or persist newer cache evidence."""

from dataclasses import replace
import json
from threading import Event, Thread

import pytest

from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Listing, WoWPlayer
from applicant_scout.wcl import CharacterCache, CharacterRanks, WCL_ERROR_RESTRICTED
from test_overlay_fetch_identity import _app, _QueuedPool, _ranks_with, _window


_OFF = MetricPreferences(
    mplus=False, raid_normal=False, raid_heroic=False, raid_mythic=False
)
_FRESH_ROWS = {
    "M": [{"encounter_id": 101, "name": "Fresh boss", "overall": 88.0, "ilvl": None}]
}
_OLD_ROWS = {
    "M": [{"encounter_id": 101, "name": "Old boss", "overall": 11.0, "ilvl": None}]
}


def _start_blocked(qtbot, tmp_path, monkeypatch, *, detail, result):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    state.listing = Listing(999, "Raid", "Raid", "", 0, difficulty_id=16)
    app = _app(fetch_status="ready" if detail else "pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _QueuedPool()
    window._panel._set_detail_mode("raid")
    entered, release = Event(), Event()
    failures = []

    def fetch(*_args, **_kwargs):
        entered.set()
        if not release.wait(5):
            raise AssertionError("Blocked test client was not released")
        return result

    monkeypatch.setattr(
        client,
        "fetch_character_raid_boss_details" if detail else "fetch_character_ranks",
        fetch,
    )
    if detail:
        assert window._launch_raid_boss_fetch_if_needed(app)
    else:
        window._launch_fetch(app)
    task = window._pool.tasks[0]

    def run():
        try:
            task.run()
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run, daemon=True)
    thread.start()
    assert entered.wait(5)
    return window, client, task, thread, release, failures


def _seed_fresh(cache, identity):
    assert cache.put(
        "Scout",
        identity.server_slug,
        identity.region,
        identity.spec_id,
        _ranks_with(raid_heroic=88, mplus_dps=89),
        identity.metric_role,
        identity.metric_preferences,
    )
    assert cache.put_raid_boss_details(
        "Scout",
        identity.server_slug,
        identity.region,
        identity.spec_id,
        _FRESH_ROWS,
        identity.metric_role,
        identity.metric_preferences,
    )
    assert cache.flush()
    return cache._save_epoch, cache._dirty, cache._path.read_bytes()


def _assert_fresh(cache, identity, tmp_path, before):
    assert (cache._save_epoch, cache._dirty, cache._path.read_bytes()) == before
    for reader in (cache, CharacterCache(tmp_path)):
        summary = reader.get(
            "Scout",
            identity.server_slug,
            identity.region,
            identity.spec_id,
            identity.metric_role,
            identity.metric_preferences,
        )
        assert (
            summary is not None and summary.raid_heroic == 88 and not summary.not_found
        )
        detail = reader.get_raid_boss_details(
            "Scout",
            identity.server_slug,
            identity.region,
            identity.spec_id,
            identity.metric_role,
            identity.metric_preferences,
        )
        assert detail is not None and detail["M"] == _FRESH_ROWS["M"]


def _finish(thread, release, failures):
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert failures == []


@pytest.mark.parametrize("retirement", ["preferences", "runtime"])
@pytest.mark.parametrize("old_result", ["not_found", "restricted", "stale_positive"])
def test_retired_summary_cannot_replace_newer_persisted_evidence(
    qtbot, tmp_path, monkeypatch, retirement, old_result
):
    result = (
        CharacterRanks.empty(not_found=True)
        if old_result == "not_found"
        else CharacterRanks.empty(error="private", error_kind=WCL_ERROR_RESTRICTED)
        if old_result == "restricted"
        else _ranks_with(raid_heroic=11, mplus_dps=12)
    )
    window, client, task, thread, release, failures = _start_blocked(
        qtbot, tmp_path, monkeypatch, detail=False, result=result
    )
    try:
        if retirement == "preferences":
            window.apply_metric_preferences(_OFF)
        else:
            window.bump_wcl_runtime_generation()
        assert not task._identity.operation.is_active()
        before = _seed_fresh(window._cache, task._identity)
        _finish(thread, release, failures)
        _assert_fresh(window._cache, task._identity, tmp_path, before)
    finally:
        _finish(thread, release, failures)
        client.close()


@pytest.mark.parametrize("retirement", ["preferences", "runtime"])
def test_retired_detail_cannot_replace_newer_persisted_evidence(
    qtbot, tmp_path, monkeypatch, retirement
):
    window, client, task, thread, release, failures = _start_blocked(
        qtbot, tmp_path, monkeypatch, detail=True, result=_OLD_ROWS
    )
    try:
        if retirement == "preferences":
            window.apply_metric_preferences(_OFF)
        else:
            window.bump_wcl_runtime_generation()
        assert not task._identity.operation.is_active()
        before = _seed_fresh(window._cache, task._identity)
        _finish(thread, release, failures)
        _assert_fresh(window._cache, task._identity, tmp_path, before)
    finally:
        _finish(thread, release, failures)
        client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_retirement_while_publication_waits_for_cache_lock_is_checked_inside_lock(
    qtbot, tmp_path, monkeypatch, detail
):
    window, client, task, thread, release, failures = _start_blocked(
        qtbot,
        tmp_path,
        monkeypatch,
        detail=detail,
        result=_OLD_ROWS if detail else CharacterRanks.empty(not_found=True),
    )
    cache = window._cache
    before = _seed_fresh(cache, task._identity)
    publishing = Event()
    method_name = "put_raid_boss_details" if detail else "put"
    original_put = getattr(cache, method_name)

    def publication(*args, **kwargs):
        publishing.set()
        return original_put(*args, **kwargs)

    monkeypatch.setattr(cache, method_name, publication)
    try:
        with cache._lock:
            release.set()
            assert publishing.wait(5)
            task._identity.operation.retire()
        _finish(thread, release, failures)
        _assert_fresh(cache, task._identity, tmp_path, before)
    finally:
        _finish(thread, release, failures)
        client.close()


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("clear_cache", [False, True])
def test_active_publication_and_cache_generation_controls(
    qtbot, tmp_path, monkeypatch, detail, clear_cache
):
    result = _FRESH_ROWS if detail else _ranks_with(raid_heroic=88, mplus_dps=89)
    window, client, task, thread, release, failures = _start_blocked(
        qtbot, tmp_path, monkeypatch, detail=detail, result=result
    )
    cache = window._cache
    try:
        if clear_cache:
            cache.clear()
        epoch = cache._save_epoch
        _finish(thread, release, failures)
        if clear_cache:
            assert cache._save_epoch == epoch
            assert cache._data == {}
        elif detail:
            assert cache._save_epoch == epoch + 1
            assert (
                cache.get_raid_boss_details(
                    "Scout",
                    task._identity.server_slug,
                    task._identity.region,
                    task._identity.spec_id,
                    task._identity.metric_role,
                    task._identity.metric_preferences,
                )["M"]
                == _FRESH_ROWS["M"]
            )
        else:
            assert cache._save_epoch == epoch + 1
            assert (
                cache.get(
                    "Scout",
                    task._identity.server_slug,
                    task._identity.region,
                    task._identity.spec_id,
                    task._identity.metric_role,
                    task._identity.metric_preferences,
                ).raid_heroic
                == 88
            )
    finally:
        _finish(thread, release, failures)
        client.close()


def test_active_old_not_found_cannot_sweep_newer_different_spec_evidence(
    qtbot, tmp_path, monkeypatch
):
    window, client, task, thread, release, failures = _start_blocked(
        qtbot,
        tmp_path,
        monkeypatch,
        detail=False,
        result=CharacterRanks.empty(not_found=True),
    )
    cache = window._cache
    fresh_identity = replace(task._identity, spec_id=72)
    try:
        assert cache.put(
            "Other",
            fresh_identity.server_slug,
            fresh_identity.region,
            71,
            _ranks_with(raid_heroic=55, mplus_dps=56),
            fresh_identity.metric_role,
            fresh_identity.metric_preferences,
        )
        before = _seed_fresh(cache, fresh_identity)
        assert task._identity.operation.is_active()
        _finish(thread, release, failures)
        assert task._identity.operation.is_active()
        _assert_fresh(cache, fresh_identity, tmp_path, before)
        unrelated = cache.get(
            "Other",
            fresh_identity.server_slug,
            fresh_identity.region,
            71,
            fresh_identity.metric_role,
            fresh_identity.metric_preferences,
        )
        assert unrelated is not None and unrelated.raid_heroic == 55
    finally:
        _finish(thread, release, failures)
        client.close()


def test_current_not_found_can_invalidate_older_scopes_without_touching_other_character(
    qtbot, tmp_path, monkeypatch
):
    # Seed older evidence before the new worker is created. Its uncached spec
    # forces a real request; a current character-wide negative remains valid.
    cache = CharacterCache(tmp_path)
    assert cache.put(
        "Scout", "realma", "EU", 72, _ranks_with(raid_heroic=88, mplus_dps=89)
    )
    assert cache.put_raid_boss_details("Scout", "realma", "EU", 72, _FRESH_ROWS)
    assert cache.put(
        "Other", "realma", "EU", 71, _ranks_with(raid_heroic=55, mplus_dps=56)
    )
    window, client, task, thread, release, failures = _start_blocked(
        qtbot,
        tmp_path,
        monkeypatch,
        detail=False,
        result=CharacterRanks.empty(not_found=True),
    )
    try:
        _finish(thread, release, failures)
        result = window._cache.get("Scout", "realma", "EU", 72)
        assert result is not None and result.not_found
        assert window._cache.get_raid_boss_details("Scout", "realma", "EU", 72) is None
        unrelated = window._cache.get("Other", "realma", "EU", 71)
        assert unrelated is not None and unrelated.raid_heroic == 55
    finally:
        _finish(thread, release, failures)
        client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_active_older_positive_cannot_overwrite_newer_same_scope_evidence(
    qtbot, tmp_path, monkeypatch, detail
):
    window, client, task, thread, release, failures = _start_blocked(
        qtbot,
        tmp_path,
        monkeypatch,
        detail=detail,
        result=_OLD_ROWS if detail else _ranks_with(raid_heroic=11, mplus_dps=12),
    )
    try:
        before = _seed_fresh(window._cache, task._identity)
        _finish(thread, release, failures)
        assert task._identity.operation.is_active()
        _assert_fresh(window._cache, task._identity, tmp_path, before)
    finally:
        _finish(thread, release, failures)
        client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_active_older_positive_cannot_resurrect_after_newer_character_negative(
    qtbot, tmp_path, monkeypatch, detail
):
    window, client, task, thread, release, failures = _start_blocked(
        qtbot,
        tmp_path,
        monkeypatch,
        detail=detail,
        result=_OLD_ROWS if detail else _ranks_with(raid_heroic=11, mplus_dps=12),
    )
    cache = window._cache
    identity = task._identity
    try:
        assert cache.put(
            "Scout",
            identity.server_slug,
            identity.region,
            72,
            CharacterRanks.empty(not_found=True),
            identity.metric_role,
            identity.metric_preferences,
        )
        before = cache._save_epoch, cache._dirty, cache._path.read_bytes()
        _finish(thread, release, failures)
        assert (cache._save_epoch, cache._dirty, cache._path.read_bytes()) == before
        negative = cache.get(
            "Scout",
            identity.server_slug,
            identity.region,
            identity.spec_id,
            identity.metric_role,
            identity.metric_preferences,
        )
        assert negative is not None and negative.not_found
        assert (
            cache.get_raid_boss_details(
                "Scout",
                identity.server_slug,
                identity.region,
                identity.spec_id,
                identity.metric_role,
                identity.metric_preferences,
            )
            is None
        )
    finally:
        _finish(thread, release, failures)
        client.close()


@pytest.mark.parametrize("detail", [False, True])
def test_independent_positive_scopes_can_publish_without_erasing_newer_scope(
    qtbot, tmp_path, monkeypatch, detail
):
    window, client, task, thread, release, failures = _start_blocked(
        qtbot,
        tmp_path,
        monkeypatch,
        detail=detail,
        result=_OLD_ROWS if detail else _ranks_with(raid_heroic=11, mplus_dps=12),
    )
    cache = window._cache
    fresh_identity = replace(task._identity, spec_id=72)
    try:
        before = _seed_fresh(cache, fresh_identity)
        _finish(thread, release, failures)
        assert cache._save_epoch == before[0] + 1
        fresh = cache.get(
            "Scout",
            fresh_identity.server_slug,
            fresh_identity.region,
            72,
            fresh_identity.metric_role,
            fresh_identity.metric_preferences,
        )
        assert fresh is not None and fresh.raid_heroic == 88
        assert (
            cache.get_raid_boss_details(
                "Scout",
                fresh_identity.server_slug,
                fresh_identity.region,
                72,
                fresh_identity.metric_role,
                fresh_identity.metric_preferences,
            )["M"]
            == _FRESH_ROWS["M"]
        )
        if detail:
            assert (
                cache.get_raid_boss_details(
                    "Scout",
                    fresh_identity.server_slug,
                    fresh_identity.region,
                    task._identity.spec_id,
                    fresh_identity.metric_role,
                    fresh_identity.metric_preferences,
                )["M"]
                == _OLD_ROWS["M"]
            )
        else:
            assert (
                cache.get(
                    "Scout",
                    fresh_identity.server_slug,
                    fresh_identity.region,
                    task._identity.spec_id,
                    fresh_identity.metric_role,
                    fresh_identity.metric_preferences,
                ).raid_heroic
                == 11
            )
    finally:
        _finish(thread, release, failures)
        client.close()


@pytest.mark.parametrize("disk_epoch", [10**12, "invalid", None])
def test_loaded_cache_discards_session_publication_metadata_and_skips_unknown_fields(
    qtbot, tmp_path, monkeypatch, disk_epoch
):
    cache = CharacterCache(tmp_path)
    assert cache.put(
        "Scout", "realma", "EU", 72, _ranks_with(raid_heroic=88, mplus_dps=89)
    )
    assert cache.put_raid_boss_details("Scout", "realma", "EU", 72, _FRESH_ROWS)
    payload = json.loads(cache._path.read_text(encoding="utf-8"))
    for entry in payload["entries"].values():
        entry["publication_epoch"] = disk_epoch
    payload["entries"]["unknown-field-entry"] = {
        **next(iter(payload["entries"].values())),
        "unexpected_field": True,
    }
    cache._path.write_text(json.dumps(payload), encoding="utf-8")
    window, client, task, thread, release, failures = _start_blocked(
        qtbot,
        tmp_path,
        monkeypatch,
        detail=False,
        result=CharacterRanks.empty(not_found=True),
    )
    try:
        assert "unknown-field-entry" not in window._cache._data
        assert all(
            entry.publication_epoch == 0 for entry in window._cache._data.values()
        )
        _finish(thread, release, failures)
        result = window._cache.get("Scout", "realma", "EU", 72)
        assert result is not None and result.not_found
        persisted = json.loads(window._cache._path.read_text(encoding="utf-8"))
        assert all(
            "publication_epoch" not in entry for entry in persisted["entries"].values()
        )
    finally:
        _finish(thread, release, failures)
        client.close()
