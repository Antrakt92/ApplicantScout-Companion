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
    add_visual_fixture_arguments,
    check_rendered_pixmap,
    parse_visual_fixture_args,
    run_visual_fixture_scenarios,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render or check the representative Settings dialog visual QA fixture."
    )
    add_visual_fixture_arguments(
        parser,
        scenario_names=sorted(SETTINGS_DIALOG_VISUAL_SCENARIOS),
        default_scenario=DEFAULT_SETTINGS_VISUAL_SCENARIO,
        scenario_help="Settings dialog visual fixture scenario to render.",
        all_help="Render or check every committed Settings dialog visual fixture scenario.",
    )
    return parse_visual_fixture_args(parser, argv)


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
    return check_rendered_pixmap(
        scenario,
        pixmap,
        visual_mode,
        label="settings dialog visual fixture",
        image_factory=QImage,
        compare_images=compare_settings_visual_images,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    existing_app = QApplication.instance()
    app = existing_app if isinstance(existing_app, QApplication) else QApplication(sys.argv)

    return run_visual_fixture_scenarios(
        args,
        scenarios=SETTINGS_DIALOG_VISUAL_SCENARIOS,
        render_fixture=lambda scenario_name: _render_fixture_pixmap(app, scenario_name),
        check_fixture=_check_rendered_pixmap,
        label="settings dialog visual fixture",
    )


if __name__ == "__main__":
    raise SystemExit(main())
