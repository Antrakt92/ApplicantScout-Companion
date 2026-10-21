import subprocess

import pytest

from applicant_scout import wow_lifecycle


@pytest.fixture(autouse=True)
def reset_launch_ownership(monkeypatch):
    monkeypatch.setattr(wow_lifecycle, "_CURRENT_SESSION_WATCHER_START", None)
    monkeypatch.setattr(wow_lifecycle, "_RETIRED_SESSION_WATCHERS", [])


def test_disabled_sync_reaps_helper_that_finishes_launching_late(monkeypatch, tmp_path):
    calls = []

    class Process:
        def poll(self):
            return None

        def terminate(self):
            calls.append('terminate')

        def wait(self, timeout):
            calls.append(('wait', timeout))
            return 0

    process = Process()
    monkeypatch.setattr(wow_lifecycle, '_CURRENT_SESSION_WATCHER', None)

    def launch(*_args, **_kwargs):
        assert wow_lifecycle.stop_current_session_watcher()
        return process

    monkeypatch.setattr(wow_lifecycle.subprocess, 'Popen', launch)
    result = wow_lifecycle.start_wow_sync_watcher(
        executable_path=tmp_path/'ApplicantScout.exe', arguments=(), check_existing=False,
    )
    assert result is None
    assert calls == ['terminate', ('wait', 2.0)]
    assert wow_lifecycle._CURRENT_SESSION_WATCHER is None


def test_overlapping_sync_enable_does_not_launch_untracked_duplicate(monkeypatch, tmp_path):
    calls = []

    class Process:
        def poll(self):
            return None

    process = Process()
    monkeypatch.setattr(wow_lifecycle, '_CURRENT_SESSION_WATCHER', None)

    def launch(*_args, **_kwargs):
        calls.append('launch')
        if len(calls) == 1:
            assert wow_lifecycle.start_wow_sync_watcher(
                executable_path=tmp_path/'ApplicantScout.exe', arguments=(),
                check_existing=False,
            ) is None
        return process

    monkeypatch.setattr(wow_lifecycle.subprocess, 'Popen', launch)
    assert wow_lifecycle.start_wow_sync_watcher(
        executable_path=tmp_path/'ApplicantScout.exe', arguments=(), check_existing=False,
    ) is process
    assert calls == ['launch']
    monkeypatch.setattr(wow_lifecycle, '_CURRENT_SESSION_WATCHER', None)


def test_failed_retired_launch_remains_owned_for_later_stop(monkeypatch, tmp_path):
    class Process:
        attempts = 0

        def poll(self):
            return None

        def terminate(self):
            self.attempts += 1
            if self.attempts == 1:
                raise PermissionError('temporarily unavailable')

        def wait(self, timeout):
            assert timeout == 2.0
            return 0

    process = Process()
    monkeypatch.setattr(wow_lifecycle, '_CURRENT_SESSION_WATCHER', None)

    def launch(*_args, **_kwargs):
        assert wow_lifecycle.stop_current_session_watcher()
        return process

    monkeypatch.setattr(wow_lifecycle.subprocess, 'Popen', launch)
    with pytest.raises(RuntimeError, match='retired'):
        wow_lifecycle.start_wow_sync_watcher(
            executable_path=tmp_path/'ApplicantScout.exe', arguments=(), check_existing=False,
        )
    assert wow_lifecycle.stop_current_session_watcher()
    assert process.attempts == 2


@pytest.mark.parametrize("failure", ["terminate", "kill", "wait"])
def test_failed_owned_helper_stop_can_be_retried(monkeypatch, failure):
    class Process:
        attempts = 0

        def poll(self):
            return None

        def terminate(self):
            self.attempts += 1
            if self.attempts == 1 and failure == 'terminate':
                raise PermissionError('temporarily unavailable')

        def wait(self, timeout):
            assert timeout == 2.0
            if self.attempts == 1:
                raise subprocess.TimeoutExpired("watcher", timeout)
            return 0

        def kill(self):
            if self.attempts == 1 and failure == "kill":
                raise PermissionError("temporarily unavailable")

    process = Process()
    monkeypatch.setattr(wow_lifecycle, '_CURRENT_SESSION_WATCHER', process)
    assert not wow_lifecycle.stop_current_session_watcher()
    with pytest.raises(RuntimeError, match="requires shutdown"):
        wow_lifecycle.start_wow_sync_watcher(check_existing=False)
    assert wow_lifecycle.stop_current_session_watcher()
    assert process.attempts == 2
