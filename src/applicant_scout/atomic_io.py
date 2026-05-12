"""Small atomic file-write helpers for local companion state."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


_PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(_PRIVATE_FILE_MODE)
    except OSError:
        # Best-effort only; Windows ACLs and filesystem policy can reject chmod.
        pass


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """Replace ``path`` with complete UTF-8 text or leave the old file intact.

    The temp file is created in the target directory so ``os.replace`` remains
    same-volume atomic. ``mkstemp`` avoids Windows open-handle replacement traps
    that come with NamedTemporaryFile.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
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
        if private:
            _chmod_private(temp_path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        if private:
            _chmod_private(path)
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
