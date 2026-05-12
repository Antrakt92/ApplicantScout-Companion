"""Render the representative overlay visual QA fixture."""

from __future__ import annotations

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

from PyQt6.QtWidgets import QApplication  # noqa: E402

from applicant_scout.overlay import OverlayWindow  # noqa: E402
from applicant_scout.wcl import CharacterCache, WCLAuth, WCLClient  # noqa: E402
from scripts.overlay_visual_fixture import (  # noqa: E402
    build_overlay_visual_state,
    prepare_overlay_visual_window,
)


def main() -> int:
    output = REPO_ROOT / "docs" / "visual" / "overlay-polish-fixture.png"
    output.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        auth = WCLAuth("visual-fixture-client", "visual-fixture-secret", tmp_path)
        client = WCLClient(auth)
        cache = CharacterCache(tmp_path)
        window = OverlayWindow(build_overlay_visual_state(), client, cache, tmp_path)
        try:
            prepare_overlay_visual_window(window)
            window.show()
            app.processEvents()
            pixmap = window.grab()
            if pixmap.isNull() or not pixmap.save(str(output)):
                return 1
        finally:
            window.close()
            client.close()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
