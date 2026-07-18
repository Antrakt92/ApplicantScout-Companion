"""Keyboard and accessibility contracts for the passive WoW overlay."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QPushButton,
    QWidget,
)

import applicant_scout.overlay as overlay_mod
from applicant_scout.overlay import (
    COL_NAME,
    ApplicantInfoPanel,
    OverlayWindow,
)
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Applicant, Listing, RosterMember
from applicant_scout.wcl import CharacterCache, WCLAuth, WCLClient


def _app(
    applicant_id: str,
    name: str,
    *,
    role: str,
    score: int,
    spec_id: int = 71,
) -> Applicant:
    return Applicant(
        applicant_id=applicant_id,
        name=name,
        cls="WARRIOR" if role != "HEALER" else "PRIEST",
        spec_id=spec_id,
        ilvl=700,
        score=score,
        role=role,
        fetch_status="ready",
    )


def _build_window(tmp_path, qtbot) -> tuple[OverlayWindow, WCLClient]:
    state = AppState()
    state.listing = Listing(
        activity_id=401,
        dungeon_name="Skyreach",
        listing_name="+12 Skyreach",
        comment="Bring interrupts",
        key_level=12,
        category_id=2,
        difficulty_id=8,
    )
    state.add_or_update(_app("tank", "Tank-Realm", role="TANK", score=2400))
    state.add_or_update(
        _app("healer", "Healer-Realm", role="HEALER", score=2200, spec_id=257)
    )
    state.add_or_update(_app("damage", "Damage-Realm", role="DAMAGER", score=2000))
    state.add_or_update_party_member(
        RosterMember(**vars(_app("party", "Party-Realm", role="TANK", score=2500)))
    )
    client = WCLClient(WCLAuth("client", "secret", tmp_path))
    window = OverlayWindow(
        state,
        client,
        CharacterCache(tmp_path),
        tmp_path,
        show_settings=lambda: None,
        game_foreground_probe=lambda: True,
    )
    window._launch_fetch = lambda applicant: None
    window._launch_raid_boss_fetch_if_needed = lambda applicant: False
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    window._refresh_table()
    return window, client


def test_overlay_exposes_named_controls_without_weakening_passive_show(qtbot, tmp_path):
    window, client = _build_window(tmp_path, qtbot)
    try:
        assert window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert window._launcher.testAttribute(
            Qt.WidgetAttribute.WA_ShowWithoutActivating
        )
        assert isinstance(window._launcher, QPushButton)
        assert window._launcher.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert window._launcher.focusPolicy() == Qt.FocusPolicy.TabFocus
        assert "#overlayLauncher:focus" in window._launcher.styleSheet()
        assert window.windowTitle() == "ApplicantScout overlay"
        assert window._launcher.accessibleName() == "Show ApplicantScout overlay"
        assert (
            window._title_bar.settings_button.accessibleName()
            == "Open ApplicantScout settings"
        )
        assert (
            window._title_bar.hide_button.accessibleName()
            == "Hide ApplicantScout overlay to launcher"
        )
        assert window._title_bar.title_label.accessibleName() == "Current listing"
        assert (
            window._title_bar.title_label.accessibleDescription()
            == "+12 Skyreach\n\nBring interrupts"
        )

        assert (
            window._tab_bar._buttons["applicants"].focusPolicy()
            == Qt.FocusPolicy.TabFocus
        )
        assert window._tab_bar._buttons["party"].accessibleName() == "Party view"
        assert window._tab_bar._key_label.buddy() is window._tab_bar._key_spin
        assert (
            window._tab_bar._key_spin.accessibleName()
            == "Manual Mythic Plus target key"
        )
        assert (
            window._tab_bar._key_up_button.accessibleName()
            == "Increase manual Mythic Plus target key"
        )
        assert window._tab_bar._key_up_button.focusPolicy() == Qt.FocusPolicy.TabFocus
        assert (
            window._tab_bar._key_down_button.accessibleName()
            == "Decrease manual Mythic Plus target key"
        )
        assert window._tab_bar._key_down_button.focusPolicy() == Qt.FocusPolicy.TabFocus

        assert (
            window._role_filter_bar._buttons["DAMAGER"].accessibleName()
            == "Damage dealer role filter"
        )
        assert (
            window._role_filter_bar._reset_btn.accessibleName() == "Clear role filters"
        )
        assert (
            window._role_filter_bar._status.accessibleName()
            == "Role filter result count"
        )
        assert window._panel.accessibleName() == "Applicant details"
        assert (
            window._panel._unpin_button.accessibleName()
            == "Clear pinned applicant Tank-Realm"
        )
        assert (
            window._panel._detail_buttons["raid"].accessibleName()
            == "Raid details view"
        )
        assert (
            window._panel._detail_buttons["mplus"].accessibleName()
            == "Mythic Plus details view"
        )

        assert window._table.focusPolicy() == Qt.FocusPolicy.TabFocus
        assert (
            window._table.selectionBehavior()
            == QAbstractItemView.SelectionBehavior.SelectRows
        )
        assert (
            window._table.selectionMode()
            == QAbstractItemView.SelectionMode.SingleSelection
        )
        assert window._table.accessibleName() == "Applicant applications table"
        assert "Up, Down" in window._table.accessibleDescription()

        assert window._accessibility_tab_controls() == (
            window._title_bar.settings_button,
            window._title_bar.hide_button,
            window._tab_bar._buttons["applicants"],
            window._tab_bar._buttons["party"],
            window._tab_bar._key_spin,
            window._tab_bar._key_up_button,
            window._tab_bar._key_down_button,
            window._role_filter_bar._buttons["TANK"],
            window._role_filter_bar._buttons["HEALER"],
            window._role_filter_bar._buttons["DAMAGER"],
            window._role_filter_bar._reset_btn,
            window._panel._wcl_retry_button,
            window._panel._unpin_button,
            window._panel._detail_buttons["raid"],
            window._panel._detail_buttons["mplus"],
            window._table,
        )

        name_item = window._table.item(0, COL_NAME)
        assert name_item is not None
        assert name_item.data(Qt.ItemDataRole.AccessibleTextRole) == "Name: Tank-Realm"
        assert "Role: Tank" in name_item.data(Qt.ItemDataRole.AccessibleDescriptionRole)

        assert window._state.listing is not None
        window._state.listing.listing_name = "R&D <push>"
        window._state.listing.comment = "Use > 2 stops"
        window._update_title()
        assert "R&amp;D &lt;push&gt;" in window._title_bar.title_label.toolTip()
        assert (
            window._title_bar.title_label.accessibleDescription()
            == "R&D <push>\n\nUse > 2 stops"
        )
    finally:
        window.close()
        client.close()


def test_keyboard_controls_use_standard_widget_actions(qtbot, tmp_path):
    window, client = _build_window(tmp_path, qtbot)
    try:
        window.show()
        window.activateWindow()

        party_button = window._tab_bar._buttons["party"]
        party_button.setFocus()
        qtbot.keyClick(party_button, Qt.Key.Key_Space)
        assert window._active_tab == "party"

        applicants_button = window._tab_bar._buttons["applicants"]
        applicants_button.setFocus()
        qtbot.keyClick(applicants_button, Qt.Key.Key_Return)
        assert window._active_tab == "applicants"

        tank_filter = window._role_filter_bar._buttons["TANK"]
        tank_filter.setFocus()
        qtbot.keyClick(tank_filter, Qt.Key.Key_Space)
        assert window._role_filter == {"TANK"}
        assert window._role_filter_bar._reset_btn.isVisible()

        reset = window._role_filter_bar._reset_btn
        reset.setFocus()
        qtbot.keyClick(reset, Qt.Key.Key_Space)
        assert window._role_filter == set()

        spin = window._tab_bar._key_spin
        spin.setFocus()
        before = spin.value()
        qtbot.keyClick(spin, Qt.Key.Key_Up)
        assert spin.value() == before + 1
    finally:
        window.close()
        client.close()


def test_table_keyboard_state_tracks_identity_across_sort_filter_and_tabs(
    qtbot, tmp_path
):
    window, client = _build_window(tmp_path, qtbot)
    try:
        window.show()
        window.activateWindow()
        window._table.setFocus()

        qtbot.keyClick(window._table, Qt.Key.Key_Home)
        assert window._keyboard_id == "tank"
        qtbot.keyClick(window._table, Qt.Key.Key_End)
        assert window._keyboard_id == "damage"
        qtbot.keyClick(window._table, Qt.Key.Key_Up)
        current_id = window._keyboard_id
        assert current_id == "healer"
        assert window._resolve_visible_id() == "healer"
        assert window._delegate._keyboard_row == window._row_for_id["healer"]

        qtbot.keyClick(window._table, Qt.Key.Key_Return)
        assert window._pinned_id == "healer"
        pinned_item = window._table.item(window._row_for_id["healer"], COL_NAME)
        assert pinned_item is not None
        assert "Pinned." in pinned_item.data(Qt.ItemDataRole.AccessibleDescriptionRole)

        window._state.applicants["healer"].score = 2600
        window._refresh_table()
        assert window._keyboard_id == "healer"
        assert window._table.currentRow() == window._row_for_id["healer"]
        rebuilt_pinned_item = window._table.item(window._row_for_id["healer"], COL_NAME)
        assert rebuilt_pinned_item is not None
        assert "Pinned." in rebuilt_pinned_item.data(
            Qt.ItemDataRole.AccessibleDescriptionRole
        )

        window._role_filter_bar._buttons["TANK"].click()
        assert window._keyboard_id == "tank"
        assert not window._table.isRowHidden(window._row_for_id["tank"])

        window._tab_bar._buttons["party"].click()
        window._table.setFocus()
        qtbot.keyClick(window._table, Qt.Key.Key_Down)
        assert window._keyboard_id == "party"
        assert window._keyboard_by_tab["applicants"] == "tank"
        assert window._keyboard_by_tab["party"] == "party"

        window._tab_bar._buttons["applicants"].click()
        assert window._keyboard_id == "tank"
        assert window._table.currentRow() == window._row_for_id["tank"]

        window._table.setFocus()
        qtbot.keyClick(window._table, Qt.Key.Key_Escape)
        assert window._pinned_id is None
        qtbot.keyClick(window._table, Qt.Key.Key_Space)
        assert window._pinned_id == "tank"
    finally:
        window.close()
        client.close()


def test_table_tab_and_backtab_leave_cell_navigation(qtbot, tmp_path, monkeypatch):
    window, client = _build_window(tmp_path, qtbot)
    try:
        window.show()
        assert not window._table.tabKeyNavigation()
        forward_focus: list[Qt.FocusReason] = []
        backward_focus: list[Qt.FocusReason] = []
        monkeypatch.setattr(
            window._title_bar.settings_button,
            "setFocus",
            lambda reason=Qt.FocusReason.OtherFocusReason: forward_focus.append(reason),
        )
        monkeypatch.setattr(
            window._role_filter_bar._buttons["DAMAGER"],
            "setFocus",
            lambda reason=Qt.FocusReason.OtherFocusReason: backward_focus.append(
                reason
            ),
        )

        qtbot.keyClick(window._table, Qt.Key.Key_Tab)
        qtbot.keyClick(
            window._table,
            Qt.Key.Key_Tab,
            modifier=Qt.KeyboardModifier.ShiftModifier,
        )

        assert forward_focus == [Qt.FocusReason.TabFocusReason]
        assert backward_focus == [Qt.FocusReason.BacktabFocusReason]
    finally:
        window.close()
        client.close()


def test_mouse_click_ends_keyboard_preview_and_pins_the_clicked_identity(
    qtbot, tmp_path
):
    window, client = _build_window(tmp_path, qtbot)
    try:
        window._on_table_keyboard_navigated(window._row_for_id["healer"])
        window._on_cell_clicked(window._row_for_id["tank"], COL_NAME)

        assert not window._keyboard_preview_active
        assert window._hover_id == "tank"
        assert window._pinned_id == "tank"
        assert window._resolve_visible_id() == "tank"
        assert window._panel._current_applicant is not None
        assert window._panel._current_applicant.applicant_id == "tank"
    finally:
        window.close()
        client.close()


def test_stationary_cursor_cannot_override_keyboard_preview_on_refresh_or_filter(
    qtbot, tmp_path, monkeypatch
):
    window, client = _build_window(tmp_path, qtbot)
    try:
        monkeypatch.setattr(window, "_resolve_hover_from_cursor", lambda: "tank")
        window._on_table_keyboard_navigated(window._row_for_id["healer"])

        window._state.applicants["healer"].score = 2600
        window._refresh_table()
        assert window._hover_id is None
        assert window._resolve_visible_id() == "healer"

        window._on_role_filter_changed({"HEALER"})
        assert window._hover_id is None
        assert window._keyboard_id == "healer"
        assert window._resolve_visible_id() == "healer"
    finally:
        window.close()
        client.close()


def test_same_cell_mouse_move_immediately_restores_pointer_identity(qtbot, tmp_path):
    window, client = _build_window(tmp_path, qtbot)
    try:
        window.show()
        viewport = window._table.viewport()
        assert viewport is not None
        healer_row = window._row_for_id["healer"]
        healer_pos = QPoint(
            8,
            window._table.rowViewportPosition(healer_row)
            + window._table.rowHeight(healer_row) // 2,
        )
        qtbot.mouseMove(viewport, healer_pos)
        window._on_cell_entered(healer_row, COL_NAME)
        window._on_table_keyboard_navigated(window._row_for_id["tank"])

        moved_pos = healer_pos + QPoint(1, 0)
        moved_global = viewport.mapToGlobal(moved_pos)
        QApplication.sendEvent(
            viewport,
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(moved_pos),
                QPointF(moved_global),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

        assert not window._keyboard_preview_active
        assert window._hover_id == "healer"
        assert window._resolve_visible_id() == "healer"
        assert window._panel._current_applicant is not None
        assert window._panel._current_applicant.applicant_id == "healer"
    finally:
        window.close()
        client.close()


def test_synthetic_viewport_leave_cannot_override_keyboard_preview(
    qtbot, tmp_path, monkeypatch
):
    window, client = _build_window(tmp_path, qtbot)
    try:
        viewport = window._table.viewport()
        assert viewport is not None
        monkeypatch.setattr(window, "_resolve_hover_from_cursor", lambda: "tank")
        window._on_table_keyboard_navigated(window._row_for_id["healer"])

        assert window.eventFilter(viewport, QEvent(QEvent.Type.Leave)) is False

        assert window._keyboard_preview_active
        assert window._hover_id is None
        assert window._resolve_visible_id() == "healer"
    finally:
        window.close()
        client.close()


def test_hide_show_clears_keyboard_delegate_and_accessible_preview(qtbot, tmp_path):
    window, client = _build_window(tmp_path, qtbot)
    try:
        window.show()
        healer_row = window._row_for_id["healer"]
        window._on_table_keyboard_navigated(healer_row)
        assert window._delegate._keyboard_row == healer_row

        window.hide()
        window.show()

        item = window._table.item(window._row_for_id["healer"], COL_NAME)
        assert item is not None
        assert not window._keyboard_preview_active
        assert window._delegate._keyboard_row == -1
        assert "Keyboard preview." not in item.data(
            Qt.ItemDataRole.AccessibleDescriptionRole
        )
    finally:
        window.close()
        client.close()


def test_mouse_hover_and_window_deactivation_only_clear_transient_keyboard_preview(
    qtbot, tmp_path
):
    window, client = _build_window(tmp_path, qtbot)
    try:
        window._on_table_keyboard_navigated(window._row_for_id["healer"])
        assert window._keyboard_preview_active
        assert window._keyboard_id == "healer"

        window._on_cell_entered(window._row_for_id["tank"], COL_NAME)
        assert not window._keyboard_preview_active
        assert window._keyboard_id == "healer"
        assert window._resolve_visible_id() == "tank"

        window._hover_id = None
        window._on_table_keyboard_navigated(window._row_for_id["healer"])
        QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
        assert not window._keyboard_preview_active
        assert window._keyboard_id == "healer"
    finally:
        window.close()
        client.close()


def test_removed_keyboard_row_moves_to_nearest_surviving_identity(qtbot, tmp_path):
    window, client = _build_window(tmp_path, qtbot)
    try:
        healer_row = window._row_for_id["healer"]
        window._on_table_keyboard_navigated(healer_row)
        window._state.remove("healer")

        window.on_applicant_removed("healer")
        window._flush_overlay_refresh()

        assert window._keyboard_id == "damage"
        assert window._table.currentRow() == window._row_for_id["damage"]
        assert "healer" not in window._row_for_id
    finally:
        window.close()
        client.close()


def test_non_contiguous_group_slots_use_visible_accessible_ordinals(qtbot, tmp_path):
    window, client = _build_window(tmp_path, qtbot)
    try:
        window._state.applicants.clear()
        window._state.add_or_update(
            _app("group:1", "First-Realm", role="TANK", score=2400)
        )
        window._state.add_or_update(
            _app("group:3", "Third-Realm", role="DAMAGER", score=2300)
        )
        window._refresh_table()

        first = window._table.item(window._row_for_id["group:1"], COL_NAME)
        third = window._table.item(window._row_for_id["group:3"], COL_NAME)
        assert first is not None
        assert third is not None
        assert "member 1 of 2" in first.data(Qt.ItemDataRole.AccessibleDescriptionRole)
        assert "member 2 of 2" in third.data(Qt.ItemDataRole.AccessibleDescriptionRole)
        assert "member 3 of 2" not in third.data(
            Qt.ItemDataRole.AccessibleDescriptionRole
        )
    finally:
        window.close()
        client.close()


def test_detail_tabs_and_retry_action_are_keyboard_operable(qtbot, monkeypatch):
    parent = QWidget()
    qtbot.addWidget(parent)
    panel = ApplicantInfoPanel(parent, MetricPreferences())
    parent.show()
    applicant = _app("damage", "Damage-Realm", role="DAMAGER", score=2000)
    raid_listing = Listing(
        activity_id=1,
        dungeon_name="",
        listing_name="Mythic raid",
        comment="",
        category_id=3,
        difficulty_id=16,
    )

    panel.setApplicantData(applicant, raid_listing)
    mplus = panel._detail_buttons["mplus"]
    assert panel._detail_tabs.isVisible()
    mplus.setFocus()
    qtbot.keyClick(mplus, Qt.Key.Key_Return)
    assert panel._detail_mode == "mplus"

    applicant.fetch_status = "error"
    applicant.error_message = "temporary"
    panel.setApplicantData(applicant, raid_listing, wcl_retry_available=True)
    retry = panel._wcl_retry_button
    assert retry.isVisible()
    retried: list[bool] = []
    fallback_requested: list[bool] = []
    panel.wclRetryRequested.connect(lambda: retried.append(True))
    panel.focusFallbackRequested.connect(lambda: fallback_requested.append(True))
    retry.setFocus()
    qtbot.keyClick(retry, Qt.Key.Key_Space)
    assert retried == [True]

    monkeypatch.setattr(
        overlay_mod,
        "_widget_has_focus",
        lambda widget: widget is retry,
    )
    applicant.fetch_status = "ready"
    panel.setApplicantData(applicant, raid_listing)
    assert not retry.isVisible()
    assert fallback_requested == [True]


def test_hiding_focused_dynamic_controls_moves_focus_without_background_steal(
    qtbot, tmp_path, monkeypatch
):
    window, client = _build_window(tmp_path, qtbot)
    try:
        window.show()
        window.activateWindow()

        focus_state: dict[str, QWidget | None] = {"widget": window._tab_bar._key_spin}
        monkeypatch.setattr(
            overlay_mod,
            "_widget_has_focus",
            lambda widget: (
                focus_state["widget"] is not None
                and (
                    focus_state["widget"] is widget
                    or widget.isAncestorOf(focus_state["widget"])
                )
            ),
        )
        applicant_focus: list[Qt.FocusReason] = []
        monkeypatch.setattr(
            window._tab_bar._buttons["applicants"],
            "setFocus",
            lambda reason=Qt.FocusReason.OtherFocusReason: applicant_focus.append(
                reason
            ),
        )
        window._tab_bar.set_target_key_visible(False)
        assert applicant_focus == [Qt.FocusReason.OtherFocusReason]

        tank_filter = window._role_filter_bar._buttons["TANK"]
        tank_focus: list[Qt.FocusReason] = []
        monkeypatch.setattr(
            tank_filter,
            "setFocus",
            lambda reason=Qt.FocusReason.OtherFocusReason: tank_focus.append(reason),
        )
        tank_filter.click()
        focus_state["widget"] = window._role_filter_bar._reset_btn
        window._role_filter_bar._reset_btn.click()
        assert tank_focus == [Qt.FocusReason.OtherFocusReason]

        window._panel.setApplicantData(window._state.applicants["tank"], pinned=True)
        focus_state["widget"] = window._panel._unpin_button
        table_focus: list[Qt.FocusReason] = []
        monkeypatch.setattr(
            window._table,
            "setFocus",
            lambda reason=Qt.FocusReason.OtherFocusReason: table_focus.append(reason),
        )
        window._panel.setApplicantData(window._state.applicants["tank"], pinned=False)
        assert table_focus == [Qt.FocusReason.OtherFocusReason]

        focus_state["widget"] = None
        window._panel.setPlaceholder()
        assert table_focus == [Qt.FocusReason.OtherFocusReason]
    finally:
        window.close()
        client.close()


def test_tray_restore_enters_focus_chain_but_launcher_restore_stays_passive(
    qtbot, tmp_path, monkeypatch
):
    window, client = _build_window(tmp_path, qtbot)
    focus_entries: list[bool] = []
    try:
        monkeypatch.setattr(
            window,
            "_focus_accessibility_entry",
            lambda: focus_entries.append(True),
        )
        window.restore_from_launcher()
        qtbot.wait(1)
        assert focus_entries == []

        window.show_launcher_only()
        window.restore_from_tray()
        qtbot.waitUntil(lambda: focus_entries == [True], timeout=1000)
    finally:
        window.close()
        client.close()
