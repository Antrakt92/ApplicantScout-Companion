import pytest
from PySide6.QtCore import QRect, Qt

import applicant_scout.settings_dialog as settings_mod
from applicant_scout.settings_dialog import SettingsDialog
from test_settings_dialog import _cfg


@pytest.mark.parametrize("first_run", [False, True])
@pytest.mark.parametrize("offscreen", [False, True])
def test_narrow_screen_contains_settings_and_restores_normal_minimum(
    qtbot, tmp_path, monkeypatch, first_run, offscreen
):
    class Screen:
        bounds = QRect(100, 50, 500, 600)

        def availableGeometry(self):
            return self.bounds

    screen = Screen()
    monkeypatch.setattr(settings_mod.QApplication, "screens", lambda: [screen])
    monkeypatch.setattr(settings_mod.QApplication, "primaryScreen", lambda: screen)
    dialog = SettingsDialog(_cfg(tmp_path), first_run=first_run)
    qtbot.addWidget(dialog)
    dialog.setGeometry(2000 if offscreen else 100, 50, 700, 900)
    dialog._clamp_runtime_geometry()
    assert screen.bounds.contains(dialog.geometry())
    assert dialog.minimumWidth() == 500
    assert dialog.body_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    screen.bounds = QRect(0, 0, 1600, 900)
    dialog._clamp_runtime_geometry()
    assert dialog.minimumWidth() == 560
    assert dialog.body_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
