"""Windows helpers for starting/stopping the companion with WoW."""

from __future__ import annotations

import csv
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import io
import os
from pathlib import Path
import subprocess
import sys
import threading


STARTUP_SHORTCUT_NAME = "ApplicantScout Companion.lnk"
WATCH_WOW_ARG = "--watch-wow"
WOW_PROCESS_NAMES = ("Wow.exe", "WowT.exe")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ARGUMENT_LIST_SEPARATOR = "\x1f"
_CURRENT_SESSION_WATCHER: subprocess.Popen | None = None
_CURRENT_SESSION_WATCHER_LOCK = threading.Lock()


@dataclass(frozen=True)
class LaunchSpec:
    executable: Path
    arguments: tuple[str, ...] = ()


def _tasklist_image_names(stdout: str) -> list[str]:
    names: list[str] = []
    for row in csv.reader(io.StringIO(stdout)):
        if row:
            image_name = row[0].strip()
            if image_name:
                names.append(image_name)
    return names


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


def companion_launch_spec() -> LaunchSpec:
    if getattr(sys, "frozen", False):
        return LaunchSpec(Path(sys.executable))
    return LaunchSpec(Path(sys.executable), ("-m", "applicant_scout"))


def is_wow_running(
    process_names: tuple[str, ...] = WOW_PROCESS_NAMES,
    *,
    unknown_on_error: bool = False,
) -> bool | None:
    """Return True when a Retail WoW process is visible to this user."""
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
        if unknown_on_error:
            return None
        return False
    expected = {name.casefold() for name in process_names}
    return any(name.casefold() in expected for name in _tasklist_image_names(completed.stdout))


def foreground_process_id() -> int | None:
    """Return the process id for the foreground window on Windows."""
    if sys.platform != "win32":
        return None
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) if pid.value else None
    except (AttributeError, OSError):
        return None


def process_name_for_pid(pid: int) -> str | None:
    """Return the executable basename for a Windows process id."""
    if sys.platform != "win32" or pid <= 0:
        return None
    handle = None
    try:
        handle = ctypes.windll.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return None
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(size),
        )
        if not ok:
            return None
        return Path(buffer.value).name
    except (AttributeError, OSError, ValueError):
        return None
    finally:
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)


def is_wow_foreground(process_names: tuple[str, ...] = WOW_PROCESS_NAMES) -> bool:
    """Return True when the active window belongs to WoW."""
    pid = foreground_process_id()
    if pid is None:
        return sys.platform != "win32"
    name = process_name_for_pid(pid)
    if not name:
        return False
    return any(name.casefold() == process_name.casefold() for process_name in process_names)


def is_wow_sync_watcher_running(
    *,
    executable_path: Path | None = None,
    arguments: tuple[str, ...] | None = None,
    current_pid: int | None = None,
) -> bool:
    """Return True when this executable already has a --watch-wow helper."""
    spec = companion_launch_spec()
    executable = executable_path or spec.executable
    expected_arguments = arguments if arguments is not None else spec.arguments
    expected_argument_text = _ARGUMENT_LIST_SEPARATOR.join(expected_arguments)
    pid = os.getpid() if current_pid is None else current_pid
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
                    "& { param("
                    "[string]$target, "
                    "[int]$currentPid, "
                    "[string]$watchArg, "
                    "[string]$needleText"
                    ") "
                    "$targetFull = [System.IO.Path]::GetFullPath($target); "
                    "$needles = if ($needleText) { "
                    "$needleText -split [char]31 "
                    "} else { @() }; "
                    "$found = Get-CimInstance Win32_Process | Where-Object { "
                    "$cmd = [string]$_.CommandLine; "
                    "$exe = [string]$_.ExecutablePath; "
                    "$matchesTarget = $false; "
                    "if ($exe) { try { "
                    "$matchesTarget = "
                    "([System.IO.Path]::GetFullPath($exe) -ieq $targetFull) "
                    "} catch { $matchesTarget = $false } }; "
                    "$matchesArgs = $cmd -and ($cmd.IndexOf("
                    "$watchArg, [System.StringComparison]::OrdinalIgnoreCase"
                    ") -ge 0); "
                    "foreach ($needle in $needles) { "
                    "if ($needle -and "
                    "$cmd.IndexOf($needle, [System.StringComparison]::OrdinalIgnoreCase) "
                    "-lt 0) { "
                    "$matchesArgs = $false } }; "
                    "$_.ProcessId -ne $currentPid -and $matchesTarget -and $matchesArgs "
                    "}; "
                    "if ($found) { exit 0 } else { exit 1 }"
                    " }"
                ),
                str(executable),
                str(pid),
                WATCH_WOW_ARG,
                expected_argument_text,
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
    arguments: tuple[str, ...],
    shortcut_path: Path,
) -> None:
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    exe = str(executable_path)
    shortcut_arguments = subprocess.list2cmdline([*arguments, WATCH_WOW_ARG])
    shortcut = str(shortcut_path)
    working_dir = str(executable_path.parent)
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "$shell = New-Object -ComObject WScript.Shell",
            f"$shortcut = $shell.CreateShortcut({_ps_single_quoted(shortcut)})",
            f"$shortcut.TargetPath = {_ps_single_quoted(exe)}",
            f"$shortcut.Arguments = {_ps_single_quoted(shortcut_arguments)}",
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
    arguments: tuple[str, ...] | None = None,
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

    spec = companion_launch_spec()
    executable = executable_path or spec.executable
    launch_arguments = arguments if arguments is not None else spec.arguments
    _create_shortcut(
        executable_path=executable,
        arguments=launch_arguments,
        shortcut_path=shortcut,
    )
    return shortcut


def start_wow_sync_watcher(
    *,
    executable_path: Path | None = None,
    arguments: tuple[str, ...] | None = None,
    check_existing: bool = True,
) -> subprocess.Popen | None:
    """Start the current-session watcher used by the Startup shortcut.

    The Startup folder only runs on Windows sign-in. When a user enables
    "Start and stop with WoW" from Settings mid-session, launch the same watcher
    immediately so the next WoW launch in this session is also covered.
    """
    spec = companion_launch_spec()
    executable = executable_path or spec.executable
    launch_arguments = arguments if arguments is not None else spec.arguments
    global _CURRENT_SESSION_WATCHER
    with _CURRENT_SESSION_WATCHER_LOCK:
        current = _CURRENT_SESSION_WATCHER
        if current is not None and current.poll() is None:
            return None
        _CURRENT_SESSION_WATCHER = None
    if check_existing and is_wow_sync_watcher_running(
        executable_path=executable,
        arguments=launch_arguments,
    ):
        return None
    process = subprocess.Popen(
        [str(executable), *launch_arguments, WATCH_WOW_ARG],
        cwd=str(executable.parent),
        close_fds=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    with _CURRENT_SESSION_WATCHER_LOCK:
        _CURRENT_SESSION_WATCHER = process
    return process


def stop_current_session_watcher(*, timeout: float = 2.0) -> bool:
    """Stop the helper launched for this settings session, if it is still live."""
    global _CURRENT_SESSION_WATCHER
    with _CURRENT_SESSION_WATCHER_LOCK:
        process = _CURRENT_SESSION_WATCHER
        _CURRENT_SESSION_WATCHER = None
    if process is None:
        return True
    try:
        if process.poll() is not None:
            return True
        process.terminate()
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=timeout)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    except (OSError, subprocess.SubprocessError):
        return False
