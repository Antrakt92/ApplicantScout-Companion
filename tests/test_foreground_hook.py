"""Event-driven game-foreground detection: hook mapping + overlay wiring.

Never installs a real WinEventHook here: ctypes seams are monkeypatched
(or bypassed via _handle_win_event) and the overlay's ForegroundHook
class is replaced with a fake before any OverlayWindow is built.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import applicant_scout.overlay as overlay_mod
from applicant_scout import foreground_hook as hook_mod
from applicant_scout.foreground_hook import ForegroundHook, ForegroundHookError
from applicant_scout.overlay import OverlayWindow
from applicant_scout.state import AppState
from applicant_scout.wcl import CharacterCache, WCLAuth, WCLClient

GAME_HWND = 0x1234
OTHER_HWND = 0x5678


def test_handle_win_event_maps_hwnd_to_game_flag() -> None:
    received: list[bool] = []
    hook = ForegroundHook(is_game_window=lambda hwnd: hwnd == GAME_HWND)
    hook._callback = received.append

    hook._handle_win_event(GAME_HWND)
    hook._handle_win_event(OTHER_HWND)

    assert received == [True, False]


def test_handle_win_event_predicate_failure_never_calls_back() -> None:
    def _boom(_hwnd: int) -> bool:
        raise RuntimeError("probe down")

    received: list[bool] = []
    hook = ForegroundHook(is_game_window=_boom)
    hook._callback = received.append

    hook._handle_win_event(GAME_HWND)

    assert received == []


@pytest.mark.skipif(sys.platform != "win32", reason="WINFUNCTYPE is Windows-only")
def test_win_event_proc_ignores_non_foreground_events() -> None:
    received: list[bool] = []
    hook = ForegroundHook(is_game_window=lambda _hwnd: True)
    hook._callback = received.append
    proc = hook._make_event_proc()

    proc(None, hook_mod.EVENT_SYSTEM_FOREGROUND, GAME_HWND, 0, 0, 0, 0)
    proc(None, 0x0001, GAME_HWND, 0, 0, 0, 0)  # some other WinEvent, ignored

    assert received == [True]


def test_start_install_failure_raises_once_and_never_calls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_proc: Callable[..., None]) -> int | None:
        raise OSError("SetWinEventHook unavailable")

    monkeypatch.setattr(ForegroundHook, "_install_hook", _boom)
    received: list[bool] = []
    hook = ForegroundHook(is_game_window=lambda _hwnd: True)

    with pytest.raises(ForegroundHookError):
        hook.start(received.append)

    assert received == []
    assert not hook.running


class _FakeHook:
    """Stand-in for ForegroundHook: records start/stop, never touches ctypes."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.callback: Callable[[bool], None] | None = None
        self.started = False
        self.stopped = False

    def start(self, callback: Callable[[bool], None]) -> None:
        self.started = True
        self.callback = callback

    def stop(self, **_kwargs: object) -> None:
        self.stopped = True


def _make_window(qtbot, tmp_path, monkeypatch, hook_cls):
    config_dir = Path(str(tmp_path))
    monkeypatch.setattr(overlay_mod, "ForegroundHook", hook_cls)
    auth = WCLAuth("client", "secret", config_dir)
    client = WCLClient(auth)
    cache = CharacterCache(config_dir)
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        config_dir,
        game_foreground_probe=lambda: True,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    return window, client


def test_hook_starts_next_to_timer_and_callback_reaches_sync(
    qtbot, tmp_path, monkeypatch
) -> None:
    created: list[_FakeHook] = []

    class _RecordingHook(_FakeHook):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            super().__init__()
            created.append(self)

    window, client = _make_window(qtbot, tmp_path, monkeypatch, _RecordingHook)
    try:
        assert len(created) == 1
        fake = created[0]
        assert fake.started
        assert window._foreground_hook is fake
        # Slow poll stays as fallback alongside the hook.
        assert window._foreground_timer.isActive()

        calls: list[bool] = []
        original = window._sync_game_foreground_visibility

        def _counting_sync() -> None:
            calls.append(True)
            original()

        monkeypatch.setattr(window, "_sync_game_foreground_visibility", _counting_sync)
        callback = cast("Callable[[bool], None]", fake.callback)
        assert callback is not None
        callback(True)
        qtbot.waitUntil(lambda: bool(calls), timeout=1000)
    finally:
        client.close()


def test_hook_install_failure_keeps_poll_behavior(qtbot, tmp_path, monkeypatch) -> None:
    class _FailingHook(_FakeHook):
        def start(self, _callback: Callable[[bool], None]) -> None:
            raise ForegroundHookError("SetWinEventHook unavailable")

    window, client = _make_window(qtbot, tmp_path, monkeypatch, _FailingHook)
    try:
        assert window._foreground_hook is None
        assert window._foreground_timer.isActive()
    finally:
        client.close()


def test_hook_stops_with_terminal_shutdown(qtbot, tmp_path, monkeypatch) -> None:
    created: list[_FakeHook] = []

    class _RecordingHook(_FakeHook):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            super().__init__()
            created.append(self)

    window, client = _make_window(qtbot, tmp_path, monkeypatch, _RecordingHook)
    try:
        assert len(created) == 1
        assert window.shutdown_fetches()
        assert created[0].stopped
        assert window._foreground_hook is None
    finally:
        client.close()


def test_hook_loss_event_reaches_sync_promptly(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Focus LOSS must reach the sync path just like gain.

    The hook maps HWND→bool for both directions; the overlay slot must not
    drop the False half (the show path is instant via hook — the hide path
    must be too, subject only to the short anti-flicker grace in sync).
    """
    created: list[_FakeHook] = []

    class _RecordingHook(_FakeHook):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            super().__init__()
            created.append(self)

    window, client = _make_window(qtbot, tmp_path, monkeypatch, _RecordingHook)
    try:
        fake = created[0]
        calls: list[bool] = []
        original = window._sync_game_foreground_visibility

        def _counting_sync() -> None:
            calls.append(True)
            original()

        monkeypatch.setattr(window, "_sync_game_foreground_visibility", _counting_sync)
        callback = cast("Callable[[bool], None]", fake.callback)
        assert callback is not None
        callback(False)
        qtbot.waitUntil(lambda: len(calls) >= 1, timeout=1000)
        callback(True)
        qtbot.waitUntil(lambda: len(calls) >= 2, timeout=1000)
    finally:
        client.close()
