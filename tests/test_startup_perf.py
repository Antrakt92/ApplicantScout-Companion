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


def test_app_bootstrap_exposes_p1_builders() -> None:
    from applicant_scout import app_bootstrap as bootstrap

    assert callable(bootstrap.build_qapplication)
    assert callable(bootstrap.build_runtime_controllers)
    assert callable(bootstrap.run_duplicate_probe)
    assert callable(bootstrap.make_quit_pipeline)
    assert bootstrap.AppRuntime is not None
    assert bootstrap.QuitPipeline is not None


def test_run_duplicate_probe_returns_none_without_command() -> None:
    from applicant_scout import app_bootstrap as bootstrap

    assert bootstrap.run_duplicate_probe(None) is None


def test_run_duplicate_probe_acknowledged_returns_zero() -> None:
    from types import SimpleNamespace

    from applicant_scout import app_bootstrap as bootstrap

    result = bootstrap.run_duplicate_probe(
        b"show-settings",
        _send_command=lambda * _a, ** _k: SimpleNamespace(
            connected=True, written=True, response=b"ok"
        ),
        _acknowledged=lambda _r: True,
    )
    assert result == 0


def test_run_duplicate_probe_silent_connection_refuses_startup() -> None:
    from types import SimpleNamespace

    from applicant_scout import app_bootstrap as bootstrap

    result = bootstrap.run_duplicate_probe(
        b"show-settings",
        _send_command=lambda * _a, ** _k: SimpleNamespace(
            connected=True, written=True, response=None, error=None
        ),
        _acknowledged=lambda _r: False,
    )
    assert result == 1


def test_main_delegates_to_app_bootstrap_builders() -> None:
    import inspect

    import applicant_scout.__main__ as main_mod

    source = inspect.getsource(main_mod.main)
    assert "run_duplicate_probe" in source
    assert "build_qapplication" in source
    assert "build_runtime_controllers" in source
    assert "make_quit_pipeline" in source
