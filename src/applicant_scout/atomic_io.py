"""Small atomic file-write helpers for local companion state."""

from __future__ import annotations

import csv
import io
import os
import stat
import subprocess
import tempfile
from pathlib import Path


_PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
_PRIVATE_DIR_MODE = stat.S_IRWXU
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ICACLS_TIMEOUT_SECONDS = 5
_WINDOWS_SYSTEM_SID = "*S-1-5-18"
_WINDOWS_ADMINISTRATORS_SID = "*S-1-5-32-544"
_WINDOWS_BROAD_ACCESS_SIDS = (
    "*S-1-1-0",  # Everyone
    "*S-1-5-11",  # Authenticated Users
    "*S-1-5-32-545",  # Users
)
_CURRENT_USER_SID_CACHE: str | None = None
_PRIVATE_ACL_CACHE: set[
    tuple[str, bool, tuple[int | None, int | None, int | None] | None]
] = set()


def _is_windows() -> bool:
    return os.name == "nt"


def _current_user_sid() -> str | None:
    try:
        completed = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_ICACLS_TIMEOUT_SECONDS,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        rows = list(csv.reader(io.StringIO(completed.stdout)))
    except csv.Error:
        return None
    if not rows or len(rows[0]) < 2:
        return None
    sid = rows[0][1].strip()
    if not sid.startswith("S-"):
        return None
    return f"*{sid}"


def _cached_current_user_sid() -> str | None:
    global _CURRENT_USER_SID_CACHE
    if _CURRENT_USER_SID_CACHE is None:
        _CURRENT_USER_SID_CACHE = _current_user_sid()
    return _CURRENT_USER_SID_CACHE


def _run_icacls(args: list[str]) -> bool:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_ICACLS_TIMEOUT_SECONDS,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _windows_private_grants(user_sid: str, *, directory: bool) -> list[str]:
    rights = "(OI)(CI)F" if directory else "(F)"
    return [
        f"{user_sid}:{rights}",
        f"{_WINDOWS_SYSTEM_SID}:{rights}",
        f"{_WINDOWS_ADMINISTRATORS_SID}:{rights}",
    ]


def _apply_windows_private_acl(path: Path, *, directory: bool) -> bool:
    user_sid = _cached_current_user_sid()
    if user_sid is None:
        return False
    path_text = os.fspath(path)
    ok = True
    for args in (
        ["icacls", path_text, "/inheritance:r", "/Q"],
        ["icacls", path_text, "/remove:g", *_WINDOWS_BROAD_ACCESS_SIDS, "/Q"],
        [
            "icacls",
            path_text,
            "/remove:d",
            *_WINDOWS_BROAD_ACCESS_SIDS,
            user_sid,
            "/Q",
        ],
        [
            "icacls",
            path_text,
            "/grant:r",
            *_windows_private_grants(user_sid, directory=directory),
            "/Q",
        ],
    ):
        ok = _run_icacls(args) and ok
    return ok


def _private_acl_cache_key(
    path: Path,
    *,
    directory: bool,
) -> tuple[str, bool, tuple[int | None, int | None, int | None] | None]:
    identity = None
    try:
        stat_result = path.stat()
    except OSError:
        pass
    else:
        dev = getattr(stat_result, "st_dev", None)
        ino = getattr(stat_result, "st_ino", None)
        if ino not in (None, 0):
            identity = (dev, int(ino), None)
        else:
            stable_time_ns = getattr(
                stat_result,
                "st_birthtime_ns",
                getattr(
                    stat_result,
                    "st_ctime_ns",
                    int(float(stat_result.st_ctime) * 1_000_000_000),
                ),
            )
            identity = (dev, None, int(stable_time_ns))
    return (os.path.normcase(os.path.abspath(os.fspath(path))), directory, identity)


def _apply_private_path_mode(path: Path, *, mode: int, directory: bool) -> bool:
    cache_key = _private_acl_cache_key(path, directory=directory)
    if cache_key in _PRIVATE_ACL_CACHE:
        return True
    try:
        path.chmod(mode)
    except OSError:
        # Best-effort only; Windows ACLs and filesystem policy can reject chmod.
        pass
    if _is_windows():
        acl_applied = _apply_windows_private_acl(path, directory=directory)
        if acl_applied:
            _PRIVATE_ACL_CACHE.add(cache_key)
        return acl_applied
    return True


def apply_private_file_mode(path: Path) -> bool:
    return _apply_private_path_mode(path, mode=_PRIVATE_FILE_MODE, directory=False)


def apply_private_directory_mode(path: Path) -> bool:
    return _apply_private_path_mode(path, mode=_PRIVATE_DIR_MODE, directory=True)


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """Replace ``path`` with complete UTF-8 text or leave the old file intact.

    The temp file is created in the target directory so ``os.replace`` remains
    same-volume atomic. ``mkstemp`` avoids Windows open-handle replacement traps
    that come with NamedTemporaryFile.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_private_ready = False
    if private:
        parent_private_ready = bool(apply_private_directory_mode(path.parent))
    fd = -1
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        # WHY: on Windows, once the target directory has private inheritable
        # grants, every temp file created inside it inherits that descriptor.
        # Re-running icacls for the temp file and final target turns each
        # settings autosave into a subprocess storm on the GUI path.
        if private and not (_is_windows() and parent_private_ready):
            apply_private_file_mode(temp_path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        if private and not (_is_windows() and parent_private_ready):
            apply_private_file_mode(path)
    except BaseException:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise
