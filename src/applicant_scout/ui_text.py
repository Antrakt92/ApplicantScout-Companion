"""Central user-facing copy for overlay text and updater progress.

Phase 1 of the strings audit: plain-function centralization only, no Qt
translation infrastructure yet. Approved terms live in
``docs/COPY-GLOSSARY.md``.
"""

from __future__ import annotations

import math

#: Single missing-data token used across overlay cells and detail text.
MISSING_DATA_TEXT = "—"

#: Approved ellipsis character; never three ASCII periods.
ELLIPSIS = "…"

_UPDATE_PHASE_MESSAGES = {
    "checking": f"Checking update{ELLIPSIS}",
    "verifying": f"Verifying update{ELLIPSIS}",
    "installing": f"Installing update{ELLIPSIS}",
}


def format_age(delta_sec: float) -> str:
    if delta_sec >= 86400.0:
        return MISSING_DATA_TEXT
    if delta_sec >= 3600.0:
        return f"{int(delta_sec // 3600)}h ago"
    if delta_sec >= 60.0:
        return f"{int(delta_sec // 60)}m ago"
    return f"{int(delta_sec)}s ago"


def format_duration(delta_sec: float) -> str:
    if delta_sec >= 86400.0:
        return "24h+"
    if delta_sec >= 3600.0:
        return f"{int(delta_sec // 3600)}h"
    if delta_sec >= 60.0:
        return f"{int(delta_sec // 60)}m"
    return f"{int(delta_sec)}s"


def format_percent(value: object) -> str:
    """Floor a 0-100 percent value with a ``%`` suffix.

    Mirrors the quota-chip pattern: flooring keeps the displayed value in
    the same band as its raw colour, and negatives clamp to zero.
    Unusable values fall back to the missing-data token.
    """
    if isinstance(value, bool):
        return MISSING_DATA_TEXT
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return MISSING_DATA_TEXT
    return f"{int(max(0.0, value))}%"


def update_phase_message(phase: str) -> str:
    """Status text for a non-downloading update phase."""
    return _UPDATE_PHASE_MESSAGES[phase]


def format_update_download(downloaded_bytes: int, total_bytes: int | None) -> str:
    """Downloading-progress text for known or unknown totals."""
    if total_bytes:
        percent = min(100, downloaded_bytes * 100 // total_bytes)
        return f"Downloading update{ELLIPSIS} {format_percent(percent)}"
    megabytes = downloaded_bytes / (1024 * 1024)
    return f"Downloading update{ELLIPSIS} {megabytes:.1f} MB"
