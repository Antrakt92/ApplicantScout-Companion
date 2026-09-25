"""Pure fetch-completion reconciliation for the overlay (P2 extraction).

Decision half of ``OverlayWindow._on_fetch_done``: given the fetched
identity, the applicant's current identity, and the delivered ranks, decide
what the window must do — without touching Qt, timers, or the thread pool.
The window method stays a thin controller: it owns waiter fan-out,
in-flight bookkeeping, quota refresh, and Applicant mutation, and branches
on the returned :class:`FetchReconciliation`.

Identity comparison is duck-typed on the ``_FetchIdentity`` fields
(applicant/row-source/charname/server/region/spec/role/generations) so this
module never imports ``overlay.py`` (which owns the Qt window). The field
list mirrors ``overlay._same_fetch_target_except_preferences`` exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .wcl import WCL_ERROR_RESTRICTED

#: Terminal fetch states that keep their row content when a stale completion
#: for another target arrives.
_TERMINAL_FETCH_STATUSES: frozenset[str] = frozenset(
    {"error", "not_found", "restricted"}
)

FetchAction = Literal[
    "no_row",
    "metrics_disabled",
    "missing_realm",
    "current_prefs_disabled",
    "already_current",
    "stale_terminal",
    "stale_relaunch",
    "apply_not_found",
    "apply_restricted",
    "apply_error",
    "apply_ready",
]


@dataclass(frozen=True, slots=True)
class FetchReconciliation:
    """What ``_on_fetch_done`` must do for one fetched identity."""

    action: FetchAction


def _same_fetch_target(left: object, right: object) -> bool:
    """Target equality ignoring metric preferences (byte-equivalent copy)."""
    return (
        getattr(left, "applicant_id", None) == getattr(right, "applicant_id", None)
        and getattr(left, "row_source", None) == getattr(right, "row_source", None)
        and getattr(left, "charname_key", None) == getattr(right, "charname_key", None)
        and getattr(left, "server_slug", None) == getattr(right, "server_slug", None)
        and getattr(left, "region", None) == getattr(right, "region", None)
        and getattr(left, "spec_id", None) == getattr(right, "spec_id", None)
        and getattr(left, "metric_role", None) == getattr(right, "metric_role", None)
        and getattr(left, "runtime_generation", None)
        == getattr(right, "runtime_generation", None)
        and getattr(left, "listing_session_generation", None)
        == getattr(right, "listing_session_generation", None)
    )


def reconcile_fetch_done(
    *,
    applicant: object | None,
    fetched_identity: object,
    current: tuple[object, str] | None,
    was_current: bool,
    ranks: object,
    metrics_enabled: bool,
) -> FetchReconciliation:
    """Decide the outcome of one fetch completion.

    ``applicant`` is the row for the fetched identity (or ``None`` when the
    row is gone); ``current`` is the freshly computed
    ``(identity, charname)`` for that applicant (or ``None`` for missing
    realm); ``ranks`` carries ``not_found``/``error``/``error_kind``.
    Branch order matches ``OverlayWindow._on_fetch_done``.
    """
    if applicant is None:
        return FetchReconciliation(action="no_row")
    if not metrics_enabled:
        return FetchReconciliation(action="metrics_disabled")
    if current is None:
        return FetchReconciliation(action="missing_realm")
    current_identity, _charname = current
    current_prefs = getattr(current_identity, "metric_preferences", None)
    if current_prefs is not None and not getattr(
        current_prefs, "any_enabled", True
    ):
        return FetchReconciliation(action="current_prefs_disabled")
    fetched_prefs = getattr(fetched_identity, "metric_preferences", None)
    covers_current = bool(
        current_prefs is not None
        and fetched_prefs is not None
        and fetched_prefs.covers(current_prefs)
    )
    same_target = _same_fetch_target(current_identity, fetched_identity)
    if (
        not was_current
        and getattr(applicant, "fetch_status", None) == "ready"
        and current_prefs is not None
        and bool(getattr(applicant, "wcl_data_covers")(current_prefs))
        and same_target
        and covers_current
    ):
        return FetchReconciliation(action="already_current")
    if not same_target or not covers_current:
        if getattr(applicant, "fetch_status", None) in _TERMINAL_FETCH_STATUSES:
            return FetchReconciliation(action="stale_terminal")
        return FetchReconciliation(action="stale_relaunch")
    if bool(getattr(ranks, "not_found", False)):
        return FetchReconciliation(action="apply_not_found")
    if getattr(ranks, "error_kind", "") == WCL_ERROR_RESTRICTED:
        return FetchReconciliation(action="apply_restricted")
    if getattr(ranks, "error", ""):
        return FetchReconciliation(action="apply_error")
    return FetchReconciliation(action="apply_ready")
