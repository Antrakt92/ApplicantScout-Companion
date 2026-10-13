"""Selected WCL dungeon population stays consistent across overlay consumers."""

from dataclasses import replace
from types import SimpleNamespace

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QHelpEvent
import pytest

from applicant_scout import overlay
from applicant_scout.constants import MPLUS_ENCOUNTERS, percentile_colour
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Applicant, Listing
from applicant_scout.wcl import CharacterCache


_PREFERENCES = MetricPreferences(mplus=True, raid_normal=False, raid_heroic=False, raid_mythic=False)
_DUNGEON = MPLUS_ENCOUNTERS[0][2]


def _listing(target=10):
    return Listing(0, _DUNGEON, "Target", "", key_level=target, category_id=2)


def _app(aid="1:1", *, selected=90, highest=10):
    bracket = dict(key_level=10, parse_percent=selected, median_percent=selected, run_count=3)
    top = dict(key_level=16, parse_percent=highest, median_percent=highest, run_count=3)
    return Applicant(
        aid, f"Scout{aid.replace(':', '')}-Ravencrest", "WARRIOR", 71, 300, 3000, "DAMAGER",
        mplus_dps=highest, mplus_dps_median=highest,
        mplus_dps_breakdown=[dict(name=_DUNGEON, **top, brackets=[bracket, top])],
        fetch_status="ready", wcl_metric_preferences=_PREFERENCES,
        wcl_availability={"mplus": "available"},
    )


@pytest.fixture
def window(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing()
    app = _app()
    state.applicants = {app.applicant_id: app}
    cache = CharacterCache(tmp_path / "cache")
    client = SimpleNamespace(region="EU", last_quota=None, quota_reset_remaining_seconds=lambda: None)
    result = overlay.OverlayWindow(state, client, cache, tmp_path, metric_preferences=_PREFERENCES)
    qtbot.addWidget(result)
    qtbot.addWidget(result._launcher)
    result._resolve_hover_from_cursor = lambda: None
    result._launch_fetch = lambda _applicant: None
    yield result
    cache.close()


def test_same_selected_dungeon_mean_colours_table_and_panel(window):
    app = window._state.applicants["1:1"]
    second = dict(name=MPLUS_ENCOUNTERS[1][2], key_level=12, parse_percent=70,
                  median_percent=50, run_count=4)
    app.mplus_dps_breakdown.append(second)
    window._pinned_id = app.applicant_id
    window._refresh_table()
    item = window._table.item(window._row_for_id[app.applicant_id], overlay.COL_MPLUS)
    assert item.text() == "80/70 +10\u201312"
    assert item.background().color().name() == percentile_colour(80)
    assert window._panel._metric_labels["M+"].text() == "M+ DPS 80/70 +10\u201312"
    details = [(row[2].text(), row[3].text()) for row in window._panel._dungeon_rows
               if not row[0].isHidden()]
    assert set(details) == {("+10", "90/90"), ("+12", "70/50")}
    assert "Selected logged key range: +10\u201312" in item.toolTip()
    assert "Highest-key overview: 10/10 +16" in item.toolTip()


def test_table_tooltip_uses_translucent_overlay_route(window, monkeypatch):
    window._refresh_table()
    item = window._table.item(0, overlay.COL_MPLUS)
    viewport = window._table.viewport()
    point = window._table.visualItemRect(item).center()
    rendered = []
    monkeypatch.setattr(overlay, "_render_tooltip", lambda parent, text, pos:
                        rendered.append((parent, text, pos)) or True)
    event = QHelpEvent(QEvent.Type.ToolTip, point, QPoint(60, 80))
    assert window.eventFilter(viewport, event)
    assert rendered == [(viewport, item.toolTip(), QPoint(60, 80))]
    assert "Target +10" in rendered[0][1]


def test_logged_key_tooltip_uses_translucent_overlay_route(window, monkeypatch):
    app = window._state.applicants["1:1"]
    window._panel.setApplicantData(app, _listing())
    label = window._panel._dungeon_rows[0][2]
    assert label in window._action_tooltip_widgets
    assert label.text() == "+10"
    assert "selected logged key +10" in label.toolTip()
    rendered = []
    monkeypatch.setattr(overlay, "_render_tooltip", lambda parent, text, pos:
                        rendered.append((parent, text, pos)) or True)
    event = QHelpEvent(QEvent.Type.ToolTip, QPoint(3, 4), QPoint(60, 80))
    assert window.eventFilter(label, event)
    assert rendered == [(label, label.toolTip(), QPoint(60, 80))]


def test_repeated_render_and_refresh_keep_accessible_population_bounded(window):
    window._pinned_id = "1:1"
    window._refresh_table()
    row = window._row_for_id["1:1"]
    app = window._state.applicants["1:1"]
    item = window._table.item(row, overlay.COL_MPLUS)
    initial = item.data(Qt.ItemDataRole.AccessibleDescriptionRole)
    for _ in range(10):
        window._render_row(row, app)
        window._sync_row_accessible_descriptions()
        window._refresh_table()
        assert window._table.item(row, overlay.COL_MPLUS) is item
        assert item.data(Qt.ItemDataRole.AccessibleDescriptionRole) == initial
    assert initial.count("WCL DPS averages") == 1
    assert initial.count("Pinned.") == 1
    assert "Target +10" in initial


@pytest.mark.parametrize("unavailable", ["pending", "loading", "restricted"])
def test_reused_row_clears_context_tooltips_and_accessibility_for_unavailable_evidence(window, unavailable):
    app = window._state.applicants["1:1"]
    window._pinned_id = app.applicant_id
    window._refresh_table()
    row = window._row_for_id[app.applicant_id]
    item = window._table.item(row, overlay.COL_MPLUS)
    assert "Target +10" in item.toolTip()
    app.fetch_status = unavailable
    window._refresh_table()
    assert window._table.item(row, overlay.COL_MPLUS) is item
    assert item.text() == ("\u2026" if unavailable != "restricted" else "\u2014")
    assert item.toolTip() == ""
    assert "Target +10" not in item.data(Qt.ItemDataRole.AccessibleDescriptionRole)
    assert window._panel._metric_labels["M+"].toolTip() == ""
    assert window._panel._metric_labels["M+"].accessibleDescription() == ""


@pytest.mark.parametrize("party", [False, True])
def test_manual_mplus_sort_uses_selected_damage_for_each_surface(window, party):
    high = _app("1:1", selected=90, highest=10)
    low = _app("2:1", selected=60, highest=99)
    if party:
        window._state.applicants.clear()
        window._state.party_members = {low.applicant_id: low, high.applicant_id: high}
        window._active_tab = "party"
        window._manual_target_key = 10
    else:
        window._state.applicants = {low.applicant_id: low, high.applicant_id: high}
    window._set_manual_sort(overlay.COL_MPLUS, True)
    assert window._id_by_row == [high.applicant_id, low.applicant_id]
    assert window._table.item(0, overlay.COL_MPLUS).text() == "90/90 +10"
    assert window._table.item(1, overlay.COL_MPLUS).text() == "60/60 +10"
    window._set_manual_sort(overlay.COL_MPLUS, False)
    assert window._id_by_row == [low.applicant_id, high.applicant_id]


def test_manual_mplus_group_sort_uses_weakest_selected_member_without_splitting(window):
    apps = [_app("10:1", selected=90, highest=99), _app("10:2", selected=30, highest=99),
            _app("20:1", selected=60, highest=10), _app("20:2", selected=50, highest=10)]
    window._state.applicants = {app.applicant_id: app for app in reversed(apps)}
    window._set_manual_sort(overlay.COL_MPLUS, True)
    assert window._id_by_row == ["20:1", "20:2", "10:1", "10:2"]
    assert [window._table.item(index, overlay.COL_MPLUS).text() for index in range(4)] == [
        "60/60 +10", "50/50 +10", "90/90 +10", "30/30 +10",
    ]


def test_unknown_logged_key_and_no_target_do_not_fabricate_key_zero(window):
    app = replace(_app(), mplus_dps_breakdown=[dict(name=_DUNGEON, key_level=0,
                                                  parse_percent=90, median_percent=90, run_count=3)])
    window._panel.setApplicantData(app, _listing(0))
    badge = window._panel._metric_labels["M+"]
    assert badge.text() == "M+ DPS 10/10"
    assert "Maximum logged key: unknown" in badge.toolTip()
    assert "+0" not in badge.toolTip()
    for row in window._panel._dungeon_rows:
        assert "+0" not in row[2].toolTip() and "+0" not in row[3].toolTip()
