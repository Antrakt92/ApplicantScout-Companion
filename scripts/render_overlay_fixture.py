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
from scripts.visual_fixture_checks import (  # noqa: E402
    add_visual_fixture_arguments,
    check_rendered_pixmap,
    parse_visual_fixture_args,
    run_visual_fixture_scenarios,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render or check the representative overlay visual QA fixture."
    )
    add_visual_fixture_arguments(
        parser,
        scenario_names=sorted(OVERLAY_VISUAL_SCENARIOS),
        default_scenario=DEFAULT_VISUAL_FIXTURE_SCENARIO,
        scenario_help="Visual fixture scenario to render.",
        all_help="Render or check every committed visual fixture scenario.",
    )
    return parse_visual_fixture_args(parser, argv)


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
        label="overlay visual fixture",
        image_factory=QImage,
        compare_images=compare_overlay_visual_images,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    existing_app = QApplication.instance()
    app = existing_app if isinstance(existing_app, QApplication) else QApplication(sys.argv)

    return run_visual_fixture_scenarios(
        args,
        scenarios=OVERLAY_VISUAL_SCENARIOS,
        render_fixture=lambda scenario_name: _render_fixture_pixmap(app, scenario_name),
        check_fixture=_check_rendered_pixmap,
        label="overlay visual fixture",
    )


if __name__ == "__main__":
    raise SystemExit(main())
