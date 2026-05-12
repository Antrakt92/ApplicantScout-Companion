"""One-shot helper: print ready-to-paste M+ encounter tuples for current season.

This spends real Warcraft Logs API quota. Run after bumping
CURRENT_MPLUS_ZONE_ID in constants.py, then copy the emitted tuples into
MPLUS_ENCOUNTERS.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

# Three .parent hops: seasonal/ -> scripts/ -> repo-root -> src/
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import httpx

from applicant_scout.config import load_config
from applicant_scout.constants import CURRENT_MPLUS_ZONE_ID
from applicant_scout.wcl import WCL_API_URL, WCLAuth


class SeasonalScriptError(RuntimeError):
    """Actionable manual-script error."""


def build_query(zone_id: int) -> str:
    return f"""
query Zones {{
  worldData {{
    zone(id: {zone_id}) {{
      id
      name
      encounters {{
        id
        name
      }}
    }}
  }}
}}
""".strip()


def json_object_response(resp: Any) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as exc:
        raise SeasonalScriptError("WCL response is not valid JSON") from exc
    if not isinstance(body, dict):
        raise SeasonalScriptError("WCL response must be a JSON object")
    return body


def _graphql_error_messages(errors: object) -> list[str]:
    if not errors:
        return []
    raw_errors = errors if isinstance(errors, list) else [errors]
    messages: list[str] = []
    for entry in raw_errors:
        if isinstance(entry, dict):
            message = entry.get("message")
            messages.append(message.strip() if isinstance(message, str) else "unknown error")
        elif isinstance(entry, str):
            messages.append(entry.strip() or "unknown error")
        else:
            messages.append("unknown error")
    return messages


def extract_zone_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SeasonalScriptError("WCL response must be a JSON object")
    errors = _graphql_error_messages(payload.get("errors"))
    if errors:
        raise SeasonalScriptError("WCL GraphQL error: " + "; ".join(errors))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SeasonalScriptError("WCL response missing data object")
    world_data = data.get("worldData")
    if not isinstance(world_data, dict):
        raise SeasonalScriptError("WCL response missing worldData object")
    zone = world_data.get("zone")
    if not isinstance(zone, dict):
        raise SeasonalScriptError("WCL response missing zone object")
    encounters = zone.get("encounters")
    if not isinstance(encounters, list) or not encounters:
        raise SeasonalScriptError("WCL zone has no encounters")

    normalized: list[dict[str, int | str]] = []
    for encounter in encounters:
        if not isinstance(encounter, dict):
            raise SeasonalScriptError("WCL encounter entry is not an object")
        encounter_id = encounter.get("id")
        name = encounter.get("name")
        if isinstance(encounter_id, bool) or not isinstance(encounter_id, int):
            raise SeasonalScriptError("WCL encounter id must be an integer")
        if not isinstance(name, str) or not name.strip():
            raise SeasonalScriptError("WCL encounter name must be a non-empty string")
        normalized.append({"id": encounter_id, "name": name.strip()})

    zone_id = zone.get("id")
    if isinstance(zone_id, bool) or not isinstance(zone_id, int):
        zone_id = CURRENT_MPLUS_ZONE_ID
    zone_name = zone.get("name")
    if not isinstance(zone_name, str):
        zone_name = ""
    return {"id": zone_id, "name": zone_name.strip(), "encounters": normalized}


def _alias_for_name(name: str, used: set[str]) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in {"a", "an", "of", "the"}]
    if len(words) >= 2:
        base = (words[0][0] + words[1][0]).lower()
    elif words:
        base = words[0][:2].lower()
    else:
        base = "d"
    if base[0].isdigit():
        base = "d" + base
    alias = base
    suffix = 2
    while alias in used:
        alias = f"{base}{suffix}"
        suffix += 1
    used.add(alias)
    return alias


def _quote_display_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_mplus_tuples(zone: dict[str, Any]) -> str:
    used_aliases: set[str] = set()
    lines = [f"# WCL zone {zone['id']}: {zone['name']}"]
    for encounter in zone["encounters"]:
        alias = _alias_for_name(str(encounter["name"]), used_aliases)
        name = _quote_display_string(str(encounter["name"]))
        lines.append(f'("{alias}", {encounter["id"]}, {name}),')
    return "\n".join(lines)


def main() -> int:
    cfg = load_config()
    auth = WCLAuth(cfg.wcl_client_id, cfg.wcl_client_secret, cfg.cache_dir)
    body = {"query": build_query(CURRENT_MPLUS_ZONE_ID)}
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            WCL_API_URL,
            json=body,
            headers={"Authorization": f"Bearer {auth.get_token()}"},
        )
    if resp.status_code != 200:
        raise SeasonalScriptError(f"WCL HTTP {resp.status_code}: {resp.text[:200]}")
    print(format_mplus_tuples(extract_zone_payload(json_object_response(resp))))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeasonalScriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
