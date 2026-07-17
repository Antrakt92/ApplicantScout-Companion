"""Explicit live check for shipped Warcraft Logs zone and encounter constants.

This performs one authenticated WCL GraphQL request and therefore spends real
API quota. The command refuses to contact WCL unless the operator supplies the
confirmation flag, reports the post-query quota snapshot, and fails closed when
the remaining quota is below the requested floor.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Three .parent hops: seasonal/ -> scripts/ -> repo-root -> src/
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import httpx

from applicant_scout.config import ConfigError, load_config
from applicant_scout.constants import (
    CURRENT_MPLUS_ZONE_ID,
    CURRENT_RAID_ENCOUNTERS,
    CURRENT_RAID_ENCOUNTER_ZONE_IDS,
    MPLUS_ENCOUNTERS,
)
from applicant_scout.wcl import WCL_API_URL, WCLAuth, WCLAuthError


DEFAULT_MINIMUM_REMAINING_POINTS = 50.0


class SeasonalWCLVerificationError(RuntimeError):
    """Actionable seasonal verification failure."""


@dataclass(frozen=True)
class ZoneSnapshot:
    zone_id: int
    name: str
    encounters: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class QuotaSnapshot:
    limit_per_hour: float
    points_spent: float
    reset_in_seconds: int

    @property
    def remaining_points(self) -> float:
        return max(0.0, self.limit_per_hour - self.points_spent)


def seasonal_zone_ids() -> tuple[int, ...]:
    return tuple(
        dict.fromkeys((CURRENT_MPLUS_ZONE_ID, *CURRENT_RAID_ENCOUNTER_ZONE_IDS))
    )


def _normalize_zone_ids(zone_ids: Iterable[int]) -> tuple[int, ...]:
    normalized = tuple(zone_ids)
    if not normalized:
        raise SeasonalWCLVerificationError("At least one WCL zone ID is required")
    if any(
        isinstance(zone_id, bool) or not isinstance(zone_id, int) or zone_id <= 0
        for zone_id in normalized
    ):
        raise SeasonalWCLVerificationError("WCL zone IDs must be positive integers")
    if len(set(normalized)) != len(normalized):
        raise SeasonalWCLVerificationError("WCL zone IDs must be unique")
    return normalized


def build_query(zone_ids: Iterable[int]) -> str:
    normalized = _normalize_zone_ids(zone_ids)

    zone_fields = "\n".join(
        (
            f"    zone_{zone_id}: zone(id: {zone_id}) {{\n"
            "      id\n"
            "      name\n"
            "      encounters { id name }\n"
            "    }"
        )
        for zone_id in normalized
    )
    return (
        "query ApplicantScoutSeasonalZones {\n"
        "  rateLimitData {\n"
        "    limitPerHour\n"
        "    pointsSpentThisHour\n"
        "    pointsResetIn\n"
        "  }\n"
        "  worldData {\n"
        f"{zone_fields}\n"
        "  }\n"
        "}"
    )


def _graphql_error_messages(errors: object) -> list[str]:
    if not errors:
        return []
    entries = errors if isinstance(errors, list) else [errors]
    messages: list[str] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("message"), str):
            messages.append(entry["message"].strip() or "unknown error")
        elif isinstance(entry, str):
            messages.append(entry.strip() or "unknown error")
        else:
            messages.append("unknown error")
    return messages


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SeasonalWCLVerificationError(f"WCL {field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SeasonalWCLVerificationError(
            f"WCL {field} must be a non-negative finite number"
        )
    return number


def _zone_snapshot(raw: object, expected_zone_id: int) -> ZoneSnapshot:
    if not isinstance(raw, dict):
        raise SeasonalWCLVerificationError(
            f"WCL response missing zone {expected_zone_id} object"
        )
    zone_id = raw.get("id")
    if isinstance(zone_id, bool) or not isinstance(zone_id, int):
        raise SeasonalWCLVerificationError(
            f"WCL zone {expected_zone_id} id must be an integer"
        )
    if zone_id != expected_zone_id:
        raise SeasonalWCLVerificationError(
            f"WCL zone alias {expected_zone_id} returned id {zone_id}"
        )
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SeasonalWCLVerificationError(f"WCL zone {zone_id} has no name")
    raw_encounters = raw.get("encounters")
    if not isinstance(raw_encounters, list) or not raw_encounters:
        raise SeasonalWCLVerificationError(f"WCL zone {zone_id} has no encounters")

    encounters: list[tuple[int, str]] = []
    for raw_encounter in raw_encounters:
        if not isinstance(raw_encounter, dict):
            raise SeasonalWCLVerificationError(
                f"WCL zone {zone_id} encounter is not an object"
            )
        encounter_id = raw_encounter.get("id")
        encounter_name = raw_encounter.get("name")
        if isinstance(encounter_id, bool) or not isinstance(encounter_id, int):
            raise SeasonalWCLVerificationError(
                f"WCL zone {zone_id} encounter id must be an integer"
            )
        if not isinstance(encounter_name, str) or not encounter_name.strip():
            raise SeasonalWCLVerificationError(
                f"WCL encounter {encounter_id} has no name"
            )
        encounters.append((encounter_id, encounter_name.strip()))
    encounter_ids = [encounter_id for encounter_id, _name in encounters]
    if len(set(encounter_ids)) != len(encounter_ids):
        raise SeasonalWCLVerificationError(
            f"WCL zone {zone_id} contains duplicate encounter IDs"
        )
    return ZoneSnapshot(zone_id, name.strip(), tuple(encounters))


def extract_payload(
    payload: object, expected_zone_ids: Iterable[int]
) -> tuple[dict[int, ZoneSnapshot], QuotaSnapshot]:
    if not isinstance(payload, dict):
        raise SeasonalWCLVerificationError("WCL response must be a JSON object")
    errors = _graphql_error_messages(payload.get("errors"))
    if errors:
        raise SeasonalWCLVerificationError("WCL GraphQL error: " + "; ".join(errors))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SeasonalWCLVerificationError("WCL response missing data object")
    world_data = data.get("worldData")
    if not isinstance(world_data, dict):
        raise SeasonalWCLVerificationError("WCL response missing worldData object")
    quota_data = data.get("rateLimitData")
    if not isinstance(quota_data, dict):
        raise SeasonalWCLVerificationError("WCL response missing rateLimitData object")

    limit_per_hour = _finite_number(quota_data.get("limitPerHour"), "limitPerHour")
    points_spent = _finite_number(
        quota_data.get("pointsSpentThisHour"), "pointsSpentThisHour"
    )
    reset_number = _finite_number(quota_data.get("pointsResetIn"), "pointsResetIn")
    if not reset_number.is_integer():
        raise SeasonalWCLVerificationError("WCL pointsResetIn must be an integer")
    if points_spent > limit_per_hour:
        raise SeasonalWCLVerificationError(
            "WCL pointsSpentThisHour exceeds limitPerHour"
        )
    quota = QuotaSnapshot(limit_per_hour, points_spent, int(reset_number))

    normalized_zone_ids = _normalize_zone_ids(expected_zone_ids)
    zones: dict[int, ZoneSnapshot] = {}
    for zone_id in normalized_zone_ids:
        zones[zone_id] = _zone_snapshot(world_data.get(f"zone_{zone_id}"), zone_id)
    return zones, quota


def _format_pairs(pairs: set[tuple[int, str]]) -> str:
    return ", ".join(f"{encounter_id}:{name}" for encounter_id, name in sorted(pairs))


def _assert_encounter_set(
    label: str,
    actual: set[tuple[int, str]],
    expected: set[tuple[int, str]],
) -> None:
    if actual == expected:
        return
    missing = expected - actual
    unexpected = actual - expected
    details: list[str] = []
    if missing:
        details.append("missing " + _format_pairs(missing))
    if unexpected:
        details.append("unexpected " + _format_pairs(unexpected))
    raise SeasonalWCLVerificationError(
        f"{label} encounter constants are stale: " + "; ".join(details)
    )


def _constant_encounter_set(
    label: str,
    rows: Iterable[tuple[int, str]],
) -> set[tuple[int, str]]:
    normalized = tuple(rows)
    encounter_ids = [encounter_id for encounter_id, _name in normalized]
    if len(set(encounter_ids)) != len(encounter_ids):
        raise SeasonalWCLVerificationError(
            f"{label} constants contain duplicate encounter IDs"
        )
    return set(normalized)


def validate_current_constants(zones: dict[int, ZoneSnapshot]) -> None:
    mplus_zone = zones.get(CURRENT_MPLUS_ZONE_ID)
    if mplus_zone is None:
        raise SeasonalWCLVerificationError(
            f"Missing current M+ WCL zone {CURRENT_MPLUS_ZONE_ID}"
        )
    expected_mplus = _constant_encounter_set(
        "M+",
        (
            (encounter_id, name)
            for _alias, encounter_id, name in MPLUS_ENCOUNTERS
        ),
    )
    _assert_encounter_set("M+", set(mplus_zone.encounters), expected_mplus)

    actual_raid: set[tuple[int, str]] = set()
    raid_ids_seen: dict[int, int] = {}
    for zone_id in CURRENT_RAID_ENCOUNTER_ZONE_IDS:
        zone = zones.get(zone_id)
        if zone is None:
            raise SeasonalWCLVerificationError(f"Missing raid WCL zone {zone_id}")
        for encounter_id, name in zone.encounters:
            previous_zone = raid_ids_seen.get(encounter_id)
            if previous_zone is not None and previous_zone != zone_id:
                raise SeasonalWCLVerificationError(
                    f"Raid encounter {encounter_id} appears in zones "
                    f"{previous_zone} and {zone_id}"
                )
            raid_ids_seen[encounter_id] = zone_id
            actual_raid.add((encounter_id, name))
    expected_raid = _constant_encounter_set(
        "Raid",
        (
            (encounter_id, name)
            for _alias, encounter_id, name in CURRENT_RAID_ENCOUNTERS
        ),
    )
    _assert_encounter_set("Raid", actual_raid, expected_raid)


def require_quota_floor(quota: QuotaSnapshot, minimum_remaining: float) -> None:
    if not math.isfinite(minimum_remaining) or minimum_remaining < 0:
        raise SeasonalWCLVerificationError(
            "minimum remaining quota must be a non-negative finite number"
        )
    if quota.remaining_points < minimum_remaining:
        raise SeasonalWCLVerificationError(
            f"WCL quota remaining after check is {quota.remaining_points:.1f}, "
            f"below required floor {minimum_remaining:.1f}; reset in "
            f"{quota.reset_in_seconds}s"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify shipped WCL M+ and raid zone/encounter constants."
    )
    parser.add_argument(
        "--confirm-spend-wcl-quota",
        action="store_true",
        help="Required acknowledgment that this live check spends WCL API quota.",
    )
    parser.add_argument(
        "--minimum-remaining-points",
        type=float,
        default=DEFAULT_MINIMUM_REMAINING_POINTS,
        help="Fail if post-query WCL quota remaining is below this value.",
    )
    return parser.parse_args(argv)


def fetch_live_payload(token: str, query: str) -> object:
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                WCL_API_URL,
                json={"query": query},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise SeasonalWCLVerificationError(f"WCL request failed: {exc}") from exc
    if response.status_code != 200:
        raise SeasonalWCLVerificationError(
            f"WCL HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise SeasonalWCLVerificationError("WCL response is not valid JSON") from exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.confirm_spend_wcl_quota:
        raise SeasonalWCLVerificationError(
            "Refusing live WCL query without --confirm-spend-wcl-quota"
        )
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise SeasonalWCLVerificationError(str(exc)) from exc
    if not cfg.wcl_client_id.strip() or not cfg.wcl_client_secret.strip():
        raise SeasonalWCLVerificationError(
            "WCL Client ID and Client Secret are required in config.env"
        )
    auth = WCLAuth(cfg.wcl_client_id, cfg.wcl_client_secret, cfg.cache_dir)
    try:
        token = auth.get_token()
    except WCLAuthError as exc:
        raise SeasonalWCLVerificationError(f"WCL OAuth failed: {exc}") from exc

    zone_ids = seasonal_zone_ids()
    zones, quota = extract_payload(
        fetch_live_payload(token, build_query(zone_ids)), zone_ids
    )
    validate_current_constants(zones)
    require_quota_floor(quota, args.minimum_remaining_points)
    zone_summary = ", ".join(
        f"{zone_id} ({zones[zone_id].name})" for zone_id in zone_ids
    )
    print(f"WCL seasonal constants match zones: {zone_summary}")
    print(
        "WCL quota after check: "
        f"spent={quota.points_spent:.1f}/{quota.limit_per_hour:.1f}, "
        f"remaining={quota.remaining_points:.1f}, reset={quota.reset_in_seconds}s"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeasonalWCLVerificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
