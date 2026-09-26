"""Widget-adjacent smoke tests for table cell adapters."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QTableWidgetItem

from applicant_scout.constants import percentile_colour
from applicant_scout.overlay import (
    COL_FIT,
    COL_ILVL,
    COL_NAME,
    COL_RIO,
    COL_SPEC,
    METRIC_COLUMN_TEXT_PADDING,
    MPLUS_GROUP_LANE_MAX_WIDTH,
    MPLUS_GROUP_LANE_MIN_WIDTH,
    MPLUS_INDIVIDUAL_LANE_MIN_WIDTH,
    NAME_COLUMN_MAX_WIDTH,
    RowWidthInput,
    _bold_cell_font,
    _fit_cell,
    _mplus_dual_cell,
    _raid_dual_cell,
    _set_cell_background,
    _set_cell_foreground,
    _text_colour_for_bg,
    measure_column_width,
)
from applicant_scout.overlay_presenters import rio_display_text
from applicant_scout.scoring import CONTEXT_MPLUS, CandidateFit
from applicant_scout.state import Applicant, Listing


def _app(**overrides) -> Applicant:
    base = Applicant(
        applicant_id="42",
        name="Drathmork-Twisting Nether",
        cls="WARRIOR",
        spec_id=71,
        ilvl=264,
        score=2443,
        main_score=0,
        role="DAMAGER",
        mplus_dps=80.0,
        mplus_dps_median=62.0,
        mplus_dps_breakdown=[
            {
                "name": "Pit of Saron",
                "parse_percent": 100.0,
                "median_percent": 80.0,
                "key_level": 14,
                "run_count": 3,
            }
        ],
        fetch_status="ready",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _mplus_listing() -> Listing:
    return Listing(
        activity_id=401,
        dungeon_name="Pit of Saron",
        listing_name="+14 Pit of Saron",
        comment="",
        key_level=14,
        category_id=2,
        difficulty_id=8,
    )


def test_mplus_dual_cell_uses_visual_boundary():
    item = _mplus_dual_cell(_app())

    assert item.text() == "80/62 +14"
    assert item.background().color().name() == QColor(percentile_colour(80.0)).name()
    assert item.foreground().color().name() == QColor(
        _text_colour_for_bg(percentile_colour(80.0))
    ).name()
    assert item.font().bold()


def test_bold_cell_font_uses_resolved_application_size(qtbot):
    # WHY: the application font resolution below requires pytest-qt's
    # QApplication even though the test does not need a widget interaction.
    assert qtbot is not None
    font = QFont()

    result = _bold_cell_font(font)

    assert result.bold()
    assert result.pointSize() > 0 or result.pixelSize() > 0


def test_mplus_dual_cell_keeps_raw_evidence_for_mplus_listing():
    item = _mplus_dual_cell(_app(), _mplus_listing())

    assert item.text() == "80/62 +14"
    assert item.background().color().name() == QColor(percentile_colour(80.0)).name()
    fit_text = _fit_cell(_app(), _mplus_listing()).text()
    assert fit_text.startswith("~") and fit_text[1:].isdigit()


def test_mplus_dual_cell_listing_error_status_precedes_stale_fit():
    item = _mplus_dual_cell(_app(fetch_status="error"), _mplus_listing())

    assert item.text() == "?"


def _explicit_role_colour(item: QTableWidgetItem, role: Qt.ItemDataRole) -> str | None:
    """Read the stored brush role — the actual delegate paint input.

    WHY: item.foreground().color() reports black even when no ForegroundRole
    is stored, so only the role data proves the contrast colour survives to
    paint (white-on-light regression guard).
    """
    stored = item.data(role)
    if isinstance(stored, QBrush):
        return stored.color().name()
    if isinstance(stored, QColor):
        return stored.name()
    return None


def test_set_cell_foreground_black_writes_explicit_role_on_fresh_item():
    item = QTableWidgetItem("42/38")

    _set_cell_foreground(item, "#000000")

    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#000000"


def test_set_cell_foreground_none_clears_stale_role():
    item = QTableWidgetItem("42/38")
    _set_cell_foreground(item, "#000000")

    _set_cell_foreground(item, None)

    assert item.data(Qt.ItemDataRole.ForegroundRole) is None


def test_set_cell_foreground_same_colour_keeps_role_without_clearing():
    item = QTableWidgetItem("42/38")
    _set_cell_foreground(item, "#000000")

    _set_cell_foreground(item, "#000000")

    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#000000"


def test_set_cell_background_black_writes_explicit_role_on_fresh_item():
    item = QTableWidgetItem("—")

    _set_cell_background(item, "#000000")

    assert _explicit_role_colour(item, Qt.ItemDataRole.BackgroundRole) == "#000000"


def test_set_cell_background_none_clears_stale_role():
    item = QTableWidgetItem("42/38")
    _set_cell_background(item, "#1eff00")

    _set_cell_background(item, None)

    assert item.data(Qt.ItemDataRole.BackgroundRole) is None


def test_raid_dual_cell_orange_stores_dark_text_role():
    item = _raid_dual_cell(97.0, 88.0, "ready")

    assert item.text() == "97/88"
    assert item.background().color().name() == QColor("#ff8000").name()
    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#000000"


def test_raid_dual_cell_green_stores_dark_text_role():
    item = _raid_dual_cell(42.0, 38.0, "ready")

    assert item.text() == "42/38"
    assert item.background().color().name() == QColor("#1eff00").name()
    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#000000"


def test_raid_dual_cell_dark_blue_keeps_white_text_role():
    item = _raid_dual_cell(62.0, 55.0, "ready")

    assert item.background().color().name() == QColor("#0070ff").name()
    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#ffffff"


def test_raid_dual_cell_rerender_light_over_dark_refreshes_text_role():
    item = _raid_dual_cell(62.0, 55.0, "ready")

    rerendered = _raid_dual_cell(97.0, 88.0, "ready", item=item)

    assert rerendered is item
    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#000000"


def test_mplus_dual_cell_orange_stores_dark_text_role():
    item = _mplus_dual_cell(_app(mplus_dps=97.0, mplus_dps_median=88.0))

    assert item.background().color().name() == QColor("#ff8000").name()
    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#000000"


def test_mplus_dual_cell_green_stores_dark_text_role():
    item = _mplus_dual_cell(_app(mplus_dps=42.0, mplus_dps_median=38.0))

    assert item.background().color().name() == QColor("#1eff00").name()
    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#000000"


def test_fit_cell_orange_stores_dark_text_role():
    fit = CandidateFit(context=CONTEXT_MPLUS, score=97.0, display="~97")
    item = _fit_cell(_app(), _mplus_listing(), fit=fit)

    assert item.text() == "~97"
    assert item.background().color().name() == QColor("#ff8000").name()
    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#000000"


def test_fit_cell_green_stores_dark_text_role():
    fit = CandidateFit(context=CONTEXT_MPLUS, score=42.0, display="~42")
    item = _fit_cell(_app(), _mplus_listing(), fit=fit)

    assert item.text() == "~42"
    assert item.background().color().name() == QColor("#1eff00").name()
    assert _explicit_role_colour(item, Qt.ItemDataRole.ForegroundRole) == "#000000"


def test_fit_cell_listing_not_found_can_show_scorecard_fit():
    listing = _mplus_listing()
    item = _fit_cell(
        _app(
            score=3200,
            fetch_status="not_found",
            mplus_dps=None,
            mplus_dps_median=None,
            mplus_dps_breakdown=[],
            rio_profile=True,
            rio_best_key=15,
            rio_best_dungeon_key=14,
            rio_timed_at_or_above=1,
            rio_timed_at_or_above_minus1=8,
            rio_timed_at_or_above_minus2=8,
            rio_completed_at_or_above_minus1=8,
            rio_dungeon_count=8,
            rio_summary_target_key=listing.key_level,
        ),
        listing,
    )

    assert item.text().startswith("~") and item.text()[1:].isdigit()
    assert item.background().style() != 0


def test_rio_display_text_shows_current_and_better_main():
    app = _app()
    app.main_score = 3468

    assert rio_display_text(app) == "2443 [3468]"


def test_rio_display_text_hides_lower_or_equal_main():
    app = _app()
    app.main_score = 2200

    assert rio_display_text(app) == "2443"


def test_measure_column_width_plain_rows_take_max_plus_padding():
    rows = [
        RowWidthInput(text="ab", line_widths=(10,)),
        RowWidthInput(text="abcde", line_widths=(34,)),
    ]

    assert measure_column_width(COL_ILVL, rows) == 34 + METRIC_COLUMN_TEXT_PADDING


def test_measure_column_width_skips_empty_rows():
    assert measure_column_width(COL_ILVL, []) == 0
    assert measure_column_width(COL_ILVL, [RowWidthInput(text="")]) == 0
    assert (
        measure_column_width(
            COL_ILVL, [RowWidthInput(text=""), RowWidthInput(text="x", line_widths=(7,))]
        )
        == 7 + METRIC_COLUMN_TEXT_PADDING
    )


def test_measure_column_width_fit_lane_formula():
    rows = [RowWidthInput(text="ignored", package_width=50, individual_width=30)]
    expected = (
        min(MPLUS_GROUP_LANE_MAX_WIDTH, max(MPLUS_GROUP_LANE_MIN_WIDTH, 50 + 12))
        + max(MPLUS_INDIVIDUAL_LANE_MIN_WIDTH, 30 + 12)
        + 1
    )

    assert measure_column_width(COL_FIT, rows) == expected


def test_measure_column_width_name_caps_at_max():
    rows = [RowWidthInput(text="x" * 50, line_widths=(10_000,))]

    assert measure_column_width(COL_NAME, rows) == NAME_COLUMN_MAX_WIDTH


def test_measure_column_width_spec_icon_adds_icon_lane():
    rows = [RowWidthInput(text="Brm", line_widths=(20,), has_icon=True, icon_width=16)]

    assert measure_column_width(COL_SPEC, rows) == 20 + METRIC_COLUMN_TEXT_PADDING + 16 + 4


def test_measure_column_width_spec_without_icon_has_no_icon_lane():
    rows = [RowWidthInput(text="Brm", line_widths=(20,))]

    assert measure_column_width(COL_SPEC, rows) == 20 + METRIC_COLUMN_TEXT_PADDING


def test_measure_column_width_rio_multiline_ignores_compact_cap():
    rows = [RowWidthInput(text="2443\n3468", line_widths=(40, 42), rio_compact_cap=10)]

    assert measure_column_width(COL_RIO, rows) == 42 + METRIC_COLUMN_TEXT_PADDING


def test_measure_column_width_rio_single_line_applies_compact_cap():
    single = RowWidthInput(text="2443", line_widths=(40,), rio_compact_cap=100)
    no_cap = RowWidthInput(text="2443", line_widths=(40,))

    assert measure_column_width(COL_RIO, [single]) == min(40 + METRIC_COLUMN_TEXT_PADDING, 100)
    assert measure_column_width(COL_RIO, [no_cap]) == 40 + METRIC_COLUMN_TEXT_PADDING
