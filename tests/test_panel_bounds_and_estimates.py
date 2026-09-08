"""Card scrolling, complete narrow values, and honest percentile/estimate display."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QTextDocument
from PyQt6.QtWidgets import QApplication

from applicant_scout import overlay, overlay_presenters as presenters
from applicant_scout.constants import CURRENT_RAID_ENCOUNTERS, percentile_colour
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Applicant, Listing


def _window(qtbot, tmp_path):
    state = AppState()
    state.listing = Listing(
        activity_id=0, dungeon_name="Raid", listing_name="Heroic", comment="",
        key_level=0, category_id=3, difficulty_id=15,
    )
    first = Applicant(
        applicant_id="1:1", name="First-Realm", cls="WARRIOR", spec_id=73,
        role="TANK", ilvl=310, score=2700, fetch_status="ready",
        raid_normal=87.9, raid_normal_median=82.9,
        raid_heroic=67.9, raid_heroic_median=62.9,
        raid_mythic=27.9, raid_mythic_median=25.9,
        mplus_dps=58.9, mplus_dps_median=50.9,
        raid_boss_parses={
            difficulty: [dict(encounter_id=encounter, name=name, overall=100, ilvl=100)
                         for _, encounter, name in CURRENT_RAID_ENCOUNTERS]
            for difficulty in ("N", "H", "M")
        },
        rio_raid_progress={difficulty: dict(boss_kills=[12] * 9) for difficulty in ("N", "H", "M")},
    )
    state.applicants = {"1:1": first, "2:1": replace(first, applicant_id="2:1", name="Second-Realm")}
    client = SimpleNamespace(region="EU", last_quota=None, quota_reset_remaining_seconds=lambda: None)
    window = overlay.OverlayWindow(
        state, client, object(), tmp_path, metric_preferences=MetricPreferences(),
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    window._resolve_hover_from_cursor = lambda: None
    window._launch_fetch = lambda _applicant: None
    window.show()
    window._refresh_table()
    window._on_cell_clicked(0, overlay.COL_NAME)
    QApplication.processEvents()
    return window


@pytest.mark.parametrize("width", [300, 730])
@pytest.mark.parametrize("height", [220, 240, 480])
def test_short_card_scrolls_without_covering_table_or_fighting_resize(qtbot, tmp_path, width, height):
    window = _window(qtbot, tmp_path)
    window.resize(min(width, window.maximumWidth()), height)
    for _ in range(3):
        QApplication.processEvents()
    assert window.height() == height
    scroll = window._panel_scroll
    table = window._table
    scroll_rect = QRect(scroll.mapTo(window, QPoint()), scroll.size())
    table_rect = QRect(table.mapTo(window, QPoint()), table.size())
    assert scroll_rect.bottom() < table_rect.top()
    assert table.height() >= table.horizontalHeader().height() + overlay.APPLICANT_ROW_HEIGHT
    assert window.rect().contains(table_rect)
    assert scroll.verticalScrollBar().maximum() > 0
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    QApplication.processEvents()
    last = window._panel._dungeon_rows[8][3]
    assert scroll.viewport().rect().intersects(QRect(last.mapTo(scroll.viewport(), QPoint()), last.size()))
    assert window.height() == height


def test_card_scroll_resets_for_new_identity_but_not_data_refresh(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    window.resize(300, 300)
    QApplication.processEvents()
    bar = window._panel_scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    previous = bar.value()
    assert previous > 0
    window._refresh_table()
    QApplication.processEvents()
    assert bar.value() == previous
    window._on_cell_clicked(1, overlay.COL_NAME)
    QApplication.processEvents()
    assert bar.value() == 0


def test_narrow_badges_and_stacked_raid_values_keep_complete_numbers(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    window.resize(300, 700)
    QApplication.processEvents()
    panel = window._panel
    for label in panel._metric_labels.values():
        assert label.fontMetrics().horizontalAdvance(label.text()) <= label.contentsRect().width()
    kills, value = panel._dungeon_rows[0][1], panel._dungeon_rows[0][3]
    assert kills.text().splitlines() == ["N×12", "H×12", "M×12"]
    assert all(f"{difficulty}100/100" in value.text() for difficulty in ("N", "H", "M"))
    assert value.text().count("<br>") == 2
    document = QTextDocument()
    document.setDefaultFont(value.font())
    document.setHtml(value.text())
    assert document.idealWidth() <= value.contentsRect().width()
    assert "N 100 / 100" in value.toolTip()
    assert "M 100 / 100" in value.accessibleDescription()
    assert kills in panel.tooltip_widgets()
    assert value in panel.tooltip_widgets()


@pytest.mark.parametrize("percent", [0.0, 24.9, 49.9, 74.9, 94.9, 98.9, 99.9, 100.0])
def test_parse_display_never_rounds_up_into_a_different_colour_band(percent):
    shown = str(int(percent))
    text, _, colour = presenters.raid_cell_visuals(percent, percent, "ready")
    assert text == f"{shown}/{shown}"
    assert colour == percentile_colour(float(shown))
    assert presenters.raid_parse_pair_text(percent, percent) == f"{shown} / {shown}"
    assert presenters.mplus_metric_display_text(percent, percent, 2) == f"{shown}/{shown}"


def test_selected_dungeon_bracket_uses_unrounded_percentile_for_display(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    applicant = replace(
        window._state.applicants["1:1"], mplus_dps_breakdown=[dict(
            name="Skyreach", key_level=20, parse_percent=99.9, median_percent=99.9, run_count=2,
            brackets=[dict(key_level=16, parse_percent=49.9, median_percent=24.9, run_count=2)],
        )],
    )
    listing = replace(window._state.listing, category_id=2, difficulty_id=8, key_level=16, dungeon_name="Skyreach")
    rows = presenters.wcl_dungeon_rows_by_name(applicant, listing)
    row = next(iter(rows.values()))
    assert row["text"] == "49/24"
    assert row["colour"] == percentile_colour(49.9)


def test_fit_estimate_uses_tilde_and_neutral_style_without_muting_wcl(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    fit_item = window._table.item(0, overlay.COL_FIT)
    raid_item = window._table.item(0, overlay.COL_H)
    assert fit_item.text().startswith("~")
    assert fit_item.text()[1:].isdigit()
    assert fit_item.background().color().name() == overlay.FIT_BACKGROUND
    assert raid_item.background().color().name() == percentile_colour(67.9)
    assert window._panel._metric_labels["Fit"].text().startswith("Fit · Heroic: ~")
    assert "estimate" in window._panel._metric_labels["Fit"].accessibleDescription()
