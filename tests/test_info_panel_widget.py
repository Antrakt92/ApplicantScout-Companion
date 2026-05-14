"""Widget tests for the compact applicant scout-card panel."""

from __future__ import annotations

import json
from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel

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
    MPLUS_INDIVIDUAL_BG_ROLE,
    MPLUS_INDIVIDUAL_TEXT_ROLE,
    MPLUS_PACKAGE_BG_ROLE,
    MPLUS_PACKAGE_TEXT_ROLE,
    ApplicantInfoPanel,
    OverlayWindow,
)
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.scoring import PackageFit
from applicant_scout.state import DEFAULT_WINDOW_WIDTH, AppState, Applicant, Listing
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

    name_label, key_label, value_label = panel._dungeon_rows[0]
    assert name_label.text() == "Pit of Saron"
    assert key_label.text() == "+14"
    assert value_label.text() == "100/80"
    assert name_label.width() == DUNGEON_NAME_WIDTH
    assert key_label.width() == DUNGEON_KEY_WIDTH
    assert value_label.width() == DUNGEON_METRIC_WIDTH


def test_panel_renders_current_and_better_main_score(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)

    panel.setApplicantData(_app(main_score=3468))

    assert panel._rio_label.text() == "RIO 2443 [3468]"


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

    _name_label, _key_label, value_label = panel._dungeon_rows[0]
    assert value_label.text() == "83"
    assert percentile_colour(83.0) in value_label.styleSheet()


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
    assert panel._dungeon_rows[0][1].text() == "+12"
    assert panel._dungeon_rows[0][2].text() == "72/60"
    assert panel._dungeon_rows[1][0].text() == "Bad Cache"
    assert panel._dungeon_rows[1][1].text() == "?"
    assert panel._dungeon_rows[1][2].text() == "—"


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


def test_overlay_window_minimum_size_matches_compact_contract(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(AppState(), client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        assert window.minimumSize().width() == DEFAULT_WINDOW_WIDTH
        assert window.minimumSize().height() == 370
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


def test_overlay_constructor_uses_safe_defaults_for_corrupt_window_json(qtbot, tmp_path):
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


def test_title_bar_hide_button_hides_overlay_without_shutdown(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        window.show()
        qtbot.waitUntil(window.isVisible, timeout=1000)

        hide_button = window._title_bar.hide_button
        assert hide_button.text() == "-"
        assert hide_button.toolTip() == "Hide overlay"

        qtbot.mouseClick(hide_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: not window.isVisible(), timeout=1000)

        assert window._state is state
        assert window._wcl_client is client
        assert not client._http.is_closed
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
            window._table.columnWidth(col)
            for col in range(window._table.columnCount())
        ]
        assert sum(widths) == window._table.viewport().width()
        assert window._table.columnWidth(COL_MPLUS) > 88

        initial_mplus_width = window._table.columnWidth(COL_MPLUS)
        window.resize(DEFAULT_WINDOW_WIDTH + 120, window.height())
        QApplication.processEvents()

        resized_widths = [
            window._table.columnWidth(col)
            for col in range(window._table.columnCount())
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
    state.add_or_update(
        _app(applicant_id="10:2", name="Damage-Realm", role="DAMAGER")
    )
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


def test_role_filter_preserves_pin_when_pinned_group_stays_visible(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.add_or_update(_app(applicant_id="10:1", name="Tank-Realm", role="TANK"))
    state.add_or_update(
        _app(applicant_id="10:2", name="Damage-Realm", role="DAMAGER")
    )
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
        assert window._panel._status_label.text() == "Hover a row for applicant details."
    finally:
        client.close()


def test_role_filter_title_count_uses_visible_group_members(qtbot, tmp_path):
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    state = AppState()
    state.listing = _listing()
    state.add_or_update(_app(applicant_id="10:1", name="Tank-Realm", role="TANK"))
    state.add_or_update(
        _app(applicant_id="10:2", name="Damage-Realm", role="DAMAGER")
    )
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
