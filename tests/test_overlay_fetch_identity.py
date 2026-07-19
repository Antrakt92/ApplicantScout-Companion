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
    DecodedApplicant,
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
)
from applicant_scout.state import AppState, Applicant, Listing, RosterMember, WoWPlayer
from applicant_scout.wcl import (
    CharacterCache,
    CharacterRanks,
    RateLimitInfo,
    WCLAuth,
    WCLClient,
    WCLApiError,
    WCL_ERROR_AUTH,
    WCL_ERROR_MALFORMED,
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


class _ShutdownPool(_QueuedPool):
    def __init__(self) -> None:
        super().__init__()
        self.cleared = False
        self.wait_args: list[int] = []

    def clear(self) -> None:
        self.cleared = True
        self.tasks.clear()

    def waitForDone(self, timeout_ms: int = -1) -> bool:
        self.wait_args.append(timeout_ms)
        return True


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
        assert window._status_label.text() == "WCL 1 active"
        assert window._status_label.property("statusState") == "active"
    finally:
        client.close()


def test_quota_label_counts_raid_detail_fetch_before_first_quota(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    state.listing = Listing(
        activity_id=999,
        dungeon_name="",
        listing_name="Raid",
        comment="",
        difficulty_id=16,
    )
    app = _app(fetch_status="ready", raid_boss_parses={})
    state.add_or_update(app)
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    window, client = _window(
        qtbot,
        tmp_path,
        state,
        metric_preferences=prefs,
    )
    queued_pool = _QueuedPool()
    window._pool = queued_pool
    window._panel._set_detail_mode("raid")

    try:
        assert window._launch_raid_boss_fetch_if_needed(app) is True

        assert len(queued_pool.tasks) == 1
        assert window._status_label.text() == "WCL 1 active"
        assert window._status_label.property("statusState") == "active"
        assert "quota will appear" in window._status_label.toolTip()
    finally:
        client.close()


def test_quota_label_idle_before_first_quota_is_not_called_no_fetch_yet(
    qtbot, tmp_path
):
    state = AppState()
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._refresh_quota_label()

        assert window._status_label.text() == "WCL —/—"
        assert window._status_label.property("statusState") == "neutral"
        assert window._status_label.accessibleDescription() == (
            "No Warcraft Logs quota data yet; no requests are active."
        )
    finally:
        client.close()


def test_quota_chip_preserves_warning_thresholds_with_compact_copy(qtbot, tmp_path):
    window, client = _window(qtbot, tmp_path, AppState())

    try:
        for spent, expected_percent, expected_state in (
            (69.9, 69, "neutral"),
            (70.0, 70, "warning"),
            (89.9, 89, "warning"),
            (90.0, 90, "critical"),
        ):
            client.last_quota = RateLimitInfo(
                limit_per_hour=100.0,
                points_spent=spent,
                reset_in_seconds=120.0,
            )
            window._refresh_quota_label()

            assert window._status_label.text() == f"WCL {expected_percent}%"
            assert window._status_label.property("statusState") == expected_state
            assert "resets in 2m" in window._status_label.toolTip()
            assert (
                window._status_label.accessibleDescription()
                == window._status_label.toolTip()
            )
    finally:
        client.close()


def test_shutdown_fetches_rejects_new_work_then_clears_and_fully_drains_pool(
    qtbot, tmp_path
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    applicant = _app(fetch_status="pending")
    state.add_or_update(applicant)
    window, client = _window(qtbot, tmp_path, state)
    pool = _ShutdownPool()
    window._pool = pool
    pool.tasks.append(object())

    try:
        assert window._foreground_timer.isActive()
        assert window._quota_timer.isActive()
        assert window._launcher.isVisible()

        assert window.shutdown_fetches() is True
        window._launch_fetch(applicant)

        assert window._closed is True
        assert not window._foreground_timer.isActive()
        assert not window._quota_timer.isActive()
        assert not window._launcher.isVisible()
        assert pool.cleared is True
        assert pool.wait_args == [-1]
        assert pool.tasks == []
        assert window.shutdown_fetches() is True
        assert pool.wait_args == [-1]
    finally:
        client.close()


def test_shutdown_fetches_ignores_late_completion_and_retry_callbacks(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    applicant = _app(fetch_status="loading")
    state.add_or_update(applicant)
    window, client = _window(qtbot, tmp_path, state)
    pool = _ShutdownPool()
    window._pool = pool
    resolved = _fetch_identity_for_applicant(
        applicant,
        state.player.full_name,
        "EU",
        ALL_METRIC_PREFERENCES,
    )
    assert resolved is not None
    identity, _charname = resolved
    window._mark_fetch_in_flight(identity)
    window._mark_raid_boss_fetch_in_flight(identity)
    syncs: list[str] = []
    window._sync_delegate_and_panel = lambda: syncs.append("sync")  # type: ignore[method-assign]

    try:
        assert window._refresh_flush_pending is False
        assert window.shutdown_fetches() is True

        window._on_fetch_done(identity, _ranks())
        window._on_raid_boss_fetch_done(identity, {}, "")
        window._retry_ready_raid_boss_fetches()

        assert applicant.fetch_status == "loading"
        assert applicant.raid_heroic is None
        assert identity.storage_key in window._fetches_in_flight
        assert identity.storage_key in window._raid_boss_fetches_in_flight
        assert window._refresh_flush_pending is False
        assert syncs == []
    finally:
        client.close()


def test_auth_chip_maps_secret_free_status_copy_and_accessibility(qtbot, tmp_path):
    window, client = _window(qtbot, tmp_path, AppState())
    auth = WCLAuth("client", "secret", tmp_path)

    def assert_chip(text: str, state: str, detail: str) -> None:
        window._refresh_auth_label()
        assert window._auth_label.text() == text
        assert window._auth_label.property("statusState") == state
        assert detail in window._auth_label.toolTip()
        assert (
            window._auth_label.accessibleDescription()
            == window._auth_label.toolTip()
        )
        assert "client" not in window._auth_label.toolTip().lower()
        assert "secret" not in window._auth_label.toolTip().lower()

    try:
        client.reconfigure_auth(auth)
        assert_chip("Auth —", "neutral", "not been checked")

        validation = client.begin_auth_validation()
        assert validation is not None
        assert_chip("Auth check", "active", "Checking")

        client.reconfigure_auth(auth, validated=True)
        assert_chip("Auth ready", "neutral", "accepted")

        client.record_api_result(succeeded=True)
        assert_chip("Auth ready", "neutral", "request succeeded")

        for error_kind, expected_text, expected_state, expected_detail in (
            (WCL_ERROR_AUTH, "Auth failed", "critical", "rejected"),
            (WCL_ERROR_NETWORK, "Auth offline", "warning", "internet access"),
            (WCL_ERROR_SERVER, "Auth issue", "warning", "unavailable"),
            (WCL_ERROR_RATE_LIMITED, "Auth issue", "warning", "limiting"),
            (WCL_ERROR_MALFORMED, "Auth issue", "warning", "unexpected response"),
            ("", "Auth issue", "warning", "validation failed"),
        ):
            client.record_api_result(succeeded=False, error_kind=error_kind)
            assert_chip(expected_text, expected_state, expected_detail)
    finally:
        client.close()


def test_auth_chip_keeps_legacy_client_without_status_api_neutral(qtbot, tmp_path):
    window, client = _window(qtbot, tmp_path, AppState())
    legacy_client = object()
    window._wcl_client = legacy_client

    try:
        window._refresh_auth_label()
        window._record_fetch_connection_status(
            _FetchIdentity(
                applicant_id="42:1",
                charname_key="scout",
                server_slug="realma",
                region="EU",
                spec_id=71,
                metric_role="DPS",
                metric_preferences=ALL_METRIC_PREFERENCES,
            ),
            _ranks(),
        )

        assert window._auth_label.text() == "Auth —"
        assert window._auth_label.property("statusState") == "neutral"
    finally:
        window._wcl_client = client
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


def test_same_character_party_and_applicant_fetch_coalesce_before_cache_write(
    qtbot,
    tmp_path,
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    applicant = _app(applicant_id="42:1", name="Scout-RealmA", fetch_status="pending")
    member = _member(
        applicant_id="scout-realma",
        name="Scout-RealmA",
        fetch_status="pending",
    )
    state.add_or_update(applicant)
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    try:
        window._launch_fetch(applicant)
        window._launch_fetch(member)

        assert len(queued_pool.tasks) == 1
        assert applicant.fetch_status == "loading"
        assert member.fetch_status == "loading"
        assert applicant.applicant_id in window._fetches_in_flight
        assert f"party:{member.applicant_id}" in window._fetches_in_flight

        window._on_fetch_done(queued_pool.tasks[0]._identity, _ranks())

        assert applicant.fetch_status == "ready"
        assert member.fetch_status == "ready"
        assert applicant.raid_heroic == 22.0
        assert member.raid_heroic == 22.0
        assert window._fetches_in_flight == {}
    finally:
        client.close()


def test_cache_hit_while_target_fetch_pending_applies_to_new_row(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    applicant = _app(applicant_id="42:1", name="Scout-RealmA", fetch_status="pending")
    member = _member(
        applicant_id="scout-realma",
        name="Scout-RealmA",
        fetch_status="pending",
    )
    state.add_or_update(applicant)
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    try:
        window._launch_fetch(applicant)
        assert len(queued_pool.tasks) == 1
        identity = queued_pool.tasks[0]._identity
        window._cache.put(
            "Scout",
            identity.server_slug,
            identity.region,
            identity.spec_id,
            _ranks(),
            identity.metric_role,
            identity.metric_preferences,
        )

        window._launch_fetch(member)

        assert applicant.fetch_status == "ready"
        assert member.fetch_status == "ready"
        assert applicant.mplus_dps == 77.0
        assert member.mplus_dps == 77.0
        assert window._fetches_in_flight == {}
        assert window._fetch_waiters_by_target == {}
        assert len(queued_pool.tasks) == 1
    finally:
        client.close()


def test_cache_hit_after_applicant_fetch_ignores_late_original_error(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    applicant = _app(applicant_id="42:1", name="Scout-RealmA", fetch_status="pending")
    member = _member(
        applicant_id="scout-realma",
        name="Scout-RealmA",
        fetch_status="pending",
    )
    state.add_or_update(applicant)
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    try:
        window._launch_fetch(applicant)
        assert len(queued_pool.tasks) == 1
        original_identity = queued_pool.tasks[0]._identity
        window._cache.put(
            "Scout",
            original_identity.server_slug,
            original_identity.region,
            original_identity.spec_id,
            _ranks(),
            original_identity.metric_role,
            original_identity.metric_preferences,
        )

        window._launch_fetch(member)
        assert applicant.fetch_status == "ready"
        assert member.fetch_status == "ready"
        assert applicant.mplus_dps == 77.0
        assert member.mplus_dps == 77.0

        window._on_fetch_done(
            original_identity,
            CharacterRanks.empty(
                error="WCL server error",
                error_kind=WCL_ERROR_SERVER,
            ),
        )

        assert applicant.fetch_status == "ready"
        assert applicant.error_message == ""
        assert applicant.wcl_error_kind == ""
        assert applicant.mplus_dps == 77.0
        assert applicant.raid_heroic == 22.0
        assert member.fetch_status == "ready"
        assert member.mplus_dps == 77.0
        assert window._fetches_in_flight == {}
        assert window._fetch_waiters_by_target == {}
    finally:
        client.close()


def test_cache_hit_after_party_fetch_ignores_late_original_not_found(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    applicant = _app(applicant_id="42:1", name="Scout-RealmA", fetch_status="pending")
    member = _member(
        applicant_id="scout-realma",
        name="Scout-RealmA",
        fetch_status="pending",
    )
    state.add_or_update(applicant)
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    try:
        window._launch_fetch(member)
        assert len(queued_pool.tasks) == 1
        original_identity = queued_pool.tasks[0]._identity
        window._cache.put(
            "Scout",
            original_identity.server_slug,
            original_identity.region,
            original_identity.spec_id,
            _ranks(),
            original_identity.metric_role,
            original_identity.metric_preferences,
        )

        window._launch_fetch(applicant)
        assert applicant.fetch_status == "ready"
        assert member.fetch_status == "ready"
        assert applicant.mplus_dps == 77.0
        assert member.mplus_dps == 77.0

        window._on_fetch_done(original_identity, CharacterRanks.empty(not_found=True))

        assert member.fetch_status == "ready"
        assert member.error_message == ""
        assert member.wcl_error_kind == ""
        assert member.mplus_dps == 77.0
        assert member.raid_heroic == 22.0
        assert applicant.fetch_status == "ready"
        assert applicant.mplus_dps == 77.0
        assert window._fetches_in_flight == {}
        assert window._fetch_waiters_by_target == {}
    finally:
        client.close()


def test_disabling_metrics_clears_coalesced_fetch_waiters(qtbot, tmp_path):
    disabled = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    applicant = _app(applicant_id="42:1", name="Scout-RealmA", fetch_status="pending")
    member = _member(
        applicant_id="scout-realma",
        name="Scout-RealmA",
        fetch_status="pending",
    )
    state.add_or_update(applicant)
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)
    queued_pool = _QueuedPool()
    window._pool = queued_pool

    try:
        window._launch_fetch(applicant)
        window._launch_fetch(member)
        assert len(queued_pool.tasks) == 1
        assert window._fetch_waiters_by_target

        window.apply_metric_preferences(disabled)

        assert window._fetches_in_flight == {}
        assert window._fetch_waiters_by_target == {}
        window._on_fetch_done(
            queued_pool.tasks[0]._identity,
            CharacterRanks.empty(error="WCL server error", error_kind=WCL_ERROR_SERVER),
        )
        assert applicant.fetch_status == "ready"
        assert member.fetch_status == "ready"
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
    network_emitted: list[CharacterRanks] = []
    task.signals.done.connect(lambda _identity, ranks: emitted.append(ranks))
    task.signals.networkDone.connect(
        lambda _identity, ranks: network_emitted.append(ranks)
    )

    task.run()

    assert client.fetch_called is True
    assert emitted[-1].raid_heroic == 44.0
    assert emitted[-1].mplus_dps == 88.0
    assert network_emitted == emitted
    assert cache.put_expected_generation == 0


def test_fetch_task_cache_hit_does_not_claim_live_api_success():
    identity = _FetchIdentity(
        applicant_id="42:1",
        charname_key="scout",
        server_slug="realma",
        region="EU",
        spec_id=71,
        metric_role="DPS",
        metric_preferences=ALL_METRIC_PREFERENCES,
    )
    cache = _UiThreadCacheProbe()
    cache.result = _ranks()
    task = _FetchTask(
        identity,
        "Scout",
        _FreshFetchClient(),
        cache,
    )  # type: ignore[arg-type]
    done: list[CharacterRanks] = []
    network_done: list[CharacterRanks] = []
    task.signals.done.connect(lambda _identity, ranks: done.append(ranks))
    task.signals.networkDone.connect(
        lambda _identity, ranks: network_done.append(ranks)
    )

    task.run()

    assert len(done) == 1
    assert network_done == []


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
    malformed = _app(
        applicant_id="malformed:1",
        fetch_status="error",
        error_message="Malformed WCL response",
        wcl_error_kind=wcl_mod.WCL_ERROR_MALFORMED,
    )
    graphql = _app(
        applicant_id="graphql:1",
        fetch_status="error",
        error_message="GraphQL error",
        wcl_error_kind=wcl_mod.WCL_ERROR_GRAPHQL,
    )
    not_found = _app(applicant_id="nf:1", fetch_status="not_found")
    state.add_or_update(missing)
    state.add_or_update(auth)
    state.add_or_update(http)
    state.add_or_update(malformed)
    state.add_or_update(graphql)
    state.add_or_update(not_found)

    try:
        assert window._retry_failed_wcl_fetches() == 0
        assert missing.fetch_status == "error"
        assert auth.fetch_status == "error"
        assert http.fetch_status == "error"
        assert malformed.fetch_status == "error"
        assert graphql.fetch_status == "error"
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


def test_manual_wcl_retry_relaunches_visible_malformed_error_only(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    visible = _app(
        fetch_status="error",
        error_message="Malformed WCL response: data is not an object",
        wcl_error_kind=wcl_mod.WCL_ERROR_MALFORMED,
    )
    other = _app(
        applicant_id="43:1",
        name="Other-RealmA",
        fetch_status="error",
        error_message="Malformed WCL response: characterData is not an object",
        wcl_error_kind=wcl_mod.WCL_ERROR_MALFORMED,
    )
    state.add_or_update(visible)
    state.add_or_update(other)
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._refresh_table()
        window._pinned_id = visible.applicant_id
        window._pinned_by_tab["applicants"] = visible.applicant_id

        window._retry_visible_wcl_error()
        identity = window._current_fetch_identity_for(visible)

        assert identity is not None
        assert visible.fetch_status == "loading"
        assert visible.error_message == ""
        assert visible.wcl_error_kind == ""
        assert identity.storage_key in window._fetches_in_flight
        assert other.fetch_status == "error"
        assert other.wcl_error_kind == wcl_mod.WCL_ERROR_MALFORMED
    finally:
        client.close()


def test_manual_wcl_retry_uses_party_storage_key_for_visible_graphql_error(
    qtbot, tmp_path
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    member = _member(
        fetch_status="error",
        error_message="GraphQL error: proxy exploded",
        wcl_error_kind=wcl_mod.WCL_ERROR_GRAPHQL,
    )
    state.add_or_update_party_member(member)
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._active_tab = "party"
        window._tab_bar.set_active("party", emit=False)
        window._refresh_table()
        window._pinned_id = member.applicant_id
        window._pinned_by_tab["party"] = member.applicant_id

        window._retry_visible_wcl_error()
        identity = window._current_fetch_identity_for(member)

        assert identity is not None
        assert identity.row_source == "party"
        assert identity.storage_key == f"party:{member.applicant_id}"
        assert member.fetch_status == "loading"
        assert identity.storage_key in window._fetches_in_flight
    finally:
        client.close()


def test_manual_wcl_retry_skips_non_manual_and_inflight_errors(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(
        fetch_status="error",
        error_message="Authentication failed",
        wcl_error_kind=WCL_ERROR_AUTH,
    )
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)

    try:
        window._refresh_table()
        window._pinned_id = app.applicant_id
        window._pinned_by_tab["applicants"] = app.applicant_id

        window._retry_visible_wcl_error()

        assert app.fetch_status == "error"
        assert window._fetches_in_flight == {}

        app.wcl_error_kind = wcl_mod.WCL_ERROR_GRAPHQL
        identity = window._current_fetch_identity_for(app)
        assert identity is not None
        window._mark_fetch_in_flight(identity)

        window._retry_visible_wcl_error()

        assert app.fetch_status == "error"
        assert len(window._fetches_in_flight) == 1
    finally:
        client.close()


def test_manual_wcl_retry_hidden_when_all_wcl_metrics_are_disabled(qtbot, tmp_path):
    disabled = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    app = _app(
        fetch_status="error",
        error_message="GraphQL error: proxy exploded",
        wcl_error_kind=wcl_mod.WCL_ERROR_GRAPHQL,
    )
    state.add_or_update(app)
    window, client = _window(
        qtbot,
        tmp_path,
        state,
        metric_preferences=disabled,
    )

    try:
        window._refresh_table()
        window._pinned_id = app.applicant_id
        window._pinned_by_tab["applicants"] = app.applicant_id
        window._sync_delegate_and_panel()

        assert window._panel._wcl_retry_button.isHidden()

        window._retry_visible_wcl_error()

        assert app.fetch_status == "error"
        assert app.wcl_error_kind == wcl_mod.WCL_ERROR_GRAPHQL
        assert window._fetches_in_flight == {}
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


def test_launch_fetch_unmapped_positive_spec_mplus_only_marks_ready_without_queue(
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
    app = _app(spec_id=999999, fetch_status="pending")
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
        assert narrow_identity.network_key not in window._fetch_waiters_by_target
        assert broad_identity.network_key in window._fetch_waiters_by_target
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
        window._launch_fetch(member)
        old_identity = window._fetches_in_flight.get(f"party:{member.applicant_id}")
        assert old_identity is not None
        assert old_identity.network_key in window._fetch_waiters_by_target

        window.bump_wcl_runtime_generation()
        identity = window._current_fetch_identity_for(member)

        assert identity is not None
        assert member.fetch_status == "loading"
        assert member.raid_heroic is None
        assert member.mplus_dps is None
        assert identity.row_source == "party"
        assert identity.runtime_generation == 1
        assert identity.storage_key in window._fetches_in_flight
        assert old_identity.network_key not in window._fetch_waiters_by_target
        assert identity.network_key in window._fetch_waiters_by_target
    finally:
        client.close()


def test_stale_network_completion_cannot_overwrite_current_auth_status(
    qtbot, tmp_path
):
    window, client = _window(qtbot, tmp_path, AppState())
    stale_identity = _FetchIdentity(
        applicant_id="42:1",
        charname_key="scout",
        server_slug="realma",
        region="EU",
        spec_id=71,
        metric_role="DPS",
        runtime_generation=0,
        metric_preferences=ALL_METRIC_PREFERENCES,
    )

    try:
        window._record_fetch_connection_status(
            stale_identity,
            CharacterRanks.empty(
                error="offline detail",
                error_kind=WCL_ERROR_NETWORK,
            ),
        )
        assert client.connection_status.error_kind == WCL_ERROR_NETWORK

        window.bump_wcl_runtime_generation()
        client.reconfigure_auth(WCLAuth("new", "new-secret", tmp_path), validated=True)
        window._record_fetch_connection_status(stale_identity, _ranks())

        assert client.connection_status.state == "oauth_ready"

        current_identity = _FetchIdentity(
            applicant_id=stale_identity.applicant_id,
            charname_key=stale_identity.charname_key,
            server_slug=stale_identity.server_slug,
            region=stale_identity.region,
            spec_id=stale_identity.spec_id,
            metric_role=stale_identity.metric_role,
            runtime_generation=1,
            metric_preferences=ALL_METRIC_PREFERENCES,
        )
        window._record_fetch_connection_status(current_identity, _ranks())
        assert client.connection_status.state == "api_ready"
    finally:
        client.close()


def test_network_character_not_found_is_successful_api_status(qtbot, tmp_path):
    window, client = _window(qtbot, tmp_path, AppState())
    identity = _FetchIdentity(
        applicant_id="42:1",
        charname_key="missing",
        server_slug="realma",
        region="EU",
        spec_id=71,
        metric_role="DPS",
        metric_preferences=ALL_METRIC_PREFERENCES,
    )

    try:
        window._record_fetch_connection_status(
            identity,
            CharacterRanks.empty(
                error="Could not find character",
                not_found=True,
            ),
        )

        assert client.connection_status.state == "api_ready"
        assert client.connection_status.error_kind == ""
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


def test_lfg_unavailable_roster_snapshot_does_not_bump_generation_or_drop_fetch(
    qtbot,
    tmp_path,
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
        applicants=[
            DecodedApplicant(
                applicant_id=42,
                member_idx=1,
                class_id=1,
                spec_id=71,
                ilvl=480,
                score=2400,
                main_score=0,
                role=2,
                name="Scout-RealmA",
                rio_dungeons=[],
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
        applicants=[],
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
        lfg_unavailable=True,
    )

    try:
        machine.apply_snapshot(listed)
        assert window._listing_session_generation == 0
        assert len(queued_pool.tasks) == 1
        applicant_identity = queued_pool.tasks[0]._identity

        machine.apply_snapshot(roster_only)

        assert window._listing_session_generation == 0
        assert state.listing is not None
        assert set(state.applicants) == {"42:1"}
        assert applicant_identity.storage_key in window._fetches_in_flight

        window._on_fetch_done(applicant_identity, _ranks())

        assert state.applicants["42:1"].fetch_status == "ready"
        assert state.applicants["42:1"].mplus_dps == 77.0
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
