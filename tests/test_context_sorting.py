"""Automatic applicant sorting follows the visible listing column across updates."""

from dataclasses import replace
from types import SimpleNamespace

from PyQt6.QtCore import Qt

from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.overlay import COL_H, COL_M, COL_MPLUS, COL_N, OverlayWindow
from applicant_scout.state import AppState, Applicant, Listing


def test_listing_changes_sort_visible_percentiles_and_move_indicator(qtbot, tmp_path):
    listing = Listing(
        activity_id=401, dungeon_name="Pit of Saron", listing_name="+16",
        comment="", key_level=16, category_id=2, difficulty_id=8,
    )
    first = Applicant(
        applicant_id="1:1", name="First-Realm", cls="WARRIOR", spec_id=71,
        role="DAMAGER", ilvl=310, score=3000, fetch_status="ready",
        mplus_dps=30, mplus_dps_median=25,
        raid_normal=95, raid_heroic=10, raid_mythic=85,
    )
    second = replace(
        first, applicant_id="2:1", name="Second-Realm", mplus_dps=90,
        raid_normal=20, raid_heroic=80, raid_mythic=5,
    )
    state = AppState()
    state.listing = listing
    state.applicants = {app.applicant_id: app for app in (first, second)}
    client = SimpleNamespace(region="EU", last_quota=None, quota_reset_remaining_seconds=lambda: None)
    window = OverlayWindow(
        state, client, object(), tmp_path,
        metric_preferences=MetricPreferences(
            mplus=True, raid_normal=True, raid_heroic=True, raid_mythic=True,
        ),
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    window._refresh_table()
    window._on_cell_clicked(window._row_for_id[first.applicant_id], 0)
    scenarios = (
        (listing, COL_MPLUS, [second.applicant_id, first.applicant_id]),
        (replace(listing, key_level=0, category_id=3, difficulty_id=14), COL_N,
         [first.applicant_id, second.applicant_id]),
        (replace(listing, key_level=0, category_id=3, difficulty_id=15), COL_H,
         [second.applicant_id, first.applicant_id]),
        (replace(listing, key_level=0, category_id=3, difficulty_id=16), COL_M,
         [first.applicant_id, second.applicant_id]),
        (replace(listing, key_level=0, category_id=2, difficulty_id=14), COL_N,
         [first.applicant_id, second.applicant_id]),
        (replace(listing, key_level=0), COL_MPLUS, [second.applicant_id, first.applicant_id]),
        (listing, COL_MPLUS, [second.applicant_id, first.applicant_id]),
    )
    for current, column, expected in scenarios:
        state.listing = current
        window.on_listing_changed()
        window._flush_overlay_refresh()
        assert window._id_by_row == expected
        header = window._table.horizontalHeader()
        assert header is not None and header.isSortIndicatorShown()
        assert header.sortIndicatorSection() == column
        assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        assert window._pinned_id == first.applicant_id
        assert window._panel._current_applicant is first

    # A later metric result updates order without losing the selected identity.
    first.mplus_dps = 99
    window.on_applicant_updated(first)
    window._flush_overlay_refresh()
    assert window._id_by_row == [first.applicant_id, second.applicant_id]
    assert window._pinned_id == first.applicant_id
    assert window._fetches_in_flight == {}
    assert window._raid_boss_fetches_in_flight == {}
