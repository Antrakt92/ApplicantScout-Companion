from __future__ import annotations

import time

from applicant_scout.overlay import (
    _FetchIdentity,
    _fetch_identity_for_applicant,
    OverlayWindow,
)
from applicant_scout.metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences
from applicant_scout.state import AppState, Applicant, WoWPlayer
from applicant_scout.wcl import (
    CharacterCache,
    CharacterRanks,
    WCLAuth,
    WCLClient,
    WCLApiError,
    WCL_ERROR_AUTH,
    WCL_ERROR_QUOTA_GUARD,
    WCL_ERROR_RATE_LIMITED,
)


class _SyncPool:
    def start(self, task):
        task.run()


def _app(**overrides) -> Applicant:
    base = Applicant(
        applicant_id="42:1",
        name="Scout-RealmA",
        cls="WARRIOR",
        spec_id=71,
        ilvl=480,
        score=2400,
        role="DAMAGER",
        fetch_status="loading",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


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
    )


def _window(
    qtbot,
    tmp_path,
    state: AppState,
    *,
    region: str = "EU",
    metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, region=region, metric_preferences=metric_preferences)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(
        state,
        client,
        cache,
        tmp_path,
        metric_preferences=metric_preferences,
    )
    window._pool = None
    qtbot.addWidget(window)
    return window, client


def test_matching_fetch_identity_applies_ranks(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app()
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        resolved = _fetch_identity_for_applicant(app, state.player.full_name, "EU")
        assert resolved is not None
        identity, _charname = resolved

        window._fetches_in_flight.add(app.applicant_id)
        window._on_fetch_done(identity, _ranks())

        assert app.fetch_status == "ready"
        assert app.error_message == ""
        assert app.raid_heroic == 22.0
        assert app.mplus_dps == 77.0
        assert app.wcl_metric_preferences == DEFAULT_METRIC_PREFERENCES
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_fetch_done_prefers_not_found_over_error_text(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app()
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        resolved = _fetch_identity_for_applicant(app, state.player.full_name, "EU")
        assert resolved is not None
        identity, _charname = resolved

        window._on_fetch_done(
            identity,
            CharacterRanks.empty(not_found=True, error="could not find character"),
        )

        assert app.fetch_status == "not_found"
        assert app.error_message == ""
        assert app.wcl_error_kind == ""
    finally:
        client.close()


def test_fetch_error_stores_error_kind(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app()
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        resolved = _fetch_identity_for_applicant(app, state.player.full_name, "EU")
        assert resolved is not None
        identity, _charname = resolved

        window._on_fetch_done(
            identity,
            CharacterRanks.empty(
                error="WCL quota guard 90% used",
                error_kind=WCL_ERROR_QUOTA_GUARD,
            ),
        )

        assert app.fetch_status == "error"
        assert app.error_message == "WCL quota guard 90% used"
        assert app.wcl_error_kind == WCL_ERROR_QUOTA_GUARD
    finally:
        client.close()


def test_fetch_task_preserves_wcl_api_error_kind(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _SyncPool()

    def fake_fetch(*_args, **_kwargs):
        raise WCLApiError("Rate limited", error_kind=WCL_ERROR_RATE_LIMITED)

    client.fetch_character_ranks = fake_fetch  # type: ignore[method-assign]

    try:
        window._launch_fetch(app)

        assert app.fetch_status == "error"
        assert app.error_message == "Rate limited"
        assert app.wcl_error_kind == WCL_ERROR_RATE_LIMITED
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_successful_fetch_clears_error_kind(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(
        fetch_status="loading",
        error_message="old",
        wcl_error_kind=WCL_ERROR_RATE_LIMITED,
    )
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        resolved = _fetch_identity_for_applicant(app, state.player.full_name, "EU")
        assert resolved is not None
        identity, _charname = resolved

        window._on_fetch_done(identity, _ranks())

        assert app.fetch_status == "ready"
        assert app.error_message == ""
        assert app.wcl_error_kind == ""
    finally:
        client.close()


def test_stale_fetch_realm_identity_does_not_apply(qtbot, tmp_path):
    state = AppState()
    app = _app(name="Scout-RealmB", mplus_dps=12.0)
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        stale_identity = _FetchIdentity(
            applicant_id=app.applicant_id,
            charname_key="scout",
            server_slug="realma",
            region="EU",
            spec_id=app.spec_id,
            metric_role="DPS",
        )

        window._on_fetch_done(stale_identity, _ranks())

        assert app.fetch_status == "loading"
        assert app.mplus_dps is None
        assert app.raid_heroic is None
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()


def test_broader_fetch_scope_applies_after_current_scope_narrows(qtbot, tmp_path):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=True,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app()
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=narrow)

    try:
        broad_identity = _FetchIdentity(
            applicant_id=app.applicant_id,
            charname_key="scout",
            server_slug="realma",
            region="EU",
            spec_id=app.spec_id,
            metric_role="DPS",
            metric_preferences=DEFAULT_METRIC_PREFERENCES,
        )
        window._fetches_in_flight.add(app.applicant_id)

        window._on_fetch_done(broad_identity, _ranks())

        assert app.fetch_status == "ready"
        assert app.raid_heroic == 22.0
        assert app.mplus_dps is None
        assert app.mplus_dps_breakdown == []
        assert app.wcl_metric_preferences == narrow
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_narrower_fetch_scope_relaunches_after_current_scope_broadens(qtbot, tmp_path):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=True,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(mplus_dps=12.0, raid_heroic=44.0)
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        narrow_identity = _FetchIdentity(
            applicant_id=app.applicant_id,
            charname_key="scout",
            server_slug="realma",
            region="EU",
            spec_id=app.spec_id,
            metric_role="DPS",
            metric_preferences=narrow,
        )

        window._on_fetch_done(narrow_identity, _ranks())

        assert app.fetch_status == "loading"
        assert app.mplus_dps is None
        assert app.raid_heroic is None
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()


def test_fetch_done_for_removed_applicant_only_clears_in_flight(qtbot, tmp_path):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)
    identity = _FetchIdentity(
        applicant_id="gone:1",
        charname_key="gone",
        server_slug="realma",
        region="EU",
        spec_id=71,
        metric_role="DPS",
    )
    window._fetches_in_flight.add(identity.applicant_id)

    try:
        window._on_fetch_done(identity, _ranks())

        assert identity.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_retry_failed_wcl_fetches_skips_before_cooldown(qtbot, tmp_path):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)
    app = _app(
        fetch_status="error",
        error_message="WCL rate-limited; retrying in 60s",
        wcl_error_kind=WCL_ERROR_RATE_LIMITED,
    )
    state.add_or_update(app)
    client._rate_limited_until = time.time() + 60.0

    try:
        assert window._retry_failed_wcl_fetches() == 0
        assert app.fetch_status == "error"
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_retry_failed_wcl_fetches_relaunches_quota_error_after_deadline(
    qtbot,
    tmp_path,
):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)
    app = _app(
        fetch_status="error",
        error_message="WCL quota guard 90% used",
        wcl_error_kind=WCL_ERROR_QUOTA_GUARD,
    )
    state.add_or_update(app)

    try:
        assert window._retry_failed_wcl_fetches() == 1
        assert app.fetch_status == "loading"
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()


def test_retry_failed_wcl_fetches_skips_permanent_failures(qtbot, tmp_path):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)
    missing = _app(
        applicant_id="missing:1",
        name="NoRealm",
        fetch_status="error",
        error_message="missing realm",
        wcl_error_kind="",
    )
    auth = _app(
        applicant_id="auth:1",
        fetch_status="error",
        error_message="OAuth failed",
        wcl_error_kind=WCL_ERROR_AUTH,
    )
    not_found = _app(applicant_id="nf:1", fetch_status="not_found")
    state.add_or_update(missing)
    state.add_or_update(auth)
    state.add_or_update(not_found)

    try:
        assert window._retry_failed_wcl_fetches() == 0
        assert missing.fetch_status == "error"
        assert auth.fetch_status == "error"
        assert not_found.fetch_status == "not_found"
        assert window._fetches_in_flight == set()
    finally:
        client.close()


def test_retry_failed_wcl_fetches_caps_batch_size(qtbot, tmp_path):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)
    apps = [
        _app(
            applicant_id=f"{idx}:1",
            name=f"Scout{idx}-RealmA",
            fetch_status="error",
            error_message="WCL quota guard 90% used",
            wcl_error_kind=WCL_ERROR_QUOTA_GUARD,
        )
        for idx in range(4)
    ]
    for app in apps:
        state.add_or_update(app)

    try:
        assert window._retry_failed_wcl_fetches() == 3
        assert sum(app.fetch_status == "loading" for app in apps) == 3
        assert sum(app.fetch_status == "error" for app in apps) == 1
    finally:
        client.close()


def test_retry_path_with_fake_pool_completes_and_clears_in_flight(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _SyncPool()
    app = _app(
        fetch_status="error",
        error_message="WCL quota guard 90% used",
        wcl_error_kind=WCL_ERROR_QUOTA_GUARD,
    )
    state.add_or_update(app)

    def fake_fetch(*_args, **_kwargs):
        return _ranks()

    client.fetch_character_ranks = fake_fetch  # type: ignore[method-assign]

    try:
        assert window._retry_failed_wcl_fetches() == 1
        assert app.fetch_status == "ready"
        assert app.error_message == ""
        assert app.wcl_error_kind == ""
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_stale_fetch_metric_role_does_not_apply(qtbot, tmp_path):
    state = AppState()
    app = _app(role="HEALER", mplus_dps=12.0)
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        stale_identity = _FetchIdentity(
            applicant_id=app.applicant_id,
            charname_key="scout",
            server_slug="realma",
            region="EU",
            spec_id=app.spec_id,
            metric_role="DPS",
        )

        window._on_fetch_done(stale_identity, _ranks())

        assert app.fetch_status == "loading"
        assert app.mplus_dps is None
        assert app.raid_heroic is None
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()


def test_stale_fetch_region_does_not_apply(qtbot, tmp_path):
    state = AppState()
    app = _app(mplus_dps=12.0)
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, region="US")

    try:
        stale_identity = _FetchIdentity(
            applicant_id=app.applicant_id,
            charname_key="scout",
            server_slug="realma",
            region="EU",
            spec_id=app.spec_id,
            metric_role="DPS",
        )

        window._on_fetch_done(stale_identity, _ranks())

        assert app.fetch_status == "loading"
        assert app.mplus_dps is None
        assert app.raid_heroic is None
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()


def test_pending_same_realm_fetch_uses_late_player_default_realm(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(name="Scout", fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _SyncPool()
    seen: list[tuple[str, str, str | None]] = []

    def fake_fetch(name, server_slug, *_args, region=None, **_kwargs):
        seen.append((name, server_slug, region))
        return _ranks()

    client.fetch_character_ranks = fake_fetch  # type: ignore[method-assign]

    try:
        window._launch_fetch(app)

        assert seen == [("Scout", "realma", "EU")]
        assert app.fetch_status == "ready"
        assert app.raid_heroic == 22.0
    finally:
        client.close()


def test_stale_in_flight_same_realm_fetch_relaunches_after_default_realm_change(
    qtbot, tmp_path
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(name="Scout", mplus_dps=12.0)
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        resolved = _fetch_identity_for_applicant(app, state.player.full_name, "EU")
        assert resolved is not None
        stale_identity, _charname = resolved
        assert stale_identity.server_slug == "realma"
        window._fetches_in_flight.add(app.applicant_id)

        state.player = WoWPlayer(full_name="Host-RealmB")
        window._on_fetch_done(stale_identity, _ranks())

        current = _fetch_identity_for_applicant(app, state.player.full_name, "EU")
        assert current is not None
        current_identity, _ = current
        assert current_identity.server_slug == "realmb"
        assert app.fetch_status == "loading"
        assert app.mplus_dps is None
        assert app.raid_heroic is None
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()


def test_fetch_task_persists_successful_results(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _SyncPool()

    def fake_fetch(*_args, **_kwargs):
        return _ranks()

    client.fetch_character_ranks = fake_fetch  # type: ignore[method-assign]

    try:
        window._launch_fetch(app)

        cached = window._cache.get("Scout", "realma", "EU", 71, "DPS")
        assert cached is not None
        assert cached.raid_heroic == 22.0
    finally:
        client.close()


def test_fetch_task_does_not_persist_not_found_results(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _SyncPool()

    def fake_fetch(*_args, **_kwargs):
        return CharacterRanks.empty(not_found=True)

    client.fetch_character_ranks = fake_fetch  # type: ignore[method-assign]

    try:
        window._launch_fetch(app)

        assert app.fetch_status == "not_found"
        assert window._cache.get("Scout", "realma", "EU", 71, "DPS") is None
    finally:
        client.close()


def test_stale_fetch_generation_does_not_apply(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(mplus_dps=12.0)
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        stale_identity = _FetchIdentity(
            applicant_id=app.applicant_id,
            charname_key="scout",
            server_slug="realma",
            region="EU",
            spec_id=app.spec_id,
            metric_role="DPS",
            runtime_generation=0,
        )
        window.bump_wcl_runtime_generation()

        window._on_fetch_done(stale_identity, _ranks())

        assert app.fetch_status == "loading"
        assert app.mplus_dps is None
        assert app.raid_heroic is None
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()
