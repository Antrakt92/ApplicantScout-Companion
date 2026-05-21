"""Render the representative overlay visual QA fixture."""

from __future__ import annotations

import os
import sys
import tempfile
import argparse
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
    OVERLAY_VISUAL_BASELINE_PATH,
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
    args = parser.parse_args(argv)
    if args.check and args.output is not None:
        parser.error("--check cannot be combined with --output")
    return args


def _render_fixture_pixmap(app: QCoreApplication):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _state, window, client = create_overlay_visual_window(tmp_path)
        try:
            show_overlay_visual_window(window, process_events=app.processEvents)
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    existing_app = QApplication.instance()
    app = existing_app if isinstance(existing_app, QApplication) else QApplication(sys.argv)

    pixmap = _render_fixture_pixmap(app)
    if args.check:
        baseline = QImage(str(OVERLAY_VISUAL_BASELINE_PATH))
        diff = compare_overlay_visual_images(baseline, pixmap.toImage())
        print(diff.message, file=sys.stderr if not diff.passed else sys.stdout)
        return 0 if diff.passed else 1

    output = args.output or OVERLAY_VISUAL_BASELINE_PATH
    _save_pixmap_atomic(pixmap, output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
