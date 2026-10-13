"""Source-order and row-transition cases across the overlay/cache boundary."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from applicant_scout import overlay, overlay_presenters
from applicant_scout.constants import CURRENT_RAID_ENCOUNTERS
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Applicant, Listing
from applicant_scout.wcl import CharacterCache, CharacterRanks, WCL_ERROR_RESTRICTED
from applicant_scout.wcl_availability import RaidBossDetails


_HEROIC = MetricPreferences(mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=False)
_HEROIC_MYTHIC = replace(_HEROIC, raid_mythic=True)


def _app(aid="1:1"):
    return Applicant(aid, "Scout-Ravencrest", "WARRIOR", 71, 300, 3000,
                     "DAMAGER", fetch_status="pending")


def _manual_rows(value):
    return RaidBossDetails({"H": [dict(encounter_id=CURRENT_RAID_ENCOUNTERS[0][1],
                                       name=CURRENT_RAID_ENCOUNTERS[0][2],
                                       overall=value, ilvl=value)]},
                           availability={"H": "available"})


def _private_summary(global_private):
    return replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"},
                   error="Private rankings" if global_private else "",
                   error_kind=WCL_ERROR_RESTRICTED if global_private else "")


def _window(qtbot, tmp_path, app):
    state = AppState()
    state.listing = Listing(0, "Raid", "Heroic", "", key_level=0,
                            category_id=3, difficulty_id=15)
    state.applicants = {app.applicant_id: app}
    client = SimpleNamespace(region="EU", last_quota=None,
                             quota_reset_remaining_seconds=lambda: None)
    cache = CharacterCache(tmp_path / "cache")
    window = overlay.OverlayWindow(state, client, cache, tmp_path, metric_preferences=_HEROIC)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    window._resolve_hover_from_cursor = lambda: None
    window._launch_fetch = lambda _applicant: None
    window._pool = SimpleNamespace(tasks=[])
    window._pool.start = window._pool.tasks.append
    return window, cache


def _apply_manual_cache(app, cache):
    rows = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                      metric_preferences=_HEROIC)
    assert rows is not None
    app.raid_boss_parses = {key: [dict(row) for row in value] for key, value in rows.items()}
    app.raid_boss_availability = dict(rows.availability)
    app.raid_boss_publication_epochs = dict(rows.publication_epochs)


@pytest.mark.parametrize("global_private", [False, True])
def test_scalar_cache_hit_keeps_later_independent_manual_capability(qtbot, tmp_path, global_private):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    try:
        assert cache.put("Scout", "ravencrest", "EU", 71, _private_summary(global_private),
                         metric_preferences=_HEROIC)
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _manual_rows(24),
                                           metric_preferences=_HEROIC)
        _apply_manual_cache(app, cache)
        later_epoch = app.raid_boss_publication_epochs["H"]
        overlay.OverlayWindow._launch_fetch(window, app)
        assert not window._pool.tasks
        assert app.wcl_availability["raid_heroic"] == "restricted"
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        assert app.raid_boss_availability["H"] == "available"
        assert app.raid_boss_publication_epochs["H"] == later_epoch
        assert overlay_presenters.raid_boss_rows_for_display(app, ["H"])[0]["value"] == "H 24 / 24"
    finally:
        cache.close()


@pytest.mark.parametrize("global_private", [False, True])
def test_scalar_cache_hit_retracts_manual_data_superseded_by_later_privacy(qtbot, tmp_path, global_private):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    try:
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _manual_rows(24),
                                           metric_preferences=_HEROIC)
        _apply_manual_cache(app, cache)
        assert cache.put("Scout", "ravencrest", "EU", 71, _private_summary(global_private),
                         metric_preferences=_HEROIC, expected_publication_epoch=cache.publication_epoch)
        overlay.OverlayWindow._launch_fetch(window, app)
        assert not window._pool.tasks
        assert not app.raid_boss_parses.get("H")
        assert overlay_presenters.raid_boss_rows_for_display(app, ["H"])[0]["value"] == ""
    finally:
        cache.close()


def test_historical_scalar_privacy_replay_keeps_live_request_for_missing_healthy_difficulty(qtbot, tmp_path):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    window._metric_preferences = _HEROIC_MYTHIC
    try:
        summary = replace(_private_summary(False), raid_mythic=70, raid_mythic_median=70,
                          availability={"raid_heroic": "restricted", "raid_mythic": "available"})
        assert cache.put("Scout", "ravencrest", "EU", 71, summary,
                         metric_preferences=_HEROIC_MYTHIC)
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _manual_rows(24),
                                           metric_preferences=_HEROIC)
        _apply_manual_cache(app, cache)
        app.fetch_status = "ready"
        assert overlay.OverlayWindow._launch_raid_boss_fetch_if_needed(window, app)
        assert len(window._pool.tasks) == 1
        task = window._pool.tasks[0]
        assert task._identity.operation.is_active()
        overlay.OverlayWindow._launch_fetch(window, app)
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        assert task._identity.operation.is_active()
        assert app.applicant_id in window._raid_boss_fetches_in_flight
        assert len(window._pool.tasks) == 1
    finally:
        cache.close()


def test_new_scalar_privacy_replay_retires_genuinely_older_manual_request(qtbot, tmp_path):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    window._metric_preferences = _HEROIC_MYTHIC
    try:
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _manual_rows(24),
                                           metric_preferences=_HEROIC)
        _apply_manual_cache(app, cache)
        app.fetch_status = "ready"
        assert overlay.OverlayWindow._launch_raid_boss_fetch_if_needed(window, app)
        task = window._pool.tasks[0]
        summary = replace(_private_summary(False), raid_mythic=70, raid_mythic_median=70,
                          availability={"raid_heroic": "restricted", "raid_mythic": "available"})
        assert cache.put("Scout", "ravencrest", "EU", 71, summary,
                         metric_preferences=_HEROIC_MYTHIC,
                         expected_publication_epoch=cache.publication_epoch)
        overlay.OverlayWindow._launch_fetch(window, app)
        assert not task._identity.operation.is_active()
        assert app.applicant_id not in window._raid_boss_fetches_in_flight
        assert not app.raid_boss_parses.get("H")
    finally:
        cache.close()


@pytest.mark.parametrize("global_private", [False, True])
def test_cached_privacy_reload_epoch_zero_keeps_current_public_manual_scope(qtbot, tmp_path, global_private):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    try:
        assert cache.put("Scout", "ravencrest", "EU", 71, _private_summary(global_private),
                         metric_preferences=_HEROIC)
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _manual_rows(24),
                                           metric_preferences=_HEROIC)
        cache.close()
        cache = CharacterCache(tmp_path / "cache")
        window._cache = cache
        _apply_manual_cache(app, cache)
        assert app.raid_boss_publication_epochs["H"] == 0
        overlay.OverlayWindow._launch_fetch(window, app)
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        assert app.raid_boss_publication_epochs["H"] == 0
    finally:
        cache.close()


@pytest.mark.parametrize("global_private", [False, True])
def test_cached_older_privacy_keeps_eligible_live_manual_receipt_after_detail_eviction(
    qtbot, tmp_path, global_private,
):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    try:
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _manual_rows(24),
                                           metric_preferences=_HEROIC)
        _apply_manual_cache(app, cache)
        cache.MAX_ENTRIES = 1
        assert cache.put("Scout", "ravencrest", "EU", 71, _private_summary(global_private),
                         metric_preferences=_HEROIC, expected_publication_epoch=0)
        assert cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                           metric_preferences=_HEROIC) is None
        assert cache.can_deliver_response("Scout", "ravencrest", "EU", 71,
                                          metric_preferences=_HEROIC,
                                          expected_publication_epoch=app.raid_boss_publication_epochs["H"],
                                          raid_details=True)
        overlay.OverlayWindow._launch_fetch(window, app)
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        assert app.raid_boss_publication_epochs["H"] == 1
    finally:
        cache.close()


def _owned_identity(window, app, *, manual=True):
    resolved = overlay._fetch_identity_for_applicant(
        app, "", "EU", window._raid_detail_preferences() if manual else window._metric_preferences,
        window._wcl_runtime_generation, window._listing_session_generation,
        row_source=window._row_source_for(app),
    )
    assert resolved is not None
    identity = replace(resolved[0], operation=overlay.FetchOperation(),
                       cache_publication_epoch=window._cache.publication_epoch)
    if manual:
        window._mark_raid_boss_fetch_in_flight(identity)
        window._mark_raid_boss_fetch_waiting_on_target(identity)
    else:
        window._mark_fetch_in_flight(identity)
        window._mark_fetch_waiting_on_target(identity)
    return identity


def _run_task(window, identity, payload, *, manual=True):
    client = SimpleNamespace()
    setattr(client, "fetch_character_raid_boss_details" if manual else "fetch_character_ranks",
            lambda *_args, **_kwargs: payload)
    task = (overlay._RaidBossFetchTask if manual else overlay._FetchTask)(identity, "Scout", client, window._cache)
    delivered, observed = [], []
    task.signals.done.connect(lambda *args: delivered.append(args))
    task.signals.networkDone.connect(lambda *args: observed.append(args))
    task.run()
    assert len(delivered) == 1
    return delivered[0], observed


def _evict(cache):
    cache.MAX_ENTRIES = 1
    assert cache.put("Other", "ravencrest", "EU", 71, CharacterRanks.empty(),
                     metric_preferences=_HEROIC)


def _public_summary(value=88):
    return replace(CharacterRanks.empty(), raid_heroic=value, raid_heroic_median=value,
                   availability={"raid_heroic": "available"})


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("later", ["unrelated", "private", "not_found"])
def test_own_accepted_response_survives_eviction_but_newer_proof_wins(qtbot, tmp_path, manual, later):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    identity = _owned_identity(window, app, manual=manual)
    peer = replace(app, applicant_id="2:1")
    window._state.applicants[peer.applicant_id] = peer
    coalesce = window._coalesce_raid_boss_fetch_if_target_in_flight if manual else window._coalesce_fetch_if_target_in_flight
    assert coalesce(replace(identity, applicant_id=peer.applicant_id, operation=None))
    try:
        delivered, observed = _run_task(window, identity, _manual_rows(88) if manual else _public_summary(), manual=manual)
        assert len(observed) == 1
        if later == "private":
            assert cache.put("Scout", "ravencrest", "EU", 71, _private_summary(False),
                             metric_preferences=_HEROIC)
        elif later == "not_found":
            assert cache.put("Scout", "ravencrest", "EU", 71,
                             replace(CharacterRanks.empty(), not_found=True), metric_preferences=_HEROIC)
        _evict(cache)
        callback = window._on_raid_boss_fetch_done if manual else window._on_fetch_done
        callback(*delivered)
        for row in (app, peer):
            if manual:
                assert (row.raid_boss_parses.get("H", [{}])[0].get("overall") == 88) is (later == "unrelated")
            else:
                assert (row.raid_heroic == 88) is (later == "unrelated")
        assert not identity.operation.is_active()
        assert not (window._raid_boss_fetches_in_flight if manual else window._fetches_in_flight)
    finally:
        cache.close()


@pytest.mark.parametrize("manual", [False, True])
def test_accepted_publication_receipt_survives_eviction_before_worker_returns(qtbot, tmp_path, monkeypatch, manual):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    identity = _owned_identity(window, app, manual=manual)
    original_flush = cache.flush
    intervened = False

    def flush(*args, **kwargs):
        nonlocal intervened
        if not intervened:
            intervened = True
            _evict(cache)
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(cache, "_schedule_save_locked", lambda: True)
    monkeypatch.setattr(cache, "flush", flush)
    try:
        delivered, observed = _run_task(window, identity, _manual_rows(88) if manual else _public_summary(), manual=manual)
        assert intervened and cache.publication_epoch == 2
        assert len(observed) == 1
        if manual:
            assert delivered[1].publication_epochs == {"H": 1}
            window._on_raid_boss_fetch_done(*delivered)
            assert app.raid_boss_parses["H"][0]["overall"] == 88
        else:
            assert delivered[0].cache_publication_epoch == 0
            assert delivered[0].response_publication_epoch == 1
            window._on_fetch_done(*delivered)
            assert app.raid_heroic == 88
    finally:
        cache.close()


def test_worker_reads_newer_positive_cache_receipt_before_later_eviction(qtbot, tmp_path):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    identity = _owned_identity(window, app, manual=False)
    calls = []
    client = SimpleNamespace(fetch_character_ranks=lambda *_args, **_kwargs: calls.append(True))
    task = overlay._FetchTask(identity, "Scout", client, cache)
    delivered, observed = [], []
    task.signals.done.connect(lambda *args: delivered.append(args))
    task.signals.networkDone.connect(lambda *args: observed.append(args))
    try:
        assert cache.put("Scout", "ravencrest", "EU", 71, _public_summary(), metric_preferences=_HEROIC)
        task.run()
        assert calls == [] and observed == []
        assert delivered[0][0].cache_publication_epoch == 0
        assert delivered[0][0].response_publication_epoch == 1
        _evict(cache)
        window._on_fetch_done(*delivered[0])
        assert app.raid_heroic == 88
        assert not identity.operation.is_active()
    finally:
        cache.close()


@pytest.mark.parametrize("generation_clear", [False, True])
def test_manual_current_not_found_clears_all_wcl_rows_but_keeps_timed_rio(qtbot, tmp_path, generation_clear):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    peer = replace(app, applicant_id="2:1")
    window._state.applicants[peer.applicant_id] = peer
    for row in (app, peer):
        row.raid_heroic = 99
        row.raid_boss_parses = {"H": _manual_rows(99)["H"]}
        row.raid_boss_availability = {"H": "available"}
        row.rio_dungeons = [dict(name="Dungeon", level=12, timed=True)]
    identity = _owned_identity(window, app)
    assert window._coalesce_raid_boss_fetch_if_target_in_flight(replace(identity, applicant_id=peer.applicant_id, operation=None))
    try:
        if generation_clear:
            cache.clear()
        delivered, observed = _run_task(window, identity, RaidBossDetails({}, not_found=True))
        assert len(observed) == 1 and observed[0][1].not_found
        assert delivered[1].not_found and delivered[3] == "not_found"
        if not generation_clear:
            assert delivered[0].response_publication_epoch == 1
            _evict(cache)
        window._on_raid_boss_fetch_done(*delivered)
        for row in (app, peer):
            assert row.fetch_status == "not_found"
            assert row.raid_heroic is None and row.raid_boss_parses == {}
            assert row.rio_dungeons == [dict(name="Dungeon", level=12, timed=True)]
            assert row.score == 3000 and row.spec_id == 71 and row.ilvl == 300
        assert not identity.operation.is_active()
        assert window._raid_boss_fetches_in_flight == {}
    finally:
        cache.close()


def test_queued_manual_not_found_cannot_clear_later_public_rows_even_after_eviction(qtbot, tmp_path):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    identity = _owned_identity(window, app)
    try:
        delivered, _observed = _run_task(window, identity, RaidBossDetails({}, not_found=True))
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _manual_rows(24), metric_preferences=_HEROIC)
        _apply_manual_cache(app, cache)
        app.fetch_status = "ready"
        _evict(cache)
        window._on_raid_boss_fetch_done(*delivered)
        assert app.fetch_status == "ready"
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        assert app.raid_boss_publication_epochs["H"] == 2
        assert not identity.operation.is_active()
    finally:
        cache.close()


@pytest.mark.usefixtures("qapp")
def test_manual_worker_cached_not_found_uses_no_detail_network_call(tmp_path):
    cache = CharacterCache(tmp_path)
    calls = []
    client = SimpleNamespace(fetch_character_raid_boss_details=lambda *_args, **_kwargs: calls.append(True))
    identity = overlay._FetchIdentity("1:1", "scout", "ravencrest", "EU", 71, "DAMAGER", metric_preferences=_HEROIC)
    try:
        assert cache.put("Scout", "ravencrest", "EU", 71,
                         replace(CharacterRanks.empty(), not_found=True), metric_preferences=_HEROIC)
        task = overlay._RaidBossFetchTask(identity, "Scout", client, cache)
        delivered, observed = [], []
        task.signals.done.connect(lambda *args: delivered.append(args))
        task.signals.networkDone.connect(lambda *args: observed.append(args))
        task.run()
        assert calls == [] and observed == []
        assert len(delivered) == 1 and delivered[0][1].not_found
        assert delivered[0][0].response_publication_epoch == 1
    finally:
        cache.close()


def test_current_not_found_preserves_routes_started_at_receipt_and_retires_older(qtbot, tmp_path):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    older = _owned_identity(window, app, manual=False)
    manual = _owned_identity(window, app)
    peer = replace(app, applicant_id="2:1", spec_id=72)
    window._state.applicants[peer.applicant_id] = peer
    try:
        delivered, _observed = _run_task(window, manual, RaidBossDetails({}, not_found=True))
        newer = _owned_identity(window, peer, manual=False)
        assert newer.cache_publication_epoch == delivered[0].response_publication_epoch == 1
        window._on_raid_boss_fetch_done(*delivered)
        assert not older.operation.is_active()
        assert newer.operation.is_active()
        assert app.fetch_status == "not_found" and peer.fetch_status == "loading"
    finally:
        cache.close()


@pytest.mark.parametrize("later", ["unrelated", "private", "not_found"])
def test_partial_manual_publication_keeps_only_eligible_scopes_and_independent_live_rows(qtbot, tmp_path, monkeypatch, later):
    app = _app()
    window, cache = _window(qtbot, tmp_path, app)
    window._metric_preferences = _HEROIC_MYTHIC
    identity = _owned_identity(window, app)
    try:
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _manual_rows(99), metric_preferences=_HEROIC)
        _apply_manual_cache(app, cache)
        provider_rows = RaidBossDetails({"H": _manual_rows(10)["H"], "M": _manual_rows(88)["H"]},
                                       availability={"H": "available", "M": "available"})
        original_flush = cache.flush
        intervened = False

        def flush(*args, **kwargs):
            nonlocal intervened
            if not intervened:
                intervened = True
                _evict(cache)
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(cache, "_schedule_save_locked", lambda: True)
        monkeypatch.setattr(cache, "flush", flush)
        delivered, observed = _run_task(window, identity, provider_rows)
        assert observed[0][1]["H"][0]["overall"] == 10
        assert delivered[1].publication_epochs == {"M": 2}
        assert not delivered[1].get("H") and delivered[1]["M"][0]["overall"] == 88
        if later == "private":
            assert cache.put("Scout", "ravencrest", "EU", 71, _private_summary(False), metric_preferences=_HEROIC)
            _evict(cache)
        elif later == "not_found":
            assert cache.put("Scout", "ravencrest", "EU", 71,
                             replace(CharacterRanks.empty(), not_found=True), metric_preferences=_HEROIC)
            _evict(cache)
        window._on_raid_boss_fetch_done(*delivered)
        if later == "unrelated":
            assert app.raid_boss_parses["H"][0]["overall"] == 99
            assert app.raid_boss_parses["M"][0]["overall"] == 88
            assert app.raid_boss_publication_epochs == {"H": 1, "M": 2}
        elif later == "private":
            assert not app.raid_boss_parses.get("H")
            assert app.raid_boss_parses["M"][0]["overall"] == 88
        else:
            assert not app.raid_boss_parses.get("H") and not app.raid_boss_parses.get("M")
    finally:
        cache.close()
