"""Pure data-to-text and render-decision helpers for the overlay."""

from __future__ import annotations

from collections.abc import Iterable
import html

from .constants import (
    CURRENT_RAID_ENCOUNTERS,
    mplus_dungeon_name_for_activity_id,
    percentile_colour,
)
from .metric_preferences import MetricPreferences
from .overlay_rows import mplus_key_level
from .scoring import (
    CONTEXT_RAID,
    RAID_TARGET_BY_DIFFICULTY_ID,
    detect_listing_context,
    effective_rio_score,
    mplus_dungeon_fit_rows,
    mplus_metric_text,
    nonnegative_int,
    positive_int,
    role_mplus_view,
    safe_percent,
)
from .state import Applicant, Listing


def rio_display_text(applicant: Applicant) -> str:
    if applicant.main_score > applicant.score and applicant.score:
        return f"{applicant.score} [{applicant.main_score}]"
    if applicant.main_score > applicant.score:
        return f"[{applicant.main_score}]"
    return str(applicant.score) if applicant.score else "—"


def rio_panel_text(applicant: Applicant) -> str:
    if applicant.main_score > applicant.score and applicant.score:
        return f"RIO {applicant.score} · main {applicant.main_score}"
    if applicant.main_score > applicant.score:
        return f"RIO main {applicant.main_score}"
    return f"RIO {applicant.score}" if applicant.score else ""


def mplus_fit_source_text(applicant: Applicant) -> str:
    _metric_label, breakdown, _best, _median = role_mplus_view(applicant)
    has_wcl = bool(breakdown)
    has_rio = bool(
        applicant.rio_profile
        or applicant.rio_dungeons
        or applicant.rio_best_key
        or applicant.rio_timed_at_or_above
        or effective_rio_score(applicant)
    )
    if has_wcl and has_rio:
        return "WCL + RaiderIO"
    if has_wcl:
        return "WCL only"
    if has_rio:
        return "RaiderIO only"
    return "score only"


def count_phrase(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


def text_colour_for_bg(bg_hex: str | None) -> str:
    """Return readable foreground for saturated percentile/class badges."""
    if not bg_hex or len(bg_hex) != 7 or not bg_hex.startswith("#"):
        return "#ffffff"
    try:
        red = int(bg_hex[1:3], 16)
        green = int(bg_hex[3:5], 16)
        blue = int(bg_hex[5:7], 16)
    except ValueError:
        return "#ffffff"
    luminance = (0.299 * red) + (0.587 * green) + (0.114 * blue)
    return "#000000" if luminance >= 145 else "#ffffff"


def raid_cell_visuals(
    best: object,
    median: object,
    fetch_status: str,
) -> tuple[str, str | None, str | None]:
    if fetch_status in {"pending", "loading"}:
        return "…", "#888", None
    if fetch_status == "error":
        return "?", "#ff5555", None
    if fetch_status == "not_found":
        return "—", "#5d5d5d", None
    best_pct = safe_percent(best)
    median_pct = safe_percent(median)
    if best_pct is None and median_pct is None:
        return "—", "#5d5d5d", None
    best_str = f"{int(round(best_pct))}" if best_pct is not None else "—"
    text = (
        best_str
        if median_pct is None
        else f"{best_str}/{int(round(median_pct))}"
    )
    bg = percentile_colour(best_pct) if best_pct is not None else None
    fg = text_colour_for_bg(bg) if bg is not None else None
    return text, fg, bg


def raid_values_for_key(
    applicant: Applicant, key: str
) -> tuple[float | None, float | None]:
    if key == "N":
        return applicant.raid_normal, applicant.raid_normal_median
    if key == "H":
        return applicant.raid_heroic, applicant.raid_heroic_median
    if key == "M":
        return applicant.raid_mythic, applicant.raid_mythic_median
    return None, None


def raid_metric_text_for_key(applicant: Applicant, key: str) -> str:
    best, median = raid_values_for_key(applicant, key)
    text, _fg, _bg = raid_cell_visuals(best, median, "ready")
    return "" if text == "—" else text


def raid_fit_evidence_text(applicant: Applicant, target: str, source: str) -> str:
    if source == "raid_exact":
        return raid_metric_text_for_key(applicant, target)
    order = ["N", "H", "M"]
    target_idx = order.index(target) if target in order else -1
    candidate_keys: list[str] = []
    if source == "raid_higher_fallback" and target_idx >= 0:
        candidate_keys = order[target_idx + 1 :]
    elif source == "raid_lower_fallback" and target_idx >= 0:
        candidate_keys = list(reversed(order[:target_idx]))
    best_key = ""
    best_score = -1.0
    for key in candidate_keys:
        best, median = raid_values_for_key(applicant, key)
        score = safe_percent(best)
        if score is None:
            score = safe_percent(median)
        if score is not None and score > best_score:
            best_key = key
            best_score = score
    raw = raid_metric_text_for_key(applicant, best_key)
    return f"{best_key} {raw}" if best_key and raw else ""


def raid_target_key_for_listing(listing: Listing | None) -> str:
    if detect_listing_context(listing) != CONTEXT_RAID or listing is None:
        return ""
    return RAID_TARGET_BY_DIFFICULTY_ID.get(listing.difficulty_id, "")


def mplus_run_count(entry: object) -> int:
    if not isinstance(entry, dict):
        return 0
    return nonnegative_int(entry.get("run_count"))


def mplus_sort_key(entry: dict) -> tuple[int, str]:
    name = entry.get("name")
    return (-mplus_key_level(entry), str(name or ""))


def normalise_dungeon_name(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def rio_dungeon_row_key(name: str, listing: Listing | None) -> str:
    row_key = normalise_dungeon_name(name)
    if listing is None:
        return row_key
    mapped_name = mplus_dungeon_name_for_activity_id(listing.activity_id)
    mapped_key = normalise_dungeon_name(mapped_name)
    if mapped_key and row_key == normalise_dungeon_name(listing.dungeon_name):
        return mapped_key
    return row_key


def rio_dungeon_rows_by_name(
    applicant: Applicant, listing: Listing | None
) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for entry in applicant.rio_dungeons:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        key = positive_int(entry.get("key_level"))
        row_key = rio_dungeon_row_key(name, listing)
        if not name or not row_key or key <= 0:
            continue
        existing = rows.get(row_key)
        if existing is None or key > positive_int(existing.get("key_level")):
            rows[row_key] = {"name": name, "key_level": key}
    return rows


def wcl_dungeon_rows_by_name(
    applicant: Applicant, listing: Listing | None
) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    if applicant.fetch_status != "ready":
        return rows
    fit_rows = mplus_dungeon_fit_rows(applicant, listing)
    if fit_rows:
        for row in fit_rows:
            row_key = normalise_dungeon_name(row.dungeon_name)
            if row_key:
                rows[row_key] = {
                    "name": row.dungeon_name,
                    "key_level": row.key_level,
                    "text": row.text,
                    "colour": row.colour,
                }
        return rows

    _metric_label, breakdown, _best, _median = role_mplus_view(applicant)
    for entry in sorted(
        [entry for entry in breakdown if isinstance(entry, dict)],
        key=mplus_sort_key,
    ):
        name = str(entry.get("name") or "").strip()
        row_key = normalise_dungeon_name(name)
        if not name or not row_key:
            continue
        best = safe_percent(entry.get("parse_percent"))
        rows[row_key] = {
            "name": name,
            "key_level": mplus_key_level(entry),
            "text": mplus_dungeon_metric_text(entry),
            "colour": percentile_colour(best) if best is not None else "#2a2a33",
        }
    return rows


def enabled_raid_difficulty_keys(metric_preferences: MetricPreferences) -> list[str]:
    keys: list[str] = []
    if metric_preferences.raid_normal:
        keys.append("N")
    if metric_preferences.raid_heroic:
        keys.append("H")
    if metric_preferences.raid_mythic:
        keys.append("M")
    return keys


def raid_boss_rows_for_display(
    applicant: Applicant, difficulties: Iterable[str]
) -> list[dict[str, object]]:
    difficulty_keys = [key for key in difficulties if key in {"M", "H", "N"}]
    parse_rows_by_difficulty = {
        key: raid_boss_parse_rows_by_encounter(applicant, key)
        for key in difficulty_keys
    }
    boss_kills_by_difficulty: dict[str, list[object]] = {}
    for key in difficulty_keys:
        progress = applicant.rio_raid_progress.get(key, {})
        boss_kills = progress.get("boss_kills") if isinstance(progress, dict) else None
        boss_kills_by_difficulty[key] = (
            boss_kills if isinstance(boss_kills, list) else []
        )
    rows: list[dict[str, object]] = []
    for idx, (_alias, encounter_id, name) in enumerate(CURRENT_RAID_ENCOUNTERS):
        kill_parts: list[str] = []
        parse_parts: list[str] = []
        parse_segments: list[dict[str, str]] = []
        colour_overalls: list[float] = []
        for difficulty in difficulty_keys:
            parse_rows = parse_rows_by_difficulty[difficulty]
            boss_kills = boss_kills_by_difficulty[difficulty]
            parse_row = parse_rows.get(encounter_id, {})
            overall = safe_percent(parse_row.get("overall"))
            ilvl = safe_percent(parse_row.get("ilvl"))
            value = raid_parse_pair_text(overall, ilvl)
            kills = 0
            if idx < len(boss_kills):
                kills = nonnegative_int(boss_kills[idx])
            if kills > 0:
                kill_parts.append(f"{difficulty}{kills}")
            if value:
                text = f"{difficulty} {value}"
                parse_parts.append(text)
                parse_segments.append(
                    {
                        "text": text,
                        "colour": (
                            percentile_colour(overall)
                            if overall is not None
                            else "#2a2a33"
                        ),
                    }
                )
                if overall is not None:
                    colour_overalls.append(overall)
        colour = percentile_colour(max(colour_overalls)) if colour_overalls else ""
        rows.append(
            {
                "name": name,
                "rio_text": " · ".join(kill_parts),
                "wcl_text": "WCL" if parse_parts else "",
                "value": " · ".join(parse_parts),
                "colour": colour,
                "segments": parse_segments,
            }
        )
    return rows


def raid_boss_parse_rows_by_encounter(
    applicant: Applicant, difficulty: str
) -> dict[int, dict[str, object]]:
    rows: dict[int, dict[str, object]] = {}
    raw_rows = applicant.raid_boss_parses.get(difficulty, [])
    if not isinstance(raw_rows, list):
        return rows
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        encounter_id = positive_int(row.get("encounter_id"))
        if encounter_id > 0:
            rows[encounter_id] = row
    return rows


def raid_parse_pair_text(overall: float | None, ilvl: float | None) -> str:
    if overall is None and ilvl is None:
        return ""
    left = str(int(round(overall))) if overall is not None else "-"
    right = str(int(round(ilvl))) if ilvl is not None else "-"
    return f"{left}-{right}"


def raid_parse_segments_html(segments: list[object]) -> str:
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        colour = str(segment.get("colour") or "#2a2a33")
        if not text:
            continue
        parts.append(
            "<span "
            f'style="background-color: {html.escape(colour, quote=True)}; '
            f"color: {text_colour_for_bg(colour)}; "
            'font-size: 11px; font-weight: bold;">'
            f"{html.escape(text)}</span>"
        )
    return "&nbsp;".join(parts)


def mplus_breakdown_all_single_run(breakdown: Iterable[object]) -> bool:
    seen_valid = False
    for entry in breakdown:
        if not isinstance(entry, dict):
            continue
        if safe_percent(entry.get("parse_percent")) is None:
            continue
        seen_valid = True
        if mplus_run_count(entry) != 1:
            return False
    return seen_valid


def mplus_dungeon_metric_text(entry: object) -> str:
    if not isinstance(entry, dict):
        return "—"
    return mplus_metric_text(
        entry.get("parse_percent"),
        entry.get("median_percent"),
        entry.get("run_count"),
    )


def format_age(delta_sec: float) -> str:
    if delta_sec >= 86400.0:
        return "—"
    if delta_sec >= 3600.0:
        return f"{int(delta_sec // 3600)}h ago"
    if delta_sec >= 60.0:
        return f"{int(delta_sec // 60)}m ago"
    return f"{int(delta_sec)}s ago"


def format_duration(delta_sec: float) -> str:
    if delta_sec >= 86400.0:
        return "24h+"
    if delta_sec >= 3600.0:
        return f"{int(delta_sec // 3600)}h"
    if delta_sec >= 60.0:
        return f"{int(delta_sec // 60)}m"
    return f"{int(delta_sec)}s"


def format_listing_tooltip(listing: Listing | None) -> str:
    if listing is None:
        return ""
    name_raw = (listing.listing_name or "").strip()
    comment_raw = (listing.comment or "").strip()
    if not name_raw and not comment_raw:
        return ""
    name = html.escape(name_raw, quote=False)
    comment = html.escape(comment_raw, quote=False)
    if not name:
        return comment
    if not comment:
        return name
    if name_raw == comment_raw:
        return name
    return f"{name}\n\n{comment}"
