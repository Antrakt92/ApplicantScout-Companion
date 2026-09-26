"""Pure health/auth chip state for the overlay status row (P2 extraction).

Canonical home of the auth-chip lookup previously living in
``overlay.py`` (``ChipState`` / ``AUTH_CHIP_STATES`` / ``auth_chip_for``).
``overlay.py`` re-exports those names so existing imports
(``tests/test_overlay_fetch_identity.py``, ``__main__``) keep working;
this module is the single source of truth — the table is not duplicated.

``health_chip_state`` is the pure decision half of
``OverlayWindow._refresh_health_label``: same branches, same strings, no Qt.
The window method stays a thin controller that gathers ``self._*`` inputs,
calls this function, and writes the resulting chip.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from . import ui_text
from .wcl import (
    WCL_ERROR_AUTH,
    WCL_ERROR_GRAPHQL,
    WCL_ERROR_HTTP,
    WCL_ERROR_MALFORMED,
    WCL_ERROR_NETWORK,
    WCL_ERROR_RATE_LIMITED,
    WCL_ERROR_SERVER,
)


@dataclass(frozen=True, slots=True)
class ChipState:
    """Plain auth-chip rendering: visible text, QSS state, tooltip detail."""

    text: str
    chip_state: str
    detail: str


_AUTH_CHIP_DEFAULT = ChipState(
    text="Auth —",
    chip_state="neutral",
    detail="Warcraft Logs credentials have not been checked in this session.",
)

# Read-only lookup shared via overlay.AUTH_CHIP_STATES; do not mutate.
AUTH_CHIP_STATES: Mapping[tuple[str, str], ChipState] = {
    ("checking", ""): ChipState(
        text="Auth check",
        chip_state="active",
        detail="Checking the active Warcraft Logs credentials.",
    ),
    ("oauth_ready", ""): ChipState(
        text="Auth ready",
        chip_state="neutral",
        detail=(
            "Warcraft Logs accepted the active credentials. Applicant API "
            "and quota data have not been queried yet."
        ),
    ),
    ("api_ready", ""): ChipState(
        text="Auth ready",
        chip_state="neutral",
        detail="The latest Warcraft Logs applicant API request succeeded.",
    ),
    ("error", WCL_ERROR_AUTH): ChipState(
        text="Auth failed",
        chip_state="critical",
        detail=(
            "Warcraft Logs rejected the active credentials. Test them in Settings."
        ),
    ),
    ("error", WCL_ERROR_NETWORK): ChipState(
        text="Auth offline",
        chip_state="warning",
        detail=(
            "Could not reach Warcraft Logs. Check internet access; displayed "
            "applicant data may be cached."
        ),
    ),
    ("error", WCL_ERROR_SERVER): ChipState(
        text="Auth issue",
        chip_state="warning",
        detail=(
            "Warcraft Logs is temporarily unavailable. Applicant requests "
            "will retry automatically."
        ),
    ),
    ("error", WCL_ERROR_RATE_LIMITED): ChipState(
        text="Auth issue",
        chip_state="warning",
        detail=(
            "Warcraft Logs is temporarily limiting requests. Applicant "
            "requests will retry automatically."
        ),
    ),
    ("error", WCL_ERROR_GRAPHQL): ChipState(
        text="Auth issue",
        chip_state="warning",
        detail=(
            "Warcraft Logs returned an unexpected response. Check the "
            "applicant row and retry."
        ),
    ),
    ("error", WCL_ERROR_HTTP): ChipState(
        text="Auth issue",
        chip_state="warning",
        detail=(
            "Warcraft Logs returned an unexpected response. Check the "
            "applicant row and retry."
        ),
    ),
    ("error", WCL_ERROR_MALFORMED): ChipState(
        text="Auth issue",
        chip_state="warning",
        detail=(
            "Warcraft Logs returned an unexpected response. Check the "
            "applicant row and retry."
        ),
    ),
    ("error", ""): ChipState(
        text="Auth issue",
        chip_state="warning",
        detail=(
            "Warcraft Logs validation failed. Open Settings to test the "
            "active credentials."
        ),
    ),
}


def auth_chip_for(state: str, error_kind: str | None) -> ChipState:
    """Pure auth-chip lookup keyed by connection state and error kind."""
    if state != "error":
        return AUTH_CHIP_STATES.get((state, ""), _AUTH_CHIP_DEFAULT)
    return AUTH_CHIP_STATES.get(("error", error_kind or ""), AUTH_CHIP_STATES[("error", "")])


def auth_chip_state(status: object) -> ChipState:
    """Chip for a WCL connection-status object (``.state``/``.error_kind``).

    Missing attributes fall back to ``"unknown"``/``""``, matching
    ``OverlayWindow._refresh_auth_label`` for legacy clients without the
    status API.
    """
    state = getattr(status, "state", "unknown")
    error_kind = getattr(status, "error_kind", "")
    return auth_chip_for(state, error_kind)


def app_update_message(latest_version: str) -> str:
    """Update chip detail shared by the health label branches."""
    return (
        f"ApplicantScout Companion {latest_version} is available.\n"
        "Open Settings to install the update."
    )


@dataclass(frozen=True, slots=True)
class HealthChipState:
    """Pure health-chip rendering: text, QSS state, tooltip, accessible text."""

    text: str
    chip_state: str
    tooltip: str
    accessible: str


def health_chip_state(
    *,
    restored_pending: bool,
    restored_saved_at: float | None,
    restored_deadline: float | None,
    wire_rejects: int,
    wire_reject_threshold: int,
    wire_reject_message: str,
    wire_reject_active: bool,
    failed_at: float | None,
    last_decode_time: float | None,
    failed_path: str,
    failed_reason: str,
    addon_warning: str | None,
    app_update_version: str | None,
    applicants_unavailable: bool,
    roster_unavailable: bool,
    lfg_unavailable: bool,
    now: float,
) -> HealthChipState:
    """Pure decision half of ``OverlayWindow._refresh_health_label``.

    Branch order and strings are byte-equivalent to the window method:
    restored snapshot → wire-version reject streak → decode failure →
    addon/app updates → partial surfaces → no decode yet → age text.
    Elapsed age stays neutral (idle listings legitimately idle); only
    explicit failure/partial/update states escalate colour.
    """
    if restored_pending:
        age = (
            ui_text.format_duration(max(0.0, now - restored_saved_at))
            if restored_saved_at
            else "?"
        )
        wait = (
            ui_text.format_duration(max(0.0, restored_deadline - now))
            if restored_deadline
            else "?"
        )
        detail = (
            "Restored a recent live snapshot while waiting for a fresh QR.\n"
            f"Snapshot age: {age}; clears in {wait} if no fresh QR arrives."
        )
        return HealthChipState(
            text="Shot restored",
            chip_state="active",
            tooltip=detail,
            accessible=detail,
        )
    if (
        wire_rejects >= wire_reject_threshold
        and failed_at is not None
        and (last_decode_time is None or failed_at >= last_decode_time)
        and wire_reject_active
    ):
        # Newer addon format: the version block never decodes, so
        # addon_version_warning stays silent. Replace the raw reject with
        # actionable guidance once the streak rules out a corrupt frame.
        delta = max(0.0, now - failed_at)
        detail = (
            f"{wire_reject_message}\n"
            f"{failed_path}\n"
            f"{failed_reason}\n"
            f"{ui_text.format_age(delta)}"
        )
        return HealthChipState(
            text="Companion update",
            chip_state="warning",
            tooltip=detail,
            accessible=detail,
        )
    if failed_at is not None and (
        last_decode_time is None or failed_at >= last_decode_time
    ):
        delta = max(0.0, now - failed_at)
        detail = (
            f"{failed_path}\n"
            f"{failed_reason}\n"
            f"{ui_text.format_age(delta)}"
        )
        return HealthChipState(
            text="Shot failed",
            chip_state="critical",
            tooltip=detail,
            accessible=detail,
        )
    if addon_warning:
        detail = addon_warning
        if app_update_version:
            detail += "\n" + app_update_message(app_update_version)
        return HealthChipState(
            text="Addon update",
            chip_state="warning",
            tooltip=detail,
            accessible=detail,
        )
    if app_update_version:
        detail = app_update_message(app_update_version)
        return HealthChipState(
            text="App update",
            chip_state="warning",
            tooltip=detail,
            accessible=detail,
        )
    last = last_decode_time
    if (applicants_unavailable or roster_unavailable) and last is not None:
        delta = max(0.0, now - last)
        stale_surfaces: list[str] = []
        if lfg_unavailable:
            stale_surfaces.append("Group Finder listing and Applicants")
        elif applicants_unavailable:
            stale_surfaces.append("Applicants")
        if roster_unavailable:
            stale_surfaces.append("Party")
        detail = (
            "The latest valid QR could not provide complete "
            + " and ".join(stale_surfaces)
            + " data; last known state is retained.\n"
            + ui_text.format_age(delta)
        )
        return HealthChipState(
            text="Shot partial",
            chip_state="warning",
            tooltip=detail,
            accessible=detail,
        )
    if last is None:
        return HealthChipState(
            text="Shot —",
            chip_state="neutral",
            tooltip="",
            accessible="No screenshot has been decoded yet.",
        )
    delta = max(0.0, now - last)
    age = ui_text.format_age(delta)
    return HealthChipState(
        text=f"Shot {age}",
        chip_state="neutral",
        tooltip="",
        accessible=f"Last screenshot decoded {age}.",
    )
