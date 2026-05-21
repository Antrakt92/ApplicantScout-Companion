"""Widget-adjacent smoke tests for table cell adapters."""

from __future__ import annotations

from PyQt6.QtGui import QColor, QFont

from applicant_scout.constants import percentile_colour
from applicant_scout.overlay import (
    _bold_cell_font,
    _mplus_dual_cell,
    _rio_display_text,
    _text_colour_for_bg,
)
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
    font = QFont()

    result = _bold_cell_font(font)

    assert result.bold()
    assert result.pointSize() > 0 or result.pixelSize() > 0


def test_mplus_dual_cell_uses_context_fit_for_mplus_listing():
    item = _mplus_dual_cell(_app(), _mplus_listing())

    assert item.text().startswith("Fit ")
    assert item.text().split()[1].isdigit()
    assert not item.text().startswith(("TOP ", "FIT ", "OK ", "RISK "))
    assert "+14" in item.text()


def test_mplus_dual_cell_listing_error_status_precedes_stale_fit():
    item = _mplus_dual_cell(_app(fetch_status="error"), _mplus_listing())

    assert item.text() == "?"


def test_mplus_dual_cell_listing_not_found_can_show_scorecard_fit():
    listing = _mplus_listing()
    item = _mplus_dual_cell(
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

    assert item.text().startswith("Fit ")
    assert "RIO" not in item.text()
    assert "+15" in item.text()


def test_rio_display_text_shows_current_and_better_main():
    app = _app()
    app.main_score = 3468

    assert _rio_display_text(app) == "2443 [3468]"


def test_rio_display_text_hides_lower_or_equal_main():
    app = _app()
    app.main_score = 2200

    assert _rio_display_text(app) == "2443"
