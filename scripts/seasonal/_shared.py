"""Shared contracts for the one-shot seasonal maintenance scripts."""

from __future__ import annotations

import httpx

from applicant_scout.constants import MPLUS_ENCOUNTERS


class SeasonalScriptError(RuntimeError):
    """Actionable manual-script error."""


def fetch_wago_csv(url: str, table_name: str, marker: str) -> str:
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise SeasonalScriptError(
            f"Wago {table_name} request failed: {exc}"
        ) from exc
    if response.status_code != 200:
        raise SeasonalScriptError(
            f"Wago {table_name} HTTP {response.status_code}: {response.text[:200]}"
        )
    text = response.text
    if marker not in text[:200]:
        raise SeasonalScriptError(
            f"Wago response does not look like {table_name} CSV"
        )
    return text


def quote_display_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def current_mplus_dungeon_names() -> list[str]:
    return [name for _alias, _encounter_id, name in MPLUS_ENCOUNTERS]
