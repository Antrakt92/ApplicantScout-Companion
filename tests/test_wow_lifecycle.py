from __future__ import annotations

from pathlib import Path

import pytest

from applicant_scout import wow_lifecycle


class _Completed:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout


def test_is_wow_running_detects_retail_process(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return _Completed(stdout='Image Name                     PID\nWow.exe                        10\n')

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fake_run)

    assert wow_lifecycle.is_wow_running()
    assert calls
    assert "tasklist" in calls[0][0].lower()


def test_is_wow_running_returns_false_when_tasklist_has_no_wow(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(stdout="No tasks are running\n"),
    )

    assert not wow_lifecycle.is_wow_running()


def test_configure_wow_sync_startup_creates_watch_shortcut(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    shortcut = tmp_path / "Startup" / "ApplicantScout Companion.lnk"
    exe = tmp_path / "ApplicantScout.exe"
    exe.write_text("", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(args, **_kwargs):
        commands.append(args)
        return _Completed()

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fake_run)

    result = wow_lifecycle.configure_wow_sync_startup(
        True,
        executable_path=exe,
        shortcut_path=shortcut,
    )

    assert result == shortcut
    assert shortcut.parent.is_dir()
    assert commands
    script = commands[0][-1]
    assert str(exe) in script
    assert "--watch-wow" in script
    assert str(shortcut) in script


def test_configure_wow_sync_startup_removes_existing_shortcut(tmp_path: Path):
    shortcut = tmp_path / "Startup" / "ApplicantScout Companion.lnk"
    shortcut.parent.mkdir(parents=True)
    shortcut.write_text("old", encoding="utf-8")

    result = wow_lifecycle.configure_wow_sync_startup(
        False,
        shortcut_path=shortcut,
    )

    assert result is None
    assert not shortcut.exists()


def test_start_wow_sync_watcher_skips_existing_watcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    exe = tmp_path / "ApplicantScout.exe"
    exe.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(wow_lifecycle, "is_wow_sync_watcher_running", lambda **_kwargs: True)
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "Popen",
        lambda args, **_kwargs: calls.append(args),
    )

    assert wow_lifecycle.start_wow_sync_watcher(executable_path=exe) is None
    assert calls == []
