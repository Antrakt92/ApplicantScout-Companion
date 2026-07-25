from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QMouseEvent

import applicant_scout.overlay as overlay_mod
import applicant_scout.settings_dialog as settings_mod
from applicant_scout.config import Config
from applicant_scout.overlay import OverlayWindow
from applicant_scout.settings_dialog import SettingsDialog
from applicant_scout.state import AppState
from applicant_scout.wcl import CharacterCache, WCLAuth, WCLClient
from applicant_scout.window_geometry import clamp_geometry_to_screens


@dataclass(frozen=True)
class _Screen:
    bounds: QRect

    def availableGeometry(self) -> QRect:
        return self.bounds

    def geometry(self) -> QRect:
        return self.bounds


def _release_event() -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(5, 5),
        QPointF(5005, 5005),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _config(tmp_path: Path) -> Config:
    retail_root = tmp_path / "World of Warcraft" / "_retail_"
    (retail_root / "Interface" / "AddOns").mkdir(parents=True)
    return Config(
        wcl_client_id="client",
        wcl_client_secret="secret",
        chatlog_path=retail_root / "Logs" / "WoWChatLog.txt",
        region="EU",
        cache_dir=tmp_path / "cache",
        config_dir=tmp_path / "config",
        screenshots_path=retail_root / "Screenshots",
        log_dir=tmp_path / "logs",
    )


def _overlay(tmp_path: Path, qtbot) -> tuple[OverlayWindow, WCLClient]:
    client = WCLClient(WCLAuth("client", "secret", tmp_path))
    window = OverlayWindow(
        AppState(),
        client,
        CharacterCache(tmp_path),
        tmp_path,
        game_foreground_probe=lambda: True,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    return window, client


def test_shared_clamp_preserves_a_visible_negative_coordinate_monitor():
    primary = _Screen(QRect(0, 0, 1920, 1040))
    secondary = _Screen(QRect(-1280, 0, 1280, 1024))

    assert clamp_geometry_to_screens(
        -1200,
        100,
        600,
        500,
        screens=(primary, secondary),
        primary_screen=primary,
    ) == (-1200, 100, 600, 500)


def test_shared_clamp_recenters_after_secondary_monitor_is_removed():
    primary = _Screen(QRect(0, 0, 1600, 900))
    secondary = _Screen(QRect(1600, 0, 1200, 900))
    geometry = (1800, 120, 600, 500)

    assert clamp_geometry_to_screens(
        *geometry,
        screens=(primary, secondary),
        primary_screen=primary,
    ) == geometry
    assert clamp_geometry_to_screens(
        *geometry,
        screens=(primary,),
        primary_screen=primary,
    ) == (500, 200, 600, 500)


def test_runtime_clamp_preserves_a_grabbable_cross_monitor_window():
    primary = _Screen(QRect(0, 0, 1600, 900))
    secondary = _Screen(QRect(1600, 0, 1200, 900))
    geometry = (1500, 100, 610, 520)

    assert clamp_geometry_to_screens(
        *geometry,
        screens=(primary, secondary),
        primary_screen=primary,
        preserve_grabbable_geometry=True,
    ) == geometry
    assert clamp_geometry_to_screens(
        5000,
        5000,
        610,
        520,
        screens=(primary, secondary),
        primary_screen=primary,
        preserve_grabbable_geometry=True,
    ) == (495, 190, 610, 520)
    assert clamp_geometry_to_screens(
        100,
        -440,
        610,
        520,
        screens=(primary,),
        primary_screen=primary,
        preserve_grabbable_geometry=True,
        grabbable_height_px=28,
    ) == (100, 0, 610, 520)


def test_overlay_title_drag_release_clamps_to_current_screens(
    monkeypatch,
    qtbot,
    tmp_path: Path,
):
    primary = _Screen(QRect(0, 0, 1600, 900))
    monkeypatch.setattr(overlay_mod.QGuiApplication, "screens", lambda: [primary])
    monkeypatch.setattr(overlay_mod.QGuiApplication, "primaryScreen", lambda: primary)
    window, client = _overlay(tmp_path, qtbot)
    try:
        window.setGeometry(5000, 5000, 610, 520)
        window._title_bar._drag_offset = QPoint(5, 5)

        window._title_bar.mouseReleaseEvent(_release_event())

        assert window.geometry() == QRect(495, 190, 610, 520)
    finally:
        window.close()
        client.close()


def test_overlay_drag_release_restores_a_hidden_title_above_the_screen(
    monkeypatch,
    qtbot,
    tmp_path: Path,
):
    primary = _Screen(QRect(0, 0, 1600, 900))
    monkeypatch.setattr(overlay_mod.QGuiApplication, "screens", lambda: [primary])
    monkeypatch.setattr(overlay_mod.QGuiApplication, "primaryScreen", lambda: primary)
    window, client = _overlay(tmp_path, qtbot)
    try:
        window.setGeometry(100, -440, 610, 520)
        window._title_bar._drag_offset = QPoint(5, 5)

        window._title_bar.mouseReleaseEvent(_release_event())

        assert window.geometry() == QRect(100, 0, 610, 520)
    finally:
        window.close()
        client.close()


def test_overlay_show_reclamps_geometry_after_topology_change(
    monkeypatch,
    qtbot,
    tmp_path: Path,
):
    primary = _Screen(QRect(0, 0, 1600, 900))
    monkeypatch.setattr(overlay_mod.QGuiApplication, "screens", lambda: [primary])
    monkeypatch.setattr(overlay_mod.QGuiApplication, "primaryScreen", lambda: primary)
    window, client = _overlay(tmp_path, qtbot)
    try:
        window.setGeometry(2000, 100, 610, 520)

        window.show()

        assert window.geometry() == QRect(495, 190, 610, 520)
    finally:
        window.close()
        client.close()


def test_settings_drag_release_clamps_to_current_screens(
    monkeypatch,
    qtbot,
    tmp_path: Path,
):
    primary = _Screen(QRect(0, 0, 1600, 900))
    monkeypatch.setattr(settings_mod.QApplication, "screens", lambda: [primary])
    monkeypatch.setattr(settings_mod.QApplication, "primaryScreen", lambda: primary)
    dialog = SettingsDialog(_config(tmp_path))
    qtbot.addWidget(dialog)
    dialog.setGeometry(5000, 5000, 600, 700)
    dialog._title_drag_offset = QPoint(5, 5)

    dialog.eventFilter(dialog.title_bar, _release_event())

    assert dialog.geometry() == QRect(500, 100, 600, 700)


def test_drag_release_keeps_grabbable_geometry_straddling_two_screens(
    monkeypatch,
    qtbot,
    tmp_path: Path,
):
    primary = _Screen(QRect(0, 0, 1600, 900))
    secondary = _Screen(QRect(1600, 0, 1200, 900))
    screens = [primary, secondary]
    monkeypatch.setattr(overlay_mod.QGuiApplication, "screens", lambda: screens)
    monkeypatch.setattr(overlay_mod.QGuiApplication, "primaryScreen", lambda: primary)
    monkeypatch.setattr(settings_mod.QApplication, "screens", lambda: screens)
    monkeypatch.setattr(settings_mod.QApplication, "primaryScreen", lambda: primary)
    window, client = _overlay(tmp_path, qtbot)
    dialog = SettingsDialog(_config(tmp_path / "settings"))
    qtbot.addWidget(dialog)
    try:
        window.setGeometry(1500, 100, 610, 520)
        dialog.setGeometry(1500, 100, 600, 700)
        window._title_bar._drag_offset = QPoint(5, 5)
        dialog._title_drag_offset = QPoint(5, 5)

        window._title_bar.mouseReleaseEvent(_release_event())
        dialog.eventFilter(dialog.title_bar, _release_event())

        assert window.geometry() == QRect(1500, 100, 610, 520)
        assert dialog.geometry() == QRect(1500, 100, 600, 700)
    finally:
        window.close()
        dialog.close()
        client.close()


def test_settings_show_reclamps_geometry_after_topology_change(
    monkeypatch,
    qtbot,
    tmp_path: Path,
):
    primary = _Screen(QRect(0, 0, 1600, 900))
    monkeypatch.setattr(settings_mod.QApplication, "screens", lambda: [primary])
    monkeypatch.setattr(settings_mod.QApplication, "primaryScreen", lambda: primary)
    dialog = SettingsDialog(_config(tmp_path))
    qtbot.addWidget(dialog)
    dialog.setGeometry(2000, 100, 600, 700)

    dialog.show()

    assert dialog.geometry() == QRect(500, 100, 600, 700)


def test_screen_removal_reclamps_both_live_frameless_windows(
    monkeypatch,
    qtbot,
    tmp_path: Path,
):
    primary = _Screen(QRect(0, 0, 1600, 900))
    monkeypatch.setattr(overlay_mod.QGuiApplication, "screens", lambda: [primary])
    monkeypatch.setattr(overlay_mod.QGuiApplication, "primaryScreen", lambda: primary)
    monkeypatch.setattr(settings_mod.QApplication, "screens", lambda: [primary])
    monkeypatch.setattr(settings_mod.QApplication, "primaryScreen", lambda: primary)
    window, client = _overlay(tmp_path, qtbot)
    dialog = SettingsDialog(_config(tmp_path / "settings"))
    qtbot.addWidget(dialog)
    try:
        window.setGeometry(2000, 100, 610, 520)
        dialog.setGeometry(2000, 100, 600, 700)

        window._on_screen_topology_changed()
        dialog._on_screen_topology_changed()

        qtbot.waitUntil(
            lambda: window.geometry() == QRect(495, 190, 610, 520)
            and dialog.geometry() == QRect(500, 100, 600, 700),
            timeout=1000,
        )
    finally:
        window.close()
        dialog.close()
        client.close()
