"""Pure render-boundary tests for overlay metric helpers.

The top applicant panel is QWidget-based now, so this module intentionally
keeps only helper contracts that are shared by the table and panel.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from PyQt6.QtCore import QRect

from applicant_scout.constants import percentile_colour
from applicant_scout.overlay import (
    COLUMN_WIDTHS,
    NAME_COLUMN_MAX_WIDTH,
    _clamp_rect_to_bounds,
    _metric_text,
    _mplus_cell_visuals,
    _mplus_dungeon_metric_text,
    _mplus_sort_key,
    _normalize_loaded_geometry,
    _raid_cell_visuals,
    _text_colour_for_bg,
)
from applicant_scout.state import (
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    WINDOW_GEOMETRY_LAYOUT_VERSION,
    Applicant,
    Listing,
    WindowGeometry,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "#666666"),
        (24, "#666666"),
        (25, "#1eff00"),
        (49, "#1eff00"),
        (50, "#0070ff"),
        (74, "#0070ff"),
        (75, "#a335ee"),
        (94, "#a335ee"),
        (95, "#ff8000"),
        (98, "#ff8000"),
        (99, "#e268a8"),
        (100, "#e5cc80"),
        (None, "#5d5d5d"),
    ],
)
def test_percentile_colour_matches_wcl_ranking_palette(value, expected):
    assert percentile_colour(value) == expected


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
        raid_mythic=None,
        raid_mythic_median=None,
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


def test_column_width_contract_is_compact():
    assert COLUMN_WIDTHS == [74, 112, 44, 84, 50, 50, 50, 88]
    assert sum(COLUMN_WIDTHS) == 552
    assert NAME_COLUMN_MAX_WIDTH == 126
    assert DEFAULT_WINDOW_WIDTH == 572
    assert WINDOW_GEOMETRY_LAYOUT_VERSION == 5


@pytest.mark.parametrize(
    ("bg", "fg"),
    [
        (percentile_colour(95), "#000000"),
        (percentile_colour(80), "#ffffff"),
        (percentile_colour(60), "#ffffff"),
        (percentile_colour(30), "#000000"),
        (percentile_colour(10), "#ffffff"),
        ("#222222", "#ffffff"),
        (None, "#ffffff"),
        ("bad", "#ffffff"),
    ],
)
def test_text_colour_for_bg_returns_readable_contrast(bg, fg):
    assert _text_colour_for_bg(bg) == fg


def test_raid_cell_visuals_use_percentile_background_and_contrast():
    bg = percentile_colour(91.0)
    assert _raid_cell_visuals(91.0, 78.0, "ready") == (
        "91/78",
        _text_colour_for_bg(bg),
        bg,
    )


def test_metric_text_handles_best_median_and_missing_values():
    assert _metric_text(80.0, 62.0) == "80/62"
    assert _metric_text(80.0, None) == "80"
    assert _metric_text(None, None) == "—"


def test_mplus_cell_visuals_dps_appends_highest_key():
    text, fg, bg = _mplus_cell_visuals(_app(role="DAMAGER"))

    assert text == "80/62 +14"
    assert bg == percentile_colour(80.0)
    assert fg == _text_colour_for_bg(bg)


def test_mplus_cell_visuals_listing_colours_fit_score_with_wcl_palette():
    text, fg, bg = _mplus_cell_visuals(_app(role="DAMAGER"), _mplus_listing())

    score = int(text.split()[1])
    assert text.startswith("Fit ")
    assert text.endswith("+14")
    assert score < 25
    assert bg == percentile_colour(float(score))
    assert fg == _text_colour_for_bg(bg)


def test_mplus_cell_visuals_healer_uses_hps_key_only():
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

    text, _fg, _bg = _mplus_cell_visuals(healer)
    assert text == "85/70 +12"


def test_mplus_cell_visuals_median_missing_keeps_key_suffix():
    text, _fg, _bg = _mplus_cell_visuals(_app(mplus_dps_median=None))
    assert text == "80 +14"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("loading", "…"),
        ("error", "?"),
        ("not_found", "—"),
    ],
)
def test_mplus_cell_visuals_status_cells_omit_key_suffix(status, expected):
    text, _fg, _bg = _mplus_cell_visuals(_app(fetch_status=status))
    assert text == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("loading", "…"),
        ("pending", "…"),
        ("error", "?"),
        ("not_found", "—"),
    ],
)
def test_mplus_cell_visuals_listing_status_precedes_stale_fit(status, expected):
    text, _fg, _bg = _mplus_cell_visuals(
        _app(fetch_status=status),
        _mplus_listing(),
    )

    assert text == expected


def test_mplus_cell_visuals_listing_loading_precedes_scorecard_fallback():
    text, _fg, _bg = _mplus_cell_visuals(
        _app(
            fetch_status="loading",
            mplus_dps=None,
            mplus_dps_median=None,
            mplus_dps_breakdown=[],
        ),
        _mplus_listing(),
    )

    assert text == "…"


def test_mplus_cell_visuals_all_missing_omits_key_suffix():
    text, _fg, _bg = _mplus_cell_visuals(
        _app(mplus_dps=None, mplus_dps_median=None)
    )
    assert text == "—"


def test_mplus_dungeon_metric_text_respects_run_count():
    assert (
        _mplus_dungeon_metric_text(
            {"parse_percent": 100.0, "median_percent": 80.0, "run_count": 3}
        )
        == "100/80"
    )
    assert (
        _mplus_dungeon_metric_text(
            {"parse_percent": 70.0, "median_percent": 50.0, "run_count": 1}
        )
        == "70"
    )
    assert (
        _mplus_dungeon_metric_text(
            {"parse_percent": "nope", "median_percent": "101", "run_count": "x"}
        )
        == "—"
    )
    assert (
        _mplus_dungeon_metric_text(
            {"parse_percent": None, "median_percent": 60.0, "run_count": 2}
        )
        == "—"
    )


def test_mplus_sort_key_puts_malformed_keys_after_valid_keys():
    rows = [
        {"name": "Bad Cache", "key_level": "14.5"},
        {"name": "Valid High", "key_level": 14},
        {"name": "String Safe", "key_level": "12"},
    ]

    assert [row["name"] for row in sorted(rows, key=_mplus_sort_key)] == [
        "Valid High",
        "String Safe",
        "Bad Cache",
    ]


def test_legacy_bloated_geometry_migrates_to_compact_default():
    geo = _normalize_loaded_geometry(WindowGeometry(10, 20, 610, 520, 1))

    assert geo == WindowGeometry(
        10,
        20,
        DEFAULT_WINDOW_WIDTH,
        DEFAULT_WINDOW_HEIGHT,
        WINDOW_GEOMETRY_LAYOUT_VERSION,
    )


def test_v2_saved_wide_geometry_migrates_width_without_forcing_custom_height():
    geo = _normalize_loaded_geometry(WindowGeometry(10, 20, 700, 760, 2))

    assert geo == WindowGeometry(
        10,
        20,
        DEFAULT_WINDOW_WIDTH,
        760,
        WINDOW_GEOMETRY_LAYOUT_VERSION,
    )


def test_v3_saved_compact_width_migrates_to_tighter_default():
    geo = _normalize_loaded_geometry(WindowGeometry(10, 20, 532, 440, 3))

    assert geo == WindowGeometry(
        10,
        20,
        DEFAULT_WINDOW_WIDTH,
        DEFAULT_WINDOW_HEIGHT,
        WINDOW_GEOMETRY_LAYOUT_VERSION,
    )


def test_legacy_custom_large_geometry_is_preserved_and_versioned():
    geo = _normalize_loaded_geometry(WindowGeometry(10, 20, 900, 700, 1))

    assert geo == WindowGeometry(
        10,
        20,
        900,
        700,
        WINDOW_GEOMETRY_LAYOUT_VERSION,
    )


def test_current_geometry_version_is_left_unchanged():
    geo = WindowGeometry(10, 20, 610, 520, WINDOW_GEOMETRY_LAYOUT_VERSION)

    assert _normalize_loaded_geometry(geo) == geo


def test_clamp_rect_shrinks_oversized_visible_geometry_to_available_bounds():
    assert _clamp_rect_to_bounds(40, 50, 5000, 3000, QRect(0, 0, 1920, 1080)) == (
        0,
        0,
        1920,
        1080,
    )


def test_clamp_rect_clamps_position_after_width_shrink_at_right_edge():
    assert _clamp_rect_to_bounds(1800, 20, 400, 300, QRect(0, 0, 1920, 1080)) == (
        1520,
        20,
        400,
        300,
    )


def test_clamp_rect_clamps_position_after_height_shrink_at_bottom_edge():
    assert _clamp_rect_to_bounds(20, 980, 400, 300, QRect(0, 0, 1920, 1080)) == (
        20,
        780,
        400,
        300,
    )


def test_clamp_rect_handles_negative_monitor_coordinates():
    assert _clamp_rect_to_bounds(-2500, 900, 900, 400, QRect(-1920, 0, 1920, 1080)) == (
        -1920,
        680,
        900,
        400,
    )


def test_clamp_rect_leaves_small_launcher_rect_inside_bounds_unchanged():
    assert _clamp_rect_to_bounds(100, 120, 42, 42, QRect(0, 0, 1920, 1080)) == (
        100,
        120,
        42,
        42,
    )
