"""Translate successful UI activity into privacy-minimal usage milestones."""

from __future__ import annotations

from collections.abc import Iterable
import time
import math
from datetime import datetime, timezone
from typing import Protocol


class UsageRecorder(Protocol):
    @property
    def consent_enabled(self) -> bool: ...

    def record(self, event: str) -> bool: ...


class UsageActivity:
    """Observe live activity only; never enumerate or serialize player identities."""

    def __init__(self, recorder: UsageRecorder) -> None:
        self.recorder = recorder
        self._fresh_surfaces: dict[str, float] = {}
        self._consent_epoch = time.time()
        self._active_day = ""
        self._wcl_day = ""

    def reset(self) -> None:
        self._fresh_surfaces.clear()
        self._consent_epoch = time.time()
        self._active_day = ""
        self._wcl_day = ""

    def snapshot_applied(self, snapshot: object) -> None:
        if not self.recorder.consent_enabled:
            self.reset()
            return
        source = getattr(snapshot, "source", None)
        mtime_ns = getattr(source, "mtime_ns", 0)
        now = time.time()
        # Cached/replayed screenshots and terminal clears aren't current usage.
        if (
            not isinstance(mtime_ns, int)
            or not 0 < mtime_ns < 2**63
            or mtime_ns / 1_000_000_000 < self._consent_epoch
            or not 0 <= now - mtime_ns / 1_000_000_000 <= 120
            or getattr(snapshot, "terminal_clear", False)
        ):
            self._fresh_surfaces.clear()
            return
        applicants = (
            not getattr(snapshot, "lfg_unavailable", False)
            and not getattr(snapshot, "applicants_unavailable", False)
            and bool(getattr(snapshot, "applicants", ()))
        )
        roster = (
            not getattr(snapshot, "roster_unavailable", False)
            and bool(getattr(snapshot, "roster", ()))
        )
        if applicants or roster:
            self.recorder.record("addon_received")
            self._fresh_surfaces = {
                surface: time.monotonic() + 120
                for surface, present in (("applicants", applicants), ("party", roster))
                if present
            }
            self._active_day = datetime.now(timezone.utc).date().isoformat()
        else:
            self._fresh_surfaces.clear()

    def rows_rendered(
        self, rows: Iterable[object], *, visible: bool, surface: str = "applicants"
    ) -> None:
        if not self.recorder.consent_enabled:
            self.reset()
            return
        today = datetime.now(timezone.utc).date().isoformat()
        if (
            not visible or time.monotonic() > self._fresh_surfaces.get(surface, 0)
            or self._active_day != today or self._wcl_day == today
        ):
            return
        if any(self._has_wcl_result(row) for row in rows):
            self.recorder.record("wcl_result")
            self._wcl_day = today

    @staticmethod
    def _has_wcl_result(row: object) -> bool:
        preferences = getattr(row, "wcl_metric_preferences", None)
        if (
            getattr(row, "fetch_status", "") != "ready"
            or preferences is None
            or not preferences.any_enabled
        ):
            return False
        return any(
            isinstance(value, (float, int)) and not isinstance(value, bool)
            and 0 <= value <= 100 and math.isfinite(value)
            for value in (
                getattr(row, key, None)
                for scope, metric in (
                    ("raid_normal", "raid_normal"),
                    ("raid_heroic", "raid_heroic"),
                    ("raid_mythic", "raid_mythic"),
                    ("mplus", "mplus_dps"),
                )
                if getattr(preferences, scope, False)
                # The overlay displays best/median and uses DPS for every M+ role.
                for key in (metric, f"{metric}_median")
            )
        )
