from __future__ import annotations

from collections.abc import Iterator
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from applicant_scout import wow_lifecycle


class _Completed:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


@pytest.fixture(autouse=True)
def _reset_current_session_watcher() -> Iterator[None]:
    with wow_lifecycle._CURRENT_SESSION_WATCHER_LOCK:
        wow_lifecycle._CURRENT_SESSION_WATCHER = None
    yield
    with wow_lifecycle._CURRENT_SESSION_WATCHER_LOCK:
        wow_lifecycle._CURRENT_SESSION_WATCHER = None


@pytest.fixture
def _tasklist_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wow_lifecycle,
        "_windows_process_image_names",
        lambda: None,
        raising=False,
    )


def test_is_wow_running_prefers_native_process_snapshot(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        wow_lifecycle,
        "_windows_process_image_names",
        lambda: ["System", "Wow.exe", "python.exe"],
        raising=False,
    )
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("tasklist fallback ran after a successful native snapshot")
        ),
    )

    assert wow_lifecycle.is_wow_running()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process enumeration only")
def test_windows_process_image_names_contains_current_process():
    names = wow_lifecycle._windows_process_image_names()

    assert names is not None
    assert Path(sys.executable).name.casefold() in {name.casefold() for name in names}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process lookup only")
def test_process_name_for_pid_matches_current_process():
    assert wow_lifecycle.process_name_for_pid(os.getpid()) == Path(sys.executable).name


def test_is_wow_running_detects_retail_process(
    monkeypatch: pytest.MonkeyPatch,
    _tasklist_fallback: None,
):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return _Completed(stdout='"Wow.exe","10","Console","1","120,000 K"\n')

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fake_run)

    assert wow_lifecycle.is_wow_running()
    assert calls
    assert calls[0] == ["tasklist", "/FO", "CSV", "/NH"]


def test_is_wow_running_detects_ptr_process(
    monkeypatch: pytest.MonkeyPatch,
    _tasklist_fallback: None,
):
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(
            stdout='"WowT.exe","10","Console","1","120,000 K"\n'
        ),
    )

    assert wow_lifecycle.is_wow_running()


def test_is_wow_running_rejects_near_match_process_name(
    monkeypatch: pytest.MonkeyPatch,
    _tasklist_fallback: None,
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
    _tasklist_fallback: None,
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
    _tasklist_fallback: None,
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
    _tasklist_fallback: None,
):
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(stdout="No tasks are running\n"),
    )

    assert not wow_lifecycle.is_wow_running()


def test_is_wow_running_can_report_unknown_on_tasklist_failure(
    monkeypatch: pytest.MonkeyPatch,
    _tasklist_fallback: None,
):
    def fail_run(*_args, **_kwargs):
        raise wow_lifecycle.subprocess.TimeoutExpired(cmd="tasklist", timeout=5)

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fail_run)

    assert wow_lifecycle.is_wow_running() is False
    assert wow_lifecycle.is_wow_running(unknown_on_error=True) is None


def test_is_wow_running_can_report_unknown_on_nonzero_tasklist_exit(
    monkeypatch: pytest.MonkeyPatch,
    _tasklist_fallback: None,
):
    monkeypatch.setattr(
        wow_lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: _Completed(
            stdout='"Wow.exe","10","Console","1","120,000 K"\n',
            returncode=1,
        ),
    )

    assert wow_lifecycle.is_wow_running() is False
    assert wow_lifecycle.is_wow_running(unknown_on_error=True) is None


@pytest.fixture
def _foreground_window(monkeypatch: pytest.MonkeyPatch) -> int:
    hwnd = 0x1234
    monkeypatch.setattr(wow_lifecycle.sys, "platform", "win32")
    monkeypatch.setattr(wow_lifecycle, "foreground_window_handle", lambda: hwnd)
    return hwnd


def test_foreground_poll_classifies_its_captured_window(monkeypatch: pytest.MonkeyPatch):
    game_window, other_window = 0x1234, 0x5678
    foreground = iter((game_window, other_window))

    def get_foreground_window() -> int:
        return next(foreground)

    def get_window_process_id(hwnd: int, pid_out) -> int:
        pid = ctypes.cast(pid_out, ctypes.POINTER(wintypes.DWORD))
        pid[0] = 101 if hwnd == game_window else 202
        return 1

    user32 = SimpleNamespace(
        GetForegroundWindow=get_foreground_window,
        GetWindowThreadProcessId=get_window_process_id,
    )
    monkeypatch.setattr(wow_lifecycle.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(
        wow_lifecycle, "process_name_for_pid", {101: "Wow.exe", 202: "other.exe"}.get,
    )

    assert wow_lifecycle.foreground_state() == "game"
    assert next(foreground) == other_window


def test_is_wow_foreground_accepts_wow_process(
    monkeypatch: pytest.MonkeyPatch, _foreground_window: int
):
    queried_windows: list[int] = []

    def window_pid(hwnd: int) -> int:
        queried_windows.append(hwnd)
        return 123

    monkeypatch.setattr(wow_lifecycle, "foreground_process_id", window_pid)
    monkeypatch.setattr(
        wow_lifecycle,
        "process_name_for_pid",
        lambda pid: "Wow.exe" if pid == 123 else None,
    )

    assert wow_lifecycle.is_wow_foreground()
    assert queried_windows == [_foreground_window]


def test_is_wow_foreground_rejects_classic_process_name(
    monkeypatch: pytest.MonkeyPatch, _foreground_window: int
):
    monkeypatch.setattr(wow_lifecycle, "foreground_process_id", lambda _hwnd: 123)
    monkeypatch.setattr(
        wow_lifecycle,
        "process_name_for_pid",
        lambda pid: "WowClassic.exe" if pid == 123 else None,
    )

    assert not wow_lifecycle.is_wow_foreground()


def test_is_wow_foreground_rejects_current_companion_process(
    monkeypatch: pytest.MonkeyPatch, _foreground_window: int
):
    monkeypatch.setattr(wow_lifecycle, "foreground_process_id", lambda _hwnd: os.getpid())
    monkeypatch.setattr(wow_lifecycle, "process_name_for_pid", lambda _pid: "python.exe")

    assert not wow_lifecycle.is_wow_foreground()


def test_is_wow_foreground_rejects_other_process(
    monkeypatch: pytest.MonkeyPatch, _foreground_window: int
):
    monkeypatch.setattr(wow_lifecycle, "foreground_process_id", lambda _hwnd: 456)
    monkeypatch.setattr(wow_lifecycle, "process_name_for_pid", lambda _pid: "chrome.exe")

    assert not wow_lifecycle.is_wow_foreground()


def test_is_wow_sync_watcher_running_excludes_current_pid_from_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    exe = tmp_path / "ApplicantScout.exe"
    exe.write_text("", encoding="utf-8")
    captured = []

    monkeypatch.setattr(wow_lifecycle.os, "getpid", lambda: 4321)

    def fake_run(args, **kwargs):
        captured.append((args, kwargs["env"]))
        return _Completed(returncode=1)

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fake_run)

    assert not wow_lifecycle.is_wow_sync_watcher_running(executable_path=exe)
    assert captured
    args, env = captured[0]
    command = args[args.index("-Command") + 1]
    assert "ProcessId" in command
    assert env["APSCOUT_WATCH_QUERY_PID"] == "4321"


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
    captured = []

    monkeypatch.setattr(wow_lifecycle.os, "getpid", lambda: 4321)

    def fake_run(args, **kwargs):
        captured.append((args, kwargs["env"]))
        return _Completed(returncode=0)

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fake_run)

    assert wow_lifecycle.is_wow_sync_watcher_running(
        executable_path=python_exe,
        arguments=("-m", "applicant_scout"),
    )
    args, env = captured[0]
    command = args[args.index("-Command") + 1]
    assert "Select-Object -Skip" not in command
    assert "$_.Name -ieq 'ApplicantScout.exe'" not in command
    assert "CommandLine" in command
    assert "IndexOf($target" not in command
    assert "[System.IO.Path]::GetFullPath($exe)" in command
    assert env["APSCOUT_WATCH_QUERY_ARGS"] == "\x1f".join(("-m", "applicant_scout"))


@pytest.mark.skipif(sys.platform != "win32", reason="Uses Windows PowerShell argument binding")
def test_watcher_probe_passes_paths_with_spaces_as_data(monkeypatch: pytest.MonkeyPatch):
    exe = Path(r"C:\Program Files\ApplicantScout\ApplicantScout.exe")
    real_run = subprocess.run

    def run_with_process_fixture(args, **kwargs):
        script_index = args.index("-Command") + 1
        args[script_index] = (
            "function Get-CimInstance { [pscustomobject]@{ "
            "ExecutablePath = $env:APSCOUT_TEST_FAKE_EXE; ProcessId = 4322; "
            "CommandLine = ('\"' + $env:APSCOUT_TEST_FAKE_EXE + '\" --watch-wow') } }; "
            + args[script_index]
        )
        kwargs["env"] = dict(kwargs.get("env", os.environ), APSCOUT_TEST_FAKE_EXE=str(exe))
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run_with_process_fixture)
    assert wow_lifecycle.is_wow_sync_watcher_running(
        executable_path=exe, arguments=(), current_pid=4321,
    )


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
    replacements: list[tuple[Path, Path]] = []

    def fake_run(args, **_kwargs):
        commands.append(args)
        return _Completed()

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", fake_run)
    monkeypatch.setattr(
        wow_lifecycle,
        "_replace_file",
        lambda source, destination: replacements.append((source, destination)),
    )

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
    assert len(replacements) == 1
    assert replacements[0][1] == shortcut
    assert str(replacements[0][0]) in script
    assert replacements[0][0].suffix == ".lnk"
    assert replacements[0][0].parent == (
        shortcut.parent / wow_lifecycle.STARTUP_SHORTCUT_STAGING_DIR_NAME
    )
    assert replacements[0][0].parent != shortcut.parent


def test_configure_wow_sync_startup_writes_dev_module_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    shortcut = tmp_path / "Startup" / "ApplicantScout Companion.lnk"
    python_exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")
    commands: list[list[str]] = []
    replacements: list[tuple[Path, Path]] = []

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", lambda args, **_kwargs: commands.append(args) or _Completed())
    monkeypatch.setattr(
        wow_lifecycle,
        "_replace_file",
        lambda source, destination: replacements.append((source, destination)),
    )

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
    assert len(replacements) == 1
    assert replacements[0][1] == shortcut


def test_configure_wow_sync_startup_timeout_preserves_existing_shortcut(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    shortcut = tmp_path / "Startup" / "ApplicantScout Companion.lnk"
    shortcut.parent.mkdir(parents=True)
    shortcut.write_text("working", encoding="utf-8")
    exe = tmp_path / "ApplicantScout.exe"
    exe.write_text("", encoding="utf-8")
    replacements: list[tuple[Path, Path]] = []

    def timeout(args, **kwargs):
        script = args[-1]
        create_line = next(
            line for line in script.splitlines() if "$shell.CreateShortcut(" in line
        )
        temporary_path = create_line.split("CreateShortcut('", 1)[1].rsplit("')", 1)[0]
        Path(temporary_path.replace("''", "'")).write_text(
            "partial",
            encoding="utf-8",
        )
        raise wow_lifecycle.subprocess.TimeoutExpired(
            "powershell",
            kwargs["timeout"],
        )

    monkeypatch.setattr(wow_lifecycle.subprocess, "run", timeout)
    monkeypatch.setattr(
        wow_lifecycle,
        "_replace_file",
        lambda source, destination: replacements.append((source, destination)),
    )

    with pytest.raises(wow_lifecycle.subprocess.TimeoutExpired):
        wow_lifecycle.configure_wow_sync_startup(
            True,
            executable_path=exe,
            shortcut_path=shortcut,
        )

    assert shortcut.read_text(encoding="utf-8") == "working"
    assert replacements == []
    assert not (
        shortcut.parent / wow_lifecycle.STARTUP_SHORTCUT_STAGING_DIR_NAME
    ).exists()



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


def test_stop_current_session_watcher_terminates_live_helper(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class FakeProcess:
        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def wait(self, timeout: float | None = None) -> int:
            calls.append(f"wait:{timeout}")
            return 0

        def kill(self) -> None:
            calls.append("kill")

    monkeypatch.setattr(
        wow_lifecycle,
        "_CURRENT_SESSION_WATCHER",
        FakeProcess(),
        raising=False,
    )

    assert wow_lifecycle.stop_current_session_watcher()

    assert calls == ["terminate", "wait:2.0"]
    assert wow_lifecycle._CURRENT_SESSION_WATCHER is None


def test_stop_current_session_watcher_ignores_finished_helper(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class FakeProcess:
        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            calls.append("terminate")

    monkeypatch.setattr(
        wow_lifecycle,
        "_CURRENT_SESSION_WATCHER",
        FakeProcess(),
        raising=False,
    )

    assert wow_lifecycle.stop_current_session_watcher()

    assert calls == []
    assert wow_lifecycle._CURRENT_SESSION_WATCHER is None


def test_stop_current_session_watcher_clears_reference_before_process_wait(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class FakeProcess:
        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def wait(self, timeout: float | None = None) -> int:
            assert wow_lifecycle._CURRENT_SESSION_WATCHER is None
            calls.append(f"wait:{timeout}")
            return 0

    monkeypatch.setattr(
        wow_lifecycle,
        "_CURRENT_SESSION_WATCHER",
        FakeProcess(),
        raising=False,
    )

    assert wow_lifecycle.stop_current_session_watcher()

    assert calls == ["terminate", "wait:2.0"]
