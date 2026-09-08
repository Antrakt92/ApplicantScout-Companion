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


def test_wcl_example_uses_fallback_when_screen_is_unavailable(qtbot, tmp_path, monkeypatch):
    popup, available = _example_popup(
        qtbot, tmp_path, monkeypatch, 1024, 768, screen_available=False
    )
    assert available.contains(popup.frameGeometry())
    assert popup.findChild(QDialogButtonBox) is not None


@pytest.mark.parametrize("screen_size", [(640, 480), (400, 400)])
def test_wcl_example_fits_small_work_area_with_close_outside_scroll(
    qtbot, tmp_path: Path, monkeypatch, screen_size
):
    popup, available = _example_popup(qtbot, tmp_path, monkeypatch, *screen_size)
    assert popup.width() <= available.width() - 24
    assert popup.height() <= available.height() - 40
    assert available.contains(popup.frameGeometry())
    scroll = popup.findChild(QScrollArea)
    buttons = popup.findChild(QDialogButtonBox)
    assert scroll is not None
    assert buttons is not None
    close = buttons.button(QDialogButtonBox.StandardButton.Close)
    assert close is not None
    assert not scroll.isAncestorOf(close)
    assert popup.rect().contains(_rect_in(close, popup))
    assert scroll.verticalScrollBar().maximum() > 0

    close_rect = _rect_in(close, popup)
    for position in (0, scroll.verticalScrollBar().maximum(), 0):
        scroll.verticalScrollBar().setValue(position)
        QApplication.processEvents()
        assert close.isVisible()
        assert _rect_in(close, popup) == close_rect

    qtbot.mouseClick(close, Qt.MouseButton.LeftButton)
    assert not popup.isVisible()


@pytest.mark.parametrize("screen_size", [(640, 480), (400, 400)])
@pytest.mark.parametrize("cursor_at_end", [False, True])
def test_wcl_example_fields_and_copy_buttons_remain_reachable_by_scrolling(
    qtbot, tmp_path: Path, monkeypatch, screen_size, cursor_at_end
):
    copied = []

    class Clipboard:
        def setText(self, value):
            copied.append(value)

    monkeypatch.setattr(settings_mod.QApplication, "clipboard", lambda: Clipboard())
    popup, _ = _example_popup(qtbot, tmp_path, monkeypatch, *screen_size)
    scroll = popup.findChild(QScrollArea)
    assert scroll is not None
    viewport = scroll.viewport()
    assert viewport is not None
    for field_name, button_name, expected in (
        ("wclExampleApplicationName", "copyWclExampleApplicationName", "ApplicantScout"),
        ("wclExampleRedirectUrl", "copyWclExampleRedirectUrl", "http://localhost"),
    ):
        field = popup.findChild(QLineEdit, field_name)
        button = popup.findChild(QPushButton, button_name)
        assert field is not None and button is not None
        assert field.isReadOnly() and field.text() == expected
        field.setCursorPosition(len(field.text()) if cursor_at_end else 0)
        for control in (field, button):
            assert scroll.isAncestorOf(control)
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            content = scroll.widget()
            assert content is not None
            rect = _rect_in(control, content)
            # QLineEdit's ensureWidgetVisible target is its input cursor, which
            # may already be visible while the field's border is clipped.
            # Exercise reachability of the entire control, not that cursor.
            scroll.ensureVisible(
                rect.center().x(), rect.center().y(),
                rect.width() // 2 + 1, rect.height() // 2 + 1,
            )
            QApplication.processEvents()
            assert viewport.rect().contains(_rect_in(control, viewport))
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        assert copied[-1] == expected

    public_client = popup.findChild(QCheckBox, "wclExamplePublicClientUnchecked")
    assert public_client is not None
    assert not public_client.isChecked() and not public_client.isEnabled()
    image = popup.findChild(QLabel, "wclSetupExampleImage")
    assert image is not None and scroll.isAncestorOf(image)
    assert image.pixmap() is not None and not image.pixmap().isNull()


def test_missing_example_image_keeps_instructions_and_close_reachable(
    qtbot, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(settings_mod, "WCL_CREATE_CLIENT_EXAMPLE_PATH", tmp_path / "absent.png")
    popup, available = _example_popup(qtbot, tmp_path, monkeypatch, 400, 400)
    image = popup.findChild(QLabel, "wclSetupExampleImage")
    scroll = popup.findChild(QScrollArea)
    assert image is not None and scroll is not None
    assert "http://localhost" in image.text()
    assert "Public Client unchecked" in image.text()
    scroll.ensureWidgetVisible(image, 0, 0)
    QApplication.processEvents()
    assert scroll.viewport().rect().contains(_rect_in(image, scroll.viewport()))
    assert available.contains(popup.frameGeometry())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows release-runner style matrix")
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
         "-q", "-k", "fields_and_copy_buttons", "--tb=short"],
        cwd=Path(__file__).resolve().parents[1], env=child_env,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
