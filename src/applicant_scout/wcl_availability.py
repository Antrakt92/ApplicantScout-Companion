"""Requested WCL evidence availability shared by transport and row models."""

from collections.abc import Mapping

from .metric_preferences import MetricPreferences


METRIC_SCOPES = ("mplus", "raid_normal", "raid_heroic", "raid_mythic")
RAID_DETAIL_SCOPES = ("N", "H", "M")
PRIVATE_RANKINGS_USER_MESSAGE = "Rankings are private on Warcraft Logs"
UNUSABLE_AVAILABILITY_STATES = frozenset({
    "restricted", "disabled", "unavailable", "error", "empty",
})
_AVAILABILITY_STATES = frozenset({"available", "partial"}) | UNUSABLE_AVAILABILITY_STATES


def normalize_availability(
    value: object, *, scopes: tuple[str, ...] = METRIC_SCOPES,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        scope: status for scope in scopes
        if isinstance(status := value.get(scope), str) and status in _AVAILABILITY_STATES
    }


def project_availability(
    availability: Mapping[str, str], metric_preferences: MetricPreferences,
) -> dict[str, str]:
    return {
        scope: status for scope, status in normalize_availability(availability).items()
        if getattr(metric_preferences, scope)
    }


def all_selected_restricted(
    availability: Mapping[str, str], metric_preferences: MetricPreferences,
) -> bool:
    enabled = [scope for scope in METRIC_SCOPES if getattr(metric_preferences, scope)]
    return bool(enabled) and all(availability.get(scope) == "restricted" for scope in enabled)


class RaidBossDetails(dict[str, list[dict[str, object]]]):
    """Detail rows with provenance while retaining the mapping consumer contract."""

    def __init__(
        self,
        rows: Mapping[str, list[dict[str, object]]] | None = None,
        *,
        availability: Mapping[str, str] | None = None,
        not_found: bool = False,
        publication_epochs: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(rows or {})
        self.availability = normalize_availability(availability, scopes=RAID_DETAIL_SCOPES)
        self.not_found = not_found
        self.publication_epochs = {
            difficulty: epoch for difficulty in RAID_DETAIL_SCOPES
            if isinstance(epoch := (publication_epochs or {}).get(difficulty), int)
            and not isinstance(epoch, bool) and epoch >= 0
        }
