"""Visual QA smoke checks for the representative overlay fixture."""

from __future__ import annotations

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

import applicant_scout.overlay as overlay_mod
from applicant_scout.constants import ALL_ROLES
from applicant_scout.overlay import OverlayWindow
from applicant_scout.wcl import CharacterCache, WCLAuth, WCLClient
from scripts.overlay_visual_fixture import (
    VISUAL_FIXTURE_PINNED_ID,
    build_overlay_visual_state,
    prepare_overlay_visual_window,
)


def _sampled_colours(image: QImage) -> set[int]:
    x_step = max(1, image.width() // 10)
    y_step = max(1, image.height() // 10)
    colours: set[int] = set()
    for x in range(0, image.width(), x_step):
        for y in range(0, image.height(), y_step):
            colours.add(image.pixelColor(x, y).rgba())
    return colours


def test_overlay_visual_fixture_renders_representative_state(qtbot, tmp_path):
    state = build_overlay_visual_state()
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        prepare_overlay_visual_window(window)
        window.show()
        qtbot.waitUntil(lambda: window._table.viewport().width() > 0, timeout=1000)
        QApplication.processEvents()

        screenshot_path = tmp_path / "overlay-polish-fixture.png"
        pixmap = window.grab()
        assert not pixmap.isNull()
        assert pixmap.save(str(screenshot_path))
        assert screenshot_path.stat().st_size > 0

        image = QImage(str(screenshot_path))
        dpr = pixmap.devicePixelRatio()
        assert image.width() == round(window.size().width() * dpr)
        assert image.height() == round(window.size().height() * dpr)
        assert len(_sampled_colours(image)) > 1

        assert window._table.rowCount() == len(state.applicants)
        assert window._pinned_id == VISUAL_FIXTURE_PINNED_ID
        assert window._hover_id is None
        assert window._panel._name_label.text() == "Bloomwell"
        assert window._role_filter_bar._active == set(ALL_ROLES)
        assert not window._role_filter_bar._reset_btn.isHidden()
        assert all(
            not window._table.isRowHidden(row)
            for row in range(window._table.rowCount())
        )
    finally:
        client.close()


def test_visual_fixture_disabled_tracking_blocks_cursor_hover(
    qtbot, monkeypatch, tmp_path
):
    state = build_overlay_visual_state()
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(state, client, cache, tmp_path)
    qtbot.addWidget(window)

    try:
        prepare_overlay_visual_window(window)
        window.show()
        qtbot.waitUntil(lambda: window._table.viewport().width() > 0, timeout=1000)
        QApplication.processEvents()

        viewport = window._table.viewport()
        row0_rect = window._table.visualRect(window._table.model().index(0, 0))
        cursor_pos = viewport.mapToGlobal(row0_rect.center())

        class FakeCursor:
            @staticmethod
            def pos() -> QPoint:
                return cursor_pos

        monkeypatch.setattr(overlay_mod, "QCursor", FakeCursor)

        assert not window._table.hasMouseTracking()
        assert not viewport.hasMouseTracking()

        window._reresolve_hover_from_cursor()

        assert window._hover_id is None
        assert window._pinned_id == VISUAL_FIXTURE_PINNED_ID
        assert window._panel._name_label.text() == "Bloomwell"
    finally:
        client.close()
