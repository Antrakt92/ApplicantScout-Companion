"""Event-driven game-foreground detection via Win32 WinEventHook.

Polling the foreground window cannot beat its own interval: after an
alt-tab the return-to-game restore waits for the next slow poll tick
(``GAME_FOREGROUND_POLL_SLOW_MS`` worst case). Windows offers
``EVENT_SYSTEM_FOREGROUND`` notifications instead, delivered through
``SetWinEventHook`` out-of-context on a thread with a message loop.

This module is intentionally QObject-free (no Qt dependency) so it stays
unit-testable: the hook thread invokes a plain ``callback(game_foreground)``
and the overlay side marshals it into Qt via signal/slot. The 3000ms poll
in ``overlay.py`` stays as a fallback — hook events can drop under load.

Exe identification reuses :mod:`applicant_scout.wow_lifecycle`
(``process_name_for_pid`` + ``WOW_PROCESS_NAMES`` — the same predicate
``is_wow_foreground`` is built on) instead of duplicating it.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import cast

_log = logging.getLogger("applicant_scout.foreground_hook")

EVENT_SYSTEM_FOREGROUND = 0x0003
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002
_WM_QUIT = 0x0012
_START_TIMEOUT_SECONDS = 5.0
_STOP_TIMEOUT_SECONDS = 2.0


class ForegroundHookError(OSError):
    """Raised once from start() when the hook cannot be installed."""


def default_is_game_window(hwnd: int) -> bool:
    """Return True when ``hwnd`` belongs to a Retail WoW process.

    Same exe-identification predicate as
    :func:`applicant_scout.wow_lifecycle.is_wow_foreground`: HWND →
    process id → :func:`process_name_for_pid` → ``WOW_PROCESS_NAMES``.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
        from . import wow_lifecycle

        name = wow_lifecycle.process_name_for_pid(int(pid.value))
    except (AttributeError, OSError, ValueError):
        return False
    if not name:
        return False
    from . import wow_lifecycle as _lifecycle

    return any(
        name.casefold() == process_name.casefold()
        for process_name in _lifecycle.WOW_PROCESS_NAMES
    )


class ForegroundHook:
    """Out-of-context EVENT_SYSTEM_FOREGROUND watcher on a daemon thread.

    Usage::

        hook = ForegroundHook()
        try:
            hook.start(overlay_callback)  # raises ForegroundHookError
        except ForegroundHookError:
            ... keep polling ...

    The callback runs on the hook thread and must be thread-safe (the
    overlay emits a Qt signal from it). Any ctypes failure surfaces once
    from :meth:`start` — this class never retry-loops and never crashes
    the caller from the hook thread (event-time errors are logged).
    """

    def __init__(
        self,
        *,
        is_game_window: Callable[[int], bool] | None = None,
    ) -> None:
        self._is_game_window = is_game_window or default_is_game_window
        self._callback: Callable[[bool], None] | None = None
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._hook_handle: int | None = None
        self._proc_ref: object = None
        self._install_error: BaseException | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        """True after a successful start() until stop()."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, callback: Callable[[bool], None]) -> None:
        """Install the hook and boot the message-loop thread.

        Raises :class:`ForegroundHookError` synchronously (once) when the
        platform is unsupported or the install fails, so the caller can
        fall back to polling unchanged.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ForegroundHookError("ForegroundHook is already started.")
            if sys.platform != "win32":
                raise ForegroundHookError(
                    "ForegroundHook needs Windows (SetWinEventHook)."
                )
            self._callback = callback
            self._install_error = None
            self._thread_id = None
            self._hook_handle = None
            ready = threading.Event()
            thread = threading.Thread(
                target=self._thread_main,
                args=(ready,),
                name="applicant-scout-foreground-hook",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        if not ready.wait(timeout=_START_TIMEOUT_SECONDS):
            raise ForegroundHookError("ForegroundHook install timed out.")
        error = self._install_error
        if error is not None:
            self._join_thread(timeout=_STOP_TIMEOUT_SECONDS)
            if isinstance(error, ForegroundHookError):
                raise error
            raise ForegroundHookError(f"SetWinEventHook failed: {error}.") from error
        if self._hook_handle is None:
            self._join_thread(timeout=_STOP_TIMEOUT_SECONDS)
            raise ForegroundHookError("SetWinEventHook returned no handle.")

    def stop(self, *, timeout: float = _STOP_TIMEOUT_SECONDS) -> None:
        """Unhook and join the message-loop thread (bounded wait)."""
        with self._lock:
            thread = self._thread
            thread_id = self._thread_id
            self._thread = None
            self._thread_id = None
        if thread is None:
            return
        if thread_id is not None and thread.is_alive():
            self._post_quit(thread_id)
        thread.join(timeout=timeout)
        if thread.is_alive():
            _log.warning("Foreground hook thread did not exit within %.1fs.", timeout)
        with self._lock:
            self._hook_handle = None
            self._callback = None

    # ── thread body ──────────────────────────────────────────────

    def _thread_main(self, ready: threading.Event) -> None:
        handle: int | None = None
        try:
            proc = self._make_event_proc()
            self._proc_ref = proc  # keep the WINFUNCTYPE alive
            handle = self._install_hook(proc)
        except Exception as exc:  # noqa: BLE001 — surfacing via start()
            self._install_error = exc
            ready.set()
            return
        if not handle:
            self._install_error = OSError("SetWinEventHook returned NULL.")
            ready.set()
            return
        with self._lock:
            self._hook_handle = handle
            try:
                self._thread_id = self._current_thread_id()
            except Exception:  # noqa: BLE001 — stop() still joins the thread
                _log.warning("Could not read foreground hook thread id.", exc_info=True)
        ready.set()
        try:
            self._run_message_loop()
        finally:
            try:
                self._uninstall_hook(handle)
            except Exception:  # noqa: BLE001 — teardown boundary
                _log.warning("UnhookWinEvent failed.", exc_info=True)

    def _handle_win_event(self, hwnd: int) -> None:
        """Resolve one foreground HWND and invoke the callback (hook thread)."""
        callback = self._callback
        if callback is None:
            return
        try:
            game_foreground = bool(self._is_game_window(hwnd))
        except Exception:  # noqa: BLE001 — never crash from the hook thread
            _log.warning("Game-window check failed.", exc_info=True)
            return
        try:
            callback(game_foreground)
        except Exception:  # noqa: BLE001 — never crash from the hook thread
            _log.warning("Foreground hook callback failed.", exc_info=True)

    # ── ctypes seams (monkeypatched in tests — no real hooks there) ──

    def _make_event_proc(self) -> Callable[..., None]:
        """Build the WinEventProc WINFUNCTYPE bound to _handle_win_event."""
        wineventproc = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            wintypes.LONG,
            wintypes.LONG,
            wintypes.DWORD,
            wintypes.DWORD,
        )

        def _proc(
            _hook: int,
            event: int,
            hwnd: int,
            _id_object: int,
            _id_child: int,
            _event_thread: int,
            _event_time: int,
        ) -> None:
            if event != EVENT_SYSTEM_FOREGROUND:
                return
            self._handle_win_event(hwnd)

        return cast("Callable[..., None]", wineventproc(_proc))

    def _install_hook(self, proc: Callable[..., None]) -> int | None:
        """Install SetWinEventHook(EVENT_SYSTEM_FOREGROUND, out-of-context)."""
        user32 = ctypes.windll.user32
        user32.SetWinEventHook.argtypes = (
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        user32.SetWinEventHook.restype = wintypes.HANDLE
        handle = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_FOREGROUND,
            None,
            proc,
            0,
            0,
            _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS,
        )
        return int(handle) if handle else None

    def _uninstall_hook(self, handle: int) -> None:
        ctypes.windll.user32.UnhookWinEvent(handle)

    def _run_message_loop(self) -> None:
        """Pump GetMessageW until WM_QUIT (posted by stop())."""
        get_message = ctypes.windll.user32.GetMessageW
        translate = ctypes.windll.user32.TranslateMessage
        dispatch = ctypes.windll.user32.DispatchMessageW
        msg = wintypes.MSG()
        while True:
            result = get_message(ctypes.byref(msg), None, 0, 0)
            if result == 0:  # WM_QUIT
                return
            if result == -1:  # error — never spin
                raise OSError("GetMessageW failed in foreground hook loop.")
            translate(ctypes.byref(msg))
            dispatch(ctypes.byref(msg))

    def _post_quit(self, thread_id: int) -> None:
        try:
            ctypes.windll.user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
        except (AttributeError, OSError, ValueError):
            _log.warning("Could not wake the foreground hook loop.", exc_info=True)

    @staticmethod
    def _current_thread_id() -> int:
        return int(ctypes.windll.kernel32.GetCurrentThreadId())

    def _join_thread(self, *, timeout: float) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
