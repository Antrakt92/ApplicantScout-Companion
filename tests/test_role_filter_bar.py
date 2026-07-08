"""Widget-level checks for the role filter toolbar.

These use pytest-qt because RoleFilterBar is real QWidget state, not pure
string formatting like the overlay HTML helpers.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt

from applicant_scout.overlay import (
    ROLE_FILTER_RESET_SIZE,
    ROLE_FILTER_RESET_TEXT,
    ROLE_FILTER_RESET_TOOLTIP,
    ROLE_ICON_FILES,
    RoleFilterBar,
    _role_icon,
)


def test_reset_button_clears_active_roles_once(qtbot):
    bar = RoleFilterBar()
    qtbot.addWidget(bar)
    bar.show()

    emitted: list[set[str]] = []
    bar.filterChanged.connect(lambda roles: emitted.append(set(roles)))

    reset_btn = bar._reset_btn
    assert reset_btn.text() == ROLE_FILTER_RESET_TEXT
    assert reset_btn.size() == ROLE_FILTER_RESET_SIZE
    assert reset_btn.isHidden()

    qtbot.mouseClick(bar._buttons["DAMAGER"], Qt.MouseButton.LeftButton)
    assert emitted == [{"DAMAGER"}]
    assert not reset_btn.isHidden()

    bar.set_status(visible=2, total=5)
    assert bar._status.text() == "showing 2 / 5 entries"

    qtbot.mouseClick(reset_btn, Qt.MouseButton.LeftButton)

    assert emitted == [{"DAMAGER"}, set()]
    assert bar._active == set()
    assert all(not btn.isChecked() for btn in bar._buttons.values())
    assert reset_btn.isHidden()
    assert bar._status.text() == ""


def test_reset_button_visible_for_all_roles_selected(qtbot):
    bar = RoleFilterBar()
    qtbot.addWidget(bar)
    bar.show()

    for role in ("TANK", "HEALER", "DAMAGER"):
        qtbot.mouseClick(bar._buttons[role], Qt.MouseButton.LeftButton)

    bar.set_status(visible=3, total=3)

    assert bar._active == {"TANK", "HEALER", "DAMAGER"}
    assert not bar._reset_btn.isHidden()
    assert bar._status.text() == ""


def test_role_filter_tooltips_and_tooltip_widget_order(qtbot):
    bar = RoleFilterBar()
    qtbot.addWidget(bar)

    assert bar._buttons["TANK"].toolTip() == "Show entries with a tank"
    assert bar._buttons["HEALER"].toolTip() == "Show entries with a healer"
    assert (
        bar._buttons["DAMAGER"].toolTip()
        == "Show entries with a damage dealer"
    )
    assert bar._reset_btn.toolTip() == ROLE_FILTER_RESET_TOOLTIP

    tooltip_widgets = bar.tooltip_widgets()

    assert tooltip_widgets == (
        bar._buttons["TANK"],
        bar._buttons["HEALER"],
        bar._buttons["DAMAGER"],
        bar._reset_btn,
    )
    assert len({id(widget) for widget in tooltip_widgets}) == 4


def test_role_icon_assets_load_for_all_roles(qtbot):
    bar = RoleFilterBar()
    qtbot.addWidget(bar)

    assert set(ROLE_ICON_FILES) == {"TANK", "HEALER", "DAMAGER"}
    for role in ROLE_ICON_FILES:
        icon = _role_icon(role)
        assert icon is not None
        assert not icon.isNull()
        assert not bar._buttons[role].icon().isNull()
