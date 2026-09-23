"""Find a moved Retail installation without scanning whole drives."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys

from .screenshots_path_probe import _looks_like_wow_retail_root
from .wow_lifecycle import _PROCESSENTRY32W


DISCOVER_WOW_ARG = "--internal-discover-wow-screenshots"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x2
_DRIVE_FIXED = 3
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _running_wow_roots() -> list[Path]:
    if sys.platform != "win32":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = kernel32.Process32FirstW.argtypes
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        return []
    roots: list[Path] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            if entry.szExeFile.casefold() in {"wow.exe", "wowt.exe"}:
                handle = kernel32.OpenProcess(
                    _PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID
                )
                if handle:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        length = wintypes.DWORD(len(buffer))
                        if kernel32.QueryFullProcessImageNameW(
                            handle, 0, buffer, ctypes.byref(length)
                        ):
                            image = Path(buffer.value)
                            if image.parent.name.casefold() == "_retail_":
                                roots.append(image.parent)
                    finally:
                        kernel32.CloseHandle(handle)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return roots


def _fixed_drive_roots() -> list[Path]:
    if sys.platform != "win32":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetLogicalDriveStringsW.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
    kernel32.GetLogicalDriveStringsW.restype = wintypes.DWORD
    kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(512)
    length = kernel32.GetLogicalDriveStringsW(len(buffer), buffer)
    if not length or length >= len(buffer):
        return []
    return [
        Path(drive)
        for drive in ctypes.wstring_at(buffer, length).split("\0")
        if drive and kernel32.GetDriveTypeW(drive) == _DRIVE_FIXED
    ]


def discover_wow_screenshots(previous: Path) -> Path | None:
    """Prefer the running client; otherwise accept one unambiguous local install."""
    previous = Path(previous)

    def valid(root: Path) -> bool:
        return (
            root.name.casefold() == "_retail_"
            and _looks_like_wow_retail_root(root)
            and (root / "Screenshots").is_dir()
        )

    running = list(dict.fromkeys(
        root / "Screenshots" for root in _running_wow_roots() if valid(root)
    ))
    if len(running) == 1:
        return running[0]
    if running:
        return None

    # Preserve a custom folder layout after a drive-letter move, then try the
    # usual Battle.net install locations. No recursive walk or network drives.
    relative_locations = [
        Path("Games") / "World of Warcraft" / "_retail_",
        Path("World of Warcraft") / "_retail_",
        Path("Program Files (x86)") / "World of Warcraft" / "_retail_",
        Path("Program Files") / "World of Warcraft" / "_retail_",
    ]
    if previous.parent.name.casefold() == "_retail_" and previous.name.casefold() == "screenshots":
        try:
            relative_locations.insert(0, previous.parent.relative_to(previous.anchor))
        except ValueError:
            pass
    found: list[Path] = []
    for drive in _fixed_drive_roots():
        for relative in dict.fromkeys(relative_locations):
            root = drive / relative
            if valid(root):
                candidate = root / "Screenshots"
                if candidate not in found:
                    found.append(candidate)
    return found[0] if len(found) == 1 else None


def run_discovery_command(previous: str) -> int:
    try:
        candidate = discover_wow_screenshots(Path(previous))
        print(json.dumps({"path": str(candidate) if candidate else None}))
    except (OSError, ValueError):
        print('{"path": null}')
    return 0


def run_bounded_discovery(previous: Path, *, timeout_seconds: float = 5) -> Path | None:
    args = [sys.executable]
    if not getattr(sys, "frozen", False):
        args += ["-m", "applicant_scout"]
    args += [DISCOVER_WOW_ARG, str(previous)]
    try:
        completed = subprocess.run(
            args, capture_output=True, check=False, timeout=timeout_seconds,
            creationflags=_CREATE_NO_WINDOW,
        )
        if completed.returncode != 0 or len(completed.stdout) > 4096:
            return None
        payload = json.loads(completed.stdout)
        if isinstance(payload, dict) and set(payload) == {"path"} and isinstance(payload["path"], str):
            return Path(payload["path"])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None
