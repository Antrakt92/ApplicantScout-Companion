"""Widget-level checks for the Applicants / Party source tabs."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontMetrics

from applicant_scout.__main__ import StateMachine
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.overlay import (
    COL_H,
    COL_MPLUS,
    COL_RIO,
    INFO_PANEL_PREFERRED_HEIGHT,
    METRIC_COLUMN_TEXT_PADDING,
    _mplus_cell_visuals,
    OverlayWindow,
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


def test_listing_created_after_roster_only_state_switches_back_to_applicants(
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

    assert win._active_tab == "applicants"
    assert win._id_by_row == []
    assert win._role_filter == set()


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
    assert win._table.item(0, COL_H).text().startswith(
        ("FIT ", "OK ", "RISK ", "SUP ", "EST ")
    )


def test_authoritative_party_replacement_clears_preserved_raid_context(
    qtbot, tmp_path
):
    state = AppState()
    sm = StateMachine(state)
    win = _window(tmp_path, qtbot, state)
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


def test_last_applicant_removed_preserves_visible_party_roster(qtbot, tmp_path):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    win.show()

    state.remove("7:1")
    win.on_applicant_removed("7:1")
    win._flush_overlay_refresh()

    assert win.isVisible()
    assert win._active_tab == "party"
    assert win._table.rowCount() == 1
    assert win._id_by_row == ["host-realm"]


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
    assert not win._party_tab_auto_selected


def test_last_applicant_removed_does_not_carry_applicant_filter_into_party_auto_switch(
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
    assert win._active_tab == "party"
    assert win._id_by_row == ["host-realm"]
    assert visible_rows == [0]
    assert win._role_filter == set()


def test_new_applicant_after_party_auto_switch_returns_to_applicants(
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

    assert win._active_tab == "applicants"
    assert win._id_by_row == ["7:1"]
    assert win._role_filter == set()


def test_clicking_auto_selected_party_tab_makes_it_manual(qtbot, tmp_path):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm", "TANK")
    state.party_members["host-realm"].fetch_status = "ready"
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _applicant: None

    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._active_tab == "party"
    assert win._party_tab_auto_selected

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    assert win._active_tab == "party"
    assert not win._party_tab_auto_selected

    state.applicants["7:1"] = _app("7:1", "Applicant-Realm", "DAMAGER")
    win.on_applicant_added(state.applicants["7:1"])
    win._flush_overlay_refresh()

    assert win._active_tab == "party"
    assert win._id_by_row == ["host-realm"]


def test_applicant_update_after_party_auto_switch_returns_to_applicants(
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

    assert win._active_tab == "applicants"
    assert win._id_by_row == ["7:1"]


def test_new_applicant_does_not_override_manual_party_tab(qtbot, tmp_path):
    state = AppState()
    state.applicants["7:1"] = _app("7:1", "Applicant-Realm")
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)
    win._launch_fetch = lambda _applicant: None

    win._refresh_table()
    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    assert win._active_tab == "party"
    assert not win._party_tab_auto_selected

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
    assert not win._party_tab_auto_selected


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


def test_empty_roster_switches_back_to_applicants_when_applicants_remain(
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
    assert win._active_tab == "applicants"
    assert win._id_by_row == ["7:1"]


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
    assert win._tab_bar._key_up_button.text() == "▲"
    assert win._tab_bar._key_down_button.text() == "▼"


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
        challenge_map_id=556,
        player_name="Leader-Realm",
    )
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)

    listing = win._effective_listing()

    assert listing is not None
    assert listing.key_level == 17
    assert listing.dungeon_name == "Pit of Saron"
    win._active_tab = "party"
    win._update_title()
    assert win._title_bar.title_label.text() == "Party — Pit of Saron +17 (1)"
    assert win._tab_bar._key_spin.value() == 17


def test_leader_key_above_30_remains_effective_and_visible(qtbot, tmp_path):
    state = AppState()
    state.leader_key = LeaderKey(
        key_level=31,
        challenge_map_id=556,
        player_name="Leader-Realm",
    )
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)

    listing = win._effective_listing()

    assert listing is not None
    assert listing.key_level == 31
    win._active_tab = "party"
    win._update_title()
    assert win._title_bar.title_label.text() == "Party — Pit of Saron +31 (1)"
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
        challenge_map_id=556,
        player_name="Leader-Realm",
    )
    state.party_members["dps-realm"] = _ready_mplus_member()
    win = _window(tmp_path, qtbot, state)

    win._tab_bar._key_spin.setValue(16)

    listing = win._effective_listing()
    assert listing is not None
    assert win._manual_target_key == 16
    assert listing.key_level == 16
    assert listing.dungeon_name == "Pit of Saron"
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
    assert win._table.item(0, 7).text() != legacy_text


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
    assert "Target +10" in win._panel._status_label.text()

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
    assert "Target +12" in win._panel._status_label.text()
    assert "Target +10" not in win._panel._status_label.text()


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
    assert win._table.item(0, 7).text() != legacy_text


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


def test_raid_listing_renders_fit_in_target_column_and_neutral_mplus(
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

    assert win._table.item(0, COL_H).text().startswith("FIT ")
    assert "82/82" in win._table.item(0, COL_H).text()
    assert win._table.item(0, COL_MPLUS).text() == "44 N=1 +18"
    _text, _fg, mplus_bg = _mplus_cell_visuals(applicant, win._effective_listing())
    assert mplus_bg is None


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

    item = win._table.item(0, COL_H)
    required = (
        QFontMetrics(item.font()).horizontalAdvance(item.text())
        + METRIC_COLUMN_TEXT_PADDING
    )
    assert item.text().startswith("FIT ")
    assert "82/82" in item.text()
    assert win._table.columnWidth(COL_H) >= required


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
    assert win._table.item(0, COL_H).text().startswith("EST ")
    assert "M 70/60" in win._table.item(0, COL_H).text()


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
