"""Widget-level checks for the Applicants / Party source tabs."""

from __future__ import annotations

from PyQt6.QtCore import Qt

from applicant_scout.overlay import _mplus_cell_visuals, OverlayWindow
from applicant_scout.state import AppState, Applicant, Listing, RosterMember


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


def _member(member_id: str, name: str, role: str = "DAMAGER") -> RosterMember:
    return RosterMember(
        applicant_id=member_id,
        name=name,
        cls="PRIEST" if role == "HEALER" else "WARRIOR",
        spec_id=257 if role == "HEALER" else 71,
        ilvl=701,
        score=3100,
        role=role,
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


def _listing(key_level: int = 12) -> Listing:
    return Listing(
        activity_id=459,
        dungeon_name="Nexus-Point Xenas",
        listing_name="+12 Competitive",
        comment="",
        key_level=key_level,
        category_id=2,
    )


def _window(tmp_path, qtbot, state: AppState) -> OverlayWindow:
    win = OverlayWindow(state, _FakeWCLClient(), _FakeCache(), tmp_path)
    qtbot.addWidget(win)
    return win


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


def test_roster_only_update_shows_party_tab(qtbot, tmp_path):
    state = AppState()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    state.party_members["host-realm"].fetch_status = "ready"
    win = _window(tmp_path, qtbot, state)

    win.on_roster_changed()
    win._flush_overlay_refresh()

    assert win.isVisible()
    assert win._active_tab == "party"
    assert win._table.rowCount() == 1


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


def test_empty_roster_update_hides_party_only_overlay(qtbot, tmp_path):
    state = AppState()
    win = _window(tmp_path, qtbot, state)
    win._active_tab = "party"
    win.show()

    win.on_roster_changed()
    win._flush_overlay_refresh()

    assert not win.isVisible()


def test_party_title_keeps_listing_key_context(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing()
    state.party_members["host-realm"] = _member("host-realm", "Host-Realm")
    win = _window(tmp_path, qtbot, state)

    qtbot.mouseClick(win._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)

    assert win._title_bar.title_label.text() == "Party — Nexus-Point Xenas +12 (1)"


def test_target_key_control_defaults_to_listing_key(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing(key_level=12)
    win = _window(tmp_path, qtbot, state)

    win._update_title()

    assert win._tab_bar._key_spin.value() == 12
    assert win._tab_bar._key_spin.width() >= 88


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
