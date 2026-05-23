"""One-shot helper: print ready-to-paste M+ LFG activity IDs.

Wago DB2 exposes Blizzard's GroupFinderActivity table, but it does not mark the
current Mythic+ season directly. This helper starts from MPLUS_ENCOUNTERS, then
selects activity groups that contain a Mythic Keystone row for those dungeon
names so historical duplicate dungeon rows do not silently leak into the
current-season mapping.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Three .parent hops: seasonal/ -> scripts/ -> repo-root -> src/
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import httpx

from applicant_scout.constants import (
    MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME,
    MPLUS_ENCOUNTERS,
)


DEFAULT_WAGO_ACTIVITY_CSV_URL = (
    "https://wago.tools/db2/GroupFinderActivity/csv?branch=wow"
)
REQUIRED_COLUMNS = frozenset(
    {
        "ID",
        "FullName_lang",
        "ShortName_lang",
        "GroupFinderCategoryID",
        "GroupFinderActivityGrpID",
        "DifficultyID",
    }
)


class SeasonalScriptError(RuntimeError):
    """Actionable manual-script error."""


@dataclass(frozen=True)
class ActivityRow:
    activity_id: int
    full_name: str
    short_name: str
    category_id: int
    group_id: int
    difficulty_id: int

    @property
    def dungeon_name(self) -> str:
        return _base_activity_name(self.full_name)


def _normalise_name(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _base_activity_name(value: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", value.strip())


def _int_field(row: dict[str, str], field: str) -> int:
    raw = row.get(field)
    if raw is None or not raw.strip():
        raise SeasonalScriptError(f"Wago GroupFinderActivity {field} is missing")
    try:
        return int(raw)
    except ValueError as exc:
        raise SeasonalScriptError(
            f"Wago GroupFinderActivity {field} must be an integer: {raw!r}"
        ) from exc


def parse_activity_rows(csv_text: str) -> list[ActivityRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = set(reader.fieldnames or [])
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise SeasonalScriptError(
            "Wago GroupFinderActivity CSV missing columns: " + ", ".join(missing)
        )

    rows: list[ActivityRow] = []
    for line_number, row in enumerate(reader, start=2):
        full_name = (row.get("FullName_lang") or "").strip()
        if not full_name:
            raise SeasonalScriptError(
                f"Wago GroupFinderActivity line {line_number} has empty FullName_lang"
            )
        rows.append(
            ActivityRow(
                activity_id=_int_field(row, "ID"),
                full_name=full_name,
                short_name=(row.get("ShortName_lang") or "").strip(),
                category_id=_int_field(row, "GroupFinderCategoryID"),
                group_id=_int_field(row, "GroupFinderActivityGrpID"),
                difficulty_id=_int_field(row, "DifficultyID"),
            )
        )
    if not rows:
        raise SeasonalScriptError("Wago GroupFinderActivity CSV has no rows")
    return rows


def _is_mythic_keystone_row(row: ActivityRow) -> bool:
    short_name = _normalise_name(row.short_name)
    return (
        row.difficulty_id == 8
        or short_name in {"mythic+", "mythic keystone"}
        or "mythic keystone" in _normalise_name(row.full_name)
    )


def extract_mplus_activity_mapping(
    csv_text: str, expected_dungeon_names: list[str]
) -> dict[int, str]:
    rows = parse_activity_rows(csv_text)
    expected_by_key = {_normalise_name(name): name for name in expected_dungeon_names}
    rows_by_group: dict[int, list[ActivityRow]] = defaultdict(list)
    for row in rows:
        rows_by_group[row.group_id].append(row)

    mapping: dict[int, str] = {}
    for expected_key, expected_name in expected_by_key.items():
        candidates: list[list[ActivityRow]] = []
        for group_rows in rows_by_group.values():
            matching_rows = [
                row
                for row in group_rows
                if _normalise_name(row.dungeon_name) == expected_key
                and row.category_id == 2
            ]
            if matching_rows and any(_is_mythic_keystone_row(row) for row in matching_rows):
                candidates.append(matching_rows)

        if not candidates:
            raise SeasonalScriptError(
                f"No Wago GroupFinderActivity group with Mythic Keystone row "
                f"found for {expected_name}"
            )
        if len(candidates) > 1:
            groups = ", ".join(str(candidate[0].group_id) for candidate in candidates)
            raise SeasonalScriptError(
                f"Multiple current-looking Wago activity groups found for "
                f"{expected_name}: {groups}"
            )

        for row in sorted(candidates[0], key=lambda item: item.activity_id):
            existing = mapping.get(row.activity_id)
            if existing is not None and existing != expected_name:
                raise SeasonalScriptError(
                    f"Duplicate activity ID {row.activity_id} maps to both "
                    f"{existing} and {expected_name}"
                )
            mapping[row.activity_id] = expected_name

    unknown_names = sorted(set(mapping.values()) - set(expected_dungeon_names))
    if unknown_names:
        raise SeasonalScriptError(
            "Extracted activity mapping contains unknown dungeon names: "
            + ", ".join(unknown_names)
        )
    return dict(sorted(mapping.items()))


def _quote_display_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_activity_mapping(mapping: dict[int, str]) -> str:
    lines = ["MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME: dict[int, str] = {"]
    for activity_id, dungeon_name in sorted(mapping.items()):
        lines.append(f"    {activity_id}: {_quote_display_string(dungeon_name)},")
    lines.append("}")
    return "\n".join(lines)


def current_mplus_dungeon_names() -> list[str]:
    return [name for _alias, _encounter_id, name in MPLUS_ENCOUNTERS]


def fetch_wago_activity_csv(url: str = DEFAULT_WAGO_ACTIVITY_CSV_URL) -> str:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url)
    if resp.status_code != 200:
        raise SeasonalScriptError(f"Wago HTTP {resp.status_code}: {resp.text[:200]}")
    text = resp.text
    if "ID,FullName_lang" not in text[:200]:
        raise SeasonalScriptError("Wago response does not look like GroupFinderActivity CSV")
    return text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print or check current-season Mythic+ LFG activity ID mapping."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the extracted Wago mapping differs from constants.py.",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_WAGO_ACTIVITY_CSV_URL,
        help="GroupFinderActivity CSV URL to read.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mapping = extract_mplus_activity_mapping(
        fetch_wago_activity_csv(args.url), current_mplus_dungeon_names()
    )
    if args.check:
        if mapping != MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME:
            print("Expected constants.py mapping:", file=sys.stderr)
            print(format_activity_mapping(mapping), file=sys.stderr)
            raise SeasonalScriptError("MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME is stale")
        print("MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME matches Wago GroupFinderActivity CSV")
        return 0
    print(format_activity_mapping(mapping))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeasonalScriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
