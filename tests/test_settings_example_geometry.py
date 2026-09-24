from pathlib import Path
import os
import subprocess
import sys

import pytest
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
)

from applicant_scout.config import Config
import applicant_scout.settings_dialog as settings_mod
from applicant_scout.settings_dialog import SettingsDialog


def _example_popup(qtbot, tmp_path, monkeypatch, width, height, *, screen_available=True):
    retail = tmp_path / "World of Warcraft" / "_retail_"
    (retail / "Interface" / "AddOns").mkdir(parents=True)
    config = Config(
        wcl_client_id="",
        wcl_client_secret="",
        chatlog_path=retail / "Logs" / "WoWChatLog.txt",
        region="EU",
        cache_dir=tmp_path / "cache",
        config_dir=tmp_path / "config",
        screenshots_path=retail / "Screenshots",
        log_dir=tmp_path / "logs",
    )
    dialog = SettingsDialog(config, first_run=True)
    qtbot.addWidget(dialog)
    available = QRect(0, 0, width, height)

    class SmallScreen:
        def availableGeometry(self):
            return QRect(available)

    monkeypatch.setattr(dialog, "screen", lambda: SmallScreen() if screen_available else None)
    popup = dialog._build_wcl_setup_example_dialog()
    qtbot.addWidget(popup)
    popup.show()
    QApplication.processEvents()
    # The window manager still sees the physical screen. Place its frame inside
    # the simulated work area to exercise the actual constrained widget layout.
    popup.move(available.topLeft())
    QApplication.processEvents()
    return popup, available


def _rect_in(widget, ancestor):
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


def _assert_control_visible(control, popup):
    assert control.isVisible()
    rect = _rect_in(control, popup)
    assert popup.contentsRect().contains(rect)
    assert popup.childAt(rect.center()) is control


def _assert_no_horizontal_scroll(popup):
    for scroll in popup.findChildren(QScrollArea):
        assert scroll.horizontalScrollBar().maximum() == 0


def _assert_wrapped_instructions_readable(popup):
    for label in popup.findChildren(QLabel):
        if label.text() and label.wordWrap():
            assert label.height() >= label.heightForWidth(label.width())
            assert popup.contentsRect().contains(_rect_in(label, popup))


def test_wcl_example_uses_fallback_when_screen_is_unavailable(qtbot, tmp_path, monkeypatch):
    popup, available = _example_popup(
        qtbot, tmp_path, monkeypatch, 1024, 768, screen_available=False
    )
    assert available.contains(popup.frameGeometry())
    assert popup.findChild(QDialogButtonBox) is not None


@pytest.mark.parametrize("screen_size", [(640, 480), (400, 400)])
def test_wcl_example_fits_small_work_area_with_close_always_visible(
    qtbot, tmp_path: Path, monkeypatch, screen_size
):
    popup, available = _example_popup(qtbot, tmp_path, monkeypatch, *screen_size)
    assert popup.width() <= available.width() - 24
    assert popup.height() <= available.height() - 40
    assert available.contains(popup.frameGeometry())
    buttons = popup.findChild(QDialogButtonBox)
    assert buttons is not None
    close = buttons.button(QDialogButtonBox.StandardButton.Close)
    assert close is not None
    _assert_control_visible(close, popup)
    _assert_no_horizontal_scroll(popup)
    _assert_wrapped_instructions_readable(popup)
    image = popup.findChild(QLabel, "wclSetupExampleImage")
    assert image is not None
    assert popup.contentsRect().contains(_rect_in(image, popup))
    assert not image.pixmap().isNull()
    assert image.pixmap().width() <= image.contentsRect().width()
    assert image.pixmap().height() <= image.contentsRect().height()

    qtbot.mouseClick(close, Qt.MouseButton.LeftButton)
    assert not popup.isVisible()


@pytest.mark.parametrize("screen_size", [(640, 480), (400, 400)])
@pytest.mark.parametrize("cursor_at_end", [False, True])
def test_wcl_example_fields_and_copy_buttons_are_fully_visible_without_scrolling(
    qtbot, tmp_path: Path, monkeypatch, screen_size, cursor_at_end
):
    copied = []

    class Clipboard:
        def setText(self, value):
            copied.append(value)

    monkeypatch.setattr(settings_mod.QApplication, "clipboard", lambda: Clipboard())
    popup, _ = _example_popup(qtbot, tmp_path, monkeypatch, *screen_size)
    _assert_no_horizontal_scroll(popup)
    for field_name, button_name, expected in (
        ("wclExampleApplicationName", "copyWclExampleApplicationName", "ApplicantScout"),
        ("wclExampleRedirectUrl", "copyWclExampleRedirectUrl", "http://localhost"),
    ):
        field = popup.findChild(QLineEdit, field_name)
        button = popup.findChild(QPushButton, button_name)
        assert field is not None and button is not None
        assert field.isReadOnly() and field.text() == expected
        field.setCursorPosition(len(field.text()) if cursor_at_end else 0)
        QApplication.processEvents()
        for control in (field, button):
            _assert_control_visible(control, popup)
        assert field.width() >= field.fontMetrics().horizontalAdvance(expected) + 12
        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 12
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        assert copied[-1] == expected
        QApplication.processEvents()
        _assert_control_visible(field, popup)
        _assert_control_visible(button, popup)

    public_client = popup.findChild(QCheckBox, "wclExamplePublicClientUnchecked")
    assert public_client is not None
    assert not public_client.isChecked() and not public_client.isEnabled()
    _assert_control_visible(public_client, popup)
    image = popup.findChild(QLabel, "wclSetupExampleImage")
    assert image is not None
    assert image.pixmap() is not None and not image.pixmap().isNull()


def test_missing_example_image_keeps_instructions_and_close_reachable(
    qtbot, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(settings_mod, "WCL_CREATE_CLIENT_EXAMPLE_PATH", tmp_path / "absent.png")
    popup, available = _example_popup(qtbot, tmp_path, monkeypatch, 400, 400)
    image = popup.findChild(QLabel, "wclSetupExampleImage")
    assert image is not None
    assert "http://localhost" in image.text()
    assert "Public Client unchecked" in image.text()
    QApplication.processEvents()
    assert image.isVisible() and image.wordWrap()
    assert image.height() >= image.heightForWidth(image.width())
    assert popup.contentsRect().contains(_rect_in(image, popup))
    _assert_no_horizontal_scroll(popup)
    for control in popup.findChildren(QLineEdit) + popup.findChildren(QPushButton):
        _assert_control_visible(control, popup)
    assert available.contains(popup.frameGeometry())


@pytest.mark.parametrize("missing_image", [False, True])
@pytest.mark.real_display
def test_wcl_example_can_grow_then_shrink_to_small_work_area(
    qtbot, tmp_path, monkeypatch, missing_image
):
    if missing_image:
        monkeypatch.setattr(settings_mod, "WCL_CREATE_CLIENT_EXAMPLE_PATH", tmp_path / "absent.png")
    popup, _ = _example_popup(qtbot, tmp_path, monkeypatch, 1024, 768)
    image = popup.findChild(QLabel, "wclSetupExampleImage")
    assert image is not None
    for width, height in ((600, 500), (900, 700), (376, 360), (616, 440)):
        popup.resize(width, height)
        QApplication.processEvents()
        assert (popup.width(), popup.height()) == (width, height)
        _assert_no_horizontal_scroll(popup)
        _assert_wrapped_instructions_readable(popup)
        for control in popup.findChildren(QLineEdit) + popup.findChildren(QPushButton):
            _assert_control_visible(control, popup)
        for field in popup.findChildren(QLineEdit):
            assert field.width() >= field.fontMetrics().horizontalAdvance(field.text()) + 12
        assert popup.contentsRect().contains(_rect_in(image, popup))
        if not missing_image:
            assert not image.pixmap().isNull()
            assert image.pixmap().width() <= image.contentsRect().width()
            assert image.pixmap().height() <= image.contentsRect().height()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows release-runner style matrix")
@pytest.mark.real_display
def test_wcl_example_controls_with_windows_release_runner_style(qapp):
    # CI uses Windows Server at 100% scaling; a Windows 11 developer desktop at
    # 125% has different cursor/border rounding. Isolate the second QApplication
    # in a child process so neither the desktop nor this suite changes style/DPI.
    child_env = os.environ.copy()
    current_scale = float(child_env.get("QT_SCALE_FACTOR", "1"))
    child_env["QT_SCALE_FACTOR"] = str(current_scale / qapp.devicePixelRatio())
    child_env["QT_STYLE_OVERRIDE"] = "windowsvista"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__).resolve()),
         "-q", "-k", "not windows_release_runner_style", "--tb=short"],
        cwd=Path(__file__).resolve().parents[1], env=child_env,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
