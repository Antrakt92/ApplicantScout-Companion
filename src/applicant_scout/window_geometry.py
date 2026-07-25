"""Shared screen-boundary helpers for frameless application windows."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from PyQt6.QtCore import QRect


def clamp_rect_to_bounds(
    x: int,
    y: int,
    w: int,
    h: int,
    bounds: QRect,
) -> tuple[int, int, int, int]:
    if bounds.width() <= 0 or bounds.height() <= 0:
        return (x, y, w, h)
    clamped_width = min(max(1, w), bounds.width())
    clamped_height = min(max(1, h), bounds.height())
    min_x = bounds.x()
    min_y = bounds.y()
    max_x = bounds.x() + bounds.width() - clamped_width
    max_y = bounds.y() + bounds.height() - clamped_height
    clamped_x = min(max(x, min_x), max_x)
    clamped_y = min(max(y, min_y), max_y)
    return (clamped_x, clamped_y, clamped_width, clamped_height)


def _screen_bounds(screen: Any, *, use_available_geometry: bool) -> QRect:
    if use_available_geometry:
        return screen.availableGeometry()
    return screen.geometry()


def clamp_geometry_to_screens(
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    screens: Iterable[Any],
    primary_screen: Any | None,
    min_visible_px: int = 80,
    use_available_geometry: bool = True,
    preserve_grabbable_geometry: bool = False,
    grabbable_height_px: int = 28,
    min_grabbable_px: int = 20,
) -> tuple[int, int, int, int]:
    """Keep a window grabbable on a current screen or center it on primary."""
    current_screens = tuple(screens)
    if not current_screens:
        return (x, y, w, h)
    if preserve_grabbable_geometry:
        title_height = max(1, grabbable_height_px)
        required_title_overlap = min(max(1, min_grabbable_px), title_height)
        for screen in current_screens:
            bounds = _screen_bounds(
                screen,
                use_available_geometry=use_available_geometry,
            )
            overlap_x = max(
                0,
                min(x + w, bounds.x() + bounds.width()) - max(x, bounds.x()),
            )
            title_overlap_y = max(
                0,
                min(y + title_height, bounds.y() + bounds.height())
                - max(y, bounds.y()),
            )
            if (
                overlap_x >= min_visible_px
                and title_overlap_y >= required_title_overlap
            ):
                return (x, y, w, h)
    for screen in current_screens:
        bounds = _screen_bounds(
            screen,
            use_available_geometry=use_available_geometry,
        )
        overlap_x = max(
            0,
            min(x + w, bounds.x() + bounds.width()) - max(x, bounds.x()),
        )
        overlap_y = max(
            0,
            min(y + h, bounds.y() + bounds.height()) - max(y, bounds.y()),
        )
        if overlap_x >= min_visible_px and overlap_y >= min_visible_px:
            return clamp_rect_to_bounds(x, y, w, h, bounds)
    if primary_screen is None:
        return (x, y, w, h)
    primary_bounds = _screen_bounds(
        primary_screen,
        use_available_geometry=use_available_geometry,
    )
    clamped_width = min(w, primary_bounds.width())
    clamped_height = min(h, primary_bounds.height())
    centered_x = primary_bounds.x() + (primary_bounds.width() - clamped_width) // 2
    centered_y = primary_bounds.y() + (primary_bounds.height() - clamped_height) // 2
    return clamp_rect_to_bounds(
        centered_x,
        centered_y,
        clamped_width,
        clamped_height,
        primary_bounds,
    )
