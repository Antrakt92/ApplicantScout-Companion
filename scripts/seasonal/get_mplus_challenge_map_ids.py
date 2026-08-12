"""One-shot helper: print ready-to-paste M+ challenge map IDs.

Wago DB2 exposes MapChallengeMode names and MythicPlusSeasonTrackedMap rows.
The addon emits MapChallengeModeID for party leader keystones, so this helper
keeps that challenge-map namespace separate from GroupFinderActivity IDs used
for active LFG listings. DB2 can include a scheduled future season before it is
live, so resolve the row set that exactly matches the shipped M+ dungeon pool
instead of assuming the largest DisplaySeasonID is active.
"""

# ruff: noqa: E402 - executable scripts bootstrap the repository import roots

from __future__ import annotations

import argparse
import csv
import io
import sys
from dataclasses import dataclass
from pathlib import Path

# Direct script execution starts with only scripts/seasonal on sys.path.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from applicant_scout.constants import (
    MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME,
)
from scripts.seasonal._shared import (
    SeasonalScriptError,
    current_mplus_dungeon_names,
    fetch_wago_csv,
    quote_display_string,
)


DEFAULT_WAGO_CHALLENGE_MAP_CSV_URL = (
    "https://wago.tools/db2/MapChallengeMode/csv?branch=wow"
)
DEFAULT_WAGO_SEASON_TRACKED_MAP_CSV_URL = (
    "https://wago.tools/db2/MythicPlusSeasonTrackedMap/csv?branch=wow"
)
REQUIRED_CHALLENGE_COLUMNS = frozenset({"Name_lang", "ID", "MapID"})
REQUIRED_TRACKED_COLUMNS = frozenset(
    {"ID", "MapChallengeModeID", "DisplaySeasonID"}
)

# Source-only MapID and tracked-row ID columns are validated during parsing but
# are not retained in the mapping rows.

@dataclass(frozen=True)
class ChallengeMapRow:
    challenge_map_id: int
    name: str


@dataclass(frozen=True)
class TrackedMapRow:
    challenge_map_id: int
    display_season_id: int


def _int_field(row: dict[str, str], field: str, table: str) -> int:
    raw = row.get(field)
    if raw is None or not raw.strip():
        raise SeasonalScriptError(f"Wago {table} {field} is missing")
    try:
        return int(raw)
    except ValueError as exc:
        raise SeasonalScriptError(
            f"Wago {table} {field} must be an integer: {raw!r}"
        ) from exc


def parse_challenge_map_rows(csv_text: str) -> list[ChallengeMapRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = set(reader.fieldnames or [])
    missing = sorted(REQUIRED_CHALLENGE_COLUMNS - columns)
    if missing:
        raise SeasonalScriptError(
            "Wago MapChallengeMode CSV missing columns: " + ", ".join(missing)
        )

    rows: list[ChallengeMapRow] = []
    for line_number, row in enumerate(reader, start=2):
        name = (row.get("Name_lang") or "").strip()
        if not name:
            raise SeasonalScriptError(
                f"Wago MapChallengeMode line {line_number} has empty Name_lang"
            )
        _int_field(row, "MapID", "MapChallengeMode")
        rows.append(
            ChallengeMapRow(
                challenge_map_id=_int_field(row, "ID", "MapChallengeMode"),
                name=name,
            )
        )
    if not rows:
        raise SeasonalScriptError("Wago MapChallengeMode CSV has no rows")
    return rows


def parse_tracked_map_rows(csv_text: str) -> list[TrackedMapRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = set(reader.fieldnames or [])
    missing = sorted(REQUIRED_TRACKED_COLUMNS - columns)
    if missing:
        raise SeasonalScriptError(
            "Wago MythicPlusSeasonTrackedMap CSV missing columns: "
            + ", ".join(missing)
        )

    rows: list[TrackedMapRow] = []
    for row in reader:
        _int_field(row, "ID", "MythicPlusSeasonTrackedMap")
        rows.append(
            TrackedMapRow(
                challenge_map_id=_int_field(
                    row, "MapChallengeModeID", "MythicPlusSeasonTrackedMap"
                ),
                display_season_id=_int_field(
                    row, "DisplaySeasonID", "MythicPlusSeasonTrackedMap"
                ),
            )
        )
    if not rows:
        raise SeasonalScriptError("Wago MythicPlusSeasonTrackedMap CSV has no rows")
    return rows


def extract_mplus_challenge_map_mapping(
    challenge_map_csv: str,
    season_tracked_map_csv: str,
    expected_dungeon_names: list[str],
) -> dict[int, str]:
    challenge_rows_by_id: dict[int, ChallengeMapRow] = {}
    for row in parse_challenge_map_rows(challenge_map_csv):
        existing = challenge_rows_by_id.get(row.challenge_map_id)
        if existing is not None and existing.name != row.name:
            raise SeasonalScriptError(
                f"Duplicate challenge map ID {row.challenge_map_id} maps to both "
                f"{existing.name} and {row.name}"
            )
        challenge_rows_by_id[row.challenge_map_id] = row

    expected_names = set(expected_dungeon_names)
    mappings_by_season: dict[int, dict[int, str]] = {}
    for row in parse_tracked_map_rows(season_tracked_map_csv):
        challenge = challenge_rows_by_id.get(row.challenge_map_id)
        if challenge is None:
            raise SeasonalScriptError(
                "Wago MythicPlusSeasonTrackedMap references missing "
                f"MapChallengeMode ID {row.challenge_map_id}"
            )
        mappings_by_season.setdefault(row.display_season_id, {})[
            row.challenge_map_id
        ] = challenge.name

    matching_mappings = [
        (display_season_id, mapping)
        for display_season_id, mapping in mappings_by_season.items()
        if set(mapping.values()) == expected_names
    ]
    if len(matching_mappings) == 1:
        return matching_mappings[0][1]

    observed = "; ".join(
        f"{display_season_id}: {', '.join(sorted(set(mapping.values())))}"
        for display_season_id, mapping in sorted(mappings_by_season.items())
    )
    if not matching_mappings:
        raise SeasonalScriptError(
            "No Wago MythicPlusSeasonTrackedMap season matches the current dungeon "
            "names. Expected: "
            f"{', '.join(sorted(expected_names))}. "
            f"Observed DisplaySeasonID mappings: {observed}"
        )
    matching_ids = ", ".join(
        str(display_season_id) for display_season_id, _mapping in matching_mappings
    )
    raise SeasonalScriptError(
        "Multiple Wago MythicPlusSeasonTrackedMap seasons match the current dungeon "
        f"names: {matching_ids}. Observed DisplaySeasonID mappings: {observed}"
    )


def format_challenge_map_mapping(mapping: dict[int, str]) -> str:
    lines = ["MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME: dict[int, str] = {"]
    for challenge_map_id, dungeon_name in sorted(mapping.items()):
        lines.append(f"    {challenge_map_id}: {quote_display_string(dungeon_name)},")
    lines.append("}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print or check the shipped Mythic+ challenge-map ID mapping."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the extracted Wago mapping differs from constants.py.",
    )
    parser.add_argument(
        "--challenge-map-url",
        default=DEFAULT_WAGO_CHALLENGE_MAP_CSV_URL,
        help="MapChallengeMode CSV URL to read.",
    )
    parser.add_argument(
        "--season-tracked-map-url",
        default=DEFAULT_WAGO_SEASON_TRACKED_MAP_CSV_URL,
        help="MythicPlusSeasonTrackedMap CSV URL to read.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mapping = extract_mplus_challenge_map_mapping(
        fetch_wago_csv(
            args.challenge_map_url,
            "MapChallengeMode",
            "Name_lang,ID,MapID",
        ),
        fetch_wago_csv(
            args.season_tracked_map_url,
            "MythicPlusSeasonTrackedMap",
            "ID,MapChallengeModeID,DisplaySeasonID",
        ),
        current_mplus_dungeon_names(),
    )
    if args.check:
        if mapping != MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME:
            print("Expected constants.py mapping:", file=sys.stderr)
            print(format_challenge_map_mapping(mapping), file=sys.stderr)
            raise SeasonalScriptError(
                "MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME is stale"
            )
        print(
            "MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME matches Wago "
            "MythicPlusSeasonTrackedMap/MapChallengeMode CSV"
        )
        return 0
    print(format_challenge_map_mapping(mapping))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeasonalScriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
