"""Widget-level checks for the Applicants / Party source tabs."""

from __future__ import annotations

import pytest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication

from applicant_scout.__main__ import StateMachine
from applicant_scout.constants import percentile_colour
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.overlay import (
    APPLICANT_ROW_HEIGHT,
    COL_SPEC,
    COL_NAME,
    COL_ILVL,
    COL_N,
    COL_H,
    COL_M,
    COL_MPLUS,
    COL_FIT,
    COL_RIO,
    INFO_PANEL_PREFERRED_HEIGHT,
    METRIC_COLUMN_TEXT_PADDING,
    MPLUS_TARGET_KEY_MAX,
    _mplus_cell_visuals,
    _fit_cell_visuals,
    OverlayWindow,
    USER_MIN_WINDOW_WIDTH,
    WIRE_VERSION_REJECT_MESSAGE,
    WIRE_VERSION_REJECT_THRESHOLD,
)
from applicant_scout.scoring import CONTEXT_RAID, detect_listing_context
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
)
from applicant_scout.state import AppState, Applicant, LeaderKey, Listing, RosterMember


class _FakeWCLClient:
    last_quota = None
    region = "EU"

    def quota_reset_remaining_seconds(self):
        return None


class _FakeCache:
    pass


@pytest.mark.parametrize("selected_tab", ["applicants", "party"])
@pytest.mark.parametrize("event", ["cleared", "roster", "roster_empty", "listing", "added", "updated", "removed"])
def test_selected_source_tab_survives_data_updates(qtbot, tmp_path, selected_tab, event):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _applicant: None
    qtbot.mouseClick(win._tab_bar._buttons[selected_tab], Qt.MouseButton.LeftButton)

    if event == "cleared":
        win.on_cleared()
    elif event == "roster":
        win.on_roster_changed()
    elif event == "roster_empty":
        state.party_members.clear()
        state.listing = _listing()
        win.on_roster_changed()
    elif event == "listing":
        state.listing = _listing()
        win.on_listing_changed()
    elif event in {"added", "updated"}:
        applicant = _app("7:1", "Applicant-Realm")
        state.applicants[applicant.applicant_id] = applicant
        getattr(win, f"on_applicant_{event}")(applicant)
    else:
        win.on_applicant_removed("7:1")
    win._flush_overlay_refresh()

    assert win._active_tab == selected_tab
    assert win._tab_bar._buttons[selected_tab].isChecked()


@pytest.mark.parametrize("empty_cycle", [False, True])
def test_initial_party_selection_survives_listing_and_applicant_updates(qtbot, tmp_path, empty_cycle):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _applicant: None
    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._active_tab == "party"

    if empty_cycle:
        state.party_members.clear()
        win.on_roster_changed()
        win.on_cleared()
        win._flush_overlay_refresh()
        assert win._active_tab == "party"
        state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
        win.on_roster_changed()

    state.listing = _listing()
    win.on_listing_changed()
    applicant = _app("7:1", "Applicant-Realm")
    state.applicants[applicant.applicant_id] = applicant
    win.on_applicant_added(applicant)
    win.on_applicant_updated(applicant)
    win._flush_overlay_refresh()

    assert win._active_tab == "party"
    assert win._id_by_row == ["host-realm"]


def _app(applicant_id: str, name: str, role: str = "DAMAGER") -> Applicant:
    return Applicant(
        applicant_id=applicant_id,
        name=name,
        cls="WARRIOR",
        spec_id=71,
        ilvl=700,
        score=3000,
        role=role,
    )


def _member(
    member_id: str,
    name: str,
    role: str = "DAMAGER",
    *,
    score: int = 3100,
    main_score: int = 0,
) -> RosterMember:
    return RosterMember(
        applicant_id=member_id,
        name=name,
        cls="PRIEST" if role == "HEALER" else "WARRIOR",
        spec_id=257 if role == "HEALER" else 71,
        ilvl=701,
        score=score,
        role=role,
        main_score=main_score,
    )


def _ready_mplus_member(member_id: str = "dps-realm") -> RosterMember:
    member = _member(member_id, "Dps-Realm", "DAMAGER")
    member.fetch_status = "ready"
    member.mplus_dps = 90.0
    member.mplus_dps_median = 80.0
    member.mplus_dps_breakdown = [
        {
            "name": "Pit of Saron",
            "parse_percent": 90.0,
            "median_percent": 80.0,
            "key_level": 10,
            "run_count": 3,
        }
    ]
    return member


def _listing(
    key_level: int = 12,
    *,
    category_id: int = 2,
    difficulty_id: int = 0,
    dungeon_name: str = "Nexus-Point Xenas",
) -> Listing:
    return Listing(
        activity_id=459,
        dungeon_name=dungeon_name,
        listing_name="+12 Competitive",
        comment="",
        key_level=key_level,
        category_id=category_id,
        difficulty_id=difficulty_id,
    )


def _version(player_name: str = "Host-Realm") -> DecodedVersion:
    return DecodedVersion(
        addon_version="0.1.0",
        game_version="12.0.5",
        region_id=3,
        player_name=player_name,
    )


def _decoded_applicant(aid: int, member_idx: int, name: str) -> DecodedApplicant:
    return DecodedApplicant(
        applicant_id=aid,
        member_idx=member_idx,
        name=name,
        spec_id=71,
        class_id=1,
        ilvl=700,
        score=3000,
        main_score=0,
        rio_profile=False,
        rio_best_key=0,
        rio_best_dungeon_key=0,
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
        rio_completed_at_or_above_minus1=0,
        rio_dungeon_count=0,
        rio_dungeons=[],
        role=2,
    )


def _decoded_roster(
    name: str, *, flags: int = 1, score: int = 2443
) -> DecodedRosterMember:
    return DecodedRosterMember(
        unit_index=0,
        flags=flags,
        subgroup=1,
        class_id=1,
        spec_id=71,
        ilvl=701,
        score=score,
        main_score=3468,
        rio_profile=True,
        rio_best_key=0,
        rio_best_dungeon_key=0,
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
        rio_completed_at_or_above_minus1=0,
        rio_dungeon_count=0,
        role=2,
        name=name,
    )


def _window(tmp_path, qtbot, state: AppState) -> OverlayWindow:
    win = OverlayWindow(state, _FakeWCLClient(), _FakeCache(), tmp_path)
    win._launch_fetch = lambda _applicant: None
    win._launch_raid_boss_fetch_if_needed = lambda _applicant: False
    qtbot.addWidget(win)
    qtbot.addWidget(win._launcher)
    return win


def _click_sort_header(qtbot, win, column):
    header = win._table.horizontalHeader()
    point = QPoint(
        header.sectionViewportPosition(column) + header.sectionSize(column) // 2,
        header.height() // 2,
    )
    qtbot.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)


def _sorting_window(qtbot, tmp_path, source):
    state = AppState()
    state.listing = _listing()
    low = _ready_mplus_member("1:1")
    low.name, low.spec_id, low.ilvl, low.score = "Alpha-Realm", 71, 310, 1500
    low.raid_normal, low.raid_heroic, low.raid_mythic, low.mplus_dps = 9, 12, 3, 10
    low.mplus_dps_median = 8
    low.mplus_dps_breakdown = [{"key_level": 5, "parse_percent": 10, "run_count": 3}]
    high = _ready_mplus_member("2:1")
    high.name, high.spec_id, high.ilvl, high.score = "Zeta-Realm", 62, 340, 3300
    high.raid_normal, high.raid_heroic, high.raid_mythic, high.mplus_dps = 99, 88, 90, 95
    high.mplus_dps_median = 90
    high.mplus_dps_breakdown = [{"key_level": 15, "parse_percent": 95, "run_count": 3}]
    target = state.applicants if source == "applicants" else state.party_members
    target.update({low.applicant_id: low, high.applicant_id: high})
    win = _window(tmp_path, qtbot, state)
    win._on_source_tab_changed(source)
    win.resize(1400, 700)
    win.show()
    win._refresh_table()
    return win, state, low, high


@pytest.mark.parametrize("source", ["applicants", "party"])
@pytest.mark.parametrize("column", [COL_SPEC, COL_NAME, COL_ILVL, COL_RIO, COL_FIT, COL_N, COL_H, COL_M, COL_MPLUS])
def test_every_header_sorts_both_directions(qtbot, tmp_path, source, column):
    win, _state, low, high = _sorting_window(qtbot, tmp_path, source)
    first_order = [low.applicant_id, high.applicant_id] if column == COL_NAME else [high.applicant_id, low.applicant_id]

    _click_sort_header(qtbot, win, column)

    assert win._id_by_row == first_order
    header = win._table.horizontalHeader()
    assert header.isSortIndicatorShown()
    assert header.sortIndicatorSection() == column
    assert header.sortIndicatorOrder() == (
        Qt.SortOrder.AscendingOrder if column in {COL_SPEC, COL_NAME} else Qt.SortOrder.DescendingOrder
    )

    _click_sort_header(qtbot, win, column)

    assert win._id_by_row == list(reversed(first_order))
    assert win._active_tab == source


@pytest.mark.parametrize("source", ["applicants", "party"])
def test_manual_sort_survives_refresh_and_keeps_pin_identity(qtbot, tmp_path, source):
    win, state, low, high = _sorting_window(qtbot, tmp_path, source)
    win._on_cell_clicked(win._row_for_id[low.applicant_id], COL_NAME)
    _click_sort_header(qtbot, win, COL_ILVL)
    assert win._id_by_row == [high.applicant_id, low.applicant_id]
    low.ilvl = 350
    state.listing = _listing(key_level=15)
    win.on_listing_changed()
    if source == "party":
        win.on_roster_changed()
    else:
        win.on_applicant_updated(low)
    win._flush_overlay_refresh()

    assert win._id_by_row == [low.applicant_id, high.applicant_id]
    assert win._pinned_id == low.applicant_id
    assert win._table.horizontalHeader().sortIndicatorSection() == COL_ILVL


def test_manual_sort_is_separate_for_each_tab(qtbot, tmp_path):
    win, state, low, high = _sorting_window(qtbot, tmp_path, "applicants")
    state.party_members.update({low.applicant_id: low, high.applicant_id: high})
    _click_sort_header(qtbot, win, COL_NAME)
    win._on_source_tab_changed("party")
    _click_sort_header(qtbot, win, COL_ILVL)
    assert win._id_by_row == [high.applicant_id, low.applicant_id]
    win._on_source_tab_changed("applicants")
    assert win._id_by_row == [low.applicant_id, high.applicant_id]
    assert win._table.horizontalHeader().sortIndicatorSection() == COL_NAME
    win._on_source_tab_changed("party")
    assert win._id_by_row == [high.applicant_id, low.applicant_id]
    assert win._table.horizontalHeader().sortIndicatorSection() == COL_ILVL


def test_spec_sort_separates_same_label_specializations(qtbot, tmp_path):
    win, state, low, high = _sorting_window(qtbot, tmp_path, "party")
    low.spec_id, low.cls = 64, "MAGE"
    high.spec_id, high.cls = 64, "MAGE"
    other = _member("3:1", "FrostDk-Realm", score=2500)
    other.spec_id, other.cls = 251, "DEATHKNIGHT"
    other.fetch_status = "ready"
    state.party_members[other.applicant_id] = other
    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._id_by_row == [high.applicant_id, other.applicant_id, low.applicant_id]

    _click_sort_header(qtbot, win, COL_SPEC)
    assert win._id_by_row == [other.applicant_id, high.applicant_id, low.applicant_id]
    _click_sort_header(qtbot, win, COL_SPEC)
    assert win._id_by_row == [high.applicant_id, low.applicant_id, other.applicant_id]


@pytest.mark.parametrize("column", [COL_FIT, COL_N, COL_H, COL_M, COL_MPLUS])
def test_parse_header_keeps_unavailable_rows_last(qtbot, tmp_path, column):
    win, state, low, high = _sorting_window(qtbot, tmp_path, "applicants")
    unknown = _app("3:1", "Missing-Realm")
    unknown.raid_normal = unknown.raid_heroic = unknown.raid_mythic = unknown.mplus_dps = 100
    unknown.fetch_status = "loading"
    state.applicants[unknown.applicant_id] = unknown
    win.on_applicant_added(unknown)
    win._flush_overlay_refresh()
    _click_sort_header(qtbot, win, column)
    assert win._id_by_row == [high.applicant_id, low.applicant_id, unknown.applicant_id]
    _click_sort_header(qtbot, win, column)
    assert win._id_by_row == [low.applicant_id, high.applicant_id, unknown.applicant_id]


def test_header_sort_keeps_joint_applications_together(qtbot, tmp_path):
    win, state, low, high = _sorting_window(qtbot, tmp_path, "applicants")
    partner = _app("1:2", "Partner-Realm")
    partner.ilvl = 360
    partner.fetch_status = "ready"
    state.applicants[partner.applicant_id] = partner
    win.on_applicant_added(partner)
    win._flush_overlay_refresh()
    _click_sort_header(qtbot, win, COL_ILVL)
    assert win._id_by_row == [high.applicant_id, low.applicant_id, partner.applicant_id]
    _click_sort_header(qtbot, win, COL_ILVL)
    assert win._id_by_row == [low.applicant_id, partner.applicant_id, high.applicant_id]
    assert win._group_size_by_raw["1"] == 2


@pytest.mark.parametrize("column", [COL_N, COL_H, COL_M])
@pytest.mark.parametrize("invalid", [-1, 150])
def test_raid_header_treats_out_of_range_parses_as_missing(qtbot, tmp_path, column, invalid):
    win, state, low, high = _sorting_window(qtbot, tmp_path, "applicants")
    unknown = _app("3:1", "Invalid-Realm")
    unknown.fetch_status = "ready"
    unknown.raid_normal = unknown.raid_heroic = unknown.raid_mythic = invalid
    state.applicants[unknown.applicant_id] = unknown
    win._refresh_table()
    assert win._table.item(win._row_for_id[unknown.applicant_id], column).text() == "—"
    _click_sort_header(qtbot, win, column)
    assert win._id_by_row == [high.applicant_id, low.applicant_id, unknown.applicant_id]
    _click_sort_header(qtbot, win, column)
    assert win._id_by_row == [low.applicant_id, high.applicant_id, unknown.applicant_id]


def test_hidden_manual_sort_column_resumes_when_shown_again(qtbot, tmp_path):
    win, _state, _low, _high = _sorting_window(qtbot, tmp_path, "party")
    _click_sort_header(qtbot, win, COL_MPLUS)
    _click_sort_header(qtbot, win, COL_MPLUS)
    win.apply_metric_preferences(MetricPreferences(mplus=False), refetch_missing=False)
    header = win._table.horizontalHeader()
    assert not header.isSortIndicatorShown()
    win.apply_metric_preferences(MetricPreferences(mplus=True), refetch_missing=False)
    assert header.isSortIndicatorShown()
    assert header.sortIndicatorSection() == COL_MPLUS
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder


@pytest.mark.parametrize("raid", [False, True])
def test_fit_sort_uses_only_the_group_estimate_that_is_displayed(qtbot, tmp_path, raid):
    win, state, low, high = _sorting_window(qtbot, tmp_path, "applicants")
    partner = _app("2:2", "Partner-Realm")
    partner.score = 3300
    partner.fetch_status = "loading"
    state.applicants[partner.applicant_id] = partner
    if raid:
        state.listing = _listing(key_level=0, category_id=3, difficulty_id=15)
        win.on_listing_changed()
    win.on_applicant_added(partner)
    win._flush_overlay_refresh()

    _click_sort_header(qtbot, win, COL_FIT)

    group_text = win._table.item(win._row_for_id[high.applicant_id], COL_FIT).text()
    if raid:
        assert not group_text.startswith("G2")
        assert win._table.item(win._row_for_id[partner.applicant_id], COL_FIT).text() == "…"
        assert win._id_by_row == [low.applicant_id, high.applicant_id, partner.applicant_id]
    else:
        assert group_text.startswith("G2")
        assert win._id_by_row == [high.applicant_id, partner.applicant_id, low.applicant_id]
    _click_sort_header(qtbot, win, COL_FIT)
    assert win._id_by_row == [low.applicant_id, high.applicant_id, partner.applicant_id]


def test_parse_sort_keeps_partially_missing_application_last(qtbot, tmp_path):
    win, state, low, high = _sorting_window(qtbot, tmp_path, "applicants")
    partner = _app("2:2", "Partner-Realm")
    partner.fetch_status = "ready"
    state.applicants[partner.applicant_id] = partner
    win.on_applicant_added(partner)
    win._flush_overlay_refresh()
    for _ in range(2):
        _click_sort_header(qtbot, win, COL_N)
        assert win._id_by_row == [low.applicant_id, high.applicant_id, partner.applicant_id]


@pytest.mark.parametrize(
    "applicants,party,stale", [(0, 0, False), (99, 40, True), (200, 40, True)]
)
@pytest.mark.parametrize("key_level", [0, MPLUS_TARGET_KEY_MAX])
@pytest.mark.real_display
def test_source_controls_fit_minimum_window_and_restore_on_resize(
    qtbot, tmp_path, applicants, party, stale, key_level
):
    state = AppState()
    state.listing = _listing()
    win = _window(tmp_path, qtbot, state)
    bar = win._tab_bar
    win.show()
    bar.set_counts(applicants=applicants, party=party)
    bar.set_applicant_count_stale(stale)
    bar.set_party_count_stale(stale)
    bar.set_target_key(key_level)
    win.resize(300, win.height())
    qtbot.wait(1)

    assert win.width() == win.minimumWidth() == 300
    widgets = [*bar._buttons.values(), bar._key_control]
    previous_right = 0
    for widget in widgets:
        point = widget.mapTo(bar, QPoint())
        assert point.x() >= previous_right
        assert point.x() + widget.width() <= bar.width() - 8
        previous_right = point.x() + widget.width()
    for button in bar._buttons.values():
        assert button.width() >= button.sizeHint().width()
    assert bar._key_spin.width() >= bar._key_spin.minimumSizeHint().width()
    key_editor = bar._key_spin.lineEdit()
    assert key_editor is not None
    assert key_editor.contentsRect().width() >= key_editor.fontMetrics().horizontalAdvance(
        bar._key_spin.text()
    )
    assert bar._key_spin.value() == key_level
    assert str(applicants) in bar._buttons["applicants"].text()
    assert str(party) in bar._buttons["party"].text()
    assert ("?" in bar._buttons["applicants"].text()) == stale
    assert ("?" in bar._buttons["party"].text()) == stale
    assert bar._key_up_button.width() == bar._key_down_button.width() == 24
    assert bar._buttons["applicants"].accessibleName() == "Applicants view"
    if stale:
        assert "last known count" in bar._buttons["applicants"].toolTip()

    win.resize(650, win.height())
    qtbot.wait(1)
    marker = "?" if stale else ""
    assert bar._buttons["applicants"].text() == f"Applicants ({applicants}{marker})"
    assert not bar._key_label.isHidden()
    assert bar._key_spin.width() == 64

    win.resize(300, win.height())
    qtbot.wait(1)
    bar.set_target_key_visible(False)
    assert bar._key_control.isHidden()
    assert bar._key_label.isHidden()
    assert bar._buttons["applicants"].text() == f"Applicants ({applicants}{marker})"
    bar.set_target_key_visible(True)
    qtbot.wait(1)
    assert bar._key_control.mapTo(bar, QPoint()).x() + bar._key_control.width() <= 292


def test_compact_source_key_buttons_and_keyboard_remain_usable(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing()
    win = _window(tmp_path, qtbot, state)
    win.show()
    win.resize(300, win.height())
    bar = win._tab_bar
    qtbot.wait(1)
    bar.set_target_key(12)
    qtbot.mouseClick(bar._key_up_button, Qt.MouseButton.LeftButton)
    assert bar._key_spin.value() == 13
    qtbot.mouseClick(bar._key_down_button, Qt.MouseButton.LeftButton)
    assert bar._key_spin.value() == 12
    qtbot.keyClick(bar._key_spin, Qt.Key.Key_Up)
    assert bar._key_spin.value() == 13
    qtbot.keyClick(bar._key_spin, Qt.Key.Key_Down)
    assert bar._key_spin.value() == 12


def test_window_helper_disables_background_raid_detail_fetch(qtbot, tmp_path):
    state = AppState()
    state.player.full_name = "Host-Ravencrest"
    state.listing = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    applicant = _app("7:1", "Applicant-Realm")
    applicant.fetch_status = "ready"
    state.applicants["7:1"] = applicant
    win = _window(tmp_path, qtbot, state)
    win.apply_metric_preferences(
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        ),
        refetch_missing=False,
    )

    assert win._launch_raid_boss_fetch_if_needed(applicant) is False
    assert win._raid_boss_fetches_in_flight == {}


def test_info_panel_defaults_to_first_visible_party_row(qtbot, tmp_path):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    state.party_members["friend-realm"] = _member(
        "friend-realm", "Friend-Realm", "HEALER"
    )
    win = _window(tmp_path, qtbot, state)

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    win._hover_id = None
    win._pinned_id = None
    win._sync_delegate_and_panel()

    assert win._panel._current_applicant is not None
    assert win._panel._current_applicant.applicant_id == win._id_by_row[0]
    assert win._panel._unpin_button.isHidden()


def test_empty_info_panel_keeps_full_height(qtbot, tmp_path):
    state = AppState()
    win = _window(tmp_path, qtbot, state)

    win._refresh_table()

    assert win._id_by_row == []
    assert win._panel._current_applicant is None
    assert win._panel._status_label.text() == (
        "Select or hover a row for applicant details."
    )
    assert win._panel.target_height() == INFO_PANEL_PREFERRED_HEIGHT


def test_tabs_switch_between_applicants_and_party_rows(qtbot, tmp_path):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    state.party_members["friend-realm"] = _member(
        "friend-realm", "Friend-Realm", "HEALER"
    )
    win = _window(tmp_path, qtbot, state)

    win._refresh_table()

    assert win._active_tab == "applicants"
    assert win._table.rowCount() == 1
    assert win._tab_bar._buttons["applicants"].text() == "Applicants (1)"
    assert win._tab_bar._buttons["party"].text() == "Party (2)"

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    assert win._active_tab == "party"
    assert win._table.rowCount() == 2


def test_applicant_title_and_tab_count_group_applications_not_member_rows(
    qtbot, tmp_path
):
    state = AppState()
    state.listing = _listing(key_level=10, dungeon_name="Mythic+")
    state.applicants["10:1"] = _app("10:1", "Tank-Realm", "TANK")
    state.applicants["10:2"] = _app("10:2", "Damage-Realm", "DAMAGER")
    state.applicants["20:1"] = _app("20:1", "Healer-Realm", "HEALER")
    win = _window(tmp_path, qtbot, state)

    win._refresh_table()
    win._update_title()

    assert win._table.rowCount() == 3
    assert win._tab_bar._buttons["applicants"].text() == "Applicants (2)"
    assert win._title_bar.title_label.text() == "M+ Applicants +10 (2)"


def test_party_tab_role_filter_hides_individual_rows(qtbot, tmp_path):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    state.party_members["friend-realm"] = _member(
        "friend-realm", "Friend-Realm", "HEALER"
    )
    state.party_members["dps-realm"] = _member("dps-realm", "Dps-Realm", "DAMAGER")
    win = _window(tmp_path, qtbot, state)

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(win._role_filter_bar._buttons["HEALER"], Qt.MouseButton.LeftButton)

    visible_rows = [
        row for row in range(win._table.rowCount()) if not win._table.isRowHidden(row)
    ]
    assert len(visible_rows) == 1
    assert win._id_by_row[visible_rows[0]] == "friend-realm"


def test_tabs_preserve_pins_independently(qtbot, tmp_path):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)

    win._refresh_table()
    win._on_cell_clicked(0, 0)
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    win._on_cell_clicked(0, 0)
    qtbot.mouseClick(win._tab_bar._buttons["applicants"], Qt.MouseButton.LeftButton)

    assert win._pinned_by_tab["applicants"] == "7:1"
    assert win._pinned_by_tab["party"] == "host-realm"
    assert win._pinned_id == "7:1"


@pytest.mark.parametrize("interaction", ["hover", "pin", "keyboard"])
@pytest.mark.parametrize("applicants_active", [True, False])
def test_slot_identity_replacement_clears_applicant_interaction_state(
    qtbot,
    tmp_path,
    monkeypatch,
    interaction,
    applicants_active,
):
    state = AppState()
    machine = StateMachine(state)
    machine.apply_snapshot(
        Snapshot(
            listing=DecodedListing(
                activity_id=401,
                dungeon_name="Pit of Saron",
                listing_name="+14",
                comment="",
                key_level=14,
                category_id=2,
            ),
            version=_version(),
            applicants=[
                _decoded_applicant(42, 1, "First-Realm"),
                _decoded_applicant(99, 1, "Stable-Realm"),
            ],
            roster=[_decoded_roster("Host-Realm")],
        )
    )
    win = _window(tmp_path, qtbot, state)
    machine.applicantAdded.connect(win.on_applicant_added)
    machine.applicantUpdated.connect(win.on_applicant_updated)
    machine.applicantRemoved.connect(win.on_applicant_removed)
    win._refresh_table()
    monkeypatch.setattr(win, "_resolve_hover_from_cursor", lambda: None)
    target_row = win._row_for_id["42:1"]

    if interaction == "hover":
        win._on_cell_entered(target_row, 0)
    elif interaction == "pin":
        win._on_cell_clicked(target_row, 0)
    else:
        win._on_table_keyboard_navigated(target_row)

    if not applicants_active:
        qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
        win._on_cell_clicked(0, 0)
        assert win._active_tab == "party"
        assert win._source_tab_initialized
        assert win._pinned_id == "host-realm"
        assert win._pinned_by_tab["party"] == "host-realm"

    machine.apply_snapshot(
        Snapshot(
            listing=DecodedListing(
                activity_id=401,
                dungeon_name="Pit of Saron",
                listing_name="+14",
                comment="",
                key_level=14,
                category_id=2,
            ),
            version=_version(),
            applicants=[
                _decoded_applicant(42, 1, "Second-Realm"),
                _decoded_applicant(99, 1, "Stable-Realm"),
            ],
            roster=[_decoded_roster("Host-Realm")],
        )
    )
    if not applicants_active:
        assert win._active_tab == "party"
        assert win._pinned_id == "host-realm"
    win._flush_overlay_refresh()

    interaction_cache = {
        "hover": win._hover_by_tab,
        "pin": win._pinned_by_tab,
        "keyboard": win._keyboard_by_tab,
    }[interaction]
    assert interaction_cache["applicants"] is None
    if applicants_active:
        active_value = {
            "hover": win._hover_id,
            "pin": win._pinned_id,
            "keyboard": win._keyboard_id,
        }[interaction]
        assert active_value is None
        if interaction == "keyboard":
            assert not win._keyboard_preview_active
    else:
        assert win._active_tab == "party"
        assert win._pinned_id == "host-realm"
        assert win._pinned_by_tab["party"] == "host-realm"


def test_hide_show_clears_inactive_tab_hover_cache(qtbot, tmp_path, monkeypatch):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)

    win.show()
    qtbot.waitUntil(win.isVisible, timeout=1000)
    win._refresh_table()
    win._on_cell_entered(0, 0)
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    assert win._hover_by_tab["applicants"] == "7:1"

    monkeypatch.setattr(win, "_resolve_hover_from_cursor", lambda: None)
    win.hide()
    win.show()
    qtbot.waitUntil(win.isVisible, timeout=1000)
    qtbot.mouseClick(win._tab_bar._buttons["applicants"], Qt.MouseButton.LeftButton)

    assert win._hover_by_tab["applicants"] is None
    assert win._hover_id is None


def test_roster_only_update_prepares_party_tab_without_forcing_overlay_open(
    qtbot, tmp_path
):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    state.party_members["host-realm"].fetch_status = "ready"
    win = _window(tmp_path, qtbot, state)

    win.on_roster_changed()
    win._flush_overlay_refresh()

    assert not win.isVisible()
    assert win._launcher.isVisible()
    assert win._collapsed_to_launcher
    assert win._active_tab == "party"
    assert win._table.rowCount() == 1


def test_listing_created_preserves_initial_party_tab_and_filter(
    qtbot, tmp_path
):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    state.party_members["host-realm"].fetch_status = "ready"
    win = _window(tmp_path, qtbot, state)

    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._active_tab == "party"

    qtbot.mouseClick(win._role_filter_bar._buttons["TANK"], Qt.MouseButton.LeftButton)
    assert win._role_filter == {"TANK"}

    state.listing = _listing()
    win.on_listing_changed()
    win._flush_overlay_refresh()

    assert win._active_tab == "party"
    assert win._id_by_row == ["host-realm"]
    assert win._role_filter == {"TANK"}


def test_party_tab_rio_cell_shows_current_and_main_scores(qtbot, tmp_path):
    state = AppState()
    state.party_members["alt-realm"] = _member(
        "alt-realm",
        "Alt-Realm",
        score=2443,
        main_score=3468,
    )
    win = _window(tmp_path, qtbot, state)

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    assert win._table.item(0, COL_RIO).text() == "2443 [3468]"


@pytest.mark.parametrize("tab", ["applicants", "party"])
def test_rio_cell_shows_season_history_at_minimum_width_and_shrinks_when_missing(
    qtbot, tmp_path, tab
):
    state = AppState()
    if tab == "party":
        player = _member("gladgee-tarrenmill", "Gladgee-TarrenMill", score=2443,
                         main_score=3468)
        state.party_members[player.applicant_id] = player
    else:
        player = _app("42:1", "Gladgee-TarrenMill")
        player.score = 2443
        player.main_score = 3468
        state.applicants[player.applicant_id] = player
    player.rio_previous_score = 3485
    player.rio_previous_season = 0
    player.rio_main_previous_score = 4020
    player.rio_main_previous_season = 0
    player.rio_warband_previous_score = 4024
    player.rio_warband_previous_season = 0
    win = _window(tmp_path, qtbot, state)
    if tab == "party":
        qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    else:
        win._refresh_table()
    win.show()
    win.resize(USER_MIN_WINDOW_WIDTH, max(win.height(), 650))
    QApplication.processEvents()

    item = win._table.item(0, COL_RIO)
    lines = item.text().splitlines()
    assert lines == [
        "2443 [3468]",
        "S1 4024",
    ]
    font_metrics = QFontMetrics(item.font())
    assert all(
        font_metrics.horizontalAdvance(line) + 4 <= win._table.columnWidth(COL_RIO)
        for line in lines
    )
    assert win._table.rowHeight(0) >= font_metrics.lineSpacing() * len(lines)

    player.score = 4300
    win._refresh_table()
    QApplication.processEvents()
    assert win._table.item(0, COL_RIO).text() == "4300"
    assert win._table.rowHeight(0) == APPLICANT_ROW_HEIGHT

    player.score = 2443
    win._refresh_table()
    QApplication.processEvents()
    assert win._table.item(0, COL_RIO).text() == "2443 [3468]\nS1 4024"

    player.rio_previous_score = 0
    player.rio_previous_season = None
    player.rio_main_previous_score = 0
    player.rio_main_previous_season = None
    player.rio_warband_previous_score = 0
    player.rio_warband_previous_season = None
    win._refresh_table()
    QApplication.processEvents()
    assert win._table.item(0, COL_RIO).text() == "2443 [3468]"
    assert win._table.rowHeight(0) == APPLICANT_ROW_HEIGHT


def test_party_rio_tooltip_omits_missing_local_history(qtbot, tmp_path):
    state = AppState()
    member = _member("alt-realm", "Alt-Realm", score=2443)
    member.rio_previous_score = 2876
    member.rio_previous_season = 2
    state.party_members["alt-realm"] = member
    win = _window(tmp_path, qtbot, state)

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    assert win._table.item(0, COL_RIO).text() == "2443\nS3 2876"
    assert win._table.item(0, COL_RIO).toolTip() == "Raider.IO · past S3 ~2876"

    member.rio_previous_score = 3485
    member.rio_previous_season = 0
    member.rio_main_previous_score = 4020
    member.rio_main_previous_season = 0
    member.rio_warband_previous_score = 4024
    member.rio_warband_previous_season = 0
    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._table.item(0, COL_RIO).text() == (
        "2443\nS1 4024"
    )
    assert win._table.item(0, COL_RIO).toolTip() == (
        "Raider.IO · past S1 ~3485 · main past S1 ~4020 · warband past S1 ~4024"
    )

    member.rio_previous_score = 0
    member.rio_previous_season = None
    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._table.item(0, COL_RIO).text() == (
        "2443\nS1 4024"
    )
    assert win._table.item(0, COL_RIO).toolTip() == (
        "Raider.IO · main past S1 ~4020 · warband past S1 ~4024"
    )

    member.rio_main_previous_score = 0
    member.rio_main_previous_season = None
    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._table.item(0, COL_RIO).text() == "2443\nS1 4024"
    assert win._table.item(0, COL_RIO).toolTip() == "Raider.IO · warband past S1 ~4024"

    member.rio_warband_previous_score = 0
    member.rio_warband_previous_season = None
    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._table.item(0, COL_RIO).text() == "2443"
    assert win._table.item(0, COL_RIO).toolTip() == ""


def test_cleared_snapshot_preserves_visible_party_roster(qtbot, tmp_path):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    win._active_tab = "party"
    win.show()

    win.on_cleared()
    win._flush_overlay_refresh()

    assert win.isVisible()
    assert win._active_tab == "party"
    assert win._table.rowCount() == 1


def test_cleared_raid_listing_preserves_party_raid_difficulty(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    member = _ready_mplus_member("host-realm")
    member.name = "Host-Realm"
    member.raid_heroic = 82.0
    member.raid_heroic_median = 82.0
    state.party_members["host-realm"] = member
    win = _window(tmp_path, qtbot, state)
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    win.apply_metric_preferences(
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        ),
        refetch_missing=False,
    )
    win.on_listing_changed()

    state.listing = None
    state.clear_all()
    win.on_cleared()
    win._flush_overlay_refresh()

    listing = win._effective_listing()
    assert listing is not None
    assert detect_listing_context(listing) == CONTEXT_RAID
    assert listing.difficulty_id == 15
    assert win._active_tab == "party"
    assert win._title_bar.title_label.text() == "Party — Manaforge Omega (1)"
    fit_item = win._table.item(0, COL_FIT)
    assert fit_item.text().startswith("~")
    assert fit_item.text()[1:].isdigit()
    assert "Heroic" in win._table.horizontalHeaderItem(COL_FIT).text()
    assert not win._table.isColumnHidden(COL_H)


def test_authoritative_party_replacement_clears_preserved_raid_context(
    qtbot, tmp_path
):
    state = AppState()
    sm = StateMachine(state)
    win = _window(tmp_path, qtbot, state)
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    sm.listingChanged.connect(win.on_listing_changed)
    sm.cleared.connect(win.on_cleared)
    sm.rosterChanged.connect(win.on_roster_changed)

    raid_snapshot = Snapshot(
        listing=DecodedListing(
            activity_id=459,
            key_level=0,
            category_id=3,
            difficulty_id=15,
            dungeon_name="Manaforge Omega",
            listing_name="Heroic raid",
            comment="",
        ),
        version=_version(),
        roster=[_decoded_roster("Host-Realm", flags=3)],
    )
    win.note_decode(raid_snapshot)
    sm.apply_snapshot(raid_snapshot)
    win._flush_overlay_refresh()

    assert win._last_raid_listing is not None
    assert win._party_roster_is_raid()

    party_snapshot = Snapshot(
        listing=None,
        version=_version(),
        roster=[_decoded_roster("Host-Realm", flags=1)],
    )
    win.note_decode(party_snapshot)
    sm.apply_snapshot(party_snapshot)
    win._flush_overlay_refresh()

    assert state.listing is None
    assert not win._party_roster_is_raid()
    assert win._last_raid_listing is None
    assert win._effective_listing() is None
    assert win._title_bar.title_label.text() == "Party (1)"
    assert not win._tab_bar._key_label.isHidden()
    assert not win._tab_bar._key_control.isHidden()


def test_same_small_raid_party_refresh_keeps_preserved_raid_context(qtbot, tmp_path):
    state = AppState()
    sm = StateMachine(state)
    win = _window(tmp_path, qtbot, state)
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    sm.listingChanged.connect(win.on_listing_changed)
    sm.cleared.connect(win.on_cleared)
    sm.rosterChanged.connect(win.on_roster_changed)

    raid_listing = DecodedListing(
        activity_id=459,
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
        listing_name="Heroic raid",
        comment="",
    )
    raid_snapshot = Snapshot(
        listing=raid_listing,
        version=_version(),
        roster=[_decoded_roster("Host-Realm", flags=1)],
    )
    win.note_decode(raid_snapshot)
    sm.apply_snapshot(raid_snapshot)

    delisted_snapshot = Snapshot(
        listing=None,
        version=_version(),
        roster=[_decoded_roster("Host-Realm", flags=1)],
    )
    win.note_decode(delisted_snapshot)
    sm.apply_snapshot(delisted_snapshot)
    win._flush_overlay_refresh()

    assert win._last_raid_listing is not None
    assert win._effective_listing() is not None

    refreshed_snapshot = Snapshot(
        listing=None,
        version=_version(),
        roster=[_decoded_roster("Host-Realm", flags=1, score=2600)],
    )
    win.note_decode(refreshed_snapshot)
    sm.apply_snapshot(refreshed_snapshot)
    win._flush_overlay_refresh()

    assert state.party_members["host-realm"].score == 2600
    assert win._last_authoritative_roster_is_raid is False
    listing = win._effective_listing()
    assert listing is not None
    assert detect_listing_context(listing) == CONTEXT_RAID
    assert listing.difficulty_id == 15
    assert win._title_bar.title_label.text() == "Party — Manaforge Omega (1)"
    assert win._tab_bar._key_label.isHidden()
    assert win._tab_bar._key_control.isHidden()


def test_roster_unavailable_refresh_cannot_commit_raid_to_party_transition(
    qtbot, tmp_path
):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    preserved = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    win._last_raid_listing = preserved
    win._last_authoritative_roster_is_raid = True

    win.note_decode(
        Snapshot(
            listing=None,
            version=_version(),
            roster=[],
            roster_unavailable=True,
        )
    )
    win.on_roster_changed()
    win._flush_overlay_refresh()

    assert win._last_authoritative_roster_is_raid is True
    assert win._last_raid_listing is preserved
    assert win._effective_listing() is preserved
    assert win._title_bar.title_label.text() == "Party — Manaforge Omega (1)"
    assert win._tab_bar._key_label.isHidden()
    assert win._tab_bar._key_control.isHidden()


def test_cleared_snapshot_does_not_carry_applicant_filter_into_party_auto_switch(
    qtbot, tmp_path
):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    state.party_members["host-realm"].fetch_status = "ready"
    win = _window(tmp_path, qtbot, state)
    win._role_filter = {"DAMAGER"}
    win._role_filter_bar._active = {"DAMAGER"}
    win.show()

    win.on_cleared()
    win._flush_overlay_refresh()

    visible_rows = [
        row for row in range(win._table.rowCount()) if not win._table.isRowHidden(row)
    ]
    assert win.isVisible()
    assert win._active_tab == "party"
    assert win._role_filter == set()
    assert win._id_by_row == ["host-realm"]
    assert visible_rows == [0]


def test_cleared_listing_resets_role_filter_before_next_applicant_session(
    qtbot, tmp_path
):
    state = AppState()
    state.listing = _listing()
    state.applicants["7:1"] = _app("7:1", "Healer-Realm", "HEALER")
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _applicant: None
    win._refresh_table()

    qtbot.mouseClick(win._role_filter_bar._buttons["HEALER"], Qt.MouseButton.LeftButton)
    state.clear_all()
    state.listing = None
    win.on_cleared()
    win._flush_overlay_refresh()

    state.listing = _listing()
    state.applicants["8:1"] = _app("8:1", "Dps-Realm", "DAMAGER")
    win.on_listing_changed()
    win.on_applicant_added(state.applicants["8:1"])
    win._flush_overlay_refresh()

    visible_rows = [
        row for row in range(win._table.rowCount()) if not win._table.isRowHidden(row)
    ]
    assert win._role_filter == set()
    assert win._id_by_row == ["8:1"]
    assert visible_rows == [0]
    assert win._title_bar.title_label.text().endswith("(1)")


def test_last_applicant_removed_preserves_visible_applicants_tab(qtbot, tmp_path):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    win.show()

    state.remove("7:1")
    win.on_applicant_removed("7:1")
    win._flush_overlay_refresh()

    assert win.isVisible()
    assert win._active_tab == "applicants"
    assert win._table.rowCount() == 0
    assert win._id_by_row == []


def test_last_applicant_removed_keeps_applicants_tab_while_listing_open(
    qtbot, tmp_path
):
    state = AppState()
    state.listing = _listing()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    win.show()

    state.remove("7:1")
    win.on_applicant_removed("7:1")
    win._flush_overlay_refresh()

    assert win.isVisible()
    assert win._active_tab == "applicants"
    assert win._table.rowCount() == 0
    assert win._id_by_row == []
    assert win._source_tab_initialized


def test_last_applicant_removed_preserves_applicants_tab_and_filter(
    qtbot, tmp_path
):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm", "DAMAGER")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    win = _window(tmp_path, qtbot, state)
    win.show()
    win._refresh_table()

    qtbot.mouseClick(win._role_filter_bar._buttons["DAMAGER"], Qt.MouseButton.LeftButton)
    state.remove("7:1")
    win.on_applicant_removed("7:1")
    win._flush_overlay_refresh()

    visible_rows = [
        row for row in range(win._table.rowCount()) if not win._table.isRowHidden(row)
    ]
    assert win.isVisible()
    assert win._active_tab == "applicants"
    assert win._id_by_row == []
    assert visible_rows == []
    assert win._role_filter == {"DAMAGER"}


def test_new_applicant_preserves_initial_party_tab_and_filter(
    qtbot, tmp_path
):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    state.party_members["host-realm"].fetch_status = "ready"
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _applicant: None

    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._active_tab == "party"

    qtbot.mouseClick(win._role_filter_bar._buttons["TANK"], Qt.MouseButton.LeftButton)
    assert win._role_filter == {"TANK"}

    state.applicants["7:1"] = _app("7:1", "Applicant-Realm", "DAMAGER")
    win.on_applicant_added(state.applicants["7:1"])
    win._flush_overlay_refresh()

    assert win._active_tab == "party"
    assert win._id_by_row == ["host-realm"]
    assert win._role_filter == {"TANK"}


def test_clicking_initial_party_tab_keeps_it_selected(qtbot, tmp_path):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    state.party_members["host-realm"].fetch_status = "ready"
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _applicant: None

    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._active_tab == "party"
    assert win._source_tab_initialized

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    assert win._active_tab == "party"
    assert win._source_tab_initialized

    state.applicants["7:1"] = _app("7:1", "Applicant-Realm", "DAMAGER")
    win.on_applicant_added(state.applicants["7:1"])
    win._flush_overlay_refresh()

    assert win._active_tab == "party"
    assert win._id_by_row == ["host-realm"]


def test_applicant_update_preserves_initial_party_tab(
    qtbot, tmp_path
):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    state.party_members["host-realm"].fetch_status = "ready"
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _applicant: None

    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._active_tab == "party"

    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    win.on_applicant_updated(state.applicants["7:1"])
    win._flush_overlay_refresh()

    assert win._active_tab == "party"
    assert win._id_by_row == ["host-realm"]


def test_new_applicant_does_not_override_manual_party_tab(qtbot, tmp_path):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _applicant: None

    win._refresh_table()
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    assert win._active_tab == "party"
    assert win._source_tab_initialized

    state.applicants["8:1"] = _app("8:1", "New-Realm")
    win.on_applicant_added(state.applicants["8:1"])
    win._flush_overlay_refresh()

    assert win._active_tab == "party"
    assert win._id_by_row == ["host-realm"]


def test_snapshot_removal_then_roster_keeps_applicants_tab_while_listing_open(
    qtbot, tmp_path
):
    state = AppState()
    sm = StateMachine(state)

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version(),
            applicants=[_decoded_applicant(7, 1, "Applicant-Realm")],
            roster=[],
        )
    )
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _member: None
    sm.applicantRemoved.connect(win.on_applicant_removed)
    sm.rosterChanged.connect(win.on_roster_changed)
    win.show()
    win._flush_overlay_refresh()

    sm.apply_snapshot(
        Snapshot(
            listing=_listing(),
            version=_version(),
            applicants=[],
            roster=[_decoded_roster("Host-Realm")],
        )
    )
    win._flush_overlay_refresh()

    assert win.isVisible()
    assert win._active_tab == "applicants"
    assert win._table.rowCount() == 0
    assert win._id_by_row == []
    assert win._source_tab_initialized


def test_empty_roster_update_hides_party_only_overlay(qtbot, tmp_path):
    state = AppState()
    win = _window(tmp_path, qtbot, state)
    win._active_tab = "party"
    win.show()

    win.on_roster_changed()
    win._flush_overlay_refresh()

    assert not win.isVisible()


def test_delayed_roster_update_does_not_carry_applicant_filter_into_party_auto_switch(
    qtbot, tmp_path
):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    state.party_members["host-realm"].fetch_status = "ready"
    win = _window(tmp_path, qtbot, state)
    win._role_filter = {"DAMAGER"}
    win._role_filter_bar._active = {"DAMAGER"}

    win.on_roster_changed()
    win._flush_overlay_refresh()

    visible_rows = [
        row for row in range(win._table.rowCount()) if not win._table.isRowHidden(row)
    ]
    assert win._active_tab == "party"
    assert win._role_filter == set()
    assert win._id_by_row == ["host-realm"]
    assert visible_rows == [0]


def test_empty_roster_preserves_party_tab_when_applicants_remain(
    qtbot, tmp_path
):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm", "DAMAGER")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    win = _window(tmp_path, qtbot, state)
    win._active_tab = "party"
    win._tab_bar.set_active("party", emit=False)
    win.show()
    win._refresh_table()

    state.party_members.clear()
    win.on_roster_changed()
    win._flush_overlay_refresh()

    assert win.isVisible()
    assert win._active_tab == "party"
    assert win._id_by_row == []


def test_empty_roster_clears_party_pin_cache_before_same_member_returns(
    qtbot, tmp_path
):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _member: None

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    win._on_cell_clicked(0, 0)
    assert win._pinned_by_tab["party"] == "host-realm"

    state.party_members.clear()
    win.on_roster_changed()
    win._flush_overlay_refresh()

    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win.on_roster_changed()
    win._flush_overlay_refresh()
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    assert win._pinned_id is None
    assert win._pinned_by_tab["party"] is None
    assert win._panel._current_applicant is not None
    assert win._panel._current_applicant.applicant_id == "host-realm"
    assert win._panel._unpin_button.isHidden()


def test_party_title_keeps_listing_key_context(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    assert win._title_bar.title_label.text() == "Party — Nexus-Point Xenas +12 (1)"


def test_party_title_count_uses_visible_role_filter_rows(qtbot, tmp_path):
    state = AppState()
    state.party_members["tank-realm"] = _member("tank-realm", "Tank-Realm", "TANK")
    state.party_members["heal-realm"] = _member(
        "heal-realm", "Heal-Realm", "HEALER"
    )
    state.party_members["dps-realm"] = _member("dps-realm", "Dps-Realm", "DAMAGER")
    win = _window(tmp_path, qtbot, state)

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(win._role_filter_bar._buttons["HEALER"], Qt.MouseButton.LeftButton)

    visible_rows = [
        row for row in range(win._table.rowCount()) if not win._table.isRowHidden(row)
    ]
    assert len(visible_rows) == 1
    assert win._id_by_row[visible_rows[0]] == "heal-realm"
    assert win._title_bar.title_label.text() == "Party (1 / 3)"


def test_party_title_filter_count_preserves_listing_context(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing()
    state.party_members["tank-realm"] = _member("tank-realm", "Tank-Realm", "TANK")
    state.party_members["heal-realm"] = _member(
        "heal-realm", "Heal-Realm", "HEALER"
    )
    state.party_members["dps-realm"] = _member("dps-realm", "Dps-Realm", "DAMAGER")
    win = _window(tmp_path, qtbot, state)

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(win._role_filter_bar._buttons["HEALER"], Qt.MouseButton.LeftButton)

    assert (
        win._title_bar.title_label.text()
        == "Party — Nexus-Point Xenas +12 (1 / 3)"
    )


def test_party_title_all_roles_selected_uses_total_count(qtbot, tmp_path):
    state = AppState()
    state.party_members["tank-realm"] = _member("tank-realm", "Tank-Realm", "TANK")
    state.party_members["heal-realm"] = _member(
        "heal-realm", "Heal-Realm", "HEALER"
    )
    state.party_members["dps-realm"] = _member("dps-realm", "Dps-Realm", "DAMAGER")
    win = _window(tmp_path, qtbot, state)

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    for role in ("TANK", "HEALER", "DAMAGER"):
        qtbot.mouseClick(win._role_filter_bar._buttons[role], Qt.MouseButton.LeftButton)

    assert win._title_bar.title_label.text() == "Party (3)"


def test_target_key_control_defaults_to_listing_key(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing(key_level=12)
    win = _window(tmp_path, qtbot, state)

    win._update_title()

    assert win._tab_bar._key_spin.value() == 12
    assert win._tab_bar._key_control.width() >= 112
    assert win._tab_bar._key_label.font().bold()
    assert win._tab_bar._key_spin.font().bold()
    assert not win._tab_bar._key_up_button.isHidden()
    assert not win._tab_bar._key_down_button.isHidden()
    assert win._tab_bar._key_up_button.text() == "+"
    assert win._tab_bar._key_down_button.text() == "−"


def test_target_key_control_hides_for_raid_contexts(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    win = _window(tmp_path, qtbot, state)

    win._update_title()

    assert win._tab_bar._key_label.isHidden()
    assert win._tab_bar._key_control.isHidden()

    state.listing = _listing(key_level=12)
    win.on_listing_changed()
    win._flush_overlay_refresh()

    assert not win._tab_bar._key_label.isHidden()
    assert not win._tab_bar._key_control.isHidden()

    member = _ready_mplus_member()
    member.is_raid_member = True
    state.listing = None
    state.party_members["dps-realm"] = member
    win.on_listing_changed()
    win.on_roster_changed()
    win._flush_overlay_refresh()
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    assert win._tab_bar._key_label.isHidden()
    assert win._tab_bar._key_control.isHidden()


def test_target_key_down_button_overrides_known_mplus_listing_without_collapsing(
    qtbot, tmp_path, monkeypatch
):
    state = AppState()
    state.listing = _listing(key_level=12)
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)
    win.restore_from_launcher()
    win._update_title()
    sync_calls = []
    original_sync = win._sync_delegate_and_panel

    def record_sync() -> None:
        sync_calls.append(True)
        original_sync()

    monkeypatch.setattr(win, "_sync_delegate_and_panel", record_sync)

    qtbot.mouseClick(win._tab_bar._key_down_button, Qt.MouseButton.LeftButton)

    assert win._manual_target_key == 11
    assert win._tab_bar._key_spin.value() == 11
    assert not win._collapsed_to_launcher
    assert len(sync_calls) == 1
    listing = win._effective_listing()
    assert listing is not None
    assert listing.key_level == 11


def test_manual_target_key_creates_effective_party_listing(qtbot, tmp_path):
    state = AppState()
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)

    assert win._effective_listing() is None

    win._tab_bar._key_spin.setValue(10)

    listing = win._effective_listing()
    assert listing is not None
    assert listing.key_level == 10
    assert listing.dungeon_name == "Mythic+"


def test_leader_key_creates_effective_party_listing(qtbot, tmp_path):
    state = AppState()
    state.leader_key = LeaderKey(
        key_level=17,
        challenge_map_id=249,
        player_name="Leader-Realm",
    )
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)

    listing = win._effective_listing()

    assert listing is not None
    assert listing.key_level == 17
    assert listing.dungeon_name == "Kings' Rest"
    win._active_tab = "party"
    win._update_title()
    assert win._title_bar.title_label.text() == "Party — Kings' Rest +17 (1)"
    assert win._tab_bar._key_spin.value() == 17


def test_leader_key_above_30_remains_effective_and_visible(qtbot, tmp_path):
    state = AppState()
    state.leader_key = LeaderKey(
        key_level=31,
        challenge_map_id=249,
        player_name="Leader-Realm",
    )
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)

    listing = win._effective_listing()

    assert listing is not None
    assert listing.key_level == 31
    win._active_tab = "party"
    win._update_title()
    assert win._title_bar.title_label.text() == "Party — Kings' Rest +31 (1)"
    assert win._tab_bar._key_spin.value() == 31


def test_unknown_leader_challenge_map_keeps_generic_party_listing(qtbot, tmp_path):
    state = AppState()
    state.leader_key = LeaderKey(
        key_level=17,
        challenge_map_id=503,
        player_name="Leader-Realm",
    )
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)

    listing = win._effective_listing()

    assert listing is not None
    assert listing.key_level == 17
    assert listing.dungeon_name == "Mythic+"
    win._update_title()
    assert win._tab_bar._key_spin.value() == 17


def test_manual_target_key_overrides_leader_key(qtbot, tmp_path):
    state = AppState()
    state.leader_key = LeaderKey(
        key_level=17,
        challenge_map_id=249,
        player_name="Leader-Realm",
    )
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)

    win._tab_bar._key_spin.setValue(16)

    listing = win._effective_listing()
    assert listing is not None
    assert win._manual_target_key == 16
    assert listing.key_level == 16
    assert listing.dungeon_name == "Kings' Rest"
    assert win._tab_bar._key_spin.value() == 16


def test_manual_target_key_above_30_is_preserved_and_steps(qtbot, tmp_path):
    state = AppState()
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)

    win._tab_bar._key_spin.setValue(31)

    listing = win._effective_listing()
    assert listing is not None
    assert listing.key_level == 31
    assert win._manual_target_key == 31
    assert win._tab_bar._key_spin.value() == 31

    qtbot.mouseClick(win._tab_bar._key_up_button, Qt.MouseButton.LeftButton)

    listing = win._effective_listing()
    assert listing is not None
    assert listing.key_level == 32
    assert win._manual_target_key == 32
    assert win._tab_bar._key_spin.value() == 32


def test_manual_target_key_recomputes_party_mplus_cells(qtbot, tmp_path):
    state = AppState()
    member = _ready_mplus_member()
    state.party_members["dps-realm"] = member
    win = _window(tmp_path, qtbot, state)
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    legacy_text = win._table.item(0, 7).text()
    win._tab_bar._key_spin.setValue(10)
    win._refresh_table()
    expected, _fg, _bg = _mplus_cell_visuals(member, win._effective_listing())

    assert legacy_text == "90/80 +10"
    assert win._table.item(0, 7).text() == expected
    assert win._table.item(0, 7).text() == legacy_text
    fit_text, _fg, _bg = _fit_cell_visuals(member, win._effective_listing())
    assert win._table.item(0, COL_FIT).text() == fit_text
    assert "+10" in win._table.horizontalHeaderItem(COL_FIT).text()


def test_manual_target_key_recomputes_pinned_party_evidence_text(qtbot, tmp_path):
    state = AppState()
    member = _ready_mplus_member()
    state.party_members[member.applicant_id] = member
    win = _window(tmp_path, qtbot, state)
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    win._pinned_id = member.applicant_id
    win._pinned_by_tab["party"] = member.applicant_id

    win._tab_bar._key_spin.setValue(10)
    win._refresh_table()
    win._sync_delegate_and_panel()
    assert "Target +10" in win._panel._metric_labels["Fit"].toolTip()
    assert win._panel._status_label.isHidden()

    win._tab_bar._key_spin.setValue(12)
    assert win._manual_target_key == 12
    assert win._effective_listing() is not None
    assert win._effective_listing().key_level == 12
    win._refresh_table()
    assert win._manual_target_key == 12
    assert win._effective_listing() is not None
    assert win._effective_listing().key_level == 12
    win._sync_delegate_and_panel()
    assert win._panel._current_listing is not None
    assert win._panel._current_listing.key_level == 12
    assert "Target +12" in win._panel._mplus_fit_status_text(
        member, win._effective_listing()
    )
    assert "Target +12" in win._panel._metric_labels["Fit"].toolTip()
    assert "Target +10" not in win._panel._metric_labels["Fit"].toolTip()
    assert win._panel._status_label.isHidden()


def test_listing_change_recomputes_party_mplus_cells(qtbot, tmp_path):
    state = AppState()
    member = _ready_mplus_member()
    state.party_members["dps-realm"] = member
    win = _window(tmp_path, qtbot, state)
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    legacy_text = win._table.item(0, 7).text()
    state.listing = _listing(key_level=10)
    win.on_listing_changed()
    win._flush_overlay_refresh()

    expected, _fg, _bg = _mplus_cell_visuals(member, win._effective_listing())
    assert legacy_text == "90/80 +10"
    assert win._table.item(0, 7).text() == expected
    assert win._table.item(0, 7).text() == legacy_text
    fit_text, _fg, _bg = _fit_cell_visuals(member, win._effective_listing())
    assert win._table.item(0, COL_FIT).text() == fit_text
    assert "+10" in win._table.horizontalHeaderItem(COL_FIT).text()


def test_real_listing_key_clears_manual_party_target_key(qtbot, tmp_path):
    state = AppState()
    member = _ready_mplus_member()
    state.party_members["dps-realm"] = member
    win = _window(tmp_path, qtbot, state)
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    win._tab_bar._key_spin.setValue(10)

    state.listing = _listing(key_level=12)
    win.on_listing_changed()
    win._flush_overlay_refresh()

    listing = win._effective_listing()
    assert win._manual_target_key is None
    assert win._tab_bar._key_spin.value() == 12
    assert listing is not None
    assert listing.key_level == 12
    expected, _fg, _bg = _mplus_cell_visuals(member, listing)
    assert win._table.item(0, 7).text() == expected


def test_manual_target_key_does_not_override_raid_listing(qtbot, tmp_path):
    state = AppState()
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)
    win._tab_bar._key_spin.setValue(10)

    state.listing = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    win.on_listing_changed()
    win._flush_overlay_refresh()

    listing = win._effective_listing()
    assert win._manual_target_key is None
    assert listing is not None
    assert listing.key_level == 0
    assert detect_listing_context(listing) == CONTEXT_RAID


def test_raid_listing_separates_fit_from_coloured_raw_parses(
    qtbot, tmp_path
):
    state = AppState()
    state.listing = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    applicant = _ready_mplus_member("7:1")
    applicant.applicant_id = "7:1"
    applicant.name = "Applicant-Realm"
    applicant.mplus_dps = 44.0
    applicant.mplus_dps_median = None
    applicant.mplus_dps_breakdown = [
        {
            "name": "Pit of Saron",
            "parse_percent": 44.0,
            "median_percent": None,
            "key_level": 18,
            "run_count": 1,
        }
    ]
    state.applicants["7:1"] = applicant
    win = _window(tmp_path, qtbot, state)
    applicant.raid_heroic = 82.0
    applicant.raid_heroic_median = 82.0
    win.apply_metric_preferences(
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        ),
        refetch_missing=False,
    )

    win._refresh_table()

    fit_item = win._table.item(0, COL_FIT)
    assert fit_item.text().startswith("~")
    assert fit_item.text()[1:].isdigit()
    assert fit_item.background().color().name() == percentile_colour(int(fit_item.text()[1:]))
    assert "82/82" in win._table.item(0, COL_H).text()
    assert win._table.item(0, COL_MPLUS).text() == "44 +18"
    _text, _fg, mplus_bg = _mplus_cell_visuals(applicant, win._effective_listing())
    assert mplus_bg is not None


def test_raid_fit_column_expands_to_fit_rendered_text(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    applicant = _ready_mplus_member("7:1")
    applicant.applicant_id = "7:1"
    applicant.name = "Applicant-Realm"
    state.applicants["7:1"] = applicant
    win = _window(tmp_path, qtbot, state)
    applicant.raid_heroic = 82.0
    applicant.raid_heroic_median = 82.0
    win.apply_metric_preferences(
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        ),
        refetch_missing=False,
    )

    win._refresh_table()

    item = win._table.item(0, COL_FIT)
    required = (
        QFontMetrics(item.font()).horizontalAdvance(item.text())
        + METRIC_COLUMN_TEXT_PADDING
    )
    assert item.text().startswith("~")
    assert item.text()[1:].isdigit()
    assert "82/82" == win._table.item(0, COL_H).text()
    assert win._table.columnWidth(COL_FIT) >= required


def test_raid_listing_forces_disabled_target_column_with_estimated_fit(
    qtbot, tmp_path
):
    state = AppState()
    state.listing = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    applicant = _app("7:1", "Applicant-Realm")
    applicant.fetch_status = "ready"
    state.applicants["7:1"] = applicant
    win = _window(tmp_path, qtbot, state)
    applicant.raid_mythic = 70.0
    applicant.raid_mythic_median = 60.0
    win.apply_metric_preferences(
        MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=True,
        ),
        refetch_missing=False,
    )

    win._refresh_table()

    assert not win._table.isColumnHidden(COL_H)
    fit_item = win._table.item(0, COL_FIT)
    assert fit_item.text().startswith("~")
    assert fit_item.text()[1:].isdigit()
    assert fit_item.background().color().name() == percentile_colour(int(fit_item.text()[1:]))
    assert win._table.item(0, COL_H).text() == "—"
    assert win._table.item(0, COL_M).text() == "70/60"
    win._on_cell_clicked(0, COL_FIT)
    fit_badge = win._panel._metric_labels["Fit"]
    assert fit_badge.text().startswith("Fit · Heroic: Estimate ~")
    assert "Target: Heroic" in fit_badge.toolTip()
    assert "M 70/60" in fit_badge.toolTip()


def test_raid_listing_target_column_keeps_loading_state(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    applicant = _app("7:1", "Applicant-Realm")
    applicant.fetch_status = "loading"
    state.applicants["7:1"] = applicant
    win = _window(tmp_path, qtbot, state)
    win._metric_preferences = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    win._panel.set_metric_preferences(win._metric_preferences)
    win._apply_metric_column_visibility()

    win._refresh_table()

    assert win._table.item(0, COL_H).text() == "…"
    assert win._table.item(0, COL_MPLUS).text() == "…"


def test_raid_group_target_column_waits_for_ready_members(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing(
        key_level=0,
        category_id=3,
        difficulty_id=15,
        dungeon_name="Manaforge Omega",
    )
    leader = _app("7:1", "Leader-Realm", "TANK")
    follower = _app("7:2", "Follower-Realm", "DAMAGER")
    leader.fetch_status = "loading"
    follower.fetch_status = "loading"
    state.applicants["7:1"] = leader
    state.applicants["7:2"] = follower
    win = _window(tmp_path, qtbot, state)
    win._metric_preferences = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    win._panel.set_metric_preferences(win._metric_preferences)
    win._apply_metric_column_visibility()

    win._refresh_table()

    assert win._table.item(0, COL_H).text() == "…"
    assert win._table.item(1, COL_H).text() == "…"


def test_empty_roster_clears_manual_target_key(qtbot, tmp_path):
    state = AppState()
    win = _window(tmp_path, qtbot, state)
    win._tab_bar._key_spin.setValue(10)

    win.on_roster_changed()
    win._flush_overlay_refresh()

    assert win._manual_target_key is None
    assert win._tab_bar._key_spin.value() == 0


@pytest.mark.parametrize(
    "main_score,main_season,warband_score,warband_season,expected",
    [
        (3590, 0, 0, None, "S1 3590"),
        (4210, 0, 4024, 0, "S1 4210"),
        (4020, 0, 4024, 0, "S1 4024"),
        (4210, 1, 4024, 0, "S2 4210"),
        (4210, 0, 4210, 1, "S2 4210"),
        (9999, None, 4024, 0, "S1 4024"),
        (0, None, 0, None, ""),
    ],
)
def test_rio_table_uses_highest_main_or_warband_history(
    main_score, main_season, warband_score, warband_season, expected
):
    from applicant_scout.overlay_presenters import rio_table_text

    member = _member("alt-realm", "Alt-Realm", score=3126)
    member.rio_main_previous_score = main_score
    member.rio_main_previous_season = main_season
    member.rio_warband_previous_score = warband_score
    member.rio_warband_previous_season = warband_season
    assert rio_table_text(member) == "3126" + ("\n" + expected if expected else "")


@pytest.mark.parametrize(
    "personal_score,personal_season,main_score,warband_score,expected",
    [
        (3240, 0, 0, 0, "S1 3240"),
        (2781, 0, 0, 0, "S1 2781"),
        (4500, 1, 4210, 4024, "S2 4500"),
        (2781, 0, 4210, 4024, "S1 4210"),
        (2781, 0, 4020, 4024, "S1 4024"),
        (9999, None, 4020, 4024, "S1 4024"),
    ],
)
def test_rio_table_considers_personal_history_with_main_and_warband(
    personal_score, personal_season, main_score, warband_score, expected
):
    from applicant_scout.overlay_presenters import rio_table_text

    member = _member("alt-realm", "Alt-Realm", score=2772)
    member.rio_previous_score = personal_score
    member.rio_previous_season = personal_season
    member.rio_main_previous_score = main_score
    member.rio_main_previous_season = 0
    member.rio_warband_previous_score = warband_score
    member.rio_warband_previous_season = 0
    assert rio_table_text(member) == "2772\n" + expected


@pytest.mark.parametrize(
    "current_score,history_score,expected",
    [
        (3215, 2824, "3215"),
        (3215, 3215, "3215\nS1 3215"),
        (3215, 3500, "3215\nS1 3500"),
        (0, 2824, "[4000]\nS1 2824"),
    ],
)
def test_rio_table_hides_history_below_current_character_score(
    current_score, history_score, expected
):
    from applicant_scout.overlay_presenters import rio_table_text

    member = _member("alt-realm", "Alt-Realm", score=current_score)
    member.rio_previous_score = history_score
    member.rio_previous_season = 0
    if not current_score:
        member.main_score = 4000
    assert rio_table_text(member) == expected


def test_rio_history_keeps_seasons_beyond_s4():
    from applicant_scout.overlay_presenters import rio_history_text, rio_table_text

    member = _member("alt-realm", "Alt-Realm", score=3000)
    member.rio_previous_score = 2876
    member.rio_previous_season = 4
    assert rio_history_text(member) == "past S5 ~2876"
    assert rio_table_text(member) == "3000"


def test_consecutive_wire_version_rejects_surface_companion_update_banner(
    qtbot, tmp_path
):
    """Unknown wire versions page the health chip after a short streak.

    addon_version_warning stays silent when the version block never decodes,
    so the overlay counts wire-version rejects: below the threshold the
    generic "Shot failed" chip is preserved; at the threshold the chip
    carries the update-companion guidance; any successfully decoded frame
    resets the streak.
    """
    assert WIRE_VERSION_REJECT_THRESHOLD == 3
    win = _window(tmp_path, qtbot, AppState())
    reason = "hex: unsupported wire version 0x0c"

    for _ in range(WIRE_VERSION_REJECT_THRESHOLD - 1):
        win.note_decode_failed("WoWScrnShot_0001.jpg", reason)
        assert win._health_label.text() == "Shot failed"
        assert win._health_label.property("statusState") == "critical"

    # An unrelated transient failure keeps the generic chip but does not
    # erase the accumulated wire-version evidence.
    win.note_decode_failed("WoWScrnShot_0002.jpg", "CRC mismatch")
    assert win._health_label.text() == "Shot failed"

    win.note_decode_failed("WoWScrnShot_0003.jpg", reason)

    assert win._health_label.text() == "Companion update"
    assert win._health_label.property("statusState") == "warning"
    assert WIRE_VERSION_REJECT_MESSAGE in win._health_label.toolTip()
    assert reason in win._health_label.toolTip()
    assert win._health_label.accessibleDescription() == win._health_label.toolTip()

    # Any successfully decoded frame resets the streak: the next reject is
    # an isolated failure again, not a newer-format banner.
    win.note_decode(Snapshot(listing=None, version=None))
    win.note_decode_failed("WoWScrnShot_0004.jpg", reason)

    assert win._health_label.text() == "Shot failed"
    assert win._health_label.property("statusState") == "critical"
