"""Shared pixel/smoke checks for generated Qt visual fixtures."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Any

from PyQt6.QtCore import Qt


@dataclass(frozen=True)
class VisualFixtureDiff:
    passed: bool
    message: str
    changed_pixels: int
    total_pixels: int
    max_channel_delta: int


def save_pixmap_atomic(pixmap: Any, output: Path, *, label: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp.png",
            dir=output.parent,
        )
        os.close(fd)
        fd = -1
        temp_path = Path(temp_name)
        if not pixmap.save(str(temp_path)):
            raise RuntimeError(f"Could not save {label} to {temp_path}")
        os.replace(temp_path, output)
        temp_path = None
    finally:
        if fd != -1:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _has_multiple_colours(image: Any) -> bool:
    first_colour: int | None = None
    for x in range(image.width()):
        for y in range(image.height()):
            colour = image.pixelColor(x, y).rgba()
            if first_colour is None:
                first_colour = colour
            elif colour != first_colour:
                return True
    return False


def validate_smoke_image(image: Any, *, label: str) -> str | None:
    if image.isNull():
        return f"{label} smoke check rendered a null image"
    if image.width() <= 0 or image.height() <= 0:
        return (
            f"{label} smoke check rendered invalid dimensions: "
            f"{image.width()}x{image.height()}"
        )
    if not _has_multiple_colours(image):
        return f"{label} smoke check rendered blank or uniform output"
    return None


def _channel_delta(expected: Any, actual: Any, x: int, y: int) -> int:
    expected_color = expected.pixelColor(x, y)
    actual_color = actual.pixelColor(x, y)
    return max(
        abs(expected_color.red() - actual_color.red()),
        abs(expected_color.green() - actual_color.green()),
        abs(expected_color.blue() - actual_color.blue()),
        abs(expected_color.alpha() - actual_color.alpha()),
    )


def compare_visual_images(
    expected: Any,
    actual: Any,
    *,
    label: str,
    regen_command: str,
    channel_tolerance: int = 12,
    max_pixel_ratio: float = 0.005,
    scale_ratio_tolerance: float = 0.01,
) -> VisualFixtureDiff:
    if expected.isNull() or actual.isNull():
        return VisualFixtureDiff(
            passed=False,
            message=(
                f"{label} comparison received a null image; "
                f"regenerate with {regen_command}"
            ),
            changed_pixels=0,
            total_pixels=0,
            max_channel_delta=0,
        )
    if expected.size() != actual.size():
        expected_width = expected.width()
        expected_height = expected.height()
        actual_width = actual.width()
        actual_height = actual.height()
        scale_x = expected_width / actual_width if actual_width else 0.0
        scale_y = expected_height / actual_height if actual_height else 0.0
        if (
            expected_width > 0
            and expected_height > 0
            and actual_width > 0
            and actual_height > 0
            and abs(scale_x - scale_y) <= scale_ratio_tolerance
        ):
            actual = actual.scaled(
                expected.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            return VisualFixtureDiff(
                passed=False,
                message=(
                    f"{label} dimension mismatch: "
                    f"expected {expected_width}x{expected_height}, "
                    f"got {actual_width}x{actual_height}; regenerate with "
                    f"{regen_command} after intentional UI changes"
                ),
                changed_pixels=0,
                total_pixels=max(actual_width * actual_height, 0),
                max_channel_delta=0,
            )

    changed_pixels = 0
    max_channel_delta = 0
    total_pixels = expected.width() * expected.height()
    for x in range(expected.width()):
        for y in range(expected.height()):
            delta = _channel_delta(expected, actual, x, y)
            max_channel_delta = max(max_channel_delta, delta)
            if delta > channel_tolerance:
                changed_pixels += 1

    changed_ratio = changed_pixels / total_pixels if total_pixels else 0.0
    passed = changed_ratio <= max_pixel_ratio
    if passed:
        return VisualFixtureDiff(
            passed=True,
            message=(
                f"{label} matched committed baseline "
                f"({changed_pixels}/{total_pixels} changed pixels over tolerance)"
            ),
            changed_pixels=changed_pixels,
            total_pixels=total_pixels,
            max_channel_delta=max_channel_delta,
        )
    return VisualFixtureDiff(
        passed=False,
        message=(
            f"{label} drift exceeded tolerance: "
            f"{changed_pixels}/{total_pixels} changed pixels "
            f"({changed_ratio:.2%}) over channel delta "
            f"{channel_tolerance}, max channel delta "
            f"{max_channel_delta}; regenerate with "
            f"{regen_command} after intentional UI changes"
        ),
        changed_pixels=changed_pixels,
        total_pixels=total_pixels,
        max_channel_delta=max_channel_delta,
    )
