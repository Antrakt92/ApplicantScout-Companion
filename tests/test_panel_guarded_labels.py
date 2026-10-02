"""Repeated panel updates avoid redundant native label mutations."""

from collections import Counter

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QLabel

from applicant_scout.overlay import ApplicantInfoPanel, _IdentityLabel
from test_info_panel_widget import _app, _raid_listing


def _values(panel):
    return [
        (
            label.text(),
            label.styleSheet(),
            label.toolTip(),
            label.accessibleName(),
            label.accessibleDescription(),
            label.isHidden(),
        )
        for label in panel.findChildren(QLabel)
    ]


def test_repeated_raid_update_avoids_identical_native_setters(qtbot, monkeypatch):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    app = _app()
    listing = _raid_listing()
    panel.setApplicantData(app, listing)
    expected = _values(panel)
    redundant = Counter()
    for setter, getter in (
        ("setText", "text"),
        ("setStyleSheet", "styleSheet"),
        ("setToolTip", "toolTip"),
        ("setAccessibleName", "accessibleName"),
        ("setAccessibleDescription", "accessibleDescription"),
    ):
        original = getattr(QLabel, setter)

        def counted(self, value, _original=original, _getter=getter, _setter=setter):
            if getattr(self, _getter)() == value:
                redundant[_setter] += 1
            return _original(self, value)

        monkeypatch.setattr(QLabel, setter, counted)
    for _ in range(3):
        panel.setApplicantData(app, listing)
    assert _values(panel) == expected
    assert not redundant


def test_identity_equal_text_restores_metadata_after_direct_qt_mutation(qtbot):
    label = _IdentityLabel("Original")
    qtbot.addWidget(label)
    label.setText("Scout")
    QLabel.setToolTip(label, "wrong tooltip")
    QLabel.setAccessibleName(label, "wrong identity")
    label.setText("Scout")
    assert label.text() == label.toolTip() == label.accessibleName() == "Scout"
    QLabel.setText(label, "changed outside wrapper")
    label.setText("Scout")
    assert label.text() == "Scout"


def test_equal_identity_update_preserves_selected_text(qtbot):
    label = _IdentityLabel("Scout-Realm")
    qtbot.addWidget(label)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSelection(0, 5)
    assert label.selectedText() == "Scout"
    label.setText("Scout-Realm")
    assert label.selectedText() == "Scout"


@pytest.mark.parametrize("status", ["ready", "error", "restricted"])
def test_changed_state_matches_fresh_panel_and_placeholder_clears(qtbot, status):
    panel = ApplicantInfoPanel(None)
    fresh = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    qtbot.addWidget(fresh)
    panel.setApplicantData(_app(), _raid_listing())
    fresh.setApplicantData(_app(), _raid_listing())
    updated = _app(
        name="Changed-Other",
        fetch_status=status,
        error_message="Unavailable",
        raid_heroic=12,
    )
    panel.setApplicantData(updated, _raid_listing(), wcl_retry_available=True)
    fresh.setApplicantData(updated, _raid_listing(), wcl_retry_available=True)
    assert _values(panel) == _values(fresh)
    panel.setPlaceholder()
    fresh.setPlaceholder()
    assert _values(panel) == _values(fresh)
    assert panel._wcl_retry_button.isHidden()


@pytest.mark.parametrize(
    "setter,getter,value",
    [
        ("setText", "text", "Expected"),
        ("setStyleSheet", "styleSheet", "color: red;"),
        ("setToolTip", "toolTip", "Expected tooltip"),
        ("setAccessibleName", "accessibleName", "Expected name"),
        ("setAccessibleDescription", "accessibleDescription", "Expected description"),
    ],
)
def test_panel_label_guard_recovers_from_direct_base_qt_mutation(
    qtbot, setter, getter, value
):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    label = panel._status_label
    getattr(label, setter)(value)
    getattr(QLabel, setter)(label, "Changed through native API")
    assert getattr(label, getter)() != value
    getattr(label, setter)(value)
    assert getattr(label, getter)() == value


def test_hiding_retry_action_releases_focus(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.show()
    panel.activateWindow()
    panel.setApplicantData(
        _app(fetch_status="error", error_message="Network unavailable"),
        _raid_listing(),
        wcl_retry_available=True,
    )
    panel._wcl_retry_button.setFocus()
    qtbot.waitUntil(panel._wcl_retry_button.hasFocus)
    panel.setApplicantData(_app(), _raid_listing())
    assert panel._wcl_retry_button.isHidden()
    assert not panel._wcl_retry_button.hasFocus()


def test_native_accessible_interface_reads_changed_label_metadata(qtbot):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.show()
    label = panel._status_label
    label.setText("Visible status")
    label.setVisible(True)
    interface = QAccessible.queryAccessibleInterface(label)
    assert interface is not None
    for name, description in (
        ("Initial status", "Initial details"),
        ("Changed status", "Changed details"),
    ):
        label.setAccessibleName(name)
        label.setAccessibleDescription(description)
        assert interface.text(QAccessible.Text.Name) == name
        assert interface.text(QAccessible.Text.Description) == description


def test_changed_accessibility_delegates_once_and_equal_values_skip_native(
    qtbot, monkeypatch
):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    label = panel._status_label
    calls = Counter()
    for setter in ("setAccessibleName", "setAccessibleDescription"):
        original = getattr(QLabel, setter)

        def counted(self, value, _setter=setter, _original=original):
            calls[_setter] += 1
            return _original(self, value)

        monkeypatch.setattr(QLabel, setter, counted)
    label.setAccessibleName("New status identity")
    label.setAccessibleDescription("New status details")
    assert calls == {"setAccessibleName": 1, "setAccessibleDescription": 1}
    label.setAccessibleName("New status identity")
    label.setAccessibleDescription("New status details")
    assert calls == {"setAccessibleName": 1, "setAccessibleDescription": 1}
