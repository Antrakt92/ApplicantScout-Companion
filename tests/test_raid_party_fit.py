"""Party raid Fit follows the group's difficulty independently of LFG listings."""

from types import SimpleNamespace

import pytest

from applicant_scout import overlay, scoring
from applicant_scout.constants import percentile_colour
from applicant_scout.state import AppState, Listing, RosterMember


def _member(name="Player-Realm", difficulty=15, *, raid=True):
    member = RosterMember(
        name.lower(), name, "MAGE", 62, 320, 3000, "DAMAGER",
        fetch_status="ready", is_raid_member=raid,
        raid_normal=95, raid_normal_median=90,
        raid_heroic=60, raid_heroic_median=50,
        raid_mythic=15, raid_mythic_median=10,
    )
    member.raid_difficulty_id = difficulty
    return member


def _window(qtbot, tmp_path, *members, listing=None):
    state = AppState()
    state.listing = listing
    state.party_members = {member.applicant_id: member for member in members}
    win = overlay.OverlayWindow(
        state, SimpleNamespace(region="EU", partition=None), SimpleNamespace(), tmp_path,
    )
    qtbot.addWidget(win)
    qtbot.addWidget(win._launcher)
    win._launch_fetch = lambda _row: None
    win._on_source_tab_changed("party")
    win.on_roster_changed()
    win._flush_overlay_refresh()
    return win, state


@pytest.mark.parametrize("difficulty,label", [(14, "Normal"), (15, "Heroic"), (16, "Mythic")])
@pytest.mark.parametrize("size", [1, 5, 15])
def test_raid_party_without_listing_has_fit_for_selected_difficulty(
    qtbot, tmp_path, difficulty, label, size,
):
    members = [_member(f"Player{index}-Realm", difficulty) for index in range(size)]
    win, state = _window(qtbot, tmp_path, *members)

    assert state.listing is None
    assert win._effective_listing().difficulty_id == difficulty
    assert not win._table.isColumnHidden(overlay.COL_FIT)
    assert win._table.horizontalHeaderItem(overlay.COL_FIT).text() == f"Fit · {label}"
    assert win._table.item(0, overlay.COL_FIT).text().startswith("~")
    assert win._tab_bar._key_control.isHidden()
    assert win._panel._active_detail_mode() == "raid"
    assert win._raid_boss_fetches_in_flight == {}


def test_difficulty_refresh_repaints_fit_colour_and_resorts_without_switching_tabs(qtbot, tmp_path):
    first, second = _member("First-Realm", 14), _member("Second-Realm", 14)
    second.raid_normal, second.raid_normal_median = 10, 10
    second.raid_mythic, second.raid_mythic_median = 99, 99
    win, state = _window(qtbot, tmp_path, first, second)
    win._on_sort_column_clicked(overlay.COL_FIT)
    assert win._id_by_row[0] == first.applicant_id
    normal_text = win._table.item(win._row_for_id[first.applicant_id], overlay.COL_FIT).text()
    normal_colour = win._table.item(win._row_for_id[first.applicant_id], overlay.COL_FIT).background().color().name()

    for member in state.party_members.values():
        member.raid_difficulty_id = 16
    win.on_roster_changed()
    win._flush_overlay_refresh()

    assert win._active_tab == "party"
    assert win._id_by_row[0] == second.applicant_id
    item = win._table.item(win._row_for_id[first.applicant_id], overlay.COL_FIT)
    fit = scoring.candidate_fit(first, win._effective_listing())
    assert item.text() == f"~{round(fit.score)}"
    assert item.text() != normal_text
    assert item.background().color().name() == percentile_colour(round(fit.score))
    assert item.background().color().name() != normal_colour
    assert win._sort_by_tab["party"] == (overlay.COL_FIT, True)


@pytest.mark.parametrize("listing", [
    Listing(0, "Dungeon", "", "", key_level=10, category_id=2),
    Listing(0, "Old raid", "", "", category_id=3, difficulty_id=16),
])
def test_current_raid_difficulty_overrides_unrelated_listing_only_on_party(qtbot, tmp_path, listing):
    win, state = _window(qtbot, tmp_path, _member(difficulty=14), listing=listing)
    win._manual_target_key = 20
    assert win._effective_listing().difficulty_id == 14
    assert win._effective_listing().key_level == 0
    assert state.listing is listing

    win._on_source_tab_changed("applicants")
    assert win._effective_listing() is listing
    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._active_tab == "applicants"


@pytest.mark.parametrize("difficulty", [0, 17])
def test_unsupported_current_context_does_not_reuse_old_raid_fit(qtbot, tmp_path, difficulty):
    old_listing = Listing(0, "Old raid", "", "", category_id=3, difficulty_id=15)
    win, state = _window(qtbot, tmp_path, _member(difficulty=difficulty), listing=old_listing)
    assert win._table.isColumnHidden(overlay.COL_FIT)
    assert win._panel._metric_labels["Fit"].isHidden()
    assert win._tab_bar._key_control.isHidden()
    state.listing = None
    win.on_listing_changed()
    win._flush_overlay_refresh()
    assert win._table.isColumnHidden(overlay.COL_FIT)
    assert win._effective_listing().difficulty_id == 0


@pytest.mark.parametrize("difficulties", [(14, 15), (14, None), (14, 0)])
def test_inconsistent_roster_difficulties_do_not_guess_fit(qtbot, tmp_path, difficulties):
    win, _state = _window(
        qtbot, tmp_path, *[_member(f"Player{i}-Realm", value) for i, value in enumerate(difficulties)],
    )
    assert win._table.isColumnHidden(overlay.COL_FIT)


def test_legacy_roster_keeps_remembered_raid_listing(qtbot, tmp_path):
    listing = Listing(0, "Known raid", "", "", category_id=3, difficulty_id=15)
    win, state = _window(qtbot, tmp_path, _member(difficulty=None), listing=listing)
    state.listing = None
    win.on_listing_changed()
    win._flush_overlay_refresh()
    assert win._effective_listing() is listing
    assert not win._table.isColumnHidden(overlay.COL_FIT)


def test_raid_to_party_drops_difficulty_and_reenables_key_selection(qtbot, tmp_path):
    win, state = _window(qtbot, tmp_path, _member())
    member = next(iter(state.party_members.values()))
    member.is_raid_member = False
    member.raid_difficulty_id = 0
    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._effective_listing() is None
    assert win._table.isColumnHidden(overlay.COL_FIT)
    assert not win._tab_bar._key_control.isHidden()
    assert win._active_tab == "party"


def test_explicit_unknown_clears_remembered_context_for_later_legacy_roster(qtbot, tmp_path):
    listing = Listing(0, "Known raid", "", "", category_id=3, difficulty_id=15)
    member = _member(difficulty=None)
    win, state = _window(qtbot, tmp_path, member, listing=listing)
    state.listing = None
    member.raid_difficulty_id = 0
    win.on_roster_changed()
    win._flush_overlay_refresh()
    state.listing = listing
    win.on_listing_changed()
    state.listing = None
    win.on_listing_changed()
    member.raid_difficulty_id = None
    win.on_roster_changed()
    win._flush_overlay_refresh()
    assert win._last_raid_listing is None
    assert win._table.isColumnHidden(overlay.COL_FIT)
