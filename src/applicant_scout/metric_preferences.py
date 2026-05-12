"""User-selected WCL metric scope."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricPreferences:
    """Controls which WCL metrics are fetched and shown.

    Disabled metrics must not be included in WCL GraphQL requests; this is a
    quota-saving preference, not just a display filter.
    """

    mplus: bool = True
    raid_normal: bool = True
    raid_heroic: bool = True
    raid_mythic: bool = True

    @property
    def raid_enabled(self) -> bool:
        return self.raid_normal or self.raid_heroic or self.raid_mythic

    @property
    def any_enabled(self) -> bool:
        return self.mplus or self.raid_enabled

    def covers(self, other: "MetricPreferences") -> bool:
        """True when this fetched scope contains every metric enabled by other."""
        return (
            (self.mplus or not other.mplus)
            and (self.raid_normal or not other.raid_normal)
            and (self.raid_heroic or not other.raid_heroic)
            and (self.raid_mythic or not other.raid_mythic)
        )

    def cache_key(self) -> str:
        parts = [
            "mp1" if self.mplus else "mp0",
            "n1" if self.raid_normal else "n0",
            "h1" if self.raid_heroic else "h0",
            "m1" if self.raid_mythic else "m0",
        ]
        return ".".join(parts)


DEFAULT_METRIC_PREFERENCES = MetricPreferences()
