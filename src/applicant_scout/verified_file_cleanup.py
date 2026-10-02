"""Windows cleanup bound to a verified file object rather than a mutable path."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
from pathlib import Path
import stat
from typing import Any


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


@lru_cache(maxsize=1)
def _windows_api() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def unlink_verified_file(
    path: Path,
    *,
    identity: tuple[int, int],
    mtime_ns: int,
    size: int,
) -> bool:
    """Preserve when the platform cannot claim exclusive mutation ownership."""
    if os.name != "nt" or identity[1] <= 0:
        return False
    import msvcrt

    kernel32 = _windows_api()
    # GENERIC_READ | DELETE; FILE_SHARE_READ denies both writers and renames.
    # OPEN_REPARSE_POINT claims the link itself, never a redirected target.
    handle = kernel32.CreateFileW(
        os.path.abspath(path), 0x80010000, 0x1, None, 3, 0x00200000, None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (2, 3):  # Already gone: no pathname deletion is needed.
            return True
        raise ctypes.WinError(error)

    descriptor = None
    try:
        # Ownership transfers only on success; close the descriptor once below.
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or getattr(current, "st_file_attributes", 0) & 0x400
            or (current.st_dev, current.st_ino) != identity
            or current.st_size != size
            or current.st_mtime_ns != mtime_ns
        ):
            return False
        disposition = _FileDispositionInfo(1)
        # FileDispositionInfo marks this handle's object for deletion on close.
        # No pathname fallback: failed sharing/validation/disposition preserves it.
        if not kernel32.SetFileInformationByHandle(
            handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        if descriptor is None:
            kernel32.CloseHandle(handle)
        else:
            os.close(descriptor)
