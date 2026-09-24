"""Small atomic file-write helpers for local companion state."""

from __future__ import annotations

import csv
from collections.abc import Callable
import io
import os
import re
import stat
import subprocess
import tempfile
import threading
from pathlib import Path


_PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
_PRIVATE_DIR_MODE = stat.S_IRWXU
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ICACLS_TIMEOUT_SECONDS = 5
_WINDOWS_SYSTEM_SID = "*S-1-5-18"
_WINDOWS_ADMINISTRATORS_SID = "*S-1-5-32-544"
_WINDOWS_SID_PATTERN = re.compile(r"S-\d+(?:-\d+){2,}\Z", re.ASCII)
_CURRENT_USER_SID_CACHE: str | None = None
_PRIVATE_ACL_CACHE: set[
    tuple[str, bool, tuple[int | None, int | None, int | None] | None]
] = set()
# Guards _PRIVATE_ACL_CACHE and the deferred startup set below: privatization
# now also runs on a background thread after startup (see H3), so the cache
# and the pending set need a lock where the GUI-thread-only path did not.
_PRIVATE_ACL_LOCK = threading.Lock()
# Startup deferral (H3): while set, Windows ACL mutations are recorded instead
# of applied so config/usage/cache init on the startup path costs chmod only.
# flush_deferred_privatization() applies the identical mutations later from a
# background thread. Same end state, ordered later. Off by default.
_STARTUP_ACL_DEFERRED = False
_DEFERRED_PRIVATE_PATHS: set[tuple[str, bool]] = set()
# Exact-match expectations for the zero-spawn already-private check.
_FULL_CONTROL_MASK = 0x1F01FF
_ACCESS_ALLOWED_ACE_TYPE = 0
_OBJECT_INHERIT_ACE_FLAG = 0x1
_CONTAINER_INHERIT_ACE_FLAG = 0x2
_SE_DACL_PROTECTED = 0x1000


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
    if _WINDOWS_SID_PATTERN.fullmatch(sid) is None:
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
    # H3 fast path: a descriptor that already matches needs no subprocess at
    # all (previously: whoami [cached] + icacls /save + up to 3 mutations).
    if _windows_dacl_matches_private(path, user_sid, directory=directory):
        return True
    path_text = os.fspath(path)
    grants = [
        "icacls",
        path_text,
        "/grant:r",
        *_windows_private_grants(user_sid, directory=directory),
        "/Q",
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="applicant-scout-acl-") as backup_dir:
            backup_path = Path(backup_dir) / "dacl.txt"
            save = ["icacls", path_text, "/save", os.fspath(backup_path), "/Q"]
            if not _run_icacls(save):
                return False

            # WHY: /reset removes arbitrary explicit ACEs but temporarily restores
            # the parent's inherited ACL. Install the recovery principals before
            # removing inheritance, so a failed final step cannot lock out the
            # current user. The saved DACL provides best-effort rollback whenever
            # any mutating step reports failure.
            mutations = (
                ["icacls", path_text, "/reset", "/Q"],
                grants,
                ["icacls", path_text, "/inheritance:r", "/Q"],
            )
            for command in mutations:
                if _run_icacls(command):
                    continue
                restored = _run_icacls(
                    [
                        "icacls",
                        os.fspath(path.parent),
                        "/restore",
                        os.fspath(backup_path),
                        "/Q",
                    ]
                )
                if not restored:
                    # Last-resort recoverability: retain inheritance and restore
                    # the known administrative principals instead of attempting
                    # another destructive reset/removal sequence.
                    _run_icacls(grants)
                    _run_icacls(["icacls", path_text, "/inheritance:e", "/Q"])
                return False
    except OSError:
        return False
    return True


def set_startup_privatization_deferred(enabled: bool) -> None:
    """Defer Windows ACL mutations to flush_deferred_privatization() (H3).

    While deferred, apply_private_*_mode performs chmod only and records the
    path; the identical icacls mutations run later on a background thread.
    Same end state, ordered later. Never weakens the final descriptor.
    """
    global _STARTUP_ACL_DEFERRED
    with _PRIVATE_ACL_LOCK:
        _STARTUP_ACL_DEFERRED = bool(enabled)


def flush_deferred_privatization() -> int:
    """Apply recorded Windows ACL mutations; return the privatized count."""
    with _PRIVATE_ACL_LOCK:
        pending = sorted(_DEFERRED_PRIVATE_PATHS)
        _DEFERRED_PRIVATE_PATHS.clear()
    applied = 0
    for path_text, directory in pending:
        path = Path(path_text)
        try:
            exists = path.exists()
        except OSError:
            continue
        if not exists:
            continue
        if _is_windows():
            if _apply_windows_private_acl(path, directory=directory):
                with _PRIVATE_ACL_LOCK:
                    _PRIVATE_ACL_CACHE.add(
                        _private_acl_cache_key(path, directory=directory)
                    )
                applied += 1
        else:
            applied += 1
    return applied


def _expected_private_sids(
    user_sid: str,
) -> tuple[bytes | None, bytes | None, bytes | None]:
    import ctypes

    def _sid_bytes(text: str) -> bytes | None:
        sid = ctypes.c_void_p()
        convert = ctypes.windll.advapi32.ConvertStringSidToSidW
        convert.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
        convert.restype = ctypes.c_int
        if not convert(text, ctypes.byref(sid)):
            return None
        length = ctypes.windll.advapi32.GetLengthSid(sid)
        raw = ctypes.string_at(sid, length)
        ctypes.windll.kernel32.LocalFree(sid)
        return raw

    return (
        _sid_bytes(user_sid.lstrip("*")),
        _sid_bytes(_WINDOWS_SYSTEM_SID.lstrip("*")),
        _sid_bytes(_WINDOWS_ADMINISTRATORS_SID.lstrip("*")),
    )


def _windows_dacl_matches_private(
    path: Path, user_sid: str, *, directory: bool
) -> bool:
    """Zero-spawn already-private check (H3 fast path).

    Reads the DACL via GetNamedSecurityInfoW and requires an exact match:
    protected (no inheritance), exactly three ACCESS_ALLOWED ACEs with full
    control for the current user, SYSTEM, and Administrators, with
    (OI)(CI) inheritance flags on directories and none on files. Anything
    else — including any API failure — returns False so the caller falls
    back to the full icacls mutation sequence. Fail-closed, never weakens.
    """
    if not _is_windows():
        return False
    try:
        import ctypes

        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        path_text = os.fspath(path)
        security_info = 0x4  # DACL_SECURITY_INFORMATION
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        GetNamedSecurityInfoW = advapi32.GetNamedSecurityInfoW
        GetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        GetNamedSecurityInfoW.restype = ctypes.c_uint
        if GetNamedSecurityInfoW(
            path_text, 1, security_info, None, None,
            ctypes.byref(dacl), None, ctypes.byref(descriptor),
        ) != 0:
            return False
        try:
            control = ctypes.c_ushort()
            GetSecurityDescriptorControl = advapi32.GetSecurityDescriptorControl
            GetSecurityDescriptorControl.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ushort),
                ctypes.POINTER(ctypes.c_ulong),
            ]
            GetSecurityDescriptorControl.restype = ctypes.c_int
            revision = ctypes.c_ulong()
            if not GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                return False
            if not control.value & _SE_DACL_PROTECTED:
                return False
            GetSecurityDescriptorDacl = advapi32.GetSecurityDescriptorDacl
            GetSecurityDescriptorDacl.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_int),
            ]
            GetSecurityDescriptorDacl.restype = ctypes.c_int
            present = ctypes.c_int()
            defaulted = ctypes.c_int()
            if not GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present),
                ctypes.byref(dacl), ctypes.byref(defaulted),
            ):
                return False
            if not present.value or not dacl.value:
                return False
            class _AclSizeInformation(ctypes.Structure):
                _fields_ = (
                    ("AceCount", ctypes.c_ulong),
                    ("AclBytesInUse", ctypes.c_ulong),
                    ("AclBytesFree", ctypes.c_ulong),
                )

            size_info = _AclSizeInformation()
            GetAclInformation = advapi32.GetAclInformation
            GetAclInformation.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ]
            GetAclInformation.restype = ctypes.c_int
            if not GetAclInformation(
                dacl, ctypes.byref(size_info), ctypes.sizeof(size_info), 2
            ):
                return False
            if size_info.AceCount != 3:
                return False
            expected_flags = (
                (_OBJECT_INHERIT_ACE_FLAG | _CONTAINER_INHERIT_ACE_FLAG)
                if directory
                else 0
            )
            expected_sids = _expected_private_sids(user_sid)
            if any(item is None for item in expected_sids):
                return False
            expected_buffers = [
                ctypes.create_string_buffer(item or b"") for item in expected_sids
            ]
            matched = [False, False, False]
            GetAce = advapi32.GetAce
            GetAce.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
            GetAce.restype = ctypes.c_int
            EqualSid = advapi32.EqualSid
            EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            EqualSid.restype = ctypes.c_int
            for index in range(3):
                ace_ptr = ctypes.c_void_p()
                if not GetAce(dacl, index, ctypes.byref(ace_ptr)):
                    return False
                base = ace_ptr.value
                if base is None:
                    return False
                ace_type = ctypes.c_ubyte.from_address(base).value
                ace_flags = ctypes.c_ubyte.from_address(base + 1).value
                mask = ctypes.c_ulong.from_address(base + 4).value
                if (
                    ace_type != _ACCESS_ALLOWED_ACE_TYPE
                    or ace_flags != expected_flags
                    or mask != _FULL_CONTROL_MASK
                ):
                    return False
                sid_ptr = base + 8
                for slot, expected_buf in enumerate(expected_buffers):
                    if matched[slot]:
                        continue
                    if EqualSid(ctypes.c_void_p(sid_ptr), expected_buf):
                        matched[slot] = True
                        break
                else:
                    return False
            return all(matched)
        finally:
            kernel32.LocalFree(descriptor)
    except (AttributeError, OSError, ValueError):
        return False


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
    with _PRIVATE_ACL_LOCK:
        if cache_key in _PRIVATE_ACL_CACHE:
            return True
    try:
        path.chmod(mode)
    except OSError:
        # Best-effort only; Windows ACLs and filesystem policy can reject chmod.
        pass
    if _is_windows():
        with _PRIVATE_ACL_LOCK:
            deferred = _STARTUP_ACL_DEFERRED
            if deferred:
                # H3: chmod is done; the identical icacls mutations are recorded
                # for the post-startup background flush (same end state, later).
                _DEFERRED_PRIVATE_PATHS.add(
                    (os.path.normcase(os.path.abspath(os.fspath(path))), directory)
                )
                return True
        acl_applied = _apply_windows_private_acl(path, directory=directory)
        if acl_applied:
            with _PRIVATE_ACL_LOCK:
                _PRIVATE_ACL_CACHE.add(cache_key)
        return acl_applied
    return True


def apply_private_file_mode(path: Path) -> bool:
    return _apply_private_path_mode(path, mode=_PRIVATE_FILE_MODE, directory=False)


def apply_private_directory_mode(path: Path) -> bool:
    return _apply_private_path_mode(path, mode=_PRIVATE_DIR_MODE, directory=True)


def _atomic_write(
    path: Path,
    *,
    private: bool,
    text_mode: bool,
    write_contents: Callable[[int], None],
) -> None:
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
            text=text_mode,
        )
        temp_path = Path(temp_name)
        # WHY: on Windows, once the target directory has private inheritable
        # grants, every temp file created inside it inherits that descriptor.
        # Re-running icacls for the temp file and final target turns each
        # settings autosave into a subprocess storm on the GUI path.
        if private and not (_is_windows() and parent_private_ready):
            temp_private_ready = bool(apply_private_file_mode(temp_path))
            if _is_windows() and not temp_private_ready:
                raise PermissionError(
                    f"Could not secure temporary private file for {path.name}"
                )
        write_contents(fd)
        owned_fd, fd = fd, -1
        os.close(owned_fd)
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


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """Replace ``path`` with complete UTF-8 text or leave the old file intact.

    The temp file is created in the target directory so ``os.replace`` remains
    same-volume atomic. ``mkstemp`` avoids Windows open-handle replacement traps
    that come with NamedTemporaryFile.
    """

    def _write(fd: int) -> None:
        # The outer transaction owns the descriptor even if flushing fails.
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_write(
        path,
        private=private,
        text_mode=True,
        write_contents=_write,
    )


def atomic_write_bytes(path: Path, contents: bytes, *, private: bool = False) -> None:
    """Replace ``path`` with exact bytes or leave the old file intact."""

    def _write(fd: int) -> None:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_write(
        path,
        private=private,
        text_mode=False,
        write_contents=_write,
    )
