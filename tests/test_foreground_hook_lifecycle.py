"""Foreground hook teardown races, using synthetic threads only."""
from __future__ import annotations

import threading

import pytest

from applicant_scout import foreground_hook as hook_mod
from applicant_scout.foreground_hook import ForegroundHook, ForegroundHookError


def test_install_timeout_cancels_late_hook(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    uninstalled = threading.Event()
    loops = []
    monkeypatch.setattr(hook_mod.sys, "platform", "win32")
    monkeypatch.setattr(hook_mod, "_START_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(hook_mod, "_STOP_TIMEOUT_SECONDS", 0.01)
    hook = ForegroundHook()
    monkeypatch.setattr(hook, "_make_event_proc", lambda: lambda: None)

    def install(_proc):
        entered.set()
        release.wait(2)
        return 123

    monkeypatch.setattr(hook, "_install_hook", install)
    monkeypatch.setattr(hook, "_current_thread_id", lambda: 456)
    monkeypatch.setattr(hook, "_uninstall_hook", lambda _handle: uninstalled.set())
    monkeypatch.setattr(hook, "_run_message_loop", lambda: loops.append(True))
    try:
        with pytest.raises(ForegroundHookError, match="timed out"):
            hook.start(lambda _active: None)
        assert entered.is_set()
        release.set()
        assert uninstalled.wait(2)
        assert loops == []
        assert hook._callback is None
    finally:
        release.set()
        hook.stop()


def test_completed_stop_allows_fresh_hook_session(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    received = []
    monkeypatch.setattr(hook_mod.sys, "platform", "win32")
    hook = ForegroundHook(is_game_window=lambda _hwnd: True)
    monkeypatch.setattr(hook, "_make_event_proc", lambda: lambda: None)
    monkeypatch.setattr(hook, "_install_hook", lambda _proc: 123)
    monkeypatch.setattr(hook, "_current_thread_id", lambda: 456)
    monkeypatch.setattr(hook, "_uninstall_hook", lambda _handle: None)
    monkeypatch.setattr(hook, "_post_quit", lambda _thread_id: release.set())

    def loop():
        entered.set()
        release.wait(2)

    monkeypatch.setattr(hook, "_run_message_loop", loop)
    try:
        for _ in range(2):
            entered.clear()
            release.clear()
            hook.start(received.append)
            assert entered.wait(2)
            hook._handle_win_event(1)
            hook.stop()
            assert not hook.running
            hook._handle_win_event(1)
        assert received == [True, True]
    finally:
        release.set()
        hook.stop()


def test_stop_timeout_keeps_thread_owned_and_blocks_restart(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(hook_mod.sys, "platform", "win32")
    hook = ForegroundHook()
    monkeypatch.setattr(hook, "_make_event_proc", lambda: lambda: None)
    monkeypatch.setattr(hook, "_install_hook", lambda _proc: 123)
    monkeypatch.setattr(hook, "_current_thread_id", lambda: 456)
    monkeypatch.setattr(hook, "_uninstall_hook", lambda _handle: None)
    monkeypatch.setattr(hook, "_post_quit", lambda _thread_id: None)

    def loop():
        entered.set()
        release.wait(2)

    monkeypatch.setattr(hook, "_run_message_loop", loop)
    try:
        hook.start(lambda _active: None)
        assert entered.wait(2)
        hook.stop(timeout=0.01)
        assert hook.running
        with pytest.raises(ForegroundHookError, match="already started"):
            hook.start(lambda _active: None)
        assert hook._callback is None
    finally:
        release.set()
        hook.stop()


def test_thread_start_failure_uses_polling_fallback_exception(monkeypatch):
    monkeypatch.setattr(hook_mod.sys, "platform", "win32")

    def fail_start(_thread):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    hook = ForegroundHook()
    with pytest.raises(ForegroundHookError, match="could not start"):
        hook.start(lambda _active: None)
    assert not hook.running
    assert hook._callback is None
    hook.stop()


def test_stop_during_window_probe_suppresses_callback():
    received = []
    hook = ForegroundHook()

    def probe(_hwnd):
        hook.stop()
        return True

    hook._is_game_window = probe
    hook._callback = received.append
    hook._handle_win_event(1)
    assert received == []


def test_unhook_preserves_pointer_sized_native_handle(monkeypatch):
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    if ctypes.sizeof(ctypes.c_void_p) < 8:
        pytest.skip("64-bit native handle contract")
    seen = []
    receiver = ctypes.CFUNCTYPE(wintypes.BOOL, wintypes.HANDLE)(
        lambda handle: seen.append(handle) or 1
    )
    # Match a windll function before its argument types have been declared.
    native = ctypes.CFUNCTYPE(wintypes.BOOL)(
        ctypes.cast(receiver, ctypes.c_void_p).value
    )
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(UnhookWinEvent=native)),
        raising=False,
    )
    handle = 0x1234567887654321
    ForegroundHook()._uninstall_hook(handle)
    assert seen == [handle]
