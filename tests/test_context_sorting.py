"""Automatic target Fit sorting survives context changes and preserves identity."""

from dataclasses import replace
from types import SimpleNamespace

from PySide6.QtCore import Qt

from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.overlay import COL_FIT, COL_MPLUS, OverlayWindow
from applicant_scout.state import AppState, Applicant, Listing


def test_listing_changes_sort_target_fit_and_move_indicator(qtbot, tmp_path):
    listing = Listing(
        activity_id=401, dungeon_name="Pit of Saron", listing_name="+16",
        comment="", key_level=16, category_id=2, difficulty_id=8,
    )
    first = Applicant(
        applicant_id="1:1", name="First-Realm", cls="WARRIOR", spec_id=71,
        role="DAMAGER", ilvl=310, score=3000, fetch_status="ready",
        mplus_dps=30, mplus_dps_median=25,
        rio_dungeons=[{"name": "Pit of Saron", "key_level": 16}],
        raid_normal=95, raid_heroic=10, raid_mythic=85,
    )
    second = replace(
        first, applicant_id="2:1", name="Second-Realm", mplus_dps=90,
        raid_normal=20, raid_heroic=80, raid_mythic=5,
        rio_dungeons=[{"name": "Pit of Saron", "key_level": 12}],
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
        (listing, COL_FIT, [first.applicant_id, second.applicant_id]),
        (replace(listing, key_level=0, category_id=3, difficulty_id=14), COL_FIT,
         [first.applicant_id, second.applicant_id]),
        (replace(listing, key_level=0, category_id=3, difficulty_id=15), COL_FIT,
         [second.applicant_id, first.applicant_id]),
        (replace(listing, key_level=0, category_id=3, difficulty_id=16), COL_FIT,
         [first.applicant_id, second.applicant_id]),
        (replace(listing, key_level=0, category_id=2, difficulty_id=14), COL_FIT,
         [first.applicant_id, second.applicant_id]),
        (replace(listing, key_level=0), COL_MPLUS, [second.applicant_id, first.applicant_id]),
        (listing, COL_FIT, [first.applicant_id, second.applicant_id]),
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

    # A raw WCL update does not change timed readiness or the selected identity.
    second.mplus_dps = 99
    window.on_applicant_updated(second)
    window._flush_overlay_refresh()
    assert window._id_by_row == [first.applicant_id, second.applicant_id]
    assert window._pinned_id == first.applicant_id

    # A stronger timed key does change the default target estimate order.
    second.rio_dungeons = [{"name": "Pit of Saron", "key_level": 20}]
    window.on_applicant_updated(second)
    window._flush_overlay_refresh()
    assert window._id_by_row == [second.applicant_id, first.applicant_id]
    assert window._pinned_id == first.applicant_id
    assert window._fetches_in_flight == {}
    assert window._raid_boss_fetches_in_flight == {}
