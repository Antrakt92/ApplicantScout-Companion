"""Shared contracts for the one-shot seasonal maintenance scripts."""

from __future__ import annotations

from applicant_scout.constants import MPLUS_ENCOUNTERS


class SeasonalScriptError(RuntimeError):
    """Actionable manual-script error."""


def quote_display_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def current_mplus_dungeon_names() -> list[str]:
    return [name for _alias, _encounter_id, name in MPLUS_ENCOUNTERS]
