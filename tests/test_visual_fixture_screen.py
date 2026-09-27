"""Visual baselines must not inherit a hosted runner's small desktop bounds."""
from PySide6.QtCore import QRect
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication
import pytest

from scripts.overlay_visual_fixture import create_overlay_visual_window, show_overlay_visual_window
from scripts.settings_dialog_visual_fixture import create_settings_visual_dialog, show_settings_visual_dialog


@pytest.mark.parametrize("scenario", ["applicants-default", "raid-listing"])
def test_overlay_fixture_size_ignores_small_desktop(qtbot, tmp_path, monkeypatch, scenario):
    def size(directory):
        directory.mkdir()
        _, window, client = create_overlay_visual_window(directory, scenario)
        qtbot.addWidget(window)
        try:
            show_overlay_visual_window(window, scenario, process_events=QApplication.processEvents)
            return window.size()
        finally:
            window.shutdown_fetches()
            client.close()

    expected = size(tmp_path / "normal")
    monkeypatch.setattr(QScreen, "availableGeometry", lambda _self: QRect(0, 0, 819, 582))
    small_screen_method = QScreen.availableGeometry
    assert size(tmp_path / "small") == expected
    assert QScreen.availableGeometry is small_screen_method


def test_settings_fixture_size_ignores_small_desktop(qtbot, monkeypatch):
    def size():
        dialog = create_settings_visual_dialog("first-run")
        qtbot.addWidget(dialog)
        show_settings_visual_dialog(dialog, process_events=QApplication.processEvents)
        result = dialog.size()
        dialog.hide()
        return result

    expected = size()
    monkeypatch.setattr(QScreen, "availableGeometry", lambda _self: QRect(0, 0, 819, 400))
    small_screen_method = QScreen.availableGeometry
    assert size() == expected
    assert QScreen.availableGeometry is small_screen_method
