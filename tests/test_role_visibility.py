"""All enabled raid and M+ columns fit the reported 940-pixel overlay width."""

from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QStyleOptionViewItem

from applicant_scout import overlay, scoring
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Applicant, Listing, RosterMember


class _Client:
    last_quota = None
    region = "EU"

    def quota_reset_remaining_seconds(self):
        return None


def _window(qtbot, tmp_path, tab, group_size, context, *, boundary_values=False):
    state = AppState()
    state.listing = Listing(
        activity_id=401,
        dungeon_name="Raid" if context == "raid" else "Pit of Saron",
        listing_name="Heroic" if context == "raid" else "+16",
        comment="",
        key_level=0 if context == "raid" else 16,
        category_id=3 if context == "raid" else 2,
        difficulty_id=15 if context == "raid" else 8,
    )
    fields = dict(
        applicant_id="1:1",
        name="Tallgrogu-Realm",
        cls="WARRIOR",
        spec_id=73,
        role="TANK",
        ilvl=310,
        score=2700,
        fetch_status="ready",
        raid_normal=87.0,
        raid_normal_median=82.0,
        raid_heroic=67.0,
        raid_heroic_median=62.0,
        raid_mythic=27.0,
        raid_mythic_median=25.0,
        mplus_dps=58.0,
        mplus_dps_median=None,
        mplus_dps_breakdown=[
            dict(
                name="Pit of Saron",
                parse_percent=58.0,
                median_percent=None,
                key_level=16,
                run_count=1,
            )
        ],
    )
    roles = ("TANK", "HEALER", "DAMAGER")
    for index in range(1, group_size + 1):
        role = roles[(index - 1) % len(roles)]
        values = dict(fields)
        values.update(
            applicant_id=f"1:{index}",
            name=f"Tallgrogu{index}-Realm",
            role=role,
            cls="PRIEST" if role == "HEALER" else "WARRIOR",
            spec_id=257 if role == "HEALER" else 73 if role == "TANK" else 71,
            raid_heroic=67.0 + index * 8,
        )
        if boundary_values:
            values.update(
                name="Mmmmmmmmmmmm-Realm",
                raid_normal=100.0,
                raid_normal_median=100.0,
                raid_heroic=100.0,
                raid_heroic_median=100.0,
                raid_mythic=100.0,
                raid_mythic_median=100.0,
                mplus_dps=100.0,
                mplus_dps_median=100.0,
                mplus_dps_breakdown=[
                    dict(
                        name="Pit of Saron",
                        parse_percent=100.0,
                        median_percent=100.0,
                        key_level=99,
                        run_count=2,
                    )
                ],
            )
        if tab == "party":
            member = RosterMember(**values, unit_index=index, is_raid_member=context == "raid")
            state.party_members[member.applicant_id] = member
        else:
            applicant = Applicant(**values)
            state.applicants[applicant.applicant_id] = applicant
    window = overlay.OverlayWindow(
        state,
        _Client(),
        object(),
        tmp_path,
        metric_preferences=MetricPreferences(
            mplus=True, raid_normal=True, raid_heroic=True, raid_mythic=True
        ),
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    window._launch_fetch = lambda _applicant: None
    window._launch_raid_boss_fetch_if_needed = lambda _applicant: False
    window._active_tab = tab
    window._refresh_table()
    return window


@pytest.mark.parametrize("tab", ["applicants", "party"])
@pytest.mark.parametrize("group_size", [1, 2, 3, 4])
@pytest.mark.parametrize("context", ["raid", "mplus"])
def test_all_raid_and_mplus_columns_fit_at_maximum_width_without_hiding_roles(
    qtbot, tmp_path, tab, group_size, context
):
    window = _window(qtbot, tmp_path, tab, group_size, context)
    window.show()
    window.resize(window.maximumWidth(), 700)
    QApplication.processEvents()
    window._refresh_table()
    QApplication.processEvents()
    table = window._table
    viewport = table.viewport()
    bar = table.horizontalScrollBar()

    assert window.width() == window.maximumWidth()
    assert viewport is not None
    assert bar is not None
    assert bar.maximum() == 0
    assert bar.value() == 0
    for column in (
        overlay.COL_SPEC,
        overlay.COL_NAME,
        overlay.COL_N,
        overlay.COL_H,
        overlay.COL_M,
        overlay.COL_MPLUS,
        overlay.COL_FIT,
    ):
        assert not table.isColumnHidden(column)
        rect = table.visualItemRect(table.item(0, column))
        assert viewport.rect().contains(rect), (column, rect, viewport.rect())
    for row in range(group_size):
        assert not table.item(row, overlay.COL_SPEC).icon().isNull()


@pytest.mark.parametrize("context", ["raid", "mplus"])
def test_roles_return_after_narrow_scrolled_table_expands_to_maximum_width(
    qtbot, tmp_path, context
):
    window = _window(qtbot, tmp_path, "applicants", 4, context)
    window.show()
    window.resize(500, 700)
    QApplication.processEvents()
    table = window._table
    bar = table.horizontalScrollBar()
    assert bar is not None and bar.maximum() > 0
    bar.setValue(bar.maximum())
    QApplication.processEvents()
    assert table.visualItemRect(table.item(0, overlay.COL_SPEC)).right() < 0

    window.resize(window.maximumWidth(), 700)
    window._refresh_table()
    QApplication.processEvents()

    assert bar.maximum() == 0
    assert bar.value() == 0
    viewport = table.viewport()
    assert viewport is not None
    assert viewport.rect().contains(table.visualItemRect(table.item(0, overlay.COL_SPEC)))
    assert not table.item(0, overlay.COL_SPEC).icon().isNull()


@pytest.mark.parametrize("context", ["raid", "mplus"])
@pytest.mark.parametrize("boundary_values", [False, True])
def test_all_columns_fit_940_physical_pixels_at_current_windows_scale(
    qtbot, tmp_path, context, boundary_values
):
    window = _window(
        qtbot, tmp_path, "applicants", 4, context, boundary_values=boundary_values
    )
    window.show()
    QApplication.processEvents()
    pixel_ratio = window.devicePixelRatioF()
    target_width = min(round(940 / pixel_ratio), window.maximumWidth())
    window.resize(target_width, round(700 / pixel_ratio))
    QApplication.processEvents()
    window._refresh_table()
    QApplication.processEvents()
    table = window._table
    viewport = table.viewport()
    bar = table.horizontalScrollBar()
    visible_width = sum(
        table.columnWidth(column)
        for column in range(table.columnCount())
        if not table.isColumnHidden(column)
    )

    if boundary_values:
        assert table.item(0, overlay.COL_NAME).text() == "Mmmmmmmmmmmm"
        assert table.item(0, overlay.COL_MPLUS).text() == "100/100 +99"
        for column in (overlay.COL_N, overlay.COL_H, overlay.COL_M):
            assert table.item(0, column).text() == "100/100"
    assert window.width() == target_width
    assert window.width() * pixel_ratio <= 941
    assert viewport is not None
    assert bar is not None
    assert visible_width <= viewport.width(), (
        pixel_ratio, window.width(), viewport.width(), visible_width
    )
    assert bar.maximum() == 0
    assert bar.value() == 0
    for column in range(table.columnCount()):
        if not table.isColumnHidden(column):
            assert viewport.rect().contains(table.visualItemRect(table.item(0, column)))
    assert not table.item(0, overlay.COL_SPEC).icon().isNull()


class _TextRecordingPainter(QPainter):
    def __init__(self, device):
        super().__init__(device)
        self.drawn_text = []

    def drawText(self, *args):
        if args and isinstance(args[-1], str):
            self.drawn_text.append(args[-1])
        return super().drawText(*args)


@pytest.mark.parametrize("context", ["raid", "mplus"])
def test_group_fit_paints_complete_scores_at_940_physical_pixels(
    qtbot, tmp_path, context
):
    window = _window(qtbot, tmp_path, "applicants", 4, context)
    window.show()
    QApplication.processEvents()
    pixel_ratio = window.devicePixelRatioF()
    window.resize(round(940 / pixel_ratio), round(800 / pixel_ratio))
    QApplication.processEvents()
    window._refresh_table()
    QApplication.processEvents()
    table = window._table
    viewport = table.viewport()
    assert viewport is not None
    image = QPixmap(viewport.size())
    painter = _TextRecordingPainter(image)
    expected_groups = set()
    expected_individuals = []
    try:
        for row in range(table.rowCount()):
            item = table.item(row, overlay.COL_FIT)
            expected_groups.add(item.data(overlay.MPLUS_PACKAGE_TEXT_ROLE))
            expected_individuals.append(item.data(overlay.MPLUS_INDIVIDUAL_TEXT_ROLE))
            option = QStyleOptionViewItem()
            option.initFrom(table)
            option.widget = table
            option.rect = table.visualItemRect(item)
            window._delegate.paint(painter, option, table.indexFromItem(item))
    finally:
        painter.end()

    assert expected_groups and None not in expected_groups
    for group_text in expected_groups:
        assert group_text in painter.drawn_text, painter.drawn_text
    for score in set(expected_individuals):
        assert painter.drawn_text.count(score) >= expected_individuals.count(score)
    assert all("…" not in text for text in painter.drawn_text)


@pytest.mark.parametrize("context", ["raid", "mplus"])
def test_content_width_limit_shrinks_when_fewer_metric_columns_are_enabled(
    qtbot, tmp_path, context,
):
    window = _window(qtbot, tmp_path, "applicants", 1, context)
    initial_limit = window.maximumWidth()
    window.apply_metric_preferences(
        MetricPreferences(mplus=True, raid_normal=False, raid_heroic=False, raid_mythic=False),
        refetch_missing=False,
    )
    assert window.maximumWidth() < initial_limit
    assert window.maximumWidth() >= 420
    window.resize(5000, 700)
    window.show()
    QApplication.processEvents()
    assert window._table.horizontalScrollBar().maximum() == 0
    assert not window._table.item(0, overlay.COL_SPEC).icon().isNull()


def test_content_width_limit_accounts_for_long_names_and_valid_boundary_metrics(qtbot, tmp_path):
    compact = _window(qtbot, tmp_path / "compact", "applicants", 1, "mplus")
    boundary = _window(
        qtbot, tmp_path / "boundary", "applicants", 4, "mplus", boundary_values=True,
    )
    assert boundary.maximumWidth() > compact.maximumWidth()
    boundary.show()
    boundary.resize(boundary.maximumWidth(), 700)
    QApplication.processEvents()
    assert boundary._table.horizontalScrollBar().maximum() == 0


@pytest.mark.parametrize("context", ["raid", "mplus"])
def test_content_width_limit_does_not_follow_stretched_columns_or_repeated_refresh(
    qtbot, tmp_path, context,
):
    window = _window(qtbot, tmp_path, "applicants", 4, context)
    window.show()
    QApplication.processEvents()
    natural_limit = window.maximumWidth()
    for requested_width in (5000, 500, 2000, 5000):
        window.resize(requested_width, 700)
        QApplication.processEvents()
        window._refresh_table()
        QApplication.processEvents()
        assert window.maximumWidth() == natural_limit
        assert window.width() == min(requested_width, natural_limit)


@pytest.mark.parametrize("context", ["raid", "mplus"])
def test_content_width_limit_is_stable_for_loading_and_unchanged_updates(qtbot, tmp_path, context):
    window = _window(qtbot, tmp_path, "applicants", 4, context)
    natural_limit = window.maximumWidth()
    rows = tuple(window._state.applicants.values())
    for status in ("loading", "loading", "ready", "ready"):
        window._state.applicants = {
            row.applicant_id: replace(row, fetch_status=status) for row in rows
        }
        window._refresh_table()
        assert window.maximumWidth() == natural_limit



def test_content_width_limit_remeasures_larger_metric_header_font(qtbot, tmp_path):
    window = _window(qtbot, tmp_path, "applicants", 1, "mplus")
    original_limit = window.maximumWidth()
    header = window._table.horizontalHeader()
    assert header is not None
    old_text_width = header.fontMetrics().horizontalAdvance("Heroic")
    header.setStyleSheet("QHeaderView { font-size: 20px; }")
    QApplication.processEvents()
    assert header.fontMetrics().horizontalAdvance("Heroic") > old_text_width
    window._refresh_table()
    assert window.maximumWidth() > original_limit
    window.show()
    window.resize(window.maximumWidth(), 700)
    QApplication.processEvents()
    assert window._table.horizontalScrollBar().maximum() == 0


def test_content_growth_during_reorder_preserves_hover_pin_and_cached_fits(
    qtbot, tmp_path, monkeypatch,
):
    window = _window(qtbot, tmp_path, "applicants", 1, "mplus")
    first = window._state.applicants["1:1"]
    second = replace(first, applicant_id="2:1", name="Bob-Realm", mplus_dps=40.0)
    window._state.applicants[second.applicant_id] = second
    window._refresh_table()
    window.show()
    window.resize(window.maximumWidth(), 700)
    QApplication.processEvents()
    window._on_cell_clicked(window._row_for_id[first.applicant_id], overlay.COL_NAME)
    QApplication.processEvents()
    window._on_cell_entered(window._row_for_id[first.applicant_id], overlay.COL_NAME)
    original_limit = window.maximumWidth()
    assert window.width() == original_limit
    assert window._id_by_row == [first.applicant_id, second.applicant_id]

    cursor_calls = []
    score_calls = []
    fallback_calls = []
    original_candidate_fit = scoring.candidate_fit

    def resolve_new_row_under_cursor():
        cursor_calls.append(window._candidate_fits_for_sync)
        return second.applicant_id

    def counted_candidate_fit(applicant, listing):
        score_calls.append(applicant.applicant_id)
        return original_candidate_fit(applicant, listing)

    def counted_fallback(applicant, listing):
        fallback_calls.append(applicant.applicant_id)
        return original_candidate_fit(applicant, listing)

    monkeypatch.setattr(window, "_resolve_hover_from_cursor", resolve_new_row_under_cursor)
    monkeypatch.setattr(scoring, "candidate_fit", counted_candidate_fit)
    monkeypatch.setattr(overlay, "candidate_fit", counted_fallback)
    window._state.applicants[second.applicant_id] = replace(
        second, name="Mmmmmmmmmmmm-Realm", mplus_dps=99.0,
    )

    # A visible window at its ceiling resizes synchronously inside this batch.
    # Cursor resolution before the new fit cache is supplied would switch rows.
    window._refresh_table()

    assert window.maximumWidth() > original_limit
    assert window.width() == window.maximumWidth()
    assert window._id_by_row == [second.applicant_id, first.applicant_id]
    assert cursor_calls == []
    assert window._hover_id == first.applicant_id
    assert window._pinned_id == first.applicant_id
    assert window._panel._current_applicant is first
    assert score_calls == [second.applicant_id]
    assert fallback_calls == []
    assert window._candidate_fits_for_sync is None
