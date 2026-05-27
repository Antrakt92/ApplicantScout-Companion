"""Render the representative overlay visual QA fixture."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

if "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = (
        "windows" if sys.platform == "win32" else "offscreen"
    )
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from PyQt6.QtCore import QCoreApplication  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from scripts.overlay_visual_fixture import (  # noqa: E402
    DEFAULT_VISUAL_FIXTURE_SCENARIO,
    OVERLAY_VISUAL_SCENARIOS,
    compare_overlay_visual_images,
    create_overlay_visual_window,
    grab_overlay_visual_image,
    show_overlay_visual_window,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render or check the representative overlay visual QA fixture."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the rendered fixture to this path instead of the committed baseline.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare a fresh render to the committed baseline without writing files.",
    )
    parser.add_argument(
        "--scenario",
        choices=sorted(OVERLAY_VISUAL_SCENARIOS),
        default=DEFAULT_VISUAL_FIXTURE_SCENARIO,
        help="Visual fixture scenario to render.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Render or check every committed visual fixture scenario.",
    )
    parser.add_argument(
        "--visual-mode",
        choices=("strict", "smoke"),
        default="strict",
        help=(
            "Check mode: strict compares committed baselines; smoke validates "
            "that each scenario renders nonblank output without pixel comparison."
        ),
    )
    args = parser.parse_args(argv)
    if args.check and args.output is not None:
        parser.error("--check cannot be combined with --output")
    if args.all and args.output is not None:
        parser.error("--all cannot be combined with --output")
    if args.visual_mode == "smoke" and not args.check:
        parser.error("--visual-mode smoke requires --check")
    return args


def _render_fixture_pixmap(app: QCoreApplication, scenario_name: str):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _state, window, client = create_overlay_visual_window(tmp_path, scenario_name)
        try:
            show_overlay_visual_window(
                window,
                scenario_name,
                process_events=app.processEvents,
            )
            pixmap = grab_overlay_visual_image(window)
            if pixmap.isNull():
                raise RuntimeError("Rendered overlay visual fixture is null")
            return pixmap
        finally:
            window.close()
            client.close()


def _save_pixmap_atomic(pixmap, output: Path) -> None:
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
            raise RuntimeError(f"Could not save overlay visual fixture to {temp_path}")
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


def _sampled_colours(image: QImage) -> set[int]:
    x_step = max(1, image.width() // 10)
    y_step = max(1, image.height() // 10)
    colours: set[int] = set()
    for x in range(0, image.width(), x_step):
        for y in range(0, image.height(), y_step):
            colours.add(image.pixelColor(x, y).rgba())
    return colours


def _validate_smoke_image(image: QImage) -> str | None:
    if image.isNull():
        return "overlay visual fixture smoke check rendered a null image"
    if image.width() <= 0 or image.height() <= 0:
        return (
            "overlay visual fixture smoke check rendered invalid dimensions: "
            f"{image.width()}x{image.height()}"
        )
    if len(_sampled_colours(image)) <= 1:
        return "overlay visual fixture smoke check rendered blank or uniform output"
    return None


def _check_rendered_pixmap(
    _scenario_name: str,
    scenario,
    pixmap,
    visual_mode: str,
) -> tuple[bool, str]:
    image = pixmap.toImage()
    if visual_mode == "smoke":
        error = _validate_smoke_image(image)
        if error is not None:
            return False, error
        return (
            True,
            "overlay visual fixture smoke check passed "
            f"({image.width()}x{image.height()} nonblank render)",
        )

    baseline = QImage(str(scenario.baseline_path))
    diff = compare_overlay_visual_images(baseline, image)
    return diff.passed, diff.message


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    existing_app = QApplication.instance()
    app = existing_app if isinstance(existing_app, QApplication) else QApplication(sys.argv)

    scenario_names = (
        sorted(OVERLAY_VISUAL_SCENARIOS) if args.all else [args.scenario]
    )
    failed = False
    for scenario_name in scenario_names:
        scenario = OVERLAY_VISUAL_SCENARIOS[scenario_name]
        pixmap = _render_fixture_pixmap(app, scenario_name)
        if args.check:
            passed, message = _check_rendered_pixmap(
                scenario_name,
                scenario,
                pixmap,
                args.visual_mode,
            )
            prefix = f"{scenario_name}: "
            print(
                prefix + message,
                file=sys.stderr if not passed else sys.stdout,
            )
            failed = failed or not passed
            continue

        output = args.output or scenario.baseline_path
        _save_pixmap_atomic(pixmap, output)
        print(output)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
