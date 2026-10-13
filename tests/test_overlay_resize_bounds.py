"""Content width constrains wide windows without replacing compact saved sizes."""

import ctypes
from ctypes import wintypes
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QHeaderView

from applicant_scout import overlay
from applicant_scout.constants import MPLUS_ENCOUNTERS
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Applicant, Listing, WindowGeometry, save_geometry


_MPLUS = MetricPreferences(mplus=True, raid_normal=False, raid_heroic=False, raid_mythic=False)


def _pump():
    for _ in range(3):
        QApplication.processEvents()


def _needed(window):
    widths = [window._metric_column_required_width(column)
              for column in range(window._table.columnCount())
              if not window._table.isColumnHidden(column)]
    scroll = window._table.verticalScrollBar().sizeHint().width()
    return max(overlay.USER_MIN_WINDOW_WIDTH, sum(widths) + scroll + overlay.WINDOW_CHROME_WIDTH)


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(overlay.OverlayLauncher, "show_at", lambda *_args: None)
    save_geometry(tmp_path, WindowGeometry(20, 20, 320, 700, overlay.WINDOW_GEOMETRY_LAYOUT_VERSION))
    state = AppState()
    state.listing = Listing(0, "Mythic+", "Synthetic", "", key_level=14, category_id=2)
    state.applicants = {"1:1": Applicant(
        "1:1", "VeryLongCharacterName-Ravencrest", "WARRIOR", 73, 310, 4000, "TANK",
        main_score=8000, fetch_status="ready",
        raid_normal=100, raid_normal_median=100, raid_heroic=100,
        raid_heroic_median=100, raid_mythic=100, raid_mythic_median=100,
        mplus_dps=100, mplus_dps_median=100,
        mplus_dps_breakdown=[dict(name=MPLUS_ENCOUNTERS[index][2], key_level=level,
                                   parse_percent=100, median_percent=100, run_count=3)
                               for index, level in enumerate((12, 16))],
    )}
    client = SimpleNamespace(region="EU", last_quota=None, quota_reset_remaining_seconds=lambda: None)
    result = overlay.OverlayWindow(state, client, object(), tmp_path, metric_preferences=_MPLUS)
    qtbot.addWidget(result)
    qtbot.addWidget(result._launcher)
    result._launch_fetch = lambda *_args: None
    result._resolve_hover_from_cursor = lambda: None
    result._foreground_timer.stop()
    result._quota_timer.stop()
    result._stop_foreground_hook()
    result._pinned_id = "1:1"
    result.show()
    result._refresh_table()
    _pump()
    yield result
    result._begin_terminal_shutdown()


def test_saved_compact_width_survives_initial_measurement_and_keeps_full_mplus_column(window):
    assert window.width() == 320
    required = window._metric_column_required_width(overlay.COL_MPLUS)
    assert window._table.columnWidth(overlay.COL_MPLUS) >= required
    assert window.minimumWidth() == 320
    assert window._table.horizontalScrollBar().maximum() > 0
    assert window._table.horizontalHeader().sectionResizeMode(overlay.COL_MPLUS) == QHeaderView.ResizeMode.Interactive


def test_wide_window_protects_actual_visible_data_from_shrink(window):
    window.resize(window.maximumWidth(), window.height())
    _pump()
    expected = min(_needed(window), window.screen().availableGeometry().width(), window.width())
    assert window.minimumWidth() == expected
    window.resize(300, window.height())
    _pump()
    assert window.width() >= expected
    assert window._table.columnWidth(overlay.COL_MPLUS) >= window._metric_column_required_width(overlay.COL_MPLUS)
    assert window._table.horizontalScrollBar().maximum() == 0


def test_partial_widening_preserves_compact_floor_instead_of_ratchet(window):
    baseline = window.minimumWidth()
    assert baseline == 320
    partial = min(350, window.maximumWidth() - 1)
    assert partial < _needed(window)
    window.resize(partial, window.height())
    _pump()
    assert window.minimumWidth() == baseline
    window.resize(320, window.height())
    _pump()
    assert window.width() == 320


def test_window_just_below_intrinsic_width_scrolls_instead_of_shrinking_last_value(window):
    partial = _needed(window) - 40
    window.resize(partial, window.height())
    _pump()
    assert window.width() == partial
    assert window._table.columnWidth(overlay.COL_MPLUS) >= window._metric_column_required_width(overlay.COL_MPLUS)
    assert window._table.horizontalScrollBar().maximum() > 0


def test_unchanged_refresh_does_not_ratchet_partially_widened_compact_floor(window):
    baseline = window.minimumWidth()
    window.resize(350, window.height())
    _pump()
    for _ in range(10):
        window._refresh_table()
        _pump()
    assert window.minimumWidth() == baseline
    window.resize(baseline, window.height())
    _pump()
    assert window.width() == baseline


def test_hidden_metrics_recompute_minimum_without_retaining_content_high_water(window):
    window.apply_metric_preferences(MetricPreferences(), refetch_missing=False)
    window.resize(window.maximumWidth(), window.height())
    _pump()
    previous = window.minimumWidth()
    window.apply_metric_preferences(replace(_MPLUS, mplus=False), refetch_missing=False)
    _pump()
    expected = min(_needed(window), window.screen().availableGeometry().width(), window.width())
    assert window.minimumWidth() == expected
    assert window.minimumWidth() < previous
    assert window._table.isColumnHidden(overlay.COL_MPLUS)


def test_shorter_visible_values_lower_floor_but_keep_existing_maximum_high_water(window):
    window.resize(window.maximumWidth(), window.height())
    _pump()
    previous_min = window.minimumWidth()
    previous_max = window.maximumWidth()
    app = window._state.applicants["1:1"]
    app.mplus_dps = 0
    app.mplus_dps_median = None
    app.mplus_dps_breakdown = []
    window._refresh_table()
    _pump()
    expected = min(_needed(window), window.screen().availableGeometry().width(), window.width())
    assert window.minimumWidth() == expected < previous_min
    assert window.maximumWidth() == previous_max


def test_smaller_available_screen_lowers_native_floor_and_keeps_scrollable_values(window, monkeypatch):
    window.resize(window.maximumWidth(), window.height())
    _pump()
    screen = window.screen()
    available = screen.availableGeometry()
    available.setWidth(400)
    narrowed = SimpleNamespace(availableGeometry=lambda: available,
                               availableGeometryChanged=screen.availableGeometryChanged)
    monkeypatch.setattr(window, "screen", lambda: narrowed)
    window._sync_window_width_limit()
    _pump()
    assert window.minimumWidth() == window.maximumWidth() == window.width() == 400
    assert window._table.columnWidth(overlay.COL_MPLUS) >= window._metric_column_required_width(overlay.COL_MPLUS)
    assert window._table.horizontalScrollBar().maximum() > 0


def test_new_text_and_font_measurements_do_not_force_compact_window_to_grow(window):
    baseline = window.width()
    app = window._state.applicants["1:1"]
    app.rio_previous_score = 9999
    app.rio_previous_season = 0
    window._refresh_table()
    font = window._table.font()
    font.setPointSize(max(16, font.pointSize() + 5))
    window._table.setFont(font)
    _pump()
    assert window.width() == baseline
    assert window.minimumWidth() == baseline
    assert window._table.columnWidth(overlay.COL_MPLUS) >= window._metric_column_required_width(overlay.COL_MPLUS)
    assert window._table.horizontalScrollBar().maximum() > 0


def test_repeated_resize_uses_saved_intrinsic_widths_without_font_measurement(window, monkeypatch):
    measured = []
    original = window._column_font_metrics
    monkeypatch.setattr(window, "_column_font_metrics", lambda font:
                        measured.append(font.toString()) or original(font))
    for _ in range(10):
        window.resize(350, window.height())
        _pump()
        window.resize(320, window.height())
        _pump()
    assert measured == []
    assert window.width() == 320 and window.minimumWidth() == 320


@pytest.mark.real_display
def test_native_bounds_match_measured_floor_in_logical_pixels(window):
    window.resize(window.maximumWidth(), window.height())
    _pump()
    expected = min(_needed(window), window.screen().availableGeometry().width(), window.width())

    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MinMax(ctypes.Structure):
        _fields_ = [("reserved", Point), ("max_size", Point), ("max_position", Point),
                   ("min_track", Point), ("max_track", Point)]

    hwnd = int(window.winId())
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send = user32.SendMessageW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    send.restype = wintypes.LPARAM
    get_dpi = user32.GetDpiForWindow
    get_dpi.argtypes = [wintypes.HWND]
    get_dpi.restype = wintypes.UINT
    native = MinMax()
    send(hwnd, 0x0024, 0, ctypes.addressof(native))
    assert window.minimumWidth() == expected
    assert window.windowHandle().minimumSize().width() == expected
    assert abs(native.min_track.x - round(expected * get_dpi(hwnd) / 96)) <= 3
