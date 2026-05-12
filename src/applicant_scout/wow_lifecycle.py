"""Windows helpers for starting/stopping the companion with WoW."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
import subprocess
import sys
import time


STARTUP_SHORTCUT_NAME = "ApplicantScout Companion.lnk"
WATCH_WOW_ARG = "--watch-wow"
WOW_PROCESS_NAMES = ("Wow.exe", "WowT.exe", "WowClassic.exe", "WowClassicT.exe")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _startup_folder() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_shortcut_path() -> Path:
    return _startup_folder() / STARTUP_SHORTCUT_NAME


def companion_executable_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(sys.argv[0]).resolve()


def is_wow_running(process_names: tuple[str, ...] = WOW_PROCESS_NAMES) -> bool:
    """Return True when a retail/classic WoW process is visible to this user."""
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = completed.stdout.casefold()
    return any(name.casefold() in output for name in process_names)


def is_wow_sync_watcher_running(*, executable_path: Path | None = None) -> bool:
    """Return True when this executable already has a --watch-wow helper."""
    executable = executable_path or companion_executable_path()
    target = str(executable).casefold()
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                (
                    "$target = $args[0]; "
                    "$found = Get-CimInstance Win32_Process | Where-Object { "
                    "$_.Name -ieq 'ApplicantScout.exe' -and $_.ExecutablePath -and "
                    "([System.IO.Path]::GetFullPath($_.ExecutablePath) -ieq "
                    "[System.IO.Path]::GetFullPath($target)) -and "
                    "$_.CommandLine -match '--watch-wow' "
                    "}; "
                    "if ($found) { exit 0 } else { exit 1 }"
                ),
                str(executable),
            ],
            check=False,
            capture_output=True,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(target)


def _ps_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _create_shortcut(
    *,
    executable_path: Path,
    shortcut_path: Path,
) -> None:
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    exe = str(executable_path)
    shortcut = str(shortcut_path)
    working_dir = str(executable_path.parent)
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "$shell = New-Object -ComObject WScript.Shell",
            f"$shortcut = $shell.CreateShortcut({_ps_single_quoted(shortcut)})",
            f"$shortcut.TargetPath = {_ps_single_quoted(exe)}",
            f"$shortcut.Arguments = {_ps_single_quoted(WATCH_WOW_ARG)}",
            f"$shortcut.WorkingDirectory = {_ps_single_quoted(working_dir)}",
            f"$shortcut.IconLocation = {_ps_single_quoted(exe + ',0')}",
            "$shortcut.WindowStyle = 7",
            "$shortcut.Save()",
        ]
    )
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=True,
        creationflags=_CREATE_NO_WINDOW,
    )


def configure_wow_sync_startup(
    enabled: bool,
    *,
    executable_path: Path | None = None,
    shortcut_path: Path | None = None,
) -> Path | None:
    """Create/remove a per-user Startup shortcut for WoW lifecycle mode."""
    shortcut = shortcut_path or startup_shortcut_path()
    if not enabled:
        try:
            shortcut.unlink()
        except FileNotFoundError:
            pass
        return None

    executable = executable_path or companion_executable_path()
    _create_shortcut(executable_path=executable, shortcut_path=shortcut)
    return shortcut


def start_wow_sync_watcher(
    *, executable_path: Path | None = None
) -> subprocess.Popen | None:
    """Start the current-session watcher used by the Startup shortcut.

    The Startup folder only runs on Windows sign-in. When a user enables
    "Start and stop with WoW" from Settings mid-session, launch the same watcher
    immediately so the next WoW launch in this session is also covered.
    """
    executable = executable_path or companion_executable_path()
    if is_wow_sync_watcher_running(executable_path=executable):
        return None
    return subprocess.Popen(
        [str(executable), WATCH_WOW_ARG],
        cwd=str(executable.parent),
        close_fds=True,
        creationflags=_CREATE_NO_WINDOW,
    )


def wait_for_wow_start(
    *,
    interval_seconds: float = 5.0,
    is_running: Callable[[], bool] = is_wow_running,
) -> None:
    while not is_running():
        time.sleep(interval_seconds)
