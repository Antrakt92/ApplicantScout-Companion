from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from applicant_scout import wow_lifecycle


class _Completed:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_is_wow_running_detects_retail_process(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return _Completed(stdout='"Wow.exe","10","Console","1","120,000 K"\n')

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fake_run)

    assert wow_lifecycle.is_wow_running()
    assert calls
    assert calls[0] == ["tasklist", "/FO", "CSV", "/NH"]


def test_is_wow_running_rejects_near_match_process_name(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(
            stdout='"NotWow.exe","10","Console","1","120,000 K"\n'
        ),
    )

    assert not wow_lifecycle.is_wow_running()


def test_is_wow_running_rejects_classic_process_name(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(
            stdout='"WowClassic.exe","10","Console","1","120,000 K"\n'
        ),
    )

    assert not wow_lifecycle.is_wow_running()


def test_is_wow_running_ignores_wow_token_outside_image_column(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(
            stdout='"python.exe","10","Console","1","Wow.exe helper"\n'
        ),
    )

    assert not wow_lifecycle.is_wow_running()


def test_is_wow_running_returns_false_when_tasklist_has_no_wow(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(stdout="No tasks are running\n"),
    )

    assert not wow_lifecycle.is_wow_running()


def test_is_wow_foreground_accepts_wow_process(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wow_lifecycle, "foreground_process_id", lambda: 123)
    monkeypatch.setattr(
        wow_lifecycle,
        "process_name_for_pid",
        lambda pid: "Wow.exe" if pid == 123 else None,
    )

    assert wow_lifecycle.is_wow_foreground()


def test_is_wow_foreground_rejects_classic_process_name(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(wow_lifecycle, "foreground_process_id", lambda: 123)
    monkeypatch.setattr(
        wow_lifecycle,
        "process_name_for_pid",
        lambda pid: "WowClassic.exe" if pid == 123 else None,
    )

    assert not wow_lifecycle.is_wow_foreground()


def test_is_wow_foreground_rejects_current_companion_process(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(wow_lifecycle, "foreground_process_id", os.getpid)
    monkeypatch.setattr(wow_lifecycle, "process_name_for_pid", lambda _pid: "python.exe")

    assert not wow_lifecycle.is_wow_foreground()


def test_is_wow_foreground_rejects_other_process(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wow_lifecycle, "foreground_process_id", lambda: 456)
    monkeypatch.setattr(wow_lifecycle, "process_name_for_pid", lambda _pid: "chrome.exe")

    assert not wow_lifecycle.is_wow_foreground()


def test_is_wow_sync_watcher_running_excludes_current_pid_from_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    exe = tmp_path / "ApplicantScout.exe"
    exe.write_text("", encoding="utf-8")
    captured: list[list[str]] = []

    monkeypatch.setattr(wow_lifecycle.os, "getpid", lambda: 4321)

    def fake_run(args, **_kwargs):
        captured.append(args)
        return _Completed(returncode=1)

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fake_run)

    assert not wow_lifecycle.is_wow_sync_watcher_running(executable_path=exe)
    assert captured
    command = captured[0][captured[0].index("-Command") + 1]
    assert "ProcessId" in command
    assert "4321" in captured[0]


def test_dev_launch_spec_runs_python_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    python_exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    module_main = tmp_path / "src" / "applicant_scout" / "__main__.py"
    python_exe.parent.mkdir(parents=True)
    module_main.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")
    module_main.write_text("", encoding="utf-8")

    monkeypatch.setattr(wow_lifecycle.sys, "frozen", False, raising=False)
    monkeypatch.setattr(wow_lifecycle.sys, "executable", str(python_exe))
    monkeypatch.setattr(wow_lifecycle.sys, "argv", [str(module_main)])

    spec = wow_lifecycle.companion_launch_spec()

    assert spec.executable == python_exe
    assert spec.arguments == ("-m", "applicant_scout")


def test_is_wow_sync_watcher_running_matches_python_module_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    python_exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")
    captured: list[list[str]] = []

    monkeypatch.setattr(wow_lifecycle.os, "getpid", lambda: 4321)

    def fake_run(args, **_kwargs):
        captured.append(args)
        return _Completed(returncode=0)

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fake_run)

    assert wow_lifecycle.is_wow_sync_watcher_running(
        executable_path=python_exe,
        arguments=("-m", "applicant_scout"),
    )
    command = captured[0][captured[0].index("-Command") + 1]
    assert "param(" in command
    assert "Select-Object -Skip" not in command
    assert "$_.Name -ieq 'ApplicantScout.exe'" not in command
    assert "CommandLine" in command
    assert "IndexOf($target" not in command
    assert "[System.IO.Path]::GetFullPath($exe)" in command
    assert "\x1f".join(("-m", "applicant_scout")) in captured[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process enumeration only")
def test_is_wow_sync_watcher_running_does_not_match_probe_for_missing_packaged_target():
    assert not wow_lifecycle.is_wow_sync_watcher_running(
        executable_path=Path(r"C:\DefinitelyMissing\ApplicantScout.exe"),
        arguments=(),
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process enumeration only")
def test_is_wow_sync_watcher_running_does_not_match_probe_for_missing_dev_target():
    assert not wow_lifecycle.is_wow_sync_watcher_running(
        executable_path=Path(r"C:\DefinitelyMissing\python.exe"),
        arguments=("-m", "applicant_scout"),
    )


def test_start_wow_sync_watcher_propagates_invalid_executable_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    exe = tmp_path / "ApplicantScout.exe"
    exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(wow_lifecycle, "is_wow_sync_watcher_running", lambda **_kwargs: False)

    def fake_popen(*_args, **_kwargs):
        raise OSError(193, "%1 is not a valid Win32 application")

    monkeypatch.setattr(wow_lifecycle.subprocess, "Popen", fake_popen)

    with pytest.raises(OSError, match="valid Win32 application"):
        wow_lifecycle.start_wow_sync_watcher(executable_path=exe)


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


def test_configure_wow_sync_startup_writes_dev_module_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    shortcut = tmp_path / "Startup" / "ApplicantScout Companion.lnk"
    python_exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", lambda args, **_kwargs: commands.append(args) or _Completed())

    result = wow_lifecycle.configure_wow_sync_startup(
        True,
        executable_path=python_exe,
        arguments=("-m", "applicant_scout"),
        shortcut_path=shortcut,
    )

    assert result == shortcut
    script = commands[0][-1]
    assert str(python_exe) in script
    assert "-m applicant_scout --watch-wow" in script



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


def test_start_wow_sync_watcher_uses_dev_module_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    python_exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(wow_lifecycle, "is_wow_sync_watcher_running", lambda **_kwargs: False)
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "Popen",
        lambda args, **_kwargs: calls.append(args),
    )

    wow_lifecycle.start_wow_sync_watcher(
        executable_path=python_exe,
        arguments=("-m", "applicant_scout"),
    )

    assert calls == [[str(python_exe), "-m", "applicant_scout", "--watch-wow"]]
