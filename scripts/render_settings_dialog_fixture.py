"""Render the representative Settings dialog visual QA fixture."""

from __future__ import annotations

import argparse
import os
import sys
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

from scripts.settings_dialog_visual_fixture import (  # noqa: E402
    DEFAULT_SETTINGS_VISUAL_SCENARIO,
    SETTINGS_DIALOG_VISUAL_SCENARIOS,
    compare_settings_visual_images,
    create_settings_visual_dialog,
    grab_settings_visual_image,
    show_settings_visual_dialog,
)
from scripts.visual_fixture_checks import (  # noqa: E402
    save_pixmap_atomic,
    validate_smoke_image,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render or check the representative Settings dialog visual QA fixture."
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
        choices=sorted(SETTINGS_DIALOG_VISUAL_SCENARIOS),
        default=DEFAULT_SETTINGS_VISUAL_SCENARIO,
        help="Settings dialog visual fixture scenario to render.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Render or check every committed Settings dialog visual fixture scenario.",
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
    dialog = create_settings_visual_dialog(scenario_name)
    try:
        show_settings_visual_dialog(
            dialog,
            process_events=app.processEvents,
        )
        pixmap = grab_settings_visual_image(dialog)
        if pixmap.isNull():
            raise RuntimeError("Rendered Settings dialog visual fixture is null")
        return pixmap
    finally:
        dialog.close()


def _check_rendered_pixmap(
    _scenario_name: str,
    scenario,
    pixmap,
    visual_mode: str,
) -> tuple[bool, str]:
    image = pixmap.toImage()
    if visual_mode == "smoke":
        error = validate_smoke_image(image, label="settings dialog visual fixture")
        if error is not None:
            return False, error
        return (
            True,
            "settings dialog visual fixture smoke check passed "
            f"({image.width()}x{image.height()} nonblank render)",
        )

    baseline = QImage(str(scenario.baseline_path))
    diff = compare_settings_visual_images(baseline, image)
    return diff.passed, diff.message


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    existing_app = QApplication.instance()
    app = existing_app if isinstance(existing_app, QApplication) else QApplication(sys.argv)

    scenario_names = (
        sorted(SETTINGS_DIALOG_VISUAL_SCENARIOS) if args.all else [args.scenario]
    )
    failed = False
    for scenario_name in scenario_names:
        scenario = SETTINGS_DIALOG_VISUAL_SCENARIOS[scenario_name]
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
        save_pixmap_atomic(pixmap, output, label="settings dialog visual fixture")
        print(output)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
