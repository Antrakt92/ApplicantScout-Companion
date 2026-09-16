"""The WCL example must fit its dialog without hiding usable copy controls."""
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtWidgets import QLabel, QLineEdit, QPushButton, QScrollArea

from applicant_scout.config import Config
import applicant_scout.settings_dialog as settings_mod
from applicant_scout.settings_dialog import SettingsDialog


def _cfg(root: Path) -> Config:
    return Config(
        wcl_client_id="example", wcl_client_secret="example", region="EU",
        chatlog_path=root / "Logs/WoWChatLog.txt", cache_dir=root / "cache",
        config_dir=root / "config", screenshots_path=root / "Screenshots",
    )


@pytest.mark.parametrize("width", [380, 560, 720])
def test_wcl_example_fits_image_and_keeps_copy_buttons_visible(qtbot, tmp_path, width):
    dialog = SettingsDialog(_cfg(tmp_path), first_run=True)
    qtbot.addWidget(dialog)
    popup = dialog._build_wcl_setup_example_dialog()
    qtbot.addWidget(popup)
    initial_width = popup.width()
    popup.resize(width, 600)
    popup.show()
    image = popup.findChild(QLabel, "wclSetupExampleImage")
    assert image is not None
    qtbot.waitUntil(lambda: image.isVisible() and image.width() > 0)
    pixmap = image.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    assert pixmap.width() <= image.contentsRect().width()
    assert pixmap.height() <= image.contentsRect().height()
    for name in ("copyWclExampleApplicationName", "copyWclExampleRedirectUrl"):
        button = popup.findChild(QPushButton, name)
        assert button is not None and button.isVisible()
        rect = QRect(button.mapTo(popup, QPoint()), button.size())
        assert popup.contentsRect().contains(rect)
        assert popup.childAt(rect.center()) is button
    for scroll in popup.findChildren(QScrollArea):
        assert scroll.horizontalScrollBar().maximum() == 0
    assert initial_width <= 640


def test_wcl_example_can_shrink_after_showing_and_growing(qtbot, tmp_path):
    dialog = SettingsDialog(_cfg(tmp_path), first_run=True)
    qtbot.addWidget(dialog)
    popup = dialog._build_wcl_setup_example_dialog()
    qtbot.addWidget(popup)
    popup.show()
    image = popup.findChild(QLabel, "wclSetupExampleImage")
    assert image is not None
    for width, height in ((600, 620), (900, 700), (380, 420), (560, 480)):
        popup.resize(width, height)
        qtbot.waitUntil(lambda: image.pixmap().size().width() <= image.width())
        assert popup.width() == width
        assert popup.height() == height
        assert image.pixmap().width() <= image.contentsRect().width()
        assert image.pixmap().height() <= image.contentsRect().height()
        for name in ("copyWclExampleApplicationName", "copyWclExampleRedirectUrl"):
            button = popup.findChild(QPushButton, name)
            assert button is not None
            rect = QRect(button.mapTo(popup, QPoint()), button.size())
            assert popup.contentsRect().contains(rect)
            assert popup.childAt(rect.center()) is button
        for edit in popup.findChildren(QLineEdit):
            assert edit.width() >= edit.fontMetrics().horizontalAdvance(edit.text()) + 12


def test_missing_wcl_example_keeps_readable_fallback_and_copy_controls(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings_mod, "WCL_CREATE_CLIENT_EXAMPLE_PATH", tmp_path / "missing.jpg")
    dialog = SettingsDialog(_cfg(tmp_path), first_run=True)
    qtbot.addWidget(dialog)
    popup = dialog._build_wcl_setup_example_dialog()
    qtbot.addWidget(popup)
    popup.resize(380, 420)
    popup.show()
    image = popup.findChild(QLabel, "wclSetupExampleImage")
    assert image is not None and image.isVisible()
    assert "Example screenshot is unavailable" in image.text()
    assert image.wordWrap()
    assert image.height() >= image.heightForWidth(image.width())
    button = popup.findChild(QPushButton, "copyWclExampleRedirectUrl")
    assert button is not None and button.isVisible() and button.isEnabled()
