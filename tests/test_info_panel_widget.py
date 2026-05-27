"""Widget tests for the compact applicant scout-card panel."""

from __future__ import annotations

import json
import time
from dataclasses import replace

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QStyleOptionViewItem,
    QTableWidget,
    QWidget,
)

import applicant_scout.overlay as overlay_mod
from applicant_scout.constants import percentile_colour
from applicant_scout.overlay import (
    COL_H,
    COL_M,
    COL_MPLUS,
    COL_N,
    DUNGEON_KEY_WIDTH,
    DUNGEON_METRIC_WIDTH,
    DUNGEON_NAME_WIDTH,
    DUNGEON_WCL_KEY_WIDTH,
    GAME_FOREGROUND_POLL_MS,
    INFO_PANEL_PREFERRED_HEIGHT,
    LAUNCHER_SIZE,
    MPLUS_INDIVIDUAL_BG_ROLE,
    MPLUS_INDIVIDUAL_TEXT_ROLE,
    MPLUS_PACKAGE_BG_ROLE,
    MPLUS_PACKAGE_TEXT_ROLE,
    RAID_KILL_WIDTH,
    RAID_METRIC_WIDTH,
    RAID_NAME_WIDTH,
    RAID_SINGLE_KILL_WIDTH,
    RAID_SINGLE_METRIC_WIDTH,
    ApplicantInfoPanel,
    OverlayWindow,
    _HoverHighlightDelegate,
    _mplus_group_cell,
)
from applicant_scout.metric_preferences import (
    DEFAULT_METRIC_PREFERENCES,
    MetricPreferences,
)
from applicant_scout.scoring import PackageFit, package_fit
from applicant_scout.state import (
    DEFAULT_WINDOW_WIDTH,
    WINDOW_GEOMETRY_LAYOUT_VERSION,
    AppState,
    Applicant,
    Listing,
    RosterMember,
)
from applicant_scout.wcl import CharacterCache, WCLAuth, WCLClient


def _app(**overrides) -> Applicant:
    base = Applicant(
        applicant_id="42",
        name="Drathmork-Twisting Nether",
        cls="WARRIOR",
        spec_id=71,
        ilvl=264,
        score=2443,
        role="DAMAGER",
        raid_normal=88.0,
        raid_normal_median=72.0,
        raid_heroic=91.0,
        raid_heroic_median=78.0,
        raid_mythic=34.0,
        raid_mythic_median=34.0,
        mplus_dps=80.0,
        mplus_dps_median=62.0,
        mplus_hps=None,
        mplus_hps_median=None,
        mplus_dps_breakdown=[
            {
                "name": "Pit of Saron",
                "parse_percent": 100.0,
                "median_percent": 80.0,
                "key_level": 14,
                "run_count": 3,
            },
            {
                "name": "Skyreach",
                "parse_percent": 70.0,
                "median_percent": None,
                "key_level": 12,
                "run_count": 1,
            },
        ],
        mplus_hps_breakdown=[],
        fetch_status="ready",
    )
    return replace(base, **overrides)


def _listing() -> Listing:
    return Listing(
        activity_id=401,
        dungeon_name="Skyreach",
        listing_name="+16 Skyreach",
        comment="",
        key_level=16,
        category_id=2,
        difficulty_id=8,
    )


def _raid_listing(difficulty_id: int = 16) -> Listing:
    return Listing(
        activity_id=0,
        dungeon_name="",
        listing_name="Mythic Raid",
        comment="",
        key_level=0,
        category_id=3,
        difficulty_id=difficulty_id,
    )


def _member(applicant_id: str = "host-realm", name: str = "Host-Realm") -> RosterMember:
    return RosterMember(
        applicant_id=applicant_id,
        name=name,
        cls="WARRIOR",
        spec_id=71,
        ilvl=701,
        score=2443,
        role="TANK",
        fetch_status="ready",
    )


def test_panel_reuses_child_labels_across_hover_updates(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)

    before = len(panel.findChildren(QLabel))
    panel.setApplicantData(_app(name="Drathmork-Twisting Nether"))
    panel.setApplicantData(_app(name="Second-Realm", cls="MAGE", spec_id=63))
    after = len(panel.findChildren(QLabel))

    assert after == before
    assert panel._name_label.text() == "Second"


def test_ready_panel_renders_identity_metrics_and_dungeons(qtbot):
    panel = ApplicantInfoPanel(None, MetricPreferences())
    qtbot.addWidget(panel)

    panel.setApplicantData(_app())

    assert panel._name_label.text() == "Drathmork"
    assert panel._realm_label.text() == "Twisting Nether"
    assert panel._spec_label.text() == "Arms"
    assert "DPS" in panel._role_label.text()
    assert panel._ilvl_label.text() == "ilvl 264"
    assert panel._rio_label.text() == "RIO 2443"
    assert panel._metric_labels["N"].text() == "N 88/72"
    assert percentile_colour(88.0) in panel._metric_labels["N"].styleSheet()
    assert panel._metric_labels["M+"].text() == "M+ DPS 80/62 +14"

    name_label, key_label, wcl_key_label, value_label = panel._dungeon_rows[0]
    assert name_label.text() == "Pit of Saron"
    assert key_label.text() == ""
    assert wcl_key_label.text() == "WCL +14"
    assert value_label.text() == "100/80"
    assert name_label.width() == DUNGEON_NAME_WIDTH
    assert key_label.width() == DUNGEON_KEY_WIDTH
    assert wcl_key_label.width() == DUNGEON_WCL_KEY_WIDTH
    assert value_label.width() == DUNGEON_METRIC_WIDTH


def test_raid_listing_panel_shows_disabled_target_badge_with_estimated_fit(qtbot):
    panel = ApplicantInfoPanel(
        None,
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=True,
        ),
    )
    qtbot.addWidget(panel)

    panel.setApplicantData(
        _app(
            raid_normal=None,
            raid_normal_median=None,
            raid_heroic=None,
            raid_heroic_median=None,
            raid_mythic=70.0,
            raid_mythic_median=60.0,
        ),
        _raid_listing(difficulty_id=15),
    )

    assert panel._metric_labels["N"].isHidden()
    assert not panel._metric_labels["H"].isHidden()
    assert panel._metric_labels["H"].text().startswith("H EST ")
    assert "M 70/60" in panel._metric_labels["H"].text()
    assert not panel._metric_labels["M"].isHidden()
    assert panel._status_label.text() != "No Warcraft Logs data"


def test_raid_listing_panel_defaults_to_raid_boss_rows(qtbot):
    panel = ApplicantInfoPanel(
        None,
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=True,
        ),
    )
    qtbot.addWidget(panel)

    panel.setApplicantData(
        _app(
            rio_raid_progress={
                "M": {
                    "killed": 1,
                    "total": 9,
                    "boss_kills": [2, 0, 0, 0, 0, 0, 0, 0, 0],
                }
            },
            raid_boss_parses={
                "M": [
                    {
                        "encounter_id": 3176,
                        "name": "Imperator Averzian",
                        "overall": 46.0,
                        "ilvl": 68.0,
                    }
                ]
            },
        ),
        _raid_listing(),
    )

    assert panel._detail_mode == "raid"
    name_label, rio_label, wcl_key_label, value_label = panel._dungeon_rows[0]
    assert name_label.text() == "Imperator Averzian"
    assert rio_label.text() == "M2"
    assert "#ffe36a" in rio_label.styleSheet()
    assert wcl_key_label.text() == ""
    assert value_label.text() == "M 46-68"
    assert name_label.width() == RAID_NAME_WIDTH
    assert rio_label.width() == RAID_SINGLE_KILL_WIDTH
    assert wcl_key_label.width() == 0
    assert value_label.width() == RAID_SINGLE_METRIC_WIDTH
    assert value_label.width() <= 80
    assert value_label.alignment() & Qt.AlignmentFlag.AlignHCenter


def test_raid_panel_combines_enabled_difficulties_without_selector(qtbot):
    panel = ApplicantInfoPanel(
        None,
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=True,
        ),
    )
    qtbot.addWidget(panel)

    panel.setApplicantData(
        _app(
            rio_raid_progress={
                "M": {
                    "killed": 1,
                    "total": 9,
                    "boss_kills": [2, 0, 0, 0, 0, 0, 0, 0, 0],
                },
                "H": {
                    "killed": 1,
                    "total": 9,
                    "boss_kills": [1, 0, 0, 0, 0, 0, 0, 0, 0],
                },
            },
            raid_boss_parses={
                "M": [
                    {
                        "encounter_id": 3176,
                        "name": "Imperator Averzian",
                        "overall": 46.0,
                        "ilvl": 68.0,
                    }
                ],
                "H": [
                    {
                        "encounter_id": 3176,
                        "name": "Imperator Averzian",
                        "overall": 83.0,
                        "ilvl": 63.0,
                    }
                ],
            },
        ),
        _raid_listing(),
    )

    assert panel._detail_mode == "raid"
    assert panel._dungeon_rows[0][0].text() == "Imperator Averzian"
    assert panel._dungeon_rows[0][1].text() == "H1 · M2"
    assert panel._dungeon_rows[0][2].text() == ""
    assert panel._dungeon_rows[0][3].text() == "H 83-63 · M 46-68"
    assert panel._dungeon_rows[0][1].width() == RAID_KILL_WIDTH
    assert panel._dungeon_rows[0][3].width() == RAID_METRIC_WIDTH
    assert panel._dungeon_rows[1][0].text() == "Vorasius"
    assert panel._dungeon_rows[1][1].text() == ""
    assert panel._visible_detail_rows == 9
    assert panel.target_height() == INFO_PANEL_PREFERRED_HEIGHT


def test_raid_listing_panel_remembers_manual_mplus_detail_tab(qtbot):
    panel = ApplicantInfoPanel(None, MetricPreferences())
    qtbot.addWidget(panel)
    app = _app()
    listing = _raid_listing()

    panel.setApplicantData(app, listing)
    panel._on_detail_mode_clicked("mplus")
    panel.setApplicantData(app, listing)

    assert panel._detail_mode == "mplus"
    assert panel._dungeon_rows[0][0].text() == "Pit of Saron"


def test_mplus_detail_widths_restore_after_raid_detail(qtbot):
    panel = ApplicantInfoPanel(None, MetricPreferences())
    qtbot.addWidget(panel)
    app = _app(
        rio_raid_progress={
            "N": {"boss_kills": [5, 0, 0, 0, 0, 0, 0, 0, 0]},
            "H": {"boss_kills": [3, 0, 0, 0, 0, 0, 0, 0, 0]},
            "M": {"boss_kills": [1, 0, 0, 0, 0, 0, 2, 0, 0]},
        },
        raid_boss_parses={
            "N": [{"encounter_id": 3176, "overall": 91.0, "ilvl": 78.0}],
            "H": [{"encounter_id": 3176, "overall": 83.0, "ilvl": 63.0}],
            "M": [{"encounter_id": 3176, "overall": 46.0, "ilvl": 68.0}],
        },
    )

    panel.setApplicantData(app, _raid_listing())
    assert panel._dungeon_rows[0][1].text() == "N5 · H3 · M1"
    assert panel._dungeon_rows[0][3].text() == "N 91-78 · H 83-63 · M 46-68"
    assert panel._dungeon_rows[0][3].width() > DUNGEON_METRIC_WIDTH

    panel._on_detail_mode_clicked("mplus")

    assert panel._dungeon_rows[0][0].text() == "Pit of Saron"
    assert panel._dungeon_rows[0][2].text() == "WCL +14"
    assert panel._dungeon_rows[0][3].text() == "100/80"
    assert panel._dungeon_rows[0][1].width() == DUNGEON_KEY_WIDTH
    assert panel._dungeon_rows[0][3].width() == DUNGEON_METRIC_WIDTH


def test_overlay_queues_raid_boss_fetch_when_panel_switches_back_to_raid(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={"M": []})
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        window._raid_boss_fetches_in_flight.clear()

        window._panel._on_detail_mode_clicked("mplus")
        assert window._raid_boss_fetches_in_flight == {}

        app.raid_boss_parses = {}
        window._panel._on_detail_mode_clicked("raid")

        assert window._raid_boss_fetches_in_flight
    finally:
        window.close()


def test_raid_boss_fetch_done_preserves_existing_mplus_data(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest")
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        base_identity = window._current_fetch_identity_for(app)
        assert base_identity is not None
        detail_identity = replace(
            base_identity,
            metric_preferences=window._raid_detail_preferences(),
        )

        window._on_raid_boss_fetch_done(
            detail_identity,
            {
                "M": [
                    {
                        "encounter_id": 3176,
                        "name": "Imperator Averzian",
                        "overall": 46.0,
                        "ilvl": 68.0,
                    }
                ]
            },
            "",
        )

        assert app.raid_boss_parses["M"][0]["overall"] == 46.0
        assert app.mplus_dps == 80.0
        assert app.mplus_dps_median == 62.0
        assert app.mplus_dps_breakdown
    finally:
        window.close()


def test_stale_raid_boss_fetch_does_not_apply_after_difficulty_change(
    qtbot, tmp_path
):
    initial_prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    updated_prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=initial_prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(
        state, client, cache, tmp_path, metric_preferences=initial_prefs
    )
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        base_identity = window._current_fetch_identity_for(app)
        assert base_identity is not None
        stale_detail_identity = replace(
            base_identity,
            metric_preferences=window._raid_detail_preferences(),
        )

        window.apply_metric_preferences(updated_prefs, refetch_missing=False)
        window._raid_boss_fetches_in_flight.clear()

        window._on_raid_boss_fetch_done(
            stale_detail_identity,
            {
                "M": [
                    {
                        "encounter_id": 3176,
                        "name": "Imperator Averzian",
                        "overall": 46.0,
                        "ilvl": 68.0,
                    }
                ]
            },
            "",
        )

        assert app.raid_boss_parses == {}
        assert window._raid_boss_fetches_in_flight
        queued = next(iter(window._raid_boss_fetches_in_flight.values()))
        assert queued.metric_preferences.raid_heroic
        assert queued.metric_preferences.raid_mythic
    finally:
        window.close()


def test_raid_boss_fetch_failure_is_scoped_to_metric_preferences(qtbot, tmp_path):
    initial_prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    updated_prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=initial_prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(
        state, client, cache, tmp_path, metric_preferences=initial_prefs
    )
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        base_identity = window._current_fetch_identity_for(app)
        assert base_identity is not None
        failed_detail_identity = replace(
            base_identity,
            metric_preferences=window._raid_detail_preferences(),
        )

        window._on_raid_boss_fetch_done(failed_detail_identity, {}, "quota")
        window._launch_raid_boss_fetch_if_needed(app)
        assert window._raid_boss_fetches_in_flight == {}

        window.apply_metric_preferences(updated_prefs, refetch_missing=False)

        assert window._raid_boss_fetches_in_flight
        queued = next(iter(window._raid_boss_fetches_in_flight.values()))
        assert queued.metric_preferences.raid_heroic
        assert queued.metric_preferences.raid_mythic
    finally:
        window.close()


def test_raid_boss_fetch_failure_is_scoped_to_character_identity(qtbot, tmp_path):
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        base_identity = window._current_fetch_identity_for(app)
        assert base_identity is not None
        failed_detail_identity = replace(
            base_identity,
            metric_preferences=window._raid_detail_preferences(),
        )

        window._on_raid_boss_fetch_done(failed_detail_identity, {}, "quota")
        window._launch_raid_boss_fetch_if_needed(app)
        assert window._raid_boss_fetches_in_flight == {}

        app.name = "Other-Ravencrest"
        app.raid_boss_parses = {}
        window._launch_raid_boss_fetch_if_needed(app)

        assert window._raid_boss_fetches_in_flight
        queued = next(iter(window._raid_boss_fetches_in_flight.values()))
        assert queued.charname_key == "other"
    finally:
        window.close()


def test_retryable_raid_boss_fetch_failure_expires(qtbot, tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: now[0])
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        base_identity = window._current_fetch_identity_for(app)
        assert base_identity is not None
        failed_detail_identity = replace(
            base_identity,
            metric_preferences=window._raid_detail_preferences(),
        )

        window._on_raid_boss_fetch_done(
            failed_detail_identity,
            {},
            "network",
            overlay_mod.WCL_ERROR_NETWORK,
        )
        window._launch_raid_boss_fetch_if_needed(app)
        assert window._raid_boss_fetches_in_flight == {}

        now[0] += 2.0
        window._launch_raid_boss_fetch_if_needed(app)

        assert window._raid_boss_fetches_in_flight
    finally:
        window.close()


def test_raid_detail_loading_status_shows_without_changing_fetch_status(
    qtbot, tmp_path
):
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()

        assert window._raid_boss_fetches_in_flight
        assert window._panel._status_label.text() == "Fetching raid boss details…"
        assert app.fetch_status == "ready"
    finally:
        window.close()


def test_raid_detail_retryable_failure_replaces_loading_status_after_done(
    qtbot, tmp_path, monkeypatch
):
    now = [100.0]
    monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: now[0])
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        failed_detail_identity = next(iter(window._raid_boss_fetches_in_flight.values()))

        window._on_raid_boss_fetch_done(
            failed_detail_identity,
            {},
            "network",
            overlay_mod.WCL_ERROR_NETWORK,
        )

        assert window._panel._status_label.text() == "Raid boss details retrying soon…"
        assert app.fetch_status == "ready"
        assert not hasattr(app, "raid_detail_status")
    finally:
        window.close()


def test_raid_detail_permanent_failure_shows_unavailable_without_requeue(
    qtbot, tmp_path
):
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        failed_detail_identity = next(iter(window._raid_boss_fetches_in_flight.values()))

        window._on_raid_boss_fetch_done(
            failed_detail_identity,
            {},
            "auth",
            overlay_mod.WCL_ERROR_AUTH,
        )
        window._launch_raid_boss_fetch_if_needed(app)

        assert window._raid_boss_fetches_in_flight == {}
        assert window._panel._status_label.text() == "Raid boss details unavailable"
        assert app.fetch_status == "ready"
    finally:
        window.close()


def test_raid_detail_status_restored_when_returning_to_raid_tab_with_inflight_fetch(
    qtbot, tmp_path
):
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        assert window._panel._status_label.text() == "Fetching raid boss details…"

        window._panel._on_detail_mode_clicked("mplus")
        assert window._panel._active_detail_mode() == "mplus"

        window._panel._on_detail_mode_clicked("raid")

        assert window._panel._status_label.text() == "Fetching raid boss details…"
        assert app.fetch_status == "ready"
    finally:
        window.close()


def test_retryable_raid_detail_failure_retries_after_idle_expiry(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr(overlay_mod, "WCL_RETRY_BATCH_DELAY_MS", 50)
    monkeypatch.setattr(overlay_mod, "WCL_RETRY_CUSHION_MS", 0)
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        failed_detail_identity = next(iter(window._raid_boss_fetches_in_flight.values()))

        window._on_raid_boss_fetch_done(
            failed_detail_identity,
            {},
            "network",
            overlay_mod.WCL_ERROR_NETWORK,
        )
        assert window._raid_boss_fetches_in_flight == {}
        assert window._raid_boss_fetch_failures

        for key in list(window._raid_boss_fetch_failures):
            window._raid_boss_fetch_failures[key] = time.monotonic() - 1.0
        window._raid_boss_retry_timer.stop()
        window._retry_ready_raid_boss_fetches()

        assert window._raid_boss_fetches_in_flight
        assert window._panel._status_label.text() == "Fetching raid boss details…"
        assert app.fetch_status == "ready"
    finally:
        window.close()


def test_raid_detail_retry_status_restored_when_returning_to_raid_tab(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr(overlay_mod, "WCL_RETRY_BATCH_DELAY_MS", 500)
    monkeypatch.setattr(overlay_mod, "WCL_RETRY_CUSHION_MS", 0)
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _raid_listing()
    app = _app(name="Scout-Ravencrest", raid_boss_parses={})
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._pool = None
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()
        failed_detail_identity = next(iter(window._raid_boss_fetches_in_flight.values()))
        window._on_raid_boss_fetch_done(
            failed_detail_identity,
            {},
            "network",
            overlay_mod.WCL_ERROR_NETWORK,
        )
        assert window._panel._status_label.text() == "Raid boss details retrying soon…"

        window._panel._on_detail_mode_clicked("mplus")
        assert window._panel._active_detail_mode() == "mplus"

        window._panel._on_detail_mode_clicked("raid")

        assert window._panel._status_label.text() == "Raid boss details retrying soon…"
    finally:
        window.close()


def test_panel_renders_current_and_better_main_score(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)

    panel.setApplicantData(_app(main_score=3468))

    assert panel._rio_label.text() == "RIO 2443 · main 3468"


def test_context_dungeon_rows_colour_the_printed_percentile(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    listing = replace(_listing(), key_level=17, listing_name="+17 Skyreach")
    app = _app(
        mplus_dps_breakdown=[
            {
                "name": "Skyreach",
                "parse_percent": 83.0,
                "median_percent": 83.0,
                "key_level": 16,
                "run_count": 1,
            }
        ],
    )

    panel.setApplicantData(app, listing)

    _name_label, _rio_label, wcl_key_label, value_label = panel._dungeon_rows[0]
    assert wcl_key_label.text() == "WCL +16"
    assert value_label.text() == "83 N=1"
    assert percentile_colour(83.0) in value_label.styleSheet()


def test_panel_renders_rio_and_wcl_dungeon_rows_side_by_side(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    app = _app(
        rio_profile=True,
        rio_dungeons=[
            {"name": "Skyreach", "key_level": 15},
            {"name": "Pit of Saron", "key_level": 16},
        ],
        mplus_dps_breakdown=[
            {
                "name": "Skyreach",
                "parse_percent": 42.0,
                "median_percent": 38.0,
                "key_level": 12,
                "run_count": 2,
            },
            {
                "name": "Pit of Saron",
                "parse_percent": 71.0,
                "median_percent": 62.0,
                "key_level": 14,
                "run_count": 2,
            },
        ],
    )

    panel.setApplicantData(app, _listing())

    name_label, rio_label, wcl_key_label, wcl_label = panel._dungeon_rows[0]
    assert name_label.text() == "Skyreach"
    assert rio_label.text() == "RIO +15"
    assert wcl_key_label.text() == "WCL +12"
    assert wcl_label.text() == "42/38"

    name_label, rio_label, wcl_key_label, wcl_label = panel._dungeon_rows[1]
    assert name_label.text() == "Pit of Saron"
    assert rio_label.text() == "RIO +16"
    assert wcl_key_label.text() == "WCL +14"
    assert wcl_label.text() == "71/62"


def test_panel_prioritises_target_dungeon_by_activity_id_when_listing_name_is_localized(
    qtbot,
):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    listing = replace(
        _listing(),
        activity_id=404,
        dungeon_name="Небесный Путь",
    )
    app = _app(
        rio_profile=True,
        rio_dungeons=[
            {"name": "Skyreach", "key_level": 15},
            {"name": "Pit of Saron", "key_level": 16},
        ],
        mplus_dps_breakdown=[
            {
                "name": "Skyreach",
                "parse_percent": 42.0,
                "median_percent": 38.0,
                "key_level": 12,
                "run_count": 2,
            },
            {
                "name": "Pit of Saron",
                "parse_percent": 71.0,
                "median_percent": 62.0,
                "key_level": 14,
                "run_count": 2,
            },
        ],
    )

    panel.setApplicantData(app, listing)

    name_label, rio_label, wcl_key_label, wcl_label = panel._dungeon_rows[0]
    assert name_label.text() == "Skyreach"
    assert rio_label.text() == "RIO +15"
    assert wcl_key_label.text() == "WCL +12"
    assert wcl_label.text() == "42/38"


def test_panel_merges_localized_rio_row_with_wcl_activity_id_mapping(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    listing = replace(
        _listing(),
        activity_id=404,
        dungeon_name="Небесный Путь",
    )
    app = _app(
        rio_profile=True,
        rio_dungeons=[{"name": "Небесный Путь", "key_level": 15}],
        mplus_dps_breakdown=[
            {
                "name": "Skyreach",
                "parse_percent": 42.0,
                "median_percent": 38.0,
                "key_level": 12,
                "run_count": 2,
            }
        ],
    )

    panel.setApplicantData(app, listing)

    name_label, rio_label, wcl_key_label, wcl_label = panel._dungeon_rows[0]
    assert name_label.text() == "Skyreach"
    assert rio_label.text() == "RIO +15"
    assert wcl_key_label.text() == "WCL +12"
    assert wcl_label.text() == "42/38"
    assert panel._dungeon_rows[1][0].isHidden()


def test_panel_renders_rio_dungeon_rows_when_wcl_has_no_logs(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    app = _app(
        fetch_status="not_found",
        mplus_dps=None,
        mplus_dps_median=None,
        mplus_dps_breakdown=[],
        rio_profile=True,
        rio_dungeons=[
            {"name": "Skyreach", "key_level": 15},
            {"name": "Pit of Saron", "key_level": 16},
        ],
    )

    panel.setApplicantData(app, _listing())

    assert panel._status_label.text() == "Not found on Warcraft Logs · RaiderIO only"
    name_label, rio_label, wcl_key_label, wcl_label = panel._dungeon_rows[0]
    assert name_label.text() == "Skyreach"
    assert rio_label.text() == "RIO +15"
    assert wcl_key_label.text() == ""
    assert wcl_label.text() == ""


def test_panel_renders_rio_fit_badge_when_wcl_has_no_logs(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    listing = _listing()
    app = _app(
        fetch_status="not_found",
        mplus_dps=None,
        mplus_dps_median=None,
        mplus_dps_breakdown=[],
        rio_profile=True,
        rio_best_key=17,
        rio_best_dungeon_key=15,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=listing.key_level,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
    )

    panel.setApplicantData(app, listing)

    assert "Not found on Warcraft Logs" in panel._status_label.text()
    assert "RaiderIO only" in panel._status_label.text()
    assert panel._metric_labels["M+"].text().startswith("M+ Fit ")
    assert panel._metric_labels["M+"].text().endswith("+17")
    assert "DPS" not in panel._metric_labels["M+"].text()
    assert "RIO" not in panel._metric_labels["M+"].text()


def test_panel_explains_solo_mplus_fit_confidence_and_source(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    listing = _listing()
    app = _app(
        mplus_dps=None,
        mplus_dps_median=None,
        mplus_dps_breakdown=[],
        rio_profile=True,
        rio_best_key=16,
        rio_best_dungeon_key=16,
        rio_timed_at_or_above=8,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=listing.key_level,
    )

    panel.setApplicantData(app, listing)

    assert panel._metric_labels["M+"].text().startswith("M+ Fit ")
    assert "conf 75%" in panel._status_label.text()
    assert "cov 8/8" in panel._status_label.text()
    assert "RaiderIO only" in panel._status_label.text()


def test_panel_renders_group_package_line(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    package = PackageFit(
        score=73.0,
        label="FIT",
        display="G2 FIT 73",
        colour="#a335ee",
        size=2,
        confidence=0.68,
        high_score=91.0,
        average_score=74.0,
        low_score=52.0,
    )

    panel.setApplicantData(_app(), package=package)

    assert panel._package_label.text() == (
        "Group FIT 73 · high 91 · avg 74 · low 52 · conf 68%"
    )
    assert not panel._package_label.isHidden()


def test_panel_renders_real_mplus_package_without_blank_label(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    listing = _listing()
    skyreach_log = {
        "name": "Skyreach",
        "key_level": 16,
        "parse_percent": 82.0,
        "median_percent": 74.0,
        "run_count": 3,
    }
    follower_log = {
        "name": "Skyreach",
        "key_level": 15,
        "parse_percent": 72.0,
        "median_percent": 68.0,
        "run_count": 3,
    }
    leader = _app(
        applicant_id="10:1",
        mplus_dps_breakdown=[skyreach_log],
    )
    follower = _app(
        applicant_id="10:2",
        mplus_dps_breakdown=[follower_log],
    )
    package = package_fit([leader, follower], listing)

    panel.setApplicantData(follower, listing, package=package)

    assert panel._package_label.text().startswith("Group fit ")
    assert " · hi/avg/low " in panel._package_label.text()
    assert " · conf " in panel._package_label.text()
    assert " · this low" in panel._package_label.text()
    assert "Group  " not in panel._package_label.text()
    assert not panel._package_label.isHidden()


def test_panel_hides_group_package_line_for_solo(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)

    panel.setApplicantData(_app(), package=PackageFit(size=1, score=80.0))

    assert panel._package_label.isHidden()


def test_healer_panel_uses_hps_breakdown_and_ignores_dps(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    healer = _app(
        role="HEALER",
        mplus_dps=99.0,
        mplus_dps_median=88.0,
        mplus_hps=85.0,
        mplus_hps_median=70.0,
        mplus_dps_breakdown=[
            {
                "name": "Damage Dungeon",
                "parse_percent": 99.0,
                "median_percent": 88.0,
                "key_level": 20,
                "run_count": 2,
            }
        ],
        mplus_hps_breakdown=[
            {
                "name": "Healing Dungeon",
                "parse_percent": 85.0,
                "median_percent": 70.0,
                "key_level": 12,
                "run_count": 2,
            }
        ],
    )

    panel.setApplicantData(healer)

    assert panel._metric_labels["M+"].text() == "M+ HPS 85/70 +12"
    assert panel._dungeon_rows[0][0].text() == "Healing Dungeon"


def test_malformed_mplus_breakdown_renders_safe_fallbacks(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    app = _app(
        mplus_dps_breakdown=[
            {
                "name": "Valid",
                "parse_percent": "72.5",
                "median_percent": "60",
                "key_level": "12",
                "run_count": "2",
            },
            {
                "name": "Bad Cache",
                "parse_percent": "nope",
                "median_percent": "101",
                "key_level": "14.5",
                "run_count": "x",
            },
        ]
    )

    panel.setApplicantData(app)

    assert panel._dungeon_rows[0][0].text() == "Valid"
    assert panel._dungeon_rows[0][1].text() == ""
    assert panel._dungeon_rows[0][2].text() == "WCL +12"
    assert panel._dungeon_rows[0][3].text() == "72/60"
    assert panel._dungeon_rows[1][0].text() == "Bad Cache"
    assert panel._dungeon_rows[1][1].text() == ""
    assert panel._dungeon_rows[1][2].text() == ""
    assert panel._dungeon_rows[1][3].text() == ""


def test_status_states_hide_metrics_and_dungeons(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)

    panel.setApplicantData(_app(fetch_status="loading"))

    assert panel._status_label.text() == "Fetching from Warcraft Logs…"
    assert not panel._status_label.isHidden()
    assert all(label.isHidden() for label in panel._metric_labels.values())
    assert panel._dungeon_widget.isHidden()

    panel.setApplicantData(_app(fetch_status="error", error_message="bad token"))
    assert panel._status_label.text() == "WCL error: bad token"

    panel.setApplicantData(_app(fetch_status="not_found"))
    assert panel._status_label.text() == "Not found on Warcraft Logs"


def test_panel_explains_error_mplus_fit_uses_raiderio_only(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    listing = _listing()
    app = _app(
        fetch_status="error",
        error_message="bad token",
        mplus_dps=None,
        mplus_dps_median=None,
        mplus_dps_breakdown=[],
        rio_profile=True,
        rio_best_key=17,
        rio_best_dungeon_key=16,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=listing.key_level,
    )

    panel.setApplicantData(app, listing)

    assert panel._metric_labels["M+"].text().startswith("M+ Fit ")
    assert panel._status_label.text() == "WCL error: bad token · RaiderIO only"


def test_ready_no_data_shows_compact_status(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)

    panel.setApplicantData(
        _app(
            raid_normal=None,
            raid_normal_median=None,
            raid_heroic=None,
            raid_heroic_median=None,
            raid_mythic=None,
            raid_mythic_median=None,
            mplus_dps=None,
            mplus_dps_median=None,
            mplus_dps_breakdown=[],
        )
    )

    assert panel._status_label.text() == "No Warcraft Logs data"
    assert not panel._status_label.isHidden()
    assert panel._dungeon_widget.isHidden()


def test_panel_external_text_labels_use_plain_text(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)

    panel.setApplicantData(
        _app(
            name="<bad>-Realm",
            mplus_dps_breakdown=[
                {
                    "name": "<script>Dungeon",
                    "parse_percent": 50.0,
                    "median_percent": None,
                    "key_level": 12,
                    "run_count": 1,
                }
            ],
        )
    )

    assert panel._name_label.textFormat() == Qt.TextFormat.PlainText
    assert panel._name_label.text() == "<bad>"
    assert panel._dungeon_rows[0][0].textFormat() == Qt.TextFormat.PlainText
    assert panel._dungeon_rows[0][0].text() == "<script>Dungeon"


def test_overlay_window_minimum_size_allows_user_compact_resize(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        metric_preferences=MetricPreferences(),
    )
    qtbot.addWidget(window)

    try:
        assert window.minimumSize().width() <= 320
        assert window.minimumSize().height() <= 240

        window.resize(320, 240)
        QApplication.processEvents()

        assert window.width() == 320
        assert window.height() == 240
    finally:
        client.close()


def test_overlay_table_position_stays_fixed_when_panel_dungeon_rows_change(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window.setGeometry(40, 40, 572, 440)
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        QApplication.processEvents()

        compact = _app(
            applicant_id="compact",
            fetch_status="ready",
            mplus_dps=None,
            mplus_dps_median=None,
            mplus_dps_breakdown=[],
            rio_dungeons=[],
        )
        detailed = _app(
            applicant_id="detailed",
            fetch_status="ready",
            mplus_dps=90.0,
            mplus_dps_median=80.0,
            mplus_dps_breakdown=[
                {
                    "name": f"Dungeon {idx}",
                    "parse_percent": 80.0 + idx,
                    "median_percent": 70.0 + idx,
                    "key_level": 10 + idx,
                    "run_count": 2,
                }
                for idx in range(8)
            ],
        )

        window._panel.setApplicantData(compact, _listing())
        QApplication.processEvents()
        compact_table_y = window._table.geometry().y()

        window._panel.setApplicantData(detailed, _listing())
        QApplication.processEvents()

        assert window._table.geometry().y() == compact_table_y
    finally:
        client.close()


def test_compact_overlay_table_screen_position_stays_fixed_when_panel_expands_up(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    compact = _app(
        applicant_id="compact",
        fetch_status="ready",
        mplus_dps=None,
        mplus_dps_median=None,
        mplus_dps_breakdown=[],
        rio_dungeons=[],
    )
    detailed = _app(
        applicant_id="detailed",
        fetch_status="ready",
        mplus_dps=90.0,
        mplus_dps_median=80.0,
        mplus_dps_breakdown=[
            {
                "name": f"Dungeon {idx}",
                "parse_percent": 80.0 + idx,
                "median_percent": 70.0 + idx,
                "key_level": 10 + idx,
                "run_count": 2,
            }
            for idx in range(8)
        ],
    )
    state.add_or_update(compact)
    state.add_or_update(detailed)
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window.setGeometry(160, 180, 360, 240)
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._refresh_table()
        QApplication.processEvents()

        window._hover_id = "compact"
        window._sync_delegate_and_panel()
        QApplication.processEvents()
        compact_table_top = window._table.mapToGlobal(QPoint(0, 0)).y()

        window._hover_id = "detailed"
        window._sync_delegate_and_panel()
        QApplication.processEvents()

        table_top = window._table.mapToGlobal(QPoint(0, 0)).y()
        # Qt's native Windows metrics can differ by a few pixels across hosted
        # runner images; the user-facing contract is no visible table jump.
        assert abs(table_top - compact_table_top) <= 4
        assert window._panel.height() > 80
        assert window.y() < 180
    finally:
        client.close()


def test_panel_height_change_batches_window_updates_to_avoid_hover_jitter(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(
        _app(
            applicant_id="detailed",
            fetch_status="ready",
            mplus_dps=90.0,
            mplus_dps_median=80.0,
            mplus_dps_breakdown=[
                {
                    "name": f"Dungeon {idx}",
                    "parse_percent": 80.0 + idx,
                    "median_percent": 70.0 + idx,
                    "key_level": 10 + idx,
                    "run_count": 2,
                }
                for idx in range(8)
            ],
        )
    )
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)
    update_states: list[bool] = []
    panel_mutations: list[bool] = []
    geometry_mutations: list[bool] = []
    original_updates = window.setUpdatesEnabled
    original_minimum = window._panel.setMinimumHeight
    original_maximum = window._panel.setMaximumHeight
    original_geometry = window._set_geometry_without_persist

    def record_updates(enabled: bool) -> None:
        update_states.append(enabled)
        original_updates(enabled)

    def record_minimum(height: int) -> None:
        panel_mutations.append(window.updatesEnabled())
        original_minimum(height)

    def record_maximum(height: int) -> None:
        panel_mutations.append(window.updatesEnabled())
        original_maximum(height)

    def record_geometry(x: int, y: int, w: int, h: int) -> None:
        geometry_mutations.append(window.updatesEnabled())
        original_geometry(x, y, w, h)

    monkeypatch.setattr(window, "setUpdatesEnabled", record_updates)
    monkeypatch.setattr(window._panel, "setMinimumHeight", record_minimum)
    monkeypatch.setattr(window._panel, "setMaximumHeight", record_maximum)
    monkeypatch.setattr(window, "_set_geometry_without_persist", record_geometry)

    try:
        window.setGeometry(160, 180, 360, 240)
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._refresh_table()
        QApplication.processEvents()

        window._hover_id = "detailed"
        window._sync_delegate_and_panel()

        assert panel_mutations
        assert geometry_mutations
        assert panel_mutations == [False, False]
        assert geometry_mutations == [False]
        assert update_states[0] is False
        assert update_states[-1] is True
    finally:
        client.close()


def test_panel_content_swap_is_batched_with_height_change_to_avoid_one_frame_jump(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(
        _app(
            applicant_id="detailed",
            fetch_status="ready",
            mplus_dps=90.0,
            mplus_dps_median=80.0,
            mplus_dps_breakdown=[
                {
                    "name": f"Dungeon {idx}",
                    "parse_percent": 80.0 + idx,
                    "median_percent": 70.0 + idx,
                    "key_level": 10 + idx,
                    "run_count": 2,
                }
                for idx in range(8)
            ],
        )
    )
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)
    content_mutations: list[bool] = []
    original_set_applicant_data = window._panel.setApplicantData
    original_set_placeholder = window._panel.setPlaceholder

    def record_set_applicant_data(*args, **kwargs) -> None:
        content_mutations.append(window.updatesEnabled())
        original_set_applicant_data(*args, **kwargs)

    def record_set_placeholder(*args, **kwargs) -> None:
        content_mutations.append(window.updatesEnabled())
        original_set_placeholder(*args, **kwargs)

    monkeypatch.setattr(window._panel, "setApplicantData", record_set_applicant_data)
    monkeypatch.setattr(window._panel, "setPlaceholder", record_set_placeholder)

    try:
        window.setGeometry(160, 180, 360, 240)
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._refresh_table()
        QApplication.processEvents()

        window._hover_id = None
        window._sync_delegate_and_panel()
        content_mutations.clear()

        window._hover_id = "detailed"
        window._sync_delegate_and_panel()

        assert content_mutations == [False]
    finally:
        client.close()


def test_geometry_leave_event_keeps_hover_when_cursor_still_over_row(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    detailed = _app(
        applicant_id="detailed",
        fetch_status="ready",
        mplus_dps=90.0,
        mplus_dps_median=80.0,
        mplus_dps_breakdown=[
            {
                "name": f"Dungeon {idx}",
                "parse_percent": 80.0 + idx,
                "median_percent": 70.0 + idx,
                "key_level": 10 + idx,
                "run_count": 2,
            }
            for idx in range(8)
        ],
    )
    state.add_or_update(detailed)
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window.setGeometry(160, 180, 360, 240)
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._refresh_table()
        QApplication.processEvents()

        window._on_cell_entered(0, 0)
        QApplication.processEvents()
        viewport = window._table.viewport()
        assert viewport is not None
        cursor_pos = viewport.mapToGlobal(
            QPoint(8, window._table.rowViewportPosition(0) + window._table.rowHeight(0) // 2)
        )
        monkeypatch.setattr(overlay_mod.QCursor, "pos", lambda: cursor_pos)

        window.eventFilter(viewport, QEvent(QEvent.Type.Leave))
        QApplication.processEvents()

        assert window._hover_id == "detailed"
        assert window._panel.height() == window._panel.target_height()
    finally:
        client.close()


def test_geometry_mouse_move_empty_local_pos_keeps_hover_when_cursor_still_over_row(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    detailed = _app(
        applicant_id="detailed",
        fetch_status="ready",
        mplus_dps=90.0,
        mplus_dps_median=80.0,
        mplus_dps_breakdown=[
            {
                "name": f"Dungeon {idx}",
                "parse_percent": 80.0 + idx,
                "median_percent": 70.0 + idx,
                "key_level": 10 + idx,
                "run_count": 2,
            }
            for idx in range(8)
        ],
    )
    state.add_or_update(detailed)
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    class FakeMouseMove:
        def type(self):
            return QEvent.Type.MouseMove

        def position(self):
            return QPointF(8, window._table.viewport().height() + 12)

    try:
        window.setGeometry(160, 180, 360, 240)
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._refresh_table()
        QApplication.processEvents()

        window._on_cell_entered(0, 0)
        QApplication.processEvents()
        viewport = window._table.viewport()
        assert viewport is not None
        cursor_pos = viewport.mapToGlobal(
            QPoint(8, window._table.rowViewportPosition(0) + window._table.rowHeight(0) // 2)
        )
        monkeypatch.setattr(overlay_mod.QCursor, "pos", lambda: cursor_pos)

        window.eventFilter(viewport, FakeMouseMove())
        QApplication.processEvents()

        assert window._hover_id == "detailed"
        assert window._panel.height() == window._panel.target_height()
    finally:
        client.close()


def test_pinned_row_can_be_unpinned_from_info_panel(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(_app(applicant_id="42", name="Pinned-Realm"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._refresh_table()
        QApplication.processEvents()

        row = window._row_for_id["42"]
        window._on_cell_clicked(row, 0)
        window._hover_id = None
        window._sync_delegate_and_panel()

        assert window._pinned_id == "42"
        assert window._pinned_by_tab["applicants"] == "42"
        assert window._panel._unpin_button.isVisible()

        qtbot.mouseClick(window._panel._unpin_button, Qt.MouseButton.LeftButton)

        assert window._pinned_id is None
        assert window._pinned_by_tab["applicants"] is None
        assert window._delegate._pinned_row == -1
        assert window._panel._current_applicant is not None
        assert window._panel._current_applicant.applicant_id == "42"
        assert window._panel._unpin_button.isHidden()
    finally:
        client.close()


def test_overlay_constructor_initializes_geometry_event_state_before_set_geometry(
    monkeypatch, qtbot, tmp_path
):
    original_set_geometry = overlay_mod.OverlayWindow.setGeometry

    def guarded_set_geometry(self, *args):
        required = (
            "_save_timer",
            "_row_for_id",
            "_id_by_row",
            "_group_size_by_raw",
            "_package_fit_by_raw",
            "_hover_id",
            "_pinned_id",
            "_role_filter",
            "_fetches_in_flight",
            "_listing_session_generation",
        )
        missing = [name for name in required if not hasattr(self, name)]
        assert missing == []
        return original_set_geometry(self, *args)

    monkeypatch.setattr(overlay_mod.OverlayWindow, "setGeometry", guarded_set_geometry)
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        assert window._save_timer.isSingleShot()
    finally:
        client.close()


def test_overlay_constructor_uses_safe_defaults_for_corrupt_window_json(
    qtbot, tmp_path
):
    (tmp_path / "window.json").write_text(
        json.dumps(
            {
                "x": "left",
                "y": None,
                "w": "wide",
                "h": False,
                "layout_version": "new-ish",
            }
        ),
        encoding="utf-8",
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        assert window.geometry().width() >= window.minimumWidth()
        assert window.geometry().height() >= window.minimumHeight()
    finally:
        client.close()


def test_overlay_constructor_clamps_oversized_saved_geometry_to_current_screen(
    qtbot, tmp_path
):
    (tmp_path / "window.json").write_text(
        json.dumps(
            {
                "x": 40,
                "y": 50,
                "w": 100000,
                "h": 100000,
                "layout_version": WINDOW_GEOMETRY_LAYOUT_VERSION,
            }
        ),
        encoding="utf-8",
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        screen = window.screen() or QApplication.primaryScreen()
        assert screen is not None
        available = screen.availableGeometry()
        assert window.geometry().width() <= available.width()
        assert window.geometry().height() <= available.height()
    finally:
        client.close()


def test_health_label_surfaces_latest_decode_failure(monkeypatch, qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        monkeypatch.setattr(overlay_mod.time, "time", lambda: 100.0)
        window.note_decode_failed("WoWScrnShot_0001.jpg", "CRC mismatch")

        assert window._health_label.text() == "shot failed"
        assert "WoWScrnShot_0001.jpg" in window._health_label.toolTip()
        assert "CRC mismatch" in window._health_label.toolTip()
    finally:
        client.close()


def test_successful_decode_clears_previous_health_failure(monkeypatch, qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        times = iter([100.0, 100.0, 105.0, 107.0])
        monkeypatch.setattr(overlay_mod.time, "time", lambda: next(times))
        window.note_decode_failed("WoWScrnShot_0001.jpg", "CRC mismatch")
        window.note_decode(object())
        window._refresh_health_label()

        assert window._health_label.text() == "shot 2s ago"
        assert window._health_label.toolTip() == ""
    finally:
        client.close()


def test_flush_geometry_stops_pending_timer_and_persists_window_json(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window.setGeometry(23, 31, 640, 420)
        window._save_timer.start()

        window.flush_geometry()

        assert not window._save_timer.isActive()
        saved = json.loads((tmp_path / "window.json").read_text(encoding="utf-8"))
        assert saved["x"] == window.geometry().x()
        assert saved["y"] == window.geometry().y()
        assert saved["w"] == window.geometry().width()
        assert saved["h"] == window.geometry().height()
    finally:
        client.close()


def test_flush_geometry_near_screen_top_persists_unexpanded_window_origin(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(
        _app(
            applicant_id="detailed",
            fetch_status="ready",
            mplus_dps=90.0,
            mplus_dps_median=80.0,
            mplus_dps_breakdown=[
                {
                    "name": f"Dungeon {idx}",
                    "parse_percent": 80.0 + idx,
                    "median_percent": 70.0 + idx,
                    "key_level": 10 + idx,
                    "run_count": 2,
                }
                for idx in range(8)
            ],
        )
    )
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        screen = window.screen() or QApplication.primaryScreen()
        assert screen is not None
        top = screen.availableGeometry().top()
        original_y = top + 8
        window.setGeometry(160, original_y, 360, 240)
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._refresh_table()
        QApplication.processEvents()

        window._on_cell_entered(0, 0)
        QApplication.processEvents()
        assert window.geometry().y() == top

        window._hover_id = None
        window._sync_delegate_and_panel()
        QApplication.processEvents()

        assert window._panel._current_applicant is not None
        assert window._panel._current_applicant.applicant_id == "detailed"
        assert window.geometry().y() == top
        assert window.geometry().height() > 240

        window.flush_geometry()

        saved = json.loads((tmp_path / "window.json").read_text(encoding="utf-8"))
        assert saved["y"] == original_y
        assert saved["h"] == 240
    finally:
        client.close()


def test_hidden_panel_refresh_preserves_unexpanded_geometry_for_quit_flush(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(
        _app(
            applicant_id="detailed",
            fetch_status="ready",
            mplus_dps=90.0,
            mplus_dps_median=80.0,
            mplus_dps_breakdown=[
                {
                    "name": f"Dungeon {idx}",
                    "parse_percent": 80.0 + idx,
                    "median_percent": 70.0 + idx,
                    "key_level": 10 + idx,
                    "run_count": 2,
                }
                for idx in range(8)
            ],
        )
    )
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window.setGeometry(160, 180, 360, 240)
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._refresh_table()
        QApplication.processEvents()

        window._on_cell_entered(0, 0)
        QApplication.processEvents()
        assert window.geometry().y() < 180
        assert window.geometry().height() > 240

        window.collapse_to_launcher()
        assert not (tmp_path / "window.json").exists()

        window._sync_delegate_and_panel()
        window.flush_geometry()

        saved_after_hidden_refresh = json.loads(
            (tmp_path / "window.json").read_text(encoding="utf-8")
        )
        assert saved_after_hidden_refresh["y"] == 180
        assert saved_after_hidden_refresh["h"] == 240
    finally:
        client.close()


def test_collapse_to_launcher_does_not_flush_geometry_on_click(
    qtbot, tmp_path, monkeypatch
):
    calls: list[object] = []
    monkeypatch.setattr(
        overlay_mod,
        "save_geometry",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._save_timer.start()

        window.collapse_to_launcher()

        assert calls == []
        assert window._save_timer.isActive()
        assert not window.isVisible()
        assert window._launcher.isVisible()
        assert window._collapsed_to_launcher
    finally:
        client.close()


def test_collapse_to_launcher_keeps_pending_geometry_save_async(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        window.setGeometry(188, 211, 430, 260)
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window._save_timer.setInterval(1)
        window._save_timer.start()

        window.collapse_to_launcher()

        assert not (tmp_path / "window.json").exists()
        qtbot.waitUntil(lambda: (tmp_path / "window.json").exists(), timeout=1000)
        saved = json.loads((tmp_path / "window.json").read_text(encoding="utf-8"))
        assert saved["x"] == 188
        assert saved["y"] == 211
        assert saved["w"] == 430
        assert saved["h"] == 260
    finally:
        client.close()


def test_overlay_starts_collapsed_with_launcher_visible(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        assert window._collapsed_to_launcher
        assert not window.isVisible()
    finally:
        client.close()


def test_overlay_launcher_waits_for_game_foreground(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": False}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        assert window._collapsed_to_launcher
        assert not window._launcher.isVisible()
        assert not window.isVisible()

        foreground["active"] = True
        window._sync_game_foreground_visibility()

        assert window._launcher.isVisible()
        assert not window.isVisible()
    finally:
        client.close()


def test_open_overlay_hides_outside_game_and_restores_when_game_returns(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        window.restore_from_launcher()
        qtbot.waitUntil(window.isVisible, timeout=1000)

        foreground["active"] = False
        monkeypatch.setattr(window, "isActiveWindow", lambda: False)
        window._sync_game_foreground_visibility()

        assert not window.isVisible()
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher

        foreground["active"] = True
        window._sync_game_foreground_visibility()

        assert window.isVisible()
        assert not window._launcher.isVisible()
    finally:
        client.close()


def test_launcher_click_during_foreground_grace_does_not_show_overlay_outside_game(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        assert window._game_foreground

        foreground["active"] = False
        window._launcher_foreground_grace_until = time.monotonic() + 10.0
        window._sync_game_foreground_visibility()
        assert window._game_foreground
        assert window._launcher.isVisible()

        monkeypatch.setattr(
            overlay_mod.OverlayLauncher, "isActiveWindow", lambda _self: True
        )
        window.restore_from_launcher()

        assert not window.isVisible()
        assert not window._launcher.isVisible()
        assert window._collapsed_to_launcher
        assert not window._game_foreground
    finally:
        client.close()


def test_launcher_restore_does_not_activate_overlay_window(qtbot, tmp_path, monkeypatch):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: True,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    activated: list[bool] = []

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        monkeypatch.setattr(
            window,
            "activateWindow",
            lambda: activated.append(True),
        )

        window.restore_from_launcher()

        qtbot.waitUntil(window.isVisible, timeout=1000)
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher
        assert activated == []
    finally:
        client.close()


def test_launcher_mouse_click_restores_overlay_when_probe_sees_launcher(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        foreground["active"] = False
        monkeypatch.setattr(
            overlay_mod.OverlayLauncher, "isActiveWindow", lambda _self: False
        )
        qtbot.mouseClick(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6)
        )

        qtbot.waitUntil(window.isVisible, timeout=1000)
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher
        assert window._game_foreground
    finally:
        client.close()


def test_launcher_restore_survives_next_foreground_poll_during_grace(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        foreground["active"] = False
        monkeypatch.setattr(
            overlay_mod.OverlayLauncher, "isActiveWindow", lambda _self: False
        )
        monkeypatch.setattr(window, "isActiveWindow", lambda: False)

        qtbot.mouseClick(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6)
        )
        qtbot.waitUntil(window.isVisible, timeout=1000)

        window._sync_game_foreground_visibility()

        assert window.isVisible()

        window._launcher_foreground_grace_until = time.monotonic() - 1.0
        window._sync_game_foreground_visibility()

        assert not window.isVisible()
    finally:
        client.close()


def test_launcher_click_restore_skips_foreground_probe_on_hot_path(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    probe_calls = {"count": 0}

    def foreground_probe() -> bool:
        probe_calls["count"] += 1
        return True

    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=foreground_probe,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        probe_calls["count"] = 0
        window._launcher._click_emit_active = True

        try:
            window.restore_from_launcher()
        finally:
            window._launcher._click_emit_active = False

        assert probe_calls["count"] == 0
        assert window.isVisible()
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher
    finally:
        client.close()


def test_launcher_click_restores_overlay_when_launcher_has_foreground(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        foreground["active"] = False
        monkeypatch.setattr(
            overlay_mod.OverlayLauncher, "isActiveWindow", lambda _self: True
        )
        window.restore_from_launcher()

        qtbot.waitUntil(window.isVisible, timeout=1000)
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher
        assert window._game_foreground
    finally:
        client.close()


def test_tray_restore_shows_and_activates_overlay_outside_game_foreground(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    activated: list[bool] = []
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: False,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        monkeypatch.setattr(
            window,
            "activateWindow",
            lambda: activated.append(True),
        )

        window.restore_from_tray()

        qtbot.waitUntil(window.isVisible, timeout=1000)
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher
        assert activated == [True]
    finally:
        client.close()


def test_open_overlay_stays_visible_while_companion_window_is_active(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        window.restore_from_launcher()
        qtbot.waitUntil(window.isVisible, timeout=1000)

        foreground["active"] = False
        monkeypatch.setattr(window, "isActiveWindow", lambda: True)
        window._sync_game_foreground_visibility()

        assert window.isVisible()
        assert not window._launcher.isVisible()

        window._state.add_or_update(_app(applicant_id="active-window-update"))
        window.on_applicant_added(window._state.applicants["active-window-update"])
        window._flush_overlay_refresh()

        assert window.isVisible()
        assert not window._launcher.isVisible()

        monkeypatch.setattr(window, "isActiveWindow", lambda: False)
        window._sync_game_foreground_visibility()

        assert not window.isVisible()
        assert not window._launcher.isVisible()
    finally:
        client.close()


def test_background_updates_do_not_show_overlay_outside_game(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    foreground = {"active": False}
    window = OverlayWindow(
        state,
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        state.add_or_update(_app(applicant_id="1"))
        window.on_applicant_added(state.applicants["1"])
        window._flush_overlay_refresh()

        assert window._collapsed_to_launcher
        assert not window.isVisible()
        assert not window._launcher.isVisible()

        foreground["active"] = True
        window._sync_game_foreground_visibility()

        assert window._launcher.isVisible()
        assert not window.isVisible()
    finally:
        client.close()


def test_title_bar_hide_button_collapses_to_launcher_without_shutdown(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)

        hide_button = window._title_bar.hide_button
        assert hide_button.text() == "-"
        assert hide_button.toolTip() == "Hide overlay"

        qtbot.mouseClick(hide_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: not window.isVisible(), timeout=1000)
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        qtbot.mouseClick(window._launcher, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(window.isVisible, timeout=1000)
        assert not window._launcher.isVisible()

        assert window._state is state
        assert window._wcl_client is client
        assert not client._http.is_closed
    finally:
        client.close()


def test_overlay_close_hides_launcher_window(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        assert window.close()

        assert not window.isVisible()
        assert not window._launcher.isVisible()
        assert not window._launcher.is_dragging()
        assert QWidget.mouseGrabber() is not window._launcher

        foreground["active"] = False
        window._sync_game_foreground_visibility()
        foreground["active"] = True
        window._sync_game_foreground_visibility()

        assert not window.isVisible()
        assert not window._launcher.isVisible()
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_automatic_overlay_hide_returns_to_launcher(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.add_or_update(_app(applicant_id="1"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)

        state.remove("1")
        window.on_applicant_removed("1")
        qtbot.waitUntil(lambda: not window.isVisible(), timeout=1000)

        assert window._launcher.isVisible()
        assert window._collapsed_to_launcher
    finally:
        client.close()


def test_launcher_drag_moves_without_restoring_overlay(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window.collapse_to_launcher()
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        original_pos = window._launcher.pos()
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(22, 18))
        qtbot.mouseRelease(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(22, 18)
        )

        assert window._launcher.pos() != original_pos
        assert window._launcher.isVisible()
        assert not window.isVisible()
        saved = json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))
        assert saved == {
            "x": window._launcher.pos().x(),
            "y": window._launcher.pos().y(),
        }
    finally:
        client.close()


def test_launcher_drag_keeps_mouse_grab_until_release(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))

        assert QWidget.mouseGrabber() is window._launcher

        qtbot.mouseRelease(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(22, 18)
        )
        assert QWidget.mouseGrabber() is None
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_survives_foreground_probe_drop(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))

        foreground["active"] = False
        window._sync_game_foreground_visibility()

        assert window._collapsed_to_launcher
        assert window._launcher.isVisible()
        assert not window.isVisible()
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_does_not_mutate_foreground_state(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        assert window._game_foreground
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        assert not window._foreground_timer.isActive()

        foreground["active"] = False
        window._sync_game_foreground_visibility()

        assert window._game_foreground
        assert window._launcher.isVisible()
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        qtbot.mouseRelease(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(28, 20)
        )
        assert window._foreground_timer.isActive()
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_release_stays_stable_through_foreground_poll(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))

        foreground["active"] = False
        window._sync_game_foreground_visibility()
        assert window._launcher.isVisible()

        qtbot.mouseMove(window._launcher, pos=QPoint(22, 18))
        qtbot.mouseRelease(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(22, 18)
        )
        released_pos = window._launcher.pos()
        qtbot.wait(GAME_FOREGROUND_POLL_MS + 150)

        assert QWidget.mouseGrabber() is None
        assert window._collapsed_to_launcher
        assert window._launcher.isVisible()
        assert window._launcher.pos() == released_pos
        assert not window.isVisible()
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_ungrab_waits_for_stable_button_up_before_finishing(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        assert window._launcher.is_dragging()
        assert not window._foreground_timer.isActive()
        monkeypatch.setattr(
            overlay_mod.QApplication,
            "mouseButtons",
            lambda: Qt.MouseButton.NoButton,
        )
        monkeypatch.setattr(
            overlay_mod, "_native_left_mouse_button_down", lambda: False
        )
        released_pos = window._launcher.pos()
        last_cursor_pos = window._launcher._last_drag_cursor_pos
        assert last_cursor_pos is not None
        monkeypatch.setattr(overlay_mod.QCursor, "pos", lambda: last_cursor_pos)
        now = {"value": 10.0}
        monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: now["value"])

        QApplication.sendEvent(
            window._launcher,
            QEvent(QEvent.Type.UngrabMouse),
        )
        window._launcher._poll_drag_cursor()

        assert window._launcher.is_dragging()
        assert not window._foreground_timer.isActive()

        now["value"] += 0.5
        window._launcher._poll_drag_cursor()
        assert window._launcher.is_dragging()

        now["value"] += 0.6
        window._launcher._poll_drag_cursor()
        assert not window._launcher.is_dragging()
        assert window._foreground_timer.isActive()
        assert QWidget.mouseGrabber() is None
        saved = json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))
        assert saved == {
            "x": released_pos.x(),
            "y": released_pos.y(),
        }
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_click_fallback_restores_overlay_without_release_grace(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        assert window._launcher.is_dragging()
        assert not window._foreground_timer.isActive()
        monkeypatch.setattr(
            overlay_mod.QApplication,
            "mouseButtons",
            lambda: Qt.MouseButton.NoButton,
        )
        monkeypatch.setattr(
            overlay_mod, "_native_left_mouse_button_down", lambda: False
        )
        now = {"value": 10.0}
        monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: now["value"])
        press_global_pos = window._launcher._press_global_pos
        assert press_global_pos is not None
        monkeypatch.setattr(overlay_mod.QCursor, "pos", lambda: press_global_pos)

        window._launcher._poll_drag_cursor()

        assert not window._launcher.is_dragging()
        assert window._foreground_timer.isActive()
        assert window.isVisible()
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_click_fallback_allows_threshold_cursor_jitter(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        assert window._launcher.is_dragging()
        monkeypatch.setattr(
            overlay_mod.QApplication,
            "mouseButtons",
            lambda: Qt.MouseButton.NoButton,
        )
        monkeypatch.setattr(
            overlay_mod, "_native_left_mouse_button_down", lambda: False
        )
        press_global_pos = window._launcher._press_global_pos
        assert press_global_pos is not None
        monkeypatch.setattr(
            overlay_mod.QCursor,
            "pos",
            lambda: press_global_pos + QPoint(3, 0),
        )

        window._launcher._poll_drag_cursor()

        assert not window._launcher.is_dragging()
        assert window.isVisible()
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher
        assert not (tmp_path / "launcher.json").exists()
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_ignores_cursor_motion_after_confirmed_button_release(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        press_global_pos = window._launcher._press_global_pos
        assert press_global_pos is not None
        monkeypatch.setattr(
            overlay_mod.QApplication,
            "mouseButtons",
            lambda: Qt.MouseButton.NoButton,
        )
        monkeypatch.setattr(
            overlay_mod, "_native_left_mouse_button_down", lambda: False
        )
        now = {"value": 20.0}
        cursor_pos = {"value": press_global_pos}
        monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: now["value"])
        monkeypatch.setattr(overlay_mod.QCursor, "pos", lambda: cursor_pos["value"])

        original_pos = window._launcher.pos()
        cursor_pos["value"] = press_global_pos + QPoint(40, 25)
        window._launcher._poll_drag_cursor()
        now["value"] += 0.5
        cursor_pos["value"] = press_global_pos + QPoint(80, 50)
        window._launcher._poll_drag_cursor()
        assert window._launcher.is_dragging()
        assert not window._foreground_timer.isActive()
        assert window._launcher.pos() == original_pos

        now["value"] += 0.6
        cursor_pos["value"] = press_global_pos + QPoint(120, 75)
        window._launcher._poll_drag_cursor()
        assert not window._launcher.is_dragging()
        assert window._launcher.pos() == original_pos
        assert window._foreground_timer.isActive()
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_uses_native_left_button_when_qt_reports_no_button(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        last_cursor_pos = window._launcher._last_drag_cursor_pos
        assert last_cursor_pos is not None
        monkeypatch.setattr(
            overlay_mod.QApplication,
            "mouseButtons",
            lambda: Qt.MouseButton.NoButton,
        )
        monkeypatch.setattr(overlay_mod, "_native_left_mouse_button_down", lambda: True)
        cursor_pos = {"value": last_cursor_pos}
        monkeypatch.setattr(overlay_mod.QCursor, "pos", lambda: cursor_pos["value"])
        now = {"value": 40.0}
        monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: now["value"])

        window._launcher._poll_drag_cursor()
        cursor_pos["value"] = last_cursor_pos + QPoint(22, 14)
        now["value"] += 2.0
        window._launcher._poll_drag_cursor()

        assert window._launcher.is_dragging()
        assert not window._foreground_timer.isActive()
        assert window._launcher.pos() != window._launcher._press_window_pos
        qtbot.mouseRelease(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(34, 24)
        )
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_survives_transient_ungrab_while_button_held(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        position_after_first_move = window._launcher.pos()
        press_global_pos = window._launcher._press_global_pos
        assert press_global_pos is not None

        monkeypatch.setattr(
            overlay_mod.QApplication,
            "mouseButtons",
            lambda: Qt.MouseButton.LeftButton,
        )
        QApplication.sendEvent(
            window._launcher,
            QEvent(QEvent.Type.UngrabMouse),
        )

        assert window._launcher.is_dragging()
        assert not window._foreground_timer.isActive()
        cursor_pos = press_global_pos + QPoint(56, 36)
        monkeypatch.setattr(overlay_mod.QCursor, "pos", lambda: cursor_pos)
        window._launcher._poll_drag_cursor()
        position_after_poll = window._launcher.pos()

        assert position_after_poll != position_after_first_move
        qtbot.mouseRelease(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(34, 24)
        )
        saved = json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))
        assert saved == {
            "x": position_after_poll.x(),
            "y": position_after_poll.y(),
        }
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_survives_foreground_drop_after_transient_ungrab(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        dragged_pos = window._launcher.pos()
        monkeypatch.setattr(
            overlay_mod.QApplication,
            "mouseButtons",
            lambda: Qt.MouseButton.LeftButton,
        )

        QApplication.sendEvent(
            window._launcher,
            QEvent(QEvent.Type.UngrabMouse),
        )
        foreground["active"] = False
        window._sync_game_foreground_visibility()

        assert window._launcher.is_dragging()
        assert window._launcher.isVisible()
        assert window._launcher.pos() == dragged_pos
        assert not window._foreground_timer.isActive()
        qtbot.mouseRelease(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(28, 20)
        )
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_poll_finishes_lost_release_and_persists_position(
    qtbot, tmp_path, monkeypatch
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        released_pos = window._launcher.pos()
        monkeypatch.setattr(
            overlay_mod.QApplication,
            "mouseButtons",
            lambda: Qt.MouseButton.NoButton,
        )
        monkeypatch.setattr(
            overlay_mod, "_native_left_mouse_button_down", lambda: False
        )
        last_cursor_pos = window._launcher._last_drag_cursor_pos
        assert last_cursor_pos is not None
        monkeypatch.setattr(overlay_mod.QCursor, "pos", lambda: last_cursor_pos)
        now = {"value": 30.0}
        monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: now["value"])

        window._launcher._poll_drag_cursor()
        now["value"] += 1.1
        window._launcher._poll_drag_cursor()

        assert not window._launcher.is_dragging()
        assert window._foreground_timer.isActive()
        saved = json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))
        assert saved == {
            "x": released_pos.x(),
            "y": released_pos.y(),
        }
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_ignores_window_deactivate_event(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        position_after_first_move = window._launcher.pos()

        QApplication.sendEvent(
            window._launcher,
            QEvent(QEvent.Type.WindowDeactivate),
        )

        assert window._launcher.is_dragging()
        assert QWidget.mouseGrabber() is window._launcher
        assert not window._foreground_timer.isActive()
        qtbot.mouseMove(window._launcher, pos=QPoint(34, 24))
        position_after_second_move = window._launcher.pos()
        qtbot.mouseRelease(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(34, 24)
        )
        assert window._foreground_timer.isActive()
        assert position_after_second_move != position_after_first_move
        saved = json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))
        assert saved == {
            "x": position_after_second_move.x(),
            "y": position_after_second_move.y(),
        }
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_refresh_does_not_reposition_during_drag(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        dragged_pos = window._launcher.pos()

        window._maybe_show()
        window.show_launcher_only()

        assert window._launcher.isVisible()
        assert window._launcher.pos() == dragged_pos
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_refresh_does_not_hide_during_drag_after_foreground_drop(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    foreground = {"active": True}
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        foreground["active"] = False
        window._sync_game_foreground_visibility()
        dragged_pos = window._launcher.pos()

        window.show_launcher_only()
        window._maybe_show()

        assert window._launcher.isVisible()
        assert window._launcher.pos() == dragged_pos
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_survives_applicant_refresh(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        dragged_pos = window._launcher.pos()

        applicant = _app(applicant_id="during-drag")
        state.add_or_update(applicant)
        window.on_applicant_added(applicant)
        window._flush_overlay_refresh()

        assert QWidget.mouseGrabber() is window._launcher
        assert window._collapsed_to_launcher
        assert window._launcher.isVisible()
        assert not window.isVisible()
        assert window._launcher.pos() == dragged_pos
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_survives_listing_refresh(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.add_or_update(_app(applicant_id="existing"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        dragged_pos = window._launcher.pos()

        state.listing = _listing()
        window.on_listing_changed()
        window._flush_overlay_refresh()

        assert QWidget.mouseGrabber() is window._launcher
        assert window._collapsed_to_launcher
        assert window._launcher.isVisible()
        assert not window.isVisible()
        assert window._launcher.pos() == dragged_pos
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_delayed_roster_after_last_applicant_removed_keeps_open_listing_on_applicants(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    applicant = _app(applicant_id="42:1")
    state.add_or_update(applicant)
    state.listing = _listing()
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        state.remove("42:1")
        window.on_applicant_removed("42:1")
        window._flush_overlay_refresh()

        assert window.isVisible()
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher
        assert window._active_tab == "applicants"

        state.add_or_update_party_member(_member())
        window.on_roster_changed()
        window._flush_overlay_refresh()

        assert window.isVisible()
        assert not window._launcher.isVisible()
        assert not window._collapsed_to_launcher
        assert window._active_tab == "applicants"
        assert not window._party_tab_auto_selected
    finally:
        if QWidget.mouseGrabber() is window._launcher:
            window._launcher.releaseMouse()
        client.close()


def test_launcher_drag_position_is_clamped_to_visible_screen(qtbot, tmp_path):
    screen = QApplication.primaryScreen()
    assert screen is not None
    geometry = screen.geometry()
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        window._launcher._move_to_drag_position(
            QPoint(geometry.right() + 5000, geometry.bottom() + 5000)
        )
        pos = window._launcher.pos()

        assert geometry.x() <= pos.x() <= geometry.right() - LAUNCHER_SIZE + 1
        assert geometry.y() <= pos.y() <= geometry.bottom() - LAUNCHER_SIZE + 1
    finally:
        client.close()


def test_launcher_drag_near_screen_edge_clamps_without_recentering(qtbot, tmp_path):
    screen = QApplication.primaryScreen()
    assert screen is not None
    geometry = screen.geometry()
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        window._launcher._move_to_drag_position(
            QPoint(geometry.right() - 5, geometry.y() + 10)
        )

        assert window._launcher.pos() == QPoint(
            geometry.x() + geometry.width() - LAUNCHER_SIZE,
            geometry.y() + 10,
        )
    finally:
        client.close()


def test_launcher_clamp_uses_physical_screen_beyond_work_area(monkeypatch):
    class FakeScreen:
        def geometry(self) -> QRect:
            return QRect(0, 0, 1920, 1080)

        def availableGeometry(self) -> QRect:
            return QRect(0, 0, 1920, 1040)

    screen = FakeScreen()
    monkeypatch.setattr(overlay_mod.QGuiApplication, "screens", lambda: [screen])
    monkeypatch.setattr(overlay_mod.QGuiApplication, "primaryScreen", lambda: screen)

    _x, y, _w, _h = overlay_mod._clamp_geometry_to_screen(
        100,
        1030,
        LAUNCHER_SIZE,
        LAUNCHER_SIZE,
        min_visible_px=1,
        use_available_geometry=False,
    )
    _work_x, work_y, _work_w, _work_h = overlay_mod._clamp_geometry_to_screen(
        100,
        1030,
        LAUNCHER_SIZE,
        LAUNCHER_SIZE,
        min_visible_px=1,
    )

    assert y == 1030
    assert work_y == 1040 - LAUNCHER_SIZE


def test_default_launcher_tracks_overlay_until_user_drags_launcher(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        first_pos = window._launcher.pos()

        window.restore_from_launcher()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window.setGeometry(240, 220, 360, 260)
        expected_after_move = window._default_launcher_position()
        assert expected_after_move != first_pos

        window.collapse_to_launcher()
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        assert window._launcher.pos() == expected_after_move
    finally:
        client.close()


def test_dragged_launcher_position_wins_over_overlay_default(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        qtbot.mousePress(window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
        qtbot.mouseMove(window._launcher, pos=QPoint(28, 20))
        qtbot.mouseRelease(
            window._launcher, Qt.MouseButton.LeftButton, pos=QPoint(28, 20)
        )
        dragged_pos = window._launcher.pos()

        window.restore_from_launcher()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        window.setGeometry(260, 260, 360, 260)
        assert window._default_launcher_position() == dragged_pos

        window.collapse_to_launcher()
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        assert window._launcher.pos() == dragged_pos
    finally:
        client.close()


def test_launcher_position_persists_across_overlay_restarts(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    first = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(first)
    qtbot.addWidget(first._launcher)

    try:
        qtbot.waitUntil(first._launcher.isVisible, timeout=1000)
        first._launcher.move(321, 234)
        first._persist_launcher_position()

        saved = json.loads((tmp_path / "launcher.json").read_text(encoding="utf-8"))
        assert saved == {"x": 321, "y": 234}

        second = OverlayWindow(AppState(), client, cache, tmp_path)
        qtbot.addWidget(second)
        qtbot.addWidget(second._launcher)
        qtbot.waitUntil(second._launcher.isVisible, timeout=1000)

        assert second._launcher.pos() == QPoint(321, 234)
    finally:
        client.close()


def test_saved_launcher_position_near_bottom_clamps_to_physical_screen_edge(qtbot, tmp_path):
    screen = QApplication.primaryScreen()
    assert screen is not None
    geometry = screen.geometry()
    saved_x = geometry.x() + max(0, (geometry.width() - LAUNCHER_SIZE) // 2)
    saved_y = geometry.y() + geometry.height() - 8
    (tmp_path / "launcher.json").write_text(
        json.dumps({"x": saved_x, "y": saved_y}),
        encoding="utf-8",
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)

    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)

        assert window._launcher.pos() == QPoint(
            saved_x,
            geometry.y() + geometry.height() - LAUNCHER_SIZE,
        )
    finally:
        client.close()


def test_default_launcher_position_uses_window_top_right_when_in_bounds(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        screen = QApplication.primaryScreen()
        assert screen is not None
        available = screen.availableGeometry()
        window.setGeometry(available.x() + 20, available.y() + 30, 320, 240)
        QApplication.processEvents()

        pos = window._default_launcher_position()

        assert pos == QPoint(window.geometry().x() + 320 - LAUNCHER_SIZE, window.geometry().y())
    finally:
        client.close()


def test_overlay_table_mplus_column_consumes_right_edge(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window.show()
        qtbot.waitUntil(lambda: window._table.viewport().width() > 0, timeout=1000)
        QApplication.processEvents()

        widths = [
            window._table.columnWidth(col) for col in range(window._table.columnCount())
        ]
        assert sum(widths) == window._table.viewport().width()
        assert window._table.columnWidth(COL_MPLUS) > 88

        initial_mplus_width = window._table.columnWidth(COL_MPLUS)
        window.resize(DEFAULT_WINDOW_WIDTH + 120, window.height())
        QApplication.processEvents()

        resized_widths = [
            window._table.columnWidth(col) for col in range(window._table.columnCount())
        ]
        assert sum(resized_widths) == window._table.viewport().width()
        assert window._table.columnWidth(COL_MPLUS) > initial_mplus_width
    finally:
        client.close()


def test_overlay_panel_uses_group_package_fit_for_any_member(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(_app(applicant_id="10:1", name="Leader-Realm"))
    state.add_or_update(_app(applicant_id="10:2", name="Follower-Realm"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        window._hover_id = "10:2"
        window._sync_delegate_and_panel()

        assert window._panel._package_label.text().startswith("Group ")
        assert not window._panel._package_label.isHidden()
    finally:
        client.close()


def test_metric_layout_refresh_does_not_expand_user_resized_window(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=DEFAULT_METRIC_PREFERENCES)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        metric_preferences=DEFAULT_METRIC_PREFERENCES,
    )
    qtbot.addWidget(window)

    try:
        window.resize(320, 240)
        QApplication.processEvents()

        window.apply_metric_preferences(MetricPreferences(mplus=True))
        QApplication.processEvents()

        assert window.width() == 320
        assert window.height() == 240
    finally:
        client.close()


def test_overlay_hover_wins_over_pin_when_tracking_enabled(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.add_or_update(_app(applicant_id="10:1", name="Hover-Realm"))
    state.add_or_update(_app(applicant_id="10:2", name="Pinned-Realm"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        assert window._table.hasMouseTracking()
        assert window._table.viewport().hasMouseTracking()

        window._pinned_id = "10:2"
        window._hover_id = "10:1"
        window._sync_delegate_and_panel()

        assert window._panel._name_label.text() == "Hover"
    finally:
        client.close()


def test_overlay_hides_disabled_metric_columns_and_panel_badges(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    prefs = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=True,
    )
    client = WCLClient(auth, metric_preferences=prefs)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(
        _app(
            raid_normal=88.0,
            raid_heroic=77.0,
            raid_mythic=66.0,
            mplus_dps=55.0,
        )
    )
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=prefs)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        window._hover_id = "42"
        window._sync_delegate_and_panel()

        assert not window._table.isColumnHidden(COL_N)
        assert window._table.isColumnHidden(COL_H)
        assert not window._table.isColumnHidden(COL_M)
        assert window._table.isColumnHidden(COL_MPLUS)
        assert not window._panel._metric_labels["N"].isHidden()
        assert window._panel._metric_labels["H"].isHidden()
        assert not window._panel._metric_labels["M"].isHidden()
        assert window._panel._metric_labels["M+"].isHidden()
        assert window._panel._dungeon_widget.isHidden()
        applicant = state.applicants["42"]
        assert applicant.mplus_dps is None
        assert applicant.mplus_dps_breakdown == []
    finally:
        client.close()


def test_apply_metric_preferences_narrowing_prunes_without_refetch(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    app = _app(wcl_metric_preferences=MetricPreferences())
    state.add_or_update(app)
    window = OverlayWindow(
        state,
        client,
        cache,
        tmp_path,
        metric_preferences=MetricPreferences(),
    )
    window._pool = None
    qtbot.addWidget(window)
    prefs = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=True,
    )

    try:
        window.apply_metric_preferences(prefs)

        assert window._table.isColumnHidden(COL_MPLUS)
        assert window._table.isColumnHidden(COL_H)
        assert app.fetch_status == "ready"
        assert app.mplus_dps is None
        assert app.mplus_dps_breakdown == []
        assert app.raid_heroic is None
        assert app.wcl_metric_preferences == prefs
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_apply_metric_preferences_broadening_refetches_missing_scope(qtbot, tmp_path):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=narrow)
    cache = CharacterCache(tmp_path)
    state = AppState()
    app = _app(
        mplus_dps=None,
        mplus_dps_breakdown=[],
        raid_heroic=None,
        raid_heroic_median=None,
        wcl_metric_preferences=narrow,
    )
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=narrow)
    window._pool = None
    qtbot.addWidget(window)

    try:
        window.apply_metric_preferences(MetricPreferences())

        assert not window._table.isColumnHidden(COL_MPLUS)
        assert not window._table.isColumnHidden(COL_H)
        assert app.fetch_status == "loading"
        assert app.wcl_metric_preferences is None
        assert app.applicant_id in window._fetches_in_flight
    finally:
        client.close()


def test_apply_metric_preferences_can_defer_refetch_when_runtime_client_stale(
    qtbot, tmp_path
):
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=True,
    )
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, metric_preferences=narrow)
    cache = CharacterCache(tmp_path)
    state = AppState()
    app = _app(
        mplus_dps=None,
        mplus_dps_breakdown=[],
        raid_heroic=None,
        raid_heroic_median=None,
        wcl_metric_preferences=narrow,
    )
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path, metric_preferences=narrow)
    window._pool = None
    qtbot.addWidget(window)

    try:
        window.apply_metric_preferences(MetricPreferences(), refetch_missing=False)

        assert not window._table.isColumnHidden(COL_MPLUS)
        assert not window._table.isColumnHidden(COL_H)
        assert app.fetch_status == "ready"
        assert app.wcl_metric_preferences is None
        assert app.applicant_id not in window._fetches_in_flight
    finally:
        client.close()


def test_package_cell_keeps_package_and_individual_mplus_for_every_group_member(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(_app(applicant_id="10:2", name="Second-Realm"))
    state.add_or_update(_app(applicant_id="10:3", name="Third-Realm"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        owner_row = window._row_for_id["10:2"]
        follower_row = window._row_for_id["10:3"]
        owner_item = window._table.item(owner_row, COL_MPLUS)
        follower_item = window._table.item(follower_row, COL_MPLUS)

        assert owner_item.data(MPLUS_PACKAGE_TEXT_ROLE).startswith("G2 ")
        assert follower_item.data(MPLUS_PACKAGE_TEXT_ROLE).startswith("G2 ")
        assert owner_item.data(MPLUS_PACKAGE_BG_ROLE) == follower_item.data(
            MPLUS_PACKAGE_BG_ROLE
        )
        assert owner_item.data(MPLUS_INDIVIDUAL_TEXT_ROLE).endswith("+14")
        assert follower_item.data(MPLUS_INDIVIDUAL_TEXT_ROLE).endswith("+14")
        assert owner_item.data(MPLUS_INDIVIDUAL_BG_ROLE)
        assert follower_item.data(MPLUS_INDIVIDUAL_BG_ROLE)
        assert window._delegate._group_marker_by_row[owner_row].first_visible
        assert window._delegate._group_marker_by_row[follower_row].last_visible
    finally:
        client.close()


def test_package_cell_does_not_use_terminal_member_stale_mplus_for_group_score(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(_app(applicant_id="10:1", name="Ready-Realm"))
    state.add_or_update(
        _app(applicant_id="10:2", name="Error-Realm", fetch_status="error")
    )
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        error_row = window._row_for_id["10:2"]
        item = window._table.item(error_row, COL_MPLUS)

        assert item.data(MPLUS_INDIVIDUAL_TEXT_ROLE) == "?"
        assert item.data(MPLUS_PACKAGE_TEXT_ROLE).startswith("G2 ")
    finally:
        client.close()


def test_group_mplus_delegate_paints_full_cell_width(qtbot):
    table = QTableWidget(1, COL_MPLUS + 1)
    qtbot.addWidget(table)
    delegate = _HoverHighlightDelegate(table)
    table.setItem(
        0,
        COL_MPLUS,
        _mplus_group_cell(
            PackageFit(size=2, display="G2 OK 58", colour="#0070ff"),
            _app(),
            _listing(),
        ),
    )
    index = table.model().index(0, COL_MPLUS)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 100, 24)
    option.widget = table
    image = QImage(option.rect.size(), QImage.Format.Format_ARGB32)
    image.fill(QColor("#000000"))
    painter = QPainter(image)
    try:
        delegate.paint(painter, option, index)
    finally:
        painter.end()

    assert image.pixelColor(
        option.rect.width() - 1, option.rect.height() // 2
    ) != QColor("#000000")


def test_overlay_table_mplus_listing_status_precedes_stale_fit_for_solo_row(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    app = _app(applicant_id="20:1", fetch_status="error")
    state.add_or_update(app)
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        row = window._row_for_id[app.applicant_id]

        assert window._table.item(row, COL_MPLUS).text() == "?"
    finally:
        client.close()


def test_role_filter_shows_whole_group_when_one_member_matches(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.add_or_update(_app(applicant_id="10:1", name="Tank-Realm", role="TANK"))
    state.add_or_update(_app(applicant_id="10:2", name="Damage-Realm", role="DAMAGER"))
    state.add_or_update(_app(applicant_id="20:1", name="Healer-Realm", role="HEALER"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        qtbot.mouseClick(
            window._role_filter_bar._buttons["DAMAGER"],
            Qt.MouseButton.LeftButton,
        )

        visibility = {
            applicant_id: not window._table.isRowHidden(row)
            for applicant_id, row in window._row_for_id.items()
        }
        assert visibility == {
            "10:1": True,
            "10:2": True,
            "20:1": False,
        }
        assert window._role_filter_bar._status.text() == "showing 2 / 3"
    finally:
        client.close()


def test_refresh_table_builds_group_markers_once(qtbot, tmp_path, monkeypatch):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.add_or_update(_app(applicant_id="10:1", name="Tank-Realm", role="TANK"))
    state.add_or_update(_app(applicant_id="10:2", name="Damage-Realm", role="DAMAGER"))
    state.add_or_update(_app(applicant_id="20:1", name="Healer-Realm", role="HEALER"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)
    original = overlay_mod._build_group_markers
    calls = 0

    def counted_build_group_markers(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(overlay_mod, "_build_group_markers", counted_build_group_markers)

    try:
        window._refresh_table()

        assert calls == 1
        assert set(window._delegate._group_marker_by_row) == {
            window._row_for_id["10:1"],
            window._row_for_id["10:2"],
        }
    finally:
        client.close()


def test_role_filter_preserves_pin_when_pinned_group_stays_visible(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.add_or_update(_app(applicant_id="10:1", name="Tank-Realm", role="TANK"))
    state.add_or_update(_app(applicant_id="10:2", name="Damage-Realm", role="DAMAGER"))
    state.add_or_update(_app(applicant_id="20:1", name="Healer-Realm", role="HEALER"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        window._pinned_id = "10:1"
        qtbot.mouseClick(
            window._role_filter_bar._buttons["DAMAGER"],
            Qt.MouseButton.LeftButton,
        )

        assert window._pinned_id == "10:1"
        assert window._panel._name_label.text() == "Tank"
    finally:
        client.close()


def test_role_filter_clears_pin_when_pinned_group_is_hidden(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.add_or_update(_app(applicant_id="10:1", name="Tank-Realm", role="TANK"))
    state.add_or_update(_app(applicant_id="20:1", name="Damage-Realm", role="DAMAGER"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        window._pinned_id = "10:1"
        qtbot.mouseClick(
            window._role_filter_bar._buttons["DAMAGER"],
            Qt.MouseButton.LeftButton,
        )

        assert window._pinned_id is None
        assert window._panel._current_applicant is not None
        assert window._panel._current_applicant.applicant_id == "20:1"
    finally:
        client.close()


def test_role_update_clears_hidden_hover_and_pin_under_active_filter(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.add_or_update(_app(applicant_id="42", name="Damage-Realm", role="DAMAGER"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        window._on_role_filter_changed({"DAMAGER"})
        window._hover_id = "42"
        window._hover_by_tab["applicants"] = "42"
        window._pinned_id = "42"
        window._pinned_by_tab["applicants"] = "42"
        window._sync_delegate_and_panel()

        state.add_or_update(_app(applicant_id="42", name="Damage-Realm", role="TANK"))
        window.on_applicant_updated(state.applicants["42"])
        window._flush_overlay_refresh()

        assert window._hover_id is None
        assert window._hover_by_tab["applicants"] is None
        assert window._pinned_id is None
        assert window._pinned_by_tab["applicants"] is None
        assert window._delegate._hover_row == -1
        assert window._delegate._pinned_row == -1
        assert (
            window._panel._status_label.text() == "Hover a row for applicant details."
        )
    finally:
        client.close()


def test_applicant_tab_pin_cache_clears_when_listing_clears_to_party(
    qtbot, tmp_path
):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(_app(applicant_id="42", name="Old-Realm"))
    state.add_or_update_party_member(_app(applicant_id="party", name="Party-Realm"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        window._on_cell_clicked(window._row_for_id["42"], 0)
        assert window._pinned_by_tab["applicants"] == "42"

        state.clear_all()
        state.listing = None
        window.on_cleared()
        window._flush_overlay_refresh()

        assert window._active_tab == "party"
        assert window._pinned_by_tab["applicants"] is None
        assert window._hover_by_tab["applicants"] is None

        state.listing = _listing()
        state.add_or_update(_app(applicant_id="42", name="New-Realm"))
        window._on_source_tab_changed("applicants")

        assert window._pinned_id is None
        assert window._panel._current_applicant is not None
        assert window._panel._current_applicant.applicant_id == "42"
        assert window._panel._name_label.text() == "New"
        assert window._panel._unpin_button.isHidden()
    finally:
        client.close()


def test_role_filter_title_count_uses_visible_group_members(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(_app(applicant_id="10:1", name="Tank-Realm", role="TANK"))
    state.add_or_update(_app(applicant_id="10:2", name="Damage-Realm", role="DAMAGER"))
    state.add_or_update(_app(applicant_id="20:1", name="Healer-Realm", role="HEALER"))
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window._refresh_table()
        window._update_title()
        qtbot.mouseClick(
            window._role_filter_bar._buttons["DAMAGER"],
            Qt.MouseButton.LeftButton,
        )

        assert window._title_bar.title_label.text().endswith("(2 / 3)")
    finally:
        client.close()
