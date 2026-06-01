from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_main_import_does_not_load_qr_decoder() -> None:
    script = (
        "import sys; "
        "import applicant_scout.__main__; "
        "print('pyzbar.wrapper' in sys.modules)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False"


def test_sync_with_wow_startup_defers_first_process_scan() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "applicant_scout"
        / "__main__.py"
    ).read_text(encoding="utf-8")
    sync_startup = source[
        source.index("    if cfg.sync_with_wow:") : source.index(
            "    update_timer = QTimer(app)"
        )
    ]

    assert "has_seen_wow=wow_watch_mode," in sync_startup
    assert "has_seen_wow=wow_watch_mode or is_wow_running()" not in sync_startup
