from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from applicant_scout import atomic_io
from applicant_scout.atomic_io import (
    apply_private_directory_mode,
    apply_private_file_mode,
    atomic_write_text,
)


class _Completed:
    def __init__(self, *, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def _temp_files(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


@pytest.fixture(autouse=True)
def _reset_atomic_io_caches():
    atomic_io._CURRENT_USER_SID_CACHE = None
    atomic_io._PRIVATE_ACL_CACHE.clear()
    yield
    atomic_io._CURRENT_USER_SID_CACHE = None
    atomic_io._PRIVATE_ACL_CACHE.clear()


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

    monkeypatch.setattr(atomic_io, "_is_windows", lambda: False)
    monkeypatch.setattr(path_type, "chmod", record_chmod)

    atomic_write_text(target, "secret", private=True)

    assert target.read_text(encoding="utf-8") == "secret"
    assert any(path == target for path, _mode in calls)
    assert any(path == tmp_path for path, _mode in calls)
    assert {mode for _path, mode in calls} <= {
        stat.S_IRUSR | stat.S_IWUSR,
        stat.S_IRWXU,
    }


def test_current_user_sid_parses_whoami_csv_output(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> _Completed:
        calls.append(args)
        return _Completed(stdout='"desktop\\dima","S-1-5-21-1-2-3-1007"\n')

    monkeypatch.setattr(atomic_io.subprocess, "run", fake_run)

    assert atomic_io._current_user_sid() == "*S-1-5-21-1-2-3-1007"
    assert calls == [["whoami", "/user", "/fo", "csv", "/nh"]]


def test_current_user_sid_returns_none_for_malformed_output(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        atomic_io.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(stdout="not csv\n"),
    )

    assert atomic_io._current_user_sid() is None


def test_apply_private_file_mode_uses_windows_acl_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "token.json"
    target.write_text("secret", encoding="utf-8")
    chmod_modes: list[int] = []
    commands: list[list[str]] = []

    def record_chmod(_self: Path, mode: int) -> None:
        chmod_modes.append(mode)

    def fake_run(args: list[str], **_kwargs: object) -> _Completed:
        commands.append(args)
        return _Completed(returncode=0)

    monkeypatch.setattr(type(target), "chmod", record_chmod)
    monkeypatch.setattr(atomic_io, "_is_windows", lambda: True)
    monkeypatch.setattr(
        atomic_io,
        "_current_user_sid",
        lambda: "*S-1-5-21-1-2-3-1007",
    )
    monkeypatch.setattr(atomic_io.subprocess, "run", fake_run)

    apply_private_file_mode(target)

    assert chmod_modes == [stat.S_IRUSR | stat.S_IWUSR]
    assert commands == [
        ["icacls", os.fspath(target), "/inheritance:r", "/Q"],
        [
            "icacls",
            os.fspath(target),
            "/remove:g",
            "*S-1-1-0",
            "*S-1-5-11",
            "*S-1-5-32-545",
            "/Q",
        ],
        [
            "icacls",
            os.fspath(target),
            "/remove:d",
            "*S-1-1-0",
            "*S-1-5-11",
            "*S-1-5-32-545",
            "*S-1-5-21-1-2-3-1007",
            "/Q",
        ],
        [
            "icacls",
            os.fspath(target),
            "/grant:r",
            "*S-1-5-21-1-2-3-1007:(F)",
            "*S-1-5-18:(F)",
            "*S-1-5-32-544:(F)",
            "/Q",
        ],
    ]


def test_apply_private_directory_mode_uses_inheritable_acl_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    commands: list[list[str]] = []

    monkeypatch.setattr(type(tmp_path), "chmod", lambda _self, _mode: None)
    monkeypatch.setattr(atomic_io, "_is_windows", lambda: True)
    monkeypatch.setattr(
        atomic_io,
        "_current_user_sid",
        lambda: "*S-1-5-21-1-2-3-1007",
    )
    monkeypatch.setattr(
        atomic_io.subprocess,
        "run",
        lambda args, **_kwargs: commands.append(args) or _Completed(returncode=0),
    )

    apply_private_directory_mode(tmp_path)

    assert commands[-1] == [
        "icacls",
        os.fspath(tmp_path),
        "/grant:r",
        "*S-1-5-21-1-2-3-1007:(OI)(CI)F",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F",
        "/Q",
    ]


def test_apply_private_file_mode_ignores_windows_acl_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "token.json"
    target.write_text("secret", encoding="utf-8")

    monkeypatch.setattr(atomic_io, "_is_windows", lambda: True)
    monkeypatch.setattr(
        atomic_io,
        "_current_user_sid",
        lambda: "*S-1-5-21-1-2-3-1007",
    )
    monkeypatch.setattr(
        atomic_io.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("icacls")),
    )

    apply_private_file_mode(target)

    assert target.read_text(encoding="utf-8") == "secret"


def test_atomic_write_text_private_mode_hardens_parent_before_temp_and_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "nested" / "config.env"
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(
        atomic_io,
        "apply_private_directory_mode",
        lambda path: calls.append(("dir", Path(path))),
    )
    monkeypatch.setattr(
        atomic_io,
        "apply_private_file_mode",
        lambda path: calls.append(("file", Path(path))),
    )

    atomic_write_text(target, "secret", private=True)

    assert target.read_text(encoding="utf-8") == "secret"
    assert calls[0] == ("dir", target.parent)
    assert calls[-1] == ("file", target)


def test_atomic_write_text_private_windows_reuses_parent_acl_for_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "config" / "config.env"
    commands: list[list[str]] = []

    if hasattr(atomic_io, "_PRIVATE_ACL_CACHE"):
        atomic_io._PRIVATE_ACL_CACHE.clear()
    if hasattr(atomic_io, "_CURRENT_USER_SID_CACHE"):
        atomic_io._CURRENT_USER_SID_CACHE = None
    monkeypatch.setattr(type(tmp_path), "chmod", lambda _self, _mode: None)
    monkeypatch.setattr(atomic_io, "_is_windows", lambda: True)
    monkeypatch.setattr(
        atomic_io,
        "_current_user_sid",
        lambda: "*S-1-5-21-1-2-3-1007",
    )
    monkeypatch.setattr(
        atomic_io.subprocess,
        "run",
        lambda args, **_kwargs: commands.append(args) or _Completed(returncode=0),
    )

    atomic_write_text(target, "secret", private=True)

    assert target.read_text(encoding="utf-8") == "secret"
    assert commands == [
        ["icacls", os.fspath(target.parent), "/inheritance:r", "/Q"],
        [
            "icacls",
            os.fspath(target.parent),
            "/remove:g",
            "*S-1-1-0",
            "*S-1-5-11",
            "*S-1-5-32-545",
            "/Q",
        ],
        [
            "icacls",
            os.fspath(target.parent),
            "/remove:d",
            "*S-1-1-0",
            "*S-1-5-11",
            "*S-1-5-32-545",
            "*S-1-5-21-1-2-3-1007",
            "/Q",
        ],
        [
            "icacls",
            os.fspath(target.parent),
            "/grant:r",
            "*S-1-5-21-1-2-3-1007:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
            "/Q",
        ],
    ]


def test_atomic_write_text_private_windows_reuses_hardened_parent_between_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "config" / "config.env"
    commands: list[list[str]] = []

    if hasattr(atomic_io, "_PRIVATE_ACL_CACHE"):
        atomic_io._PRIVATE_ACL_CACHE.clear()
    if hasattr(atomic_io, "_CURRENT_USER_SID_CACHE"):
        atomic_io._CURRENT_USER_SID_CACHE = None
    monkeypatch.setattr(type(tmp_path), "chmod", lambda _self, _mode: None)
    monkeypatch.setattr(atomic_io, "_is_windows", lambda: True)
    monkeypatch.setattr(
        atomic_io,
        "_current_user_sid",
        lambda: "*S-1-5-21-1-2-3-1007",
    )
    monkeypatch.setattr(
        atomic_io.subprocess,
        "run",
        lambda args, **_kwargs: commands.append(args) or _Completed(returncode=0),
    )

    atomic_write_text(target, "one", private=True)
    atomic_write_text(target, "two", private=True)

    assert target.read_text(encoding="utf-8") == "two"
    assert [command[1] for command in commands] == [os.fspath(target.parent)] * 4


def test_apply_private_directory_mode_rechecks_recreated_windows_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "config"
    target.mkdir()
    commands: list[list[str]] = []

    monkeypatch.setattr(type(tmp_path), "chmod", lambda _self, _mode: None)
    monkeypatch.setattr(atomic_io, "_is_windows", lambda: True)
    monkeypatch.setattr(
        atomic_io,
        "_current_user_sid",
        lambda: "*S-1-5-21-1-2-3-1007",
    )
    monkeypatch.setattr(
        atomic_io.subprocess,
        "run",
        lambda args, **_kwargs: commands.append(args) or _Completed(returncode=0),
    )

    assert atomic_io.apply_private_directory_mode(target)
    target.rmdir()
    target.mkdir()
    assert atomic_io.apply_private_directory_mode(target)

    assert [command[1] for command in commands] == [os.fspath(target)] * 8


def test_private_acl_cache_key_prefers_inode_over_changing_ctime():
    class FakePath:
        def __init__(self, *, ctime_ns: int) -> None:
            self._ctime_ns = ctime_ns

        def __fspath__(self) -> str:
            return "C:/Example/AppData/Local/ApplicantScout/config"

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_dev=10, st_ino=42, st_ctime_ns=self._ctime_ns)

    first = atomic_io._private_acl_cache_key(
        cast(Any, FakePath(ctime_ns=100)),
        directory=True,
    )
    second = atomic_io._private_acl_cache_key(
        cast(Any, FakePath(ctime_ns=200)),
        directory=True,
    )

    assert first == second


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration test")
def test_apply_private_file_mode_removes_inherited_everyone_acl(tmp_path: Path):
    parent = tmp_path / "public-parent"
    parent.mkdir()
    target = parent / "token.json"
    target.write_text("secret", encoding="utf-8")
    everyone_sid = "*S-1-1-0"
    subprocess.run(
        ["icacls", os.fspath(parent), "/grant", f"{everyone_sid}:(OI)(CI)R", "/Q"],
        check=True,
        capture_output=True,
        text=True,
    )
    before = subprocess.run(
        ["icacls", os.fspath(target), "/findsid", everyone_sid],
        check=False,
        capture_output=True,
        text=True,
    )
    assert "No files with a matching SID was found" not in before.stdout

    apply_private_file_mode(target)

    after = subprocess.run(
        ["icacls", os.fspath(target), "/findsid", everyone_sid],
        check=False,
        capture_output=True,
        text=True,
    )
    assert "No files with a matching SID was found" in after.stdout


def test_apply_private_file_mode_ignores_os_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "token.json"
    target.write_text("secret", encoding="utf-8")

    def fail_chmod(_self: Path, _mode: int) -> None:
        raise PermissionError("policy")

    monkeypatch.setattr(atomic_io, "_is_windows", lambda: False)
    monkeypatch.setattr(type(target), "chmod", fail_chmod)

    apply_private_file_mode(target)

    assert target.read_text(encoding="utf-8") == "secret"


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
