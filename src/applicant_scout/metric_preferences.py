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

    @classmethod
    def from_cache_key(cls, raw: str) -> "MetricPreferences | None":
        parts = raw.split(".")
        if len(parts) != 4:
            return None
        expected = (
            ("mp0", "mp1"),
            ("n0", "n1"),
            ("h0", "h1"),
            ("m0", "m1"),
        )
        if any(
            part not in allowed for part, allowed in zip(parts, expected, strict=True)
        ):
            return None
        return cls(
            mplus=parts[0] == "mp1",
            raid_normal=parts[1] == "n1",
            raid_heroic=parts[2] == "h1",
            raid_mythic=parts[3] == "m1",
        )


DEFAULT_METRIC_PREFERENCES = MetricPreferences(
    mplus=True,
    raid_normal=False,
    raid_heroic=False,
    raid_mythic=False,
)


def effective_wcl_preferences_for_spec(
    spec_id: int,
    metric_preferences: MetricPreferences,
) -> MetricPreferences:
    """Return the WCL scope that is useful for the current spec snapshot."""
    if spec_id > 0 or not metric_preferences.mplus:
        return metric_preferences
    return MetricPreferences(
        mplus=False,
        raid_normal=metric_preferences.raid_normal,
        raid_heroic=metric_preferences.raid_heroic,
        raid_mythic=metric_preferences.raid_mythic,
    )
