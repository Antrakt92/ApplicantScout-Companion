"""Visual QA smoke checks for the Settings dialog fixture."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

if "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "windows" if sys.platform == "win32" else "offscreen"

import pytest
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QToolButton, QWidget

from applicant_scout import __version__
from scripts import render_settings_dialog_fixture
from scripts.settings_dialog_visual_fixture import (
    DEFAULT_SETTINGS_VISUAL_SCENARIO,
    SETTINGS_DIALOG_VISUAL_BASELINE_PATH,
    SETTINGS_DIALOG_VISUAL_SCENARIOS,
    SETTINGS_VISUAL_FIXTURE_REGEN_COMMAND,
    create_settings_visual_dialog,
    grab_settings_visual_image,
    show_settings_visual_dialog,
)
from scripts.visual_fixture_checks import validate_smoke_image


def test_settings_visual_fixture_scenarios_are_small_and_unique():
    assert DEFAULT_SETTINGS_VISUAL_SCENARIO == "normal-default"
    assert set(SETTINGS_DIALOG_VISUAL_SCENARIOS) == {
        "normal-default",
        "first-run",
        "saved",
        "validation-error",
        "save-warning",
        "update-available",
        "update-installing-disabled",
    }
    assert (
        SETTINGS_DIALOG_VISUAL_SCENARIOS[DEFAULT_SETTINGS_VISUAL_SCENARIO].baseline_path
        == SETTINGS_DIALOG_VISUAL_BASELINE_PATH
    )
    assert len(
        {
            scenario.baseline_path
            for scenario in SETTINGS_DIALOG_VISUAL_SCENARIOS.values()
        }
    ) == len(SETTINGS_DIALOG_VISUAL_SCENARIOS)
    assert "--all" in SETTINGS_VISUAL_FIXTURE_REGEN_COMMAND


def test_render_settings_dialog_fixture_cli_defaults_to_single_default_scenario():
    args = render_settings_dialog_fixture.parse_args([])

    assert args.scenario == DEFAULT_SETTINGS_VISUAL_SCENARIO
    assert not args.all
    assert args.visual_mode == "strict"


def test_render_settings_dialog_fixture_cli_accepts_all_scenarios():
    args = render_settings_dialog_fixture.parse_args(["--check", "--all"])

    assert args.check
    assert args.all


def test_render_settings_dialog_fixture_cli_accepts_explicit_smoke_check_mode():
    args = render_settings_dialog_fixture.parse_args(
        ["--check", "--all", "--visual-mode", "smoke"]
    )

    assert args.check
    assert args.visual_mode == "smoke"


def test_render_settings_dialog_fixture_cli_rejects_smoke_without_check():
    with pytest.raises(SystemExit):
        render_settings_dialog_fixture.parse_args(["--visual-mode", "smoke"])


def test_render_settings_dialog_fixture_cli_rejects_output_with_all():
    with pytest.raises(SystemExit):
        render_settings_dialog_fixture.parse_args(["--all", "--output", "out.png"])


@pytest.mark.parametrize("scenario_name", sorted(SETTINGS_DIALOG_VISUAL_SCENARIOS))
def test_settings_visual_fixture_scenarios_render_nonblank(
    qtbot, scenario_name: str
):
    dialog = create_settings_visual_dialog(scenario_name)
    qtbot.addWidget(dialog)

    show_settings_visual_dialog(dialog, process_events=QApplication.processEvents)

    pixmap = grab_settings_visual_image(dialog)
    assert not pixmap.isNull()
    image = pixmap.toImage()
    assert validate_smoke_image(image, label="settings dialog visual fixture") is None
    scenario = SETTINGS_DIALOG_VISUAL_SCENARIOS[scenario_name]
    assert scenario.baseline_path.name.startswith("settings-dialog-fixture")


def test_settings_visual_fixture_normal_state_uses_stable_fake_inputs(qtbot):
    dialog = create_settings_visual_dialog("normal-default")
    qtbot.addWidget(dialog)

    assert dialog.client_id_edit.text() == "fixture-client-id"
    assert dialog.client_secret_edit.text() == "fixture-client-secret"
    assert dialog.screenshots_edit.text() == ""
    assert dialog.status_label.text() == ""
    assert "Users" not in dialog.screenshots_edit.text()


def test_settings_visual_fixture_update_available_state_shows_install_button(qtbot):
    dialog = create_settings_visual_dialog("update-available")
    qtbot.addWidget(dialog)

    update_button = dialog.findChild(QToolButton, "installUpdate")
    assert update_button is not None
    assert not update_button.isHidden()
    assert update_button.isEnabled()
    assert "v0.8.0" in update_button.toolTip()


def test_settings_visual_fixture_installer_state_disables_controls_without_async(
    qtbot,
):
    dialog = create_settings_visual_dialog("update-installing-disabled")
    qtbot.addWidget(dialog)

    update_button = dialog.findChild(QToolButton, "installUpdate")
    assert update_button is not None
    assert not update_button.isHidden()
    assert not update_button.isEnabled()
    assert update_button.accessibleDescription() == (
        "Installing ApplicantScout update..."
    )
    assert dialog.status_label.text() == "Installing update."
    assert dialog.status_label.property("statusState") == "busy"
    assert not dialog.client_id_edit.isEnabled()
    assert not dialog.client_secret_edit.isEnabled()
    assert not dialog.region_combo.isEnabled()
    assert not dialog.screenshots_edit.isEnabled()
    assert not dialog.wcl_clients_link.isEnabled()
    assert not dialog.support_button.isEnabled()
    assert not dialog.close_button.isEnabled()


@pytest.mark.parametrize(
    ("scenario_name", "expected_state", "expected_text"),
    [
        ("saved", "success", "Saved."),
        (
            "validation-error",
            "error",
            "WCL Client ID and Secret are required.",
        ),
        (
            "save-warning",
            "warning",
            "Saved for this app session, but environment overrides are active.",
        ),
    ],
)
def test_settings_visual_fixture_status_states_are_semantic(
    qtbot,
    scenario_name: str,
    expected_state: str,
    expected_text: str,
):
    dialog = create_settings_visual_dialog(scenario_name)
    qtbot.addWidget(dialog)

    assert dialog.status_label.property("statusState") == expected_state
    assert dialog.status_label.text() == expected_text
    assert dialog.status_label.accessibleDescription() == expected_text


def test_settings_visual_fixture_first_run_layout_has_setup_buttons(qtbot):
    dialog = create_settings_visual_dialog("first-run")
    qtbot.addWidget(dialog)

    start_button = dialog.findChild(QPushButton, "startCompanion")
    quit_button = dialog.findChild(QPushButton, "quitApplicantScout")
    assert start_button is not None
    assert start_button.text() == "Start companion"
    assert quit_button is not None
    assert quit_button.text() == "Quit setup"
    assert dialog.close_button.accessibleName() == "Close setup"


def test_settings_visual_fixture_first_run_uses_real_metric_defaults(qtbot):
    dialog = create_settings_visual_dialog("first-run")
    qtbot.addWidget(dialog)

    assert dialog.mplus_check.isChecked()
    assert dialog.raid_normal_check.isChecked()
    assert dialog.raid_heroic_check.isChecked()
    assert dialog.raid_mythic_check.isChecked()
    assert dialog.sync_with_wow_check.isChecked()


@pytest.mark.parametrize("scenario, height_limit", [("normal-default", 510), ("first-run", 660)])
def test_settings_compact_body_keeps_footer_next_to_content(qtbot, monkeypatch, scenario, height_limit):
    # Qt's automatic initial size is capped at two thirds of the host screen.
    # Measure the unscrolled form at its preferred size on a controlled screen;
    # constrained-screen scrolling is covered separately below.
    screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1600, 900))
    monkeypatch.setattr(QApplication, "screens", lambda: [screen])
    monkeypatch.setattr(QApplication, "primaryScreen", lambda: screen)
    dialog = create_settings_visual_dialog(scenario)
    qtbot.addWidget(dialog)
    dialog.ensurePolished()
    dialog.resize(dialog.sizeHint())
    dialog.show()
    QApplication.processEvents()
    assert dialog.height() <= height_limit
    if scenario == "first-run":
        assert dialog.autosave_hint.text() == "Saved when you start"
        assert "usage statistics choice saves immediately" in dialog.autosave_hint.accessibleDescription()
        assert dialog.findChild(QLabel, "settingsHeroEyebrow") is None
        assert "save" not in dialog.findChild(QLabel, "settingsHeroText").text().lower()
    else:
        assert dialog.autosave_hint.text() == "Valid changes save automatically"
    usage = dialog.findChild(QWidget, "usageStatisticsSection")
    footer = dialog.findChild(QWidget, "settingsFooter")
    assert usage is not None and footer is not None
    usage_bottom = usage.mapTo(dialog, QPoint(0, usage.height())).y()
    footer_top = footer.mapTo(dialog, QPoint()).y()
    assert 0 <= footer_top - usage_bottom <= 24
    scrollbar = dialog.body_scroll.verticalScrollBar()
    assert scrollbar is not None and scrollbar.maximum() == 0
    privacy = dialog.findChild(QLabel, "usagePrivacyLink")
    unavailable = dialog.findChild(QLabel, "usageUnavailableStatus")
    assert privacy is not None and unavailable is not None
    assert privacy.openExternalLinks() and "PRIVACY.md" in privacy.text()
    assert "active storage after 90 UTC days" in dialog.usage_check.toolTip()
    assert "recovery history can last 7 more days" in dialog.usage_check.toolTip()
    assert "Existing choices are preserved" in dialog.usage_check.accessibleDescription()
    assert dialog.usage_check.isChecked() and dialog.usage_check.isEnabled()
    for control in (dialog.usage_check, privacy, unavailable):
        rect = QRect(control.mapTo(usage, QPoint()), control.size())
        assert usage.rect().contains(rect)
    assert unavailable.fontMetrics().horizontalAdvance(unavailable.text()) <= unavailable.width()


@pytest.mark.parametrize("scenario", ["normal-default", "first-run"])
def test_compact_actions_and_form_stay_reachable_on_small_screen(qtbot, monkeypatch, scenario):
    bounds = QRect(0, 0, 800, 360)
    screen = SimpleNamespace(availableGeometry=lambda: bounds)
    monkeypatch.setattr(QApplication, "screens", lambda: [screen])
    monkeypatch.setattr(QApplication, "primaryScreen", lambda: screen)
    dialog = create_settings_visual_dialog(scenario)
    qtbot.addWidget(dialog)
    dialog.resize(dialog.minimumWidth(), 700)
    dialog.show()
    QApplication.processEvents()
    assert dialog.height() == 360
    assert bounds.contains(dialog.geometry())
    vertical = dialog.body_scroll.verticalScrollBar()
    horizontal = dialog.body_scroll.horizontalScrollBar()
    assert vertical is not None and vertical.maximum() > 0
    assert horizontal is not None and horizontal.maximum() == 0
    actions = [dialog.test_button]
    if scenario == "first-run":
        assert dialog.start_button is not None and dialog.setup_quit_button is not None
        actions.extend([dialog.start_button, dialog.setup_quit_button])
    for control in actions:
        assert control is not None and control.isVisible()
        assert dialog.rect().contains(QRect(control.mapTo(dialog, QPoint()), control.size()))
    viewport = dialog.body_scroll.viewport()
    body = dialog.body_scroll.widget()
    assert viewport is not None and body is not None
    for control in (dialog.client_id_edit, dialog.client_secret_edit, dialog.screenshots_edit,
                    dialog.sync_with_wow_check, dialog.usage_check):
        # Scroll the whole control into view; ensureWidgetVisible on QLineEdit
        # follows its cursor rectangle rather than the complete field.
        vertical.setValue(control.mapTo(body, QPoint()).y())
        QApplication.processEvents()
        assert viewport.rect().contains(QRect(control.mapTo(viewport, QPoint()), control.size()))


def test_settings_keyboard_order_follows_credentials_then_help_and_scouting(qtbot):
    dialog = create_settings_visual_dialog("normal-default")
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.activateWindow()
    dialog.client_id_edit.setFocus()
    QApplication.processEvents()
    privacy = dialog.findChild(QLabel, "usagePrivacyLink")
    expected = (
        dialog.client_secret_edit, dialog.reveal_secret_button, dialog.region_combo,
        dialog.wcl_clients_link, dialog.wcl_example_button, dialog.screenshots_edit,
        dialog.browse_button, dialog.raid_normal_check, dialog.raid_heroic_check,
        dialog.raid_mythic_check, dialog.mplus_check, dialog.sync_with_wow_check,
        dialog.usage_check, privacy,
    )
    for control in expected:
        qtbot.keyClick(dialog.focusWidget(), Qt.Key.Key_Tab)
        assert dialog.focusWidget() is control


def test_settings_visual_fixture_titles_are_version_stable(qtbot):
    normal = create_settings_visual_dialog("normal-default")
    first_run = create_settings_visual_dialog("first-run")
    qtbot.addWidget(normal)
    qtbot.addWidget(first_run)

    assert __version__ not in normal.windowTitle()
    assert __version__ not in normal.title_label.text()
    assert normal.windowTitle() == "ApplicantScout Companion · Visual QA"
    assert first_run.windowTitle() == (
        "ApplicantScout Companion · First-run setup · Visual QA"
    )
    assert first_run.title_label.text() == first_run.windowTitle()


def test_settings_visual_fixture_no_tray_close_copy_is_assertion_only(qtbot):
    dialog = create_settings_visual_dialog(
        "normal-default",
        hide_to_tray_on_close=False,
    )
    qtbot.addWidget(dialog)

    assert "no-tray" not in SETTINGS_DIALOG_VISUAL_SCENARIOS
    assert dialog.close_button.toolTip() == "Quit ApplicantScout."
    assert dialog.close_button.accessibleName() == "Quit ApplicantScout"
    assert dialog.close_button.accessibleDescription() == "Quit ApplicantScout."


def _solid_image(width: int, height: int, color: QColor) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    return image


class _FakePixmap:
    def __init__(self, image: QImage) -> None:
        self._image = image

    def toImage(self) -> QImage:
        return self._image


class _FakeScenario:
    baseline_path = "baseline.png"


def test_render_settings_dialog_fixture_strict_check_uses_pixel_baseline(monkeypatch):
    baseline = _solid_image(4, 4, QColor(10, 20, 30, 255))
    actual = _solid_image(4, 4, QColor(10, 20, 30, 255))
    calls: list[tuple[QImage, QImage]] = []
    original_compare = render_settings_dialog_fixture.compare_settings_visual_images

    def fake_compare(expected: QImage, rendered: QImage):
        calls.append((expected, rendered))
        return original_compare(expected, rendered)

    monkeypatch.setattr(render_settings_dialog_fixture, "QImage", lambda _path: baseline)
    monkeypatch.setattr(
        render_settings_dialog_fixture,
        "compare_settings_visual_images",
        fake_compare,
    )

    passed, message = render_settings_dialog_fixture._check_rendered_pixmap(
        "sample", _FakeScenario(), _FakePixmap(actual), "strict"
    )

    assert passed
    assert "matched committed baseline" in message
    assert calls == [(baseline, actual)]


def test_render_settings_dialog_fixture_smoke_check_skips_pixel_baseline(monkeypatch):
    actual = _solid_image(6, 6, QColor(10, 20, 30, 255))
    actual.setPixelColor(0, 0, QColor(240, 240, 240, 255))

    def fail_if_called(*_args):
        raise AssertionError("smoke mode must not compare against pixel baselines")

    monkeypatch.setattr(render_settings_dialog_fixture, "QImage", fail_if_called)
    monkeypatch.setattr(
        render_settings_dialog_fixture,
        "compare_settings_visual_images",
        fail_if_called,
    )

    passed, message = render_settings_dialog_fixture._check_rendered_pixmap(
        "sample", _FakeScenario(), _FakePixmap(actual), "smoke"
    )

    assert passed
    assert "smoke check passed" in message


def test_render_settings_dialog_fixture_smoke_check_rejects_blank_render():
    actual = _solid_image(6, 6, QColor(10, 20, 30, 255))

    passed, message = render_settings_dialog_fixture._check_rendered_pixmap(
        "sample", _FakeScenario(), _FakePixmap(actual), "smoke"
    )

    assert not passed
    assert "blank or uniform" in message


def test_settings_visual_fixture_keeps_password_field_masked(qtbot):
    dialog = create_settings_visual_dialog("normal-default")
    qtbot.addWidget(dialog)

    secret_field = dialog.findChild(QLineEdit, "wclClientSecret")
    assert secret_field is not None
    assert secret_field.echoMode() == QLineEdit.EchoMode.Password
