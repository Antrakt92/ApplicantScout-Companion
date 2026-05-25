from __future__ import annotations

import time

import httpx

import applicant_scout.wcl as wcl_mod
from applicant_scout.__main__ import StateMachine
from applicant_scout.overlay import (
    _FetchIdentity,
    _FetchTask,
    _fetch_identity_for_applicant,
    OverlayWindow,
)
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.screenshot import (
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
)
from applicant_scout.state import AppState, Applicant, Listing, RosterMember, WoWPlayer
from applicant_scout.wcl import (
    CharacterCache,
    CharacterRanks,
    WCLAuth,
    WCLClient,
    WCLApiError,
    WCL_ERROR_AUTH,
    WCL_ERROR_NETWORK,
    WCL_ERROR_QUOTA_GUARD,
    WCL_ERROR_RATE_LIMITED,
    WCL_ERROR_SERVER,
)

ALL_METRIC_PREFERENCES = MetricPreferences()


class _SyncPool:
    def start(self, task):
        task.run()


class _QueuedPool:
    def __init__(self) -> None:
        self.tasks = []

    def start(self, task) -> None:
        self.tasks.append(task)


class _UiThreadCacheProbe:
    generation = 0

    def __init__(self) -> None:
        self.get_called = False
        self.result: CharacterRanks | None = None

    def get(self, *_args, **_kwargs):
        self.get_called = True
        return self.result


class _GenerationChangingCache:
    generation = 0

    def __init__(self) -> None:
        self.put_expected_generation: int | None = None

    def get(self, *_args, **_kwargs):
        self.generation += 1
        return _ranks_with(raid_heroic=22.0, mplus_dps=77.0)

    def put(self, *_args, expected_generation: int | None = None, **_kwargs) -> bool:
        self.put_expected_generation = expected_generation
        return False


class _FreshFetchClient:
    def __init__(self) -> None:
        self.fetch_called = False

    def fetch_character_ranks(self, *_args, **_kwargs) -> CharacterRanks:
        self.fetch_called = True
        return _ranks_with(raid_heroic=44.0, mplus_dps=88.0)


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


def _member(**overrides) -> RosterMember:
    base = RosterMember(
        applicant_id="scout-realma",
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


def _ranks_with(*, raid_heroic: float, mplus_dps: float) -> CharacterRanks:
    ranks = _ranks()
    ranks.raid_heroic = raid_heroic
    ranks.mplus_dps = mplus_dps
    return ranks


def _window(
    qtbot,
    tmp_path,
    state: AppState,
    *,
    region: str = "EU",
    metric_preferences: MetricPreferences = ALL_METRIC_PREFERENCES,
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


def test_quota_label_shows_in_flight_fetch_before_first_quota(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    try:
        window._launch_fetch(app)

        assert len(queued_pool.tasks) == 1
        assert window._status_label.text() == "WCL: fetching 1 fetch (quota pending)"
    finally:
        client.close()


def test_quota_label_idle_before_first_quota_is_not_called_no_fetch_yet(
    qtbot, tmp_path
):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._refresh_quota_label()

        assert window._status_label.text() == "WCL: — / — (idle, no quota yet)"
    finally:
        client.close()


def test_applicant_burst_coalesces_overlay_refresh(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    state.listing = Listing(
        activity_id=401,
        dungeon_name="Pit of Saron",
        listing_name="+14",
        comment="",
        key_level=14,
    )
    applicants = [
        _app(applicant_id=f"{idx}:1", name=f"Scout{idx}-RealmA", fetch_status="pending")
        for idx in range(1, 5)
    ]
    for applicant in applicants:
        state.add_or_update(applicant)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    refreshes: list[str] = []
    title_updates: list[str] = []
    show_requests: list[str] = []
    window._pool = queued_pool
    window._refresh_table = lambda: refreshes.append("refresh")  # type: ignore[method-assign]
    window._update_title = lambda: title_updates.append("title")  # type: ignore[method-assign]
    window._maybe_show = lambda: show_requests.append("show")  # type: ignore[method-assign]

    try:
        for applicant in applicants:
            window.on_applicant_added(applicant)

        assert len(queued_pool.tasks) == len(applicants)
        assert refreshes == []
        qtbot.waitUntil(lambda: len(refreshes) == 1, timeout=1000)
        assert title_updates == ["title"]
        assert show_requests == ["show"]
    finally:
        client.close()


def test_cache_hit_applies_without_queueing_worker(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool
    window._cache.put(
        "Scout",
        "realma",
        "EU",
        71,
        _ranks(),
        "DPS",
        ALL_METRIC_PREFERENCES,
    )

    try:
        window._launch_fetch(app)

        assert queued_pool.tasks == []
        assert app.fetch_status == "ready"
        assert app.raid_heroic == 22.0
        assert app.mplus_dps == 77.0
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_fetch_task_refetches_when_cache_generation_changes_after_hit():
    identity = _FetchIdentity(
        applicant_id="42:1",
        charname_key="scout",
        server_slug="realma",
        region="EU",
        spec_id=71,
        metric_role="DPS",
        metric_preferences=ALL_METRIC_PREFERENCES,
    )
    cache = _GenerationChangingCache()
    client = _FreshFetchClient()
    task = _FetchTask(identity, "Scout", client, cache)  # type: ignore[arg-type]
    emitted: list[CharacterRanks] = []
    task.signals.done.connect(lambda _identity, ranks: emitted.append(ranks))

    task.run()

    assert client.fetch_called is True
    assert emitted[-1].raid_heroic == 44.0
    assert emitted[-1].mplus_dps == 88.0
    assert cache.put_expected_generation == 0


def test_fetch_done_burst_coalesces_overlay_refresh(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    apps = [
        _app(applicant_id=f"{idx}:1", name=f"Scout{idx}-RealmA") for idx in range(1, 4)
    ]
    for applicant in apps:
        state.add_or_update(applicant)
    window, client = _window(qtbot, tmp_path, state)
    refreshes: list[str] = []
    window._refresh_table = lambda: refreshes.append("refresh")  # type: ignore[method-assign]

    try:
        identities: list[_FetchIdentity] = []
        for applicant in apps:
            resolved = _fetch_identity_for_applicant(
                applicant,
                state.player.full_name,
                "EU",
                ALL_METRIC_PREFERENCES,
            )
            assert resolved is not None
            identity, _charname = resolved
            identities.append(identity)
            window._mark_fetch_in_flight(identity)

        for identity in identities:
            window._on_fetch_done(identity, _ranks())

        assert refreshes == []
        qtbot.waitUntil(lambda: len(refreshes) == 1, timeout=1000)
    finally:
        client.close()


def test_matching_fetch_identity_applies_ranks(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app()
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        resolved = _fetch_identity_for_applicant(
            app,
            state.player.full_name,
            "EU",
            ALL_METRIC_PREFERENCES,
        )
        assert resolved is not None
        identity, _charname = resolved

        window._mark_fetch_in_flight(identity)
        window._on_fetch_done(identity, _ranks())

        assert app.fetch_status == "ready"
        assert app.error_message == ""
        assert app.raid_heroic == 22.0
        assert app.mplus_dps == 77.0
        assert app.wcl_metric_preferences == ALL_METRIC_PREFERENCES
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_fetch_done_prefers_not_found_over_error_text(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app()
    app.fetch_status = "ready"
    app.raid_heroic = 88.0
    app.mplus_dps = 77.0
    app.mplus_dps_breakdown = [
        {"name": "Skyreach", "parse_percent": 90.0, "key_level": 16, "run_count": 3}
    ]
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        resolved = _fetch_identity_for_applicant(
            app,
            state.player.full_name,
            "EU",
            ALL_METRIC_PREFERENCES,
        )
        assert resolved is not None
        identity, _charname = resolved

        window._on_fetch_done(
            identity,
            CharacterRanks.empty(not_found=True, error="could not find character"),
        )

        assert app.fetch_status == "not_found"
        assert app.error_message == ""
        assert app.wcl_error_kind == ""
        assert app.raid_heroic is None
        assert app.mplus_dps is None
        assert app.mplus_dps_breakdown == []
    finally:
        client.close()


def test_fetch_error_stores_error_kind(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app()
    app.fetch_status = "ready"
    app.raid_heroic = 88.0
    app.mplus_dps = 77.0
    app.mplus_dps_breakdown = [
        {"name": "Skyreach", "parse_percent": 90.0, "key_level": 16, "run_count": 3}
    ]
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        resolved = _fetch_identity_for_applicant(
            app,
            state.player.full_name,
            "EU",
            ALL_METRIC_PREFERENCES,
        )
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
        assert app.raid_heroic is None
        assert app.mplus_dps is None
        assert app.mplus_dps_breakdown == []
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


def test_fetch_task_marks_timeout_as_network_error(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _SyncPool()

    def fake_fetch(*_args, **_kwargs):
        raise httpx.ReadTimeout("The read operation timed out")

    client.fetch_character_ranks = fake_fetch  # type: ignore[method-assign]

    try:
        window._launch_fetch(app)

        assert app.fetch_status == "error"
        assert app.error_message == "The read operation timed out"
        assert app.wcl_error_kind == WCL_ERROR_NETWORK
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
        resolved = _fetch_identity_for_applicant(
            app,
            state.player.full_name,
            "EU",
            ALL_METRIC_PREFERENCES,
        )
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
            metric_preferences=ALL_METRIC_PREFERENCES,
        )
        window._mark_fetch_in_flight(broad_identity)

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
    window._mark_fetch_in_flight(identity)

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


def test_retry_failed_wcl_fetches_skips_server_error_before_cooldown(
    qtbot,
    tmp_path,
):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)
    app = _app(
        fetch_status="error",
        error_message="WCL server error; retrying in 30s",
        wcl_error_kind=WCL_ERROR_SERVER,
    )
    state.add_or_update(app)
    client._server_retry_until = time.time() + 30.0

    try:
        assert window._retry_failed_wcl_fetches() == 0
        assert app.fetch_status == "error"
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_matching_party_fetch_identity_applies_ranks_to_party_member_only(
    qtbot, tmp_path
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    member = _member()
    colliding_applicant = _app(
        applicant_id=member.applicant_id,
        name="Other-RealmA",
        fetch_status="loading",
    )
    state.add_or_update(colliding_applicant)
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)

    try:
        resolved = _fetch_identity_for_applicant(
            member,
            state.player.full_name,
            "EU",
            ALL_METRIC_PREFERENCES,
            row_source="party",
        )
        assert resolved is not None
        identity, _charname = resolved

        window._mark_fetch_in_flight(identity)
        window._on_fetch_done(identity, _ranks())

        assert member.fetch_status == "ready"
        assert member.raid_heroic == 22.0
        assert member.mplus_dps == 77.0
        assert colliding_applicant.fetch_status == "loading"
        assert colliding_applicant.mplus_dps is None
        assert identity.storage_key not in window._fetches_in_flight
    finally:
        client.close()


def test_retry_failed_wcl_fetches_skips_network_error_before_cooldown(
    qtbot,
    tmp_path,
):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)
    app = _app(
        fetch_status="error",
        error_message="WCL network error; retrying in 30s",
        wcl_error_kind=WCL_ERROR_NETWORK,
    )
    state.add_or_update(app)
    client._network_retry_until = time.time() + 30.0

    try:
        assert window._retry_failed_wcl_fetches() == 0
        assert app.fetch_status == "error"
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_retry_failed_wcl_fetches_relaunches_server_error_after_deadline(
    qtbot,
    tmp_path,
):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)
    app = _app(
        fetch_status="error",
        error_message="WCL server error; retrying in 30s",
        wcl_error_kind=WCL_ERROR_SERVER,
    )
    state.add_or_update(app)

    try:
        assert window._retry_failed_wcl_fetches() == 1
        assert app.fetch_status == "loading"
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()


def test_retry_failed_wcl_fetches_relaunches_network_error(
    qtbot,
    tmp_path,
):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)
    app = _app(
        fetch_status="error",
        error_message="The read operation timed out",
        wcl_error_kind=WCL_ERROR_NETWORK,
    )
    state.add_or_update(app)

    try:
        assert window._retry_failed_wcl_fetches() == 1
        assert app.fetch_status == "loading"
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()


def test_retry_failed_wcl_fetches_relaunches_party_member_error(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    window, client = _window(qtbot, tmp_path, state)
    member = _member(
        fetch_status="error",
        error_message="The read operation timed out",
        wcl_error_kind=WCL_ERROR_NETWORK,
    )
    state.add_or_update_party_member(member)

    try:
        assert window._retry_failed_wcl_fetches() == 1
        identity = window._current_fetch_identity_for(member)

        assert identity is not None
        assert member.fetch_status == "loading"
        assert identity.row_source == "party"
        assert identity.storage_key in window._fetches_in_flight
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
    http = _app(
        applicant_id="http:1",
        fetch_status="error",
        error_message="Unexpected HTTP 400",
        wcl_error_kind=wcl_mod.WCL_ERROR_HTTP,
    )
    not_found = _app(applicant_id="nf:1", fetch_status="not_found")
    state.add_or_update(missing)
    state.add_or_update(auth)
    state.add_or_update(http)
    state.add_or_update(not_found)

    try:
        assert window._retry_failed_wcl_fetches() == 0
        assert missing.fetch_status == "error"
        assert auth.fetch_status == "error"
        assert http.fetch_status == "error"
        assert not_found.fetch_status == "not_found"
        assert window._fetches_in_flight == {}
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
        window._mark_fetch_in_flight(stale_identity)

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

        cached = window._cache.get(
            "Scout",
            "realma",
            "EU",
            71,
            "DPS",
            ALL_METRIC_PREFERENCES,
        )
        assert cached is not None
        assert cached.raid_heroic == 22.0
    finally:
        client.close()


def test_fetch_task_started_before_clear_does_not_repopulate_character_cache(
    qtbot,
    tmp_path,
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    def fake_fetch(*_args, **_kwargs):
        return _ranks()

    client.fetch_character_ranks = fake_fetch  # type: ignore[method-assign]

    try:
        window._launch_fetch(app)
        assert len(queued_pool.tasks) == 1

        window._cache.clear()
        queued_pool.tasks[0].run()

        assert app.fetch_status == "ready"
        assert app.raid_heroic == 22.0
        assert window._cache.get("Scout", "realma", "EU", 71, "DPS") is None
        assert not window._cache._path.exists()
    finally:
        client.close()


def test_fetch_task_started_before_clear_does_not_repopulate_not_found_cache(
    qtbot,
    tmp_path,
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    def fake_fetch(*_args, **_kwargs):
        return CharacterRanks.empty(not_found=True)

    client.fetch_character_ranks = fake_fetch  # type: ignore[method-assign]

    try:
        window._launch_fetch(app)
        assert len(queued_pool.tasks) == 1

        window._cache.clear()
        queued_pool.tasks[0].run()

        assert app.fetch_status == "not_found"
        assert window._cache.get("Scout", "realma", "EU", 71, "DPS") is None
        assert not window._cache._path.exists()
    finally:
        client.close()


def test_launch_fetch_checks_cache_before_queueing_worker(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    cache_probe = _UiThreadCacheProbe()
    cache_probe.result = _ranks()
    window._pool = queued_pool
    window._cache = cache_probe  # type: ignore[assignment]

    try:
        window._launch_fetch(app)

        assert queued_pool.tasks == []
        assert app.fetch_status == "ready"
        assert app.raid_heroic == 22.0
        assert cache_probe.get_called
    finally:
        client.close()


def test_launch_fetch_unknown_spec_mplus_only_marks_ready_without_queue(
    qtbot, tmp_path
):
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(spec_id=0, fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=prefs)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    try:
        window._launch_fetch(app)

        assert queued_pool.tasks == []
        assert window._in_flight_identity(app.applicant_id) is None
        assert app.fetch_status == "ready"
        assert app.wcl_metric_preferences is None
        assert app.mplus_dps is None
    finally:
        client.close()


def test_unknown_spec_raid_fetch_uses_effective_preferences_without_loop(
    qtbot, tmp_path
):
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    effective = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(spec_id=0, fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=prefs)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    try:
        window._launch_fetch(app)
        identity = window._in_flight_identity(app.applicant_id)

        assert identity is not None
        assert identity.metric_preferences == effective
        assert len(queued_pool.tasks) == 1

        window._on_fetch_done(identity, _ranks_with(raid_heroic=44.0, mplus_dps=88.0))

        assert app.fetch_status == "ready"
        assert app.raid_heroic == 44.0
        assert app.mplus_dps is None
        assert app.wcl_metric_preferences == effective
        assert window._in_flight_identity(app.applicant_id) is None

        window.apply_metric_preferences(prefs)

        assert len(queued_pool.tasks) == 1
        assert app.fetch_status == "ready"
        assert app.wcl_metric_preferences == effective
    finally:
        client.close()


def test_window_init_projects_unknown_spec_to_effective_preferences(qtbot, tmp_path):
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    effective = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(
        spec_id=0,
        fetch_status="ready",
        raid_heroic=44.0,
        mplus_dps=88.0,
        wcl_metric_preferences=effective,
    )
    state.add_or_update(app)

    window, client = _window(qtbot, tmp_path, state, metric_preferences=prefs)

    try:
        assert app.raid_heroic == 44.0
        assert app.mplus_dps is None
        assert app.wcl_metric_preferences == effective
        assert window._in_flight_identity(app.applicant_id) is None
    finally:
        client.close()


def test_fetch_task_uses_broader_cached_scope_for_narrow_identity(qtbot, tmp_path):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=narrow)
    window._pool = _SyncPool()
    window._cache.put(
        "Scout",
        "realma",
        "EU",
        71,
        _ranks(),
        "DPS",
        ALL_METRIC_PREFERENCES,
    )

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("cache hit should avoid WCL fetch")

    client.fetch_character_ranks = fail_fetch  # type: ignore[method-assign]

    try:
        window._launch_fetch(app)

        assert app.fetch_status == "ready"
        assert app.raid_normal is None
        assert app.raid_heroic == 22.0
        assert app.raid_mythic is None
        assert app.mplus_dps is None
        assert app.wcl_metric_preferences == narrow
    finally:
        client.close()


def test_relist_same_applicant_id_launches_new_fetch_despite_old_in_flight(
    qtbot,
    tmp_path,
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    old_app = _app(fetch_status="pending")
    state.add_or_update(old_app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._launch_fetch(old_app)
        old_identity = window._in_flight_identity(old_app.applicant_id)
        assert old_identity is not None

        window.on_cleared()
        state.applicants.clear()
        new_app = _app(fetch_status="pending")
        state.add_or_update(new_app)

        window._launch_fetch(new_app)
        new_identity = window._in_flight_identity(new_app.applicant_id)

        assert new_app.fetch_status == "loading"
        assert new_identity is not None
        assert new_identity != old_identity
        assert (
            new_identity.listing_session_generation
            > old_identity.listing_session_generation
        )
    finally:
        client.close()


def test_stale_completion_after_relist_does_not_clear_new_in_flight(
    qtbot,
    tmp_path,
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    old_app = _app(fetch_status="pending")
    state.add_or_update(old_app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._launch_fetch(old_app)
        old_identity = window._in_flight_identity(old_app.applicant_id)
        assert old_identity is not None

        window.on_cleared()
        state.applicants.clear()
        new_app = _app(fetch_status="pending")
        state.add_or_update(new_app)
        window._launch_fetch(new_app)
        new_identity = window._in_flight_identity(new_app.applicant_id)
        assert new_identity is not None

        window._on_fetch_done(
            old_identity, _ranks_with(raid_heroic=44.0, mplus_dps=88.0)
        )

        assert new_app.fetch_status == "loading"
        assert new_app.raid_heroic is None
        assert new_app.mplus_dps is None
        assert window._in_flight_identity(new_app.applicant_id) == new_identity
    finally:
        client.close()


def test_stale_completion_after_relist_does_not_overwrite_ready_new_results(
    qtbot,
    tmp_path,
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    old_app = _app(fetch_status="pending")
    state.add_or_update(old_app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._launch_fetch(old_app)
        old_identity = window._in_flight_identity(old_app.applicant_id)
        assert old_identity is not None

        window.on_cleared()
        state.applicants.clear()
        new_app = _app(fetch_status="pending")
        state.add_or_update(new_app)
        window._launch_fetch(new_app)
        new_identity = window._in_flight_identity(new_app.applicant_id)
        assert new_identity is not None

        window._on_fetch_done(
            new_identity, _ranks_with(raid_heroic=55.0, mplus_dps=66.0)
        )
        assert new_app.fetch_status == "ready"
        assert new_app.raid_heroic == 55.0
        assert new_app.mplus_dps == 66.0
        assert window._in_flight_identity(new_app.applicant_id) is None

        window._on_fetch_done(
            old_identity, _ranks_with(raid_heroic=11.0, mplus_dps=22.0)
        )

        assert new_app.fetch_status == "ready"
        assert new_app.raid_heroic == 55.0
        assert new_app.mplus_dps == 66.0
        assert window._in_flight_identity(new_app.applicant_id) is None
    finally:
        client.close()


def test_metric_broadening_allows_broader_fetch_when_narrow_fetch_in_flight(
    qtbot,
    tmp_path,
):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=narrow)

    try:
        window._launch_fetch(app)
        narrow_identity = window._in_flight_identity(app.applicant_id)
        assert narrow_identity is not None
        assert narrow_identity.metric_preferences == narrow

        window.apply_metric_preferences(ALL_METRIC_PREFERENCES)
        broad_identity = window._in_flight_identity(app.applicant_id)

        assert broad_identity is not None
        assert broad_identity != narrow_identity
        assert broad_identity.metric_preferences == ALL_METRIC_PREFERENCES
    finally:
        client.close()


def test_metric_broadening_refetches_party_member_with_missing_scope(
    qtbot,
    tmp_path,
):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    member = _member(
        fetch_status="ready",
        raid_heroic=44.0,
        wcl_metric_preferences=narrow,
    )
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=narrow)

    try:
        window.apply_metric_preferences(ALL_METRIC_PREFERENCES)
        identity = window._current_fetch_identity_for(member)

        assert identity is not None
        assert member.fetch_status == "loading"
        assert member.raid_heroic is None
        assert member.mplus_dps is None
        assert identity.row_source == "party"
        assert identity.storage_key in window._fetches_in_flight
    finally:
        client.close()


def test_old_narrow_completion_does_not_clear_ready_broad_data(qtbot, tmp_path):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=narrow)

    try:
        window._launch_fetch(app)
        narrow_identity = window._in_flight_identity(app.applicant_id)
        assert narrow_identity is not None

        window.apply_metric_preferences(ALL_METRIC_PREFERENCES)
        broad_identity = window._in_flight_identity(app.applicant_id)
        assert broad_identity is not None

        window._on_fetch_done(
            broad_identity, _ranks_with(raid_heroic=55.0, mplus_dps=66.0)
        )
        assert app.fetch_status == "ready"
        assert app.raid_heroic == 55.0
        assert app.mplus_dps == 66.0

        window._on_fetch_done(
            narrow_identity, _ranks_with(raid_heroic=11.0, mplus_dps=22.0)
        )

        assert app.fetch_status == "ready"
        assert app.raid_heroic == 55.0
        assert app.mplus_dps == 66.0
        assert window._in_flight_identity(app.applicant_id) is None
    finally:
        client.close()


def test_old_narrow_completion_does_not_refetch_after_broad_not_found(
    qtbot, tmp_path
):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=narrow)

    try:
        window._launch_fetch(app)
        narrow_identity = window._in_flight_identity(app.applicant_id)
        assert narrow_identity is not None

        window.apply_metric_preferences(ALL_METRIC_PREFERENCES)
        broad_identity = window._in_flight_identity(app.applicant_id)
        assert broad_identity is not None

        window._on_fetch_done(broad_identity, CharacterRanks.empty(not_found=True))
        assert app.fetch_status == "not_found"

        window._on_fetch_done(
            narrow_identity, _ranks_with(raid_heroic=11.0, mplus_dps=22.0)
        )

        assert app.fetch_status == "not_found"
        assert window._in_flight_identity(app.applicant_id) is None
    finally:
        client.close()


def test_old_narrow_completion_does_not_refetch_after_broad_error(qtbot, tmp_path):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=narrow)

    try:
        window._launch_fetch(app)
        narrow_identity = window._in_flight_identity(app.applicant_id)
        assert narrow_identity is not None

        window.apply_metric_preferences(ALL_METRIC_PREFERENCES)
        broad_identity = window._in_flight_identity(app.applicant_id)
        assert broad_identity is not None

        window._on_fetch_done(
            broad_identity,
            CharacterRanks.empty(error="WCL server error", error_kind=WCL_ERROR_SERVER),
        )
        assert app.fetch_status == "error"
        assert app.error_message == "WCL server error"

        window._on_fetch_done(
            narrow_identity, _ranks_with(raid_heroic=11.0, mplus_dps=22.0)
        )

        assert app.fetch_status == "error"
        assert app.error_message == "WCL server error"
        assert window._in_flight_identity(app.applicant_id) is None
    finally:
        client.close()


def test_completion_after_disabling_all_metrics_is_ignored(qtbot, tmp_path):
    disabled = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._launch_fetch(app)
        identity = window._in_flight_identity(app.applicant_id)
        assert identity is not None

        window.apply_metric_preferences(disabled)
        assert app.fetch_status == "ready"
        assert app.applicant_id not in window._fetches_in_flight

        window._on_fetch_done(
            identity,
            CharacterRanks.empty(error="WCL server error", error_kind=WCL_ERROR_SERVER),
        )
        assert app.fetch_status == "ready"
        assert app.error_message == ""
        assert app.wcl_error_kind == ""

        window._on_fetch_done(identity, CharacterRanks.empty(not_found=True))
        assert app.fetch_status == "ready"
        assert app.error_message == ""
        assert app.wcl_error_kind == ""
    finally:
        client.close()


def test_completion_after_unknown_spec_effective_metrics_empty_is_ignored(
    qtbot, tmp_path
):
    raid_and_mplus = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    mplus_only = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(spec_id=0, fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state, metric_preferences=raid_and_mplus)

    try:
        window._launch_fetch(app)
        identity = window._in_flight_identity(app.applicant_id)
        assert identity is not None

        window.apply_metric_preferences(mplus_only)
        assert app.fetch_status == "ready"
        assert app.applicant_id not in window._fetches_in_flight

        window._on_fetch_done(
            identity,
            CharacterRanks.empty(error="WCL server error", error_kind=WCL_ERROR_SERVER),
        )
        assert app.fetch_status == "ready"
        assert app.error_message == ""
        assert app.wcl_error_kind == ""

        window._on_fetch_done(identity, CharacterRanks.empty(not_found=True))
        assert app.fetch_status == "ready"
        assert app.error_message == ""
        assert app.wcl_error_kind == ""
    finally:
        client.close()


def test_party_completion_after_disabling_all_metrics_clears_in_flight_key(
    qtbot, tmp_path
):
    disabled = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    member = _member(fetch_status="pending")
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._launch_fetch(member)
        identity = window._fetches_in_flight.get(f"party:{member.applicant_id}")
        assert identity is not None

        window.apply_metric_preferences(disabled)

        assert f"party:{member.applicant_id}" not in window._fetches_in_flight
        window._on_fetch_done(
            identity,
            CharacterRanks.empty(error="WCL server error", error_kind=WCL_ERROR_SERVER),
        )
        assert member.fetch_status == "ready"
        assert member.error_message == ""
        assert member.wcl_error_kind == ""
    finally:
        client.close()


def test_wcl_runtime_generation_bump_refetches_party_members(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    member = _member(fetch_status="ready", raid_heroic=44.0, mplus_dps=88.0)
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)

    try:
        window.bump_wcl_runtime_generation()
        identity = window._current_fetch_identity_for(member)

        assert identity is not None
        assert member.fetch_status == "loading"
        assert member.raid_heroic is None
        assert member.mplus_dps is None
        assert identity.row_source == "party"
        assert identity.runtime_generation == 1
        assert identity.storage_key in window._fetches_in_flight
    finally:
        client.close()


def test_fetch_task_persists_not_found_and_reuses_across_identity_churn(
    qtbot, tmp_path
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(fetch_status="pending")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _SyncPool()
    calls = 0

    def fake_fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return CharacterRanks.empty(not_found=True)

    client.fetch_character_ranks = fake_fetch  # type: ignore[method-assign]

    try:
        window._launch_fetch(app)

        assert app.fetch_status == "not_found"
        assert calls == 1
        cached = window._cache.get("Scout", "realma", "EU", 71, "DPS")
        assert cached is not None
        assert cached.not_found is True

        app.spec_id = 72
        app.role = "HEALER"
        app.fetch_status = "pending"
        window._launch_fetch(app)

        assert app.fetch_status == "not_found"
        assert calls == 1
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


def test_listing_clear_roster_snapshot_does_not_requeue_stale_party_fetch(
    qtbot,
    tmp_path,
    monkeypatch,
):
    state = AppState()
    machine = StateMachine(state)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool
    machine.applicantAdded.connect(window.on_applicant_added)
    machine.applicantUpdated.connect(window.on_applicant_updated)
    machine.applicantRemoved.connect(window.on_applicant_removed)
    machine.listingChanged.connect(window.on_listing_changed)
    machine.rosterChanged.connect(window.on_roster_changed)
    machine.cleared.connect(window.on_cleared)

    listed = Snapshot(
        listing=DecodedListing(
            activity_id=401,
            dungeon_name="Pit of Saron",
            listing_name="+14",
            comment="",
            key_level=14,
            category_id=2,
        ),
        version=DecodedVersion(
            addon_version="1.0.0",
            game_version="12.0.0",
            region_id=3,
            player_name="Host-RealmA",
        ),
    )
    roster_only = Snapshot(
        listing=None,
        version=DecodedVersion(
            addon_version="1.0.0",
            game_version="12.0.0",
            region_id=3,
            player_name="Host-RealmA",
        ),
        roster=[
            DecodedRosterMember(
                unit_index=0,
                flags=1,
                subgroup=1,
                class_id=1,
                spec_id=71,
                ilvl=480,
                score=2400,
                main_score=0,
                role=2,
                name="Host-RealmA",
            )
        ],
    )

    try:
        machine.apply_snapshot(listed)
        assert window._listing_session_generation == 0

        launcher_calls: list[str] = []
        monkeypatch.setattr(
            window,
            "show_launcher_only",
            lambda: launcher_calls.append("show_launcher_only"),
        )

        machine.apply_snapshot(roster_only)

        assert launcher_calls == []
        assert window._listing_session_generation == 1
        assert len(queued_pool.tasks) == 1
        stale_identity = queued_pool.tasks[0]._identity
        assert stale_identity.row_source == "party"
        assert stale_identity.listing_session_generation == 1

        window._on_fetch_done(stale_identity, _ranks())

        assert len(queued_pool.tasks) == 1
        assert state.party_members["host-realma"].fetch_status == "ready"
    finally:
        client.close()


def test_listing_clear_with_unchanged_party_roster_does_not_leave_loading_without_inflight(
    qtbot,
    tmp_path,
    monkeypatch,
):
    state = AppState()
    machine = StateMachine(state)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool
    machine.applicantAdded.connect(window.on_applicant_added)
    machine.applicantUpdated.connect(window.on_applicant_updated)
    machine.applicantRemoved.connect(window.on_applicant_removed)
    machine.listingChanged.connect(window.on_listing_changed)
    machine.rosterChanged.connect(window.on_roster_changed)
    machine.cleared.connect(window.on_cleared)

    listed = Snapshot(
        listing=DecodedListing(
            activity_id=401,
            dungeon_name="Pit of Saron",
            listing_name="+14",
            comment="",
            key_level=14,
            category_id=2,
        ),
        version=DecodedVersion(
            addon_version="1.0.0",
            game_version="12.0.0",
            region_id=3,
            player_name="Host-RealmA",
        ),
        roster=[
            DecodedRosterMember(
                unit_index=0,
                flags=1,
                subgroup=1,
                class_id=1,
                spec_id=71,
                ilvl=480,
                score=2400,
                main_score=0,
                role=2,
                name="Host-RealmA",
            )
        ],
    )
    roster_only = Snapshot(
        listing=None,
        version=DecodedVersion(
            addon_version="1.0.0",
            game_version="12.0.0",
            region_id=3,
            player_name="Host-RealmA",
        ),
        roster=listed.roster,
    )

    try:
        machine.apply_snapshot(listed)
        assert len(queued_pool.tasks) == 1

        launcher_calls: list[str] = []
        monkeypatch.setattr(
            window,
            "show_launcher_only",
            lambda: launcher_calls.append("show_launcher_only"),
        )

        machine.apply_snapshot(roster_only)

        member = state.party_members["host-realma"]
        assert launcher_calls == []
        assert member.fetch_status == "loading"
        assert len(queued_pool.tasks) == 2
        assert len(window._fetches_in_flight) == 1
        identity = queued_pool.tasks[-1]._identity
        assert identity.row_source == "party"
        assert identity.listing_session_generation == 1
    finally:
        client.close()
