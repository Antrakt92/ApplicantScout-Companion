"""Throttled warnings for hot observer/scheduler paths.

First occurrence logs a warning (optionally with traceback); repeats within
``interval_s`` are counted and summarized on the next emission instead of
spamming the log on every screenshot or flush event.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable


_log = logging.getLogger("applicant_scout.log_throttle")

_state_lock = threading.Lock()
_last_emit: dict[str, float] = {}
_suppressed: dict[str, int] = {}


def _default_monotonic() -> float:
    return time.monotonic()


_monotonic: Callable[[], float] = _default_monotonic


def reset_for_tests() -> None:
    """Clear throttle state (tests only)."""
    with _state_lock:
        _last_emit.clear()
        _suppressed.clear()


def suppressed_count(key: str) -> int:
    """How many repeats are currently buffered for ``key``."""
    with _state_lock:
        return _suppressed.get(key, 0)


def warn_once_per_interval(
    key: str,
    msg: str,
    interval_s: float = 60,
    *,
    logger: logging.Logger | None = None,
    exc_info: bool = False,
    monotonic: Callable[[], float] | None = None,
) -> bool:
    """Log ``msg`` at WARNING at most once per ``interval_s`` for ``key``.

    Returns True when the message was emitted, False when it was buffered
    as a repeat. When a buffered repeat window expires, the next emission
    appends ``(repeated N times)`` so volume stays visible without
    per-event spam. The first emission may carry ``exc_info`` traceback;
    repeats never do.
    """
    target = logger if logger is not None else _log
    now_fn = monotonic if monotonic is not None else _monotonic
    try:
        now = now_fn()
    except Exception:  # noqa: BLE001 - clock must not break callers
        now = 0.0
    try:
        interval = float(interval_s)
    except (TypeError, ValueError):
        interval = 60.0
    if interval < 0:
        interval = 0.0
    with _state_lock:
        last = _last_emit.get(key)
        if last is None or (now - last) >= interval:
            repeats = _suppressed.pop(key, 0)
            _last_emit[key] = now
            emit = True
        else:
            _suppressed[key] = _suppressed.get(key, 0) + 1
            emit = False
    if not emit:
        return False
    if repeats:
        target.warning("%s (repeated %d times)", msg, repeats, exc_info=False)
    else:
        target.warning("%s", msg, exc_info=exc_info)
    return True
