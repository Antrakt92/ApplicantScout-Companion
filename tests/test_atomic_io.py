from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from applicant_scout import atomic_io
from applicant_scout.atomic_io import atomic_write_text


def _temp_files(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


def test_atomic_write_text_replaces_file_contents(tmp_path: Path):
    target = tmp_path / "state.txt"
    target.write_text("old", encoding="utf-8")

    atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert _temp_files(target) == []


def test_atomic_write_text_creates_parent_directory(tmp_path: Path):
    target = tmp_path / "nested" / "config.env"

    atomic_write_text(target, "WCL_CLIENT_ID=client\n")

    assert target.read_text(encoding="utf-8") == "WCL_CLIENT_ID=client\n"


def test_atomic_write_text_failed_replace_preserves_old_file_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "token.json"
    target.write_text("old-token", encoding="utf-8")

    def fail_replace(_src: object, _dst: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="locked"):
        atomic_write_text(target, "new-token")

    assert target.read_text(encoding="utf-8") == "old-token"
    assert _temp_files(target) == []


def test_atomic_write_text_failed_flush_preserves_old_file_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "cache.json"
    target.write_text("old-cache", encoding="utf-8")

    def fail_fsync(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(target, "new-cache")

    assert target.read_text(encoding="utf-8") == "old-cache"
    assert _temp_files(target) == []


def test_atomic_write_text_private_mode_chmods_temp_and_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "config.env"
    calls: list[tuple[Path, int]] = []
    path_type = type(target)
    original_chmod = path_type.chmod

    def record_chmod(self: Path, mode: int) -> None:
        calls.append((self, mode))
        original_chmod(self, mode)

    monkeypatch.setattr(path_type, "chmod", record_chmod)

    atomic_write_text(target, "secret", private=True)

    assert target.read_text(encoding="utf-8") == "secret"
    assert any(path == target for path, _mode in calls)
    assert all(mode == stat.S_IRUSR | stat.S_IWUSR for _path, mode in calls)


def test_atomic_write_text_uses_target_directory_for_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "state.txt"
    seen: list[tuple[str, str]] = []
    original_mkstemp = atomic_io.tempfile.mkstemp

    def record_mkstemp(*args, **kwargs):
        seen.append((kwargs["prefix"], os.fspath(kwargs["dir"])))
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(atomic_io.tempfile, "mkstemp", record_mkstemp)

    atomic_write_text(target, "ok")

    assert seen == [(f".{target.name}.", os.fspath(tmp_path))]
