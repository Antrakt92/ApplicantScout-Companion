"""Keyboard sorting uses native menus while preserving grouped row identity."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication, QMenu

from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.overlay import COL_H, COL_ILVL, COL_NAME, USER_MIN_WINDOW_WIDTH
from applicant_scout.state import AppState
from test_overlay_tabs import _app, _member
from test_overlay_fetch_identity import _ShutdownPool, _window


@pytest.fixture
def sort_window(qtbot, tmp_path):
    state = AppState()
    for aid, name, ilvl, status in (
        ("1:1", "Bravo-Realm", 200, "ready"),
        ("1:2", "Charlie-Realm", 200, "ready"),
        ("2:1", "Alpha-Realm", 300, "ready"),
        ("3:1", "Zulu-Realm", 999, "loading"),
    ):
        app = _app(aid, name)
        app.ilvl = ilvl
        app.raid_heroic = ilvl / 10
        app.fetch_status = status
        state.add_or_update(app)
    state.party_members["a"] = _member("a", "PartyA-Realm", score=1000)
    state.party_members["z"] = _member("z", "PartyZ-Realm", score=3000)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _ShutdownPool()
    window._launch_fetch = lambda _app: None
    window._refresh_table()
    window.show()
    window.activateWindow()
    try:
        yield window
    finally:
        window.shutdown_fetches()
        window.close()
        client.close()


def _open(qtbot, window):
    window._sort_button.setFocus()
    qtbot.keyClick(window._sort_button, Qt.Key.Key_Return)
    qtbot.waitUntil(window._sort_menu.isVisible)
    return window._sort_menu


def _choose(qtbot, menu, action):
    actions = [
        a
        for a in menu.actions()
        if not a.isSeparator() and a.isVisible() and a.isEnabled()
    ]
    qtbot.keyClick(menu, Qt.Key.Key_Home)
    if menu.activeAction() is None:
        qtbot.keyClick(menu, Qt.Key.Key_Down)
    for _ in range(actions.index(action)):
        qtbot.keyClick(menu, Qt.Key.Key_Down)
    assert menu.activeAction() is action


def _column(qtbot, window, column, descending):
    menu = _open(qtbot, window)
    label = window._table.horizontalHeaderItem(column).text()
    action = next(
        a for a in menu.actions() if a.menu() is not None and a.text() == label
    )
    _choose(qtbot, menu, action)
    qtbot.keyClick(menu, Qt.Key.Key_Right)
    submenu = action.menu()
    qtbot.waitUntil(submenu.isVisible)
    target = next(
        a
        for a in submenu.actions()
        if a.text() == ("Descending" if descending else "Ascending")
    )
    _choose(qtbot, submenu, target)
    qtbot.keyClick(submenu, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not window._sort_menu.isVisible())


def test_sort_control_is_keyboard_focusable_native_popup(sort_window, qtbot):
    window = sort_window
    assert hasattr(window, "_sort_button")
    assert window._sort_button.focusPolicy() != Qt.FocusPolicy.NoFocus
    qtbot.mouseClick(
        window._role_filter_bar._buttons["DAMAGER"], Qt.MouseButton.LeftButton
    )
    reset = window._role_filter_bar._reset_btn
    assert not reset.isHidden()
    reset.setFocus()
    qtbot.keyClick(reset, Qt.Key.Key_Tab)
    assert window._sort_button.hasFocus()
    menu = _open(qtbot, window)
    assert isinstance(menu, QMenu)
    qtbot.keyClick(menu, Qt.Key.Key_Escape)
    qtbot.waitUntil(window._sort_button.hasFocus)


@pytest.mark.parametrize("descending", [False, True])
def test_keyboard_directions_preserve_groups_unready_pin_and_no_fetch(
    sort_window, qtbot, descending
):
    window = sort_window
    window._pin_applicant_id("1:2")
    window._table.setCurrentCell(0, COL_NAME)
    qtbot.keyClick(window._table, Qt.Key.Key_Down)
    keyboard_id = window._keyboard_id
    assert keyboard_id is not None
    _column(qtbot, window, COL_H, descending)
    assert window._active_manual_sort() == (COL_H, descending)
    assert window._id_by_row[-1] == "3:1"
    expected = ["2:1", "1:1", "1:2"] if descending else ["1:1", "1:2", "2:1"]
    assert window._id_by_row[:-1] == expected
    assert window._pinned_id == "1:2"
    assert window._keyboard_id == keyboard_id
    assert window._id_by_row[window._table.currentRow()] == keyboard_id
    assert window._pool.tasks == []
    assert set(window._row_for_id) == set(window._state.applicants)


def test_keyboard_default_reset_and_per_tab_sort(sort_window, qtbot):
    window = sort_window
    default = list(window._id_by_row)
    _column(qtbot, window, COL_NAME, True)
    qtbot.mouseClick(window._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    _column(qtbot, window, COL_NAME, False)
    assert window._active_manual_sort() == (COL_NAME, False)
    qtbot.mouseClick(window._tab_bar._buttons["applicants"], Qt.MouseButton.LeftButton)
    assert window._active_manual_sort() == (COL_NAME, True)
    menu = _open(qtbot, window)
    action = next(a for a in menu.actions() if "Default" in a.text())
    _choose(qtbot, menu, action)
    qtbot.keyClick(menu, Qt.Key.Key_Return)
    assert window._active_manual_sort() is None
    assert window._id_by_row == default
    assert window._sort_by_tab["party"] == (COL_NAME, False)


def test_menu_uses_current_visible_labels_and_checks_selection(sort_window, qtbot):
    window = sort_window
    window._set_manual_sort(COL_ILVL, True)
    window._table.setColumnHidden(COL_NAME, True)
    window._table.horizontalHeaderItem(COL_ILVL).setText("Changed gear label")
    menu = _open(qtbot, window)
    titles = [a.text() for a in menu.actions() if a.menu() is not None]
    assert "Changed gear label" in titles
    assert window._table.horizontalHeaderItem(COL_NAME).text() not in titles
    submenu = next(a.menu() for a in menu.actions() if a.text() == "Changed gear label")
    assert next(a for a in submenu.actions() if a.text() == "Descending").isChecked()
    assert not next(a for a in submenu.actions() if a.text() == "Ascending").isChecked()
    qtbot.keyClick(menu, Qt.Key.Key_Escape)


@pytest.mark.parametrize("column", [None, COL_H])
def test_stale_sort_dispatch_cannot_change_new_source_sort(sort_window, qtbot, column):
    window = sort_window
    window._sort_by_tab["party"] = (COL_NAME, False)
    menu = _open(qtbot, window)
    old_context = window._sort_menu_context
    window._select_tab_state("party")
    assert not menu.isVisible()
    window._apply_sort_menu_choice(old_context, column, True)
    assert window._active_manual_sort() == (COL_NAME, False)
    assert window._pool.tasks == []


@pytest.mark.parametrize("change", ["visibility", "header"])
def test_popup_survives_stable_refresh_but_closes_when_context_changes(
    sort_window, qtbot, change
):
    window = sort_window
    menu = _open(qtbot, window)
    window._refresh_table()
    assert menu.isVisible()
    old_context = window._sort_menu_context
    if change == "visibility":
        window.apply_metric_preferences(MetricPreferences(raid_heroic=False))
    else:
        window._table.horizontalHeaderItem(COL_NAME).setText("Updated identity")
        window._update_sort_accessibility()
    assert not menu.isVisible()
    window._apply_sort_menu_choice(old_context, COL_H, True)
    assert window._active_manual_sort() is None


def test_native_accessibility_describes_current_source_and_order(sort_window, qtbot):
    window = sort_window
    interface = QAccessible.queryAccessibleInterface(window._sort_button)
    assert interface is not None
    assert interface.text(QAccessible.Text.Name) == "Sort applicants"
    assert "default grouped order" in interface.text(QAccessible.Text.Description)
    _column(qtbot, window, COL_NAME, False)
    assert "ascending" in interface.text(QAccessible.Text.Description)
    assert window._table.horizontalHeaderItem(COL_NAME).text() in interface.text(
        QAccessible.Text.Description
    )
    qtbot.mouseClick(window._tab_bar._buttons["party"], Qt.MouseButton.LeftButton)
    assert interface.text(QAccessible.Text.Name) == "Sort party"
    assert "default grouped order" in interface.text(QAccessible.Text.Description)


def test_hiding_overlay_closes_native_popup(sort_window, qtbot):
    window = sort_window
    menu = _open(qtbot, window)
    window.hide()
    assert not menu.isVisible()


@pytest.mark.parametrize("role", ["TANK", "HEALER", "DAMAGER"])
def test_minimum_width_active_filter_preserves_buttons_and_wide_status(
    sort_window, qtbot, role
):
    window = sort_window
    window.resize(900, window.height())
    QApplication.processEvents()
    bar = window._role_filter_bar
    full_texts = {key: button.text() for key, button in bar._buttons.items()}
    qtbot.mouseClick(window._role_filter_bar._buttons[role], Qt.MouseButton.LeftButton)
    window.resize(USER_MIN_WINDOW_WIDTH, window.height())
    QApplication.processEvents()
    bar = window._role_filter_bar
    bar.layout().activate()
    buttons = [*bar._buttons.values(), bar._reset_btn, window._sort_button]
    assert not bar._reset_btn.isHidden() and not window._sort_button.isHidden()
    geometries = []
    for button in buttons:
        assert not button.isHidden()
        minimum = (
            button.minimumWidth()
            if button is bar._reset_btn
            else button.minimumSizeHint().width()
        )
        assert button.width() >= minimum
        geometry = button.geometry()
        assert geometry.left() >= 0 and geometry.right() < bar.width()
        geometries.append(geometry)
    for left, right in zip(geometries, geometries[1:]):
        assert left.right() < right.left()
    assert bar._status.isHidden()
    assert bar.layout().contentsMargins().left() == 4
    window.resize(900, window.height())
    QApplication.processEvents()
    bar.layout().activate()
    expected_status = "" if role == "DAMAGER" else "showing 0 / 3 entries"
    assert bar._status.text() == expected_status
    assert bar._status.isHidden() == (not bool(expected_status))
    assert {key: button.text() for key, button in bar._buttons.items()} == full_texts
    assert all(not button.toolTip() for button in bar._buttons.values())
    assert bar.layout().contentsMargins().left() == 8
    assert bar.layout().spacing() == 5
