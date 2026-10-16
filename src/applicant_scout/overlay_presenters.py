"""Pure data-to-text and render-decision helpers for the overlay."""

from __future__ import annotations

from collections.abc import Iterable
import html

from .constants import (
    CURRENT_RAID_ENCOUNTERS,
    MPLUS_ENCOUNTERS,
    mplus_dungeon_name_for_activity_id,
    percentile_colour,
)
from .metric_preferences import MetricPreferences
from .overlay_rows import mplus_key_level
from .ui_text import MISSING_DATA_TEXT
from .scoring import (
    CONTEXT_MPLUS,
    CONTEXT_RAID,
    CandidateFit,
    PackageFit,
    RAID_TARGET_BY_DIFFICULTY_ID,
    detect_listing_context,
    MPlusPerformanceSummary,
    mplus_performance_rows,
    mplus_performance_summary,
    mplus_metric_text,
    nonnegative_int,
    normalise_dungeon_name,
    positive_int,
    raid_performance_score,
    safe_percent,
    wcl_scope_usable,
)
from .state import Applicant, Listing
from .wcl_availability import UNUSABLE_AVAILABILITY_STATES


def rio_display_text(applicant: Applicant) -> str:
    if applicant.main_score > applicant.score and applicant.score:
        return f"{applicant.score} [{applicant.main_score}]"
    if applicant.main_score > applicant.score:
        return f"[{applicant.main_score}]"
    return str(applicant.score) if applicant.score else MISSING_DATA_TEXT


def _rio_history_values(applicant: Applicant) -> list[tuple[str, int, int]]:
    histories = (
        ("character", applicant.rio_previous_score, applicant.rio_previous_season),
        ("main", applicant.rio_main_previous_score, applicant.rio_main_previous_season),
        ("warband", applicant.rio_warband_previous_score, applicant.rio_warband_previous_season),
    )
    return [
        (owner, score, season)
        for owner, score, season in histories
        if isinstance(score, int) and not isinstance(score, bool) and score > 0
        and isinstance(season, int) and not isinstance(season, bool) and season >= 0
    ]


def rio_history_text(applicant: Applicant, *, source: str | None = None) -> str:
    parts = []
    for owner, score, season in _rio_history_values(applicant):
        if source is not None and source != owner:
            continue
        prefix = "" if owner == "character" else f"{owner} "
        parts.append(f"{prefix}past S{season + 1} ~{score}")
    return " · ".join(parts)


def rio_table_text(applicant: Applicant) -> str:
    lines = [rio_display_text(applicant)]
    histories = [
        (score, owner == "warband", season)
        for owner, score, season in _rio_history_values(applicant)
    ]
    if histories:
        # Keep the winning score's season; Warband wins equal-score ties.
        score, _, season = max(histories)
        if score >= applicant.score:
            lines.append(f"S{season + 1} {score}")
    return "\n".join(lines)


def rio_panel_text(applicant: Applicant) -> str:
    if applicant.main_score > applicant.score and applicant.score:
        return f"RIO {applicant.score} · main {applicant.main_score}"
    if applicant.main_score > applicant.score:
        return f"RIO main {applicant.main_score}"
    return f"RIO {applicant.score}" if applicant.score else ""


def count_phrase(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


def wcl_scope_status(applicant: Applicant, scope: str) -> str:
    """Keep stale numbers behind the same availability boundary as their source."""
    if applicant.fetch_status != "ready":
        return applicant.fetch_status
    state = applicant.wcl_availability.get(scope)
    if state == "restricted":
        return "restricted"
    if state == "error":
        return "error"
    if not wcl_scope_usable(applicant, scope):
        return "not_found"
    return "ready"


def package_fit_text(package: PackageFit) -> str:
    if package.is_complete:
        return f"G{package.size} ~{int(round(package.score))}"
    state = "partial" if package.available_count else "unknown"
    return f"G{package.size} {state}"


def fit_accessible_text(text: str) -> str:
    if not text or text == MISSING_DATA_TEXT:
        return "Target evidence unknown"
    if text == "…":
        return "Target evidence loading"
    return text.replace("~", "approximately ")


def fit_badge_context_text(fit: CandidateFit) -> str:
    """Keep the evidence scope visible beside the estimate itself."""
    if fit.context == CONTEXT_MPLUS:
        total = len(MPLUS_ENCOUNTERS)
        parts = ([f"WCL {fit.wcl_dungeon_count}/{total}"]
                 if fit.wcl_score is not None else [])
        if fit.wcl_score is not None:
            if fit.wcl_run_count == 1:
                parts.append("1 log")
            elif fit.wcl_dungeon_count == 1 and fit.wcl_run_count is None:
                parts.append("limited WCL")
        if fit.timed_experience_score is not None:
            parts.append(f"timed {fit.dungeon_count}/{total}")
            if fit.wcl_score is None:
                parts.append("no nearby WCL")
        else:
            parts.append("timed unknown")
        return " · ".join(parts)
    if fit.context == CONTEXT_RAID:
        if not fit.raid_total_bosses:
            return "sample unknown"
        prefix = f"{fit.evidence_source} " if fit.evidence_source else ""
        return f"{prefix}{fit.raid_boss_count}/{fit.raid_total_bosses} zone bosses"
    return ""


def fit_evidence_text(fit: CandidateFit) -> str:
    """Describe observed evidence without turning completeness into probability."""
    if fit.context == CONTEXT_MPLUS:
        parts = []
        if fit.wcl_score is not None:
            parts.append(f"{fit.wcl_dungeon_count}/{len(MPLUS_ENCOUNTERS)} nearby WCL dungeons")
            if fit.wcl_key_min > 0:
                key_range = (f"+{fit.wcl_key_min}" if fit.wcl_key_min == fit.wcl_key_max
                             else f"+{fit.wcl_key_min}–{fit.wcl_key_max}")
                parts.append(f"Fit WCL range {key_range}")
            parts.append("WCL sample count unknown" if fit.wcl_run_count is None
                         else count_phrase(fit.wcl_run_count, "WCL sample"))
            if fit.wcl_all_single_run:
                parts.append("Each selected bracket has one recorded run")
            parts.append(f"Contextual WCL component {fit.wcl_score:.1f}/100")
        else:
            parts.append("No nearby WCL logs; timed keys only"
                         if fit.timed_experience_score is not None else "No nearby WCL logs")
        if fit.timed_experience_score is not None:
            parts.extend([
                f"{fit.dungeon_count}/{len(MPLUS_ENCOUNTERS)} dungeons timed",
                f"{fit.relevant_dungeon_count} timed at +{max(fit.target_key - 1, 2)} or higher"
                if fit.target_key else f"{fit.relevant_dungeon_count} relevant timed dungeons",
                f"Timed experience component {fit.timed_experience_score:.1f}/89",
            ])
        else:
            parts.append("Timed history unknown")
    elif fit.context == CONTEXT_RAID:
        parts = (
            [f"{fit.raid_boss_count}/{fit.raid_total_bosses} WCL zone bosses logged",
             count_phrase(fit.raid_kill_count, "recorded kill")
             if fit.raid_kill_count else "Repeat sample count unknown"]
            if fit.raid_total_bosses else ["WCL zone boss coverage and kill count unknown"]
        )
        if fit.evidence_source:
            source = {"N": "Normal", "H": "Heroic", "M": "Mythic"}.get(
                fit.evidence_source, fit.evidence_source,
            )
            parts.append(f"Source difficulty {source}")
    else:
        return fit.evidence_note
    if fit.evidence_note:
        parts.append(fit.evidence_note)
    return " · ".join(parts)


def text_colour_for_bg(bg_hex: str | None) -> str:
    """Choose the stronger black/white contrast for percentile and class badges."""
    if not bg_hex or len(bg_hex) != 7 or not bg_hex.startswith("#"):
        return "#ffffff"
    try:
        red = int(bg_hex[1:3], 16)
        green = int(bg_hex[3:5], 16)
        blue = int(bg_hex[5:7], 16)
    except ValueError:
        return "#ffffff"
    components = [value / 255 for value in (red, green, blue)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in components]
    luminance = sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    black_contrast = (luminance + 0.05) / 0.05
    white_contrast = 1.05 / (luminance + 0.05)
    return "#000000" if black_contrast >= white_contrast else "#ffffff"


def raid_cell_visuals(
    best: object,
    median: object,
    fetch_status: str,
) -> tuple[str, str | None, str | None]:
    if fetch_status in {"pending", "loading"}:
        return "…", "#888", None
    if fetch_status == "error":
        return "?", "#ff5555", None
    if fetch_status in {"not_found", "restricted"}:
        return MISSING_DATA_TEXT, "#5d5d5d", None
    best_pct = safe_percent(best)
    median_pct = safe_percent(median)
    if best_pct is None and median_pct is None:
        return MISSING_DATA_TEXT, "#5d5d5d", None
    best_str = f"{int(best_pct)}" if best_pct is not None else MISSING_DATA_TEXT
    text = (
        best_str
        if median_pct is None
        else f"{best_str}/{int(median_pct)}"
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
    return "" if text == MISSING_DATA_TEXT else text


def raid_fit_evidence_text(
    applicant: Applicant, target: str, source: str, *, selected_difficulty: str = "",
) -> str:
    if applicant.fetch_status != "ready":
        return ""
    order = ["N", "H", "M"]
    target_idx = order.index(target) if target in order else -1
    candidate_keys: list[str] = []
    if selected_difficulty:
        candidate_keys = [selected_difficulty] if selected_difficulty in order else []
    elif source == "raid_exact" and target_idx >= 0:
        candidate_keys = [target]
    elif source == "raid_higher_fallback" and target_idx >= 0:
        candidate_keys = order[target_idx + 1 :]
    elif source == "raid_lower_fallback" and target_idx >= 0:
        candidate_keys = order[:target_idx]
    best_key = ""
    best_score = -1.0
    for key in candidate_keys:
        scope = {"N": "raid_normal", "H": "raid_heroic", "M": "raid_mythic"}[key]
        if wcl_scope_status(applicant, scope) != "ready":
            continue
        best, median = raid_values_for_key(applicant, key)
        score = raid_performance_score(best, median)
        if score is not None and score > best_score:
            best_key = key
            best_score = score
    raw = raid_metric_text_for_key(applicant, best_key)
    if not best_key or not raw:
        return ""
    return raw if source == "raid_exact" else f"{best_key} {raw}"


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
    if wcl_scope_status(applicant, "mplus") != "ready":
        return rows
    for row in mplus_performance_rows(applicant, listing):
        row_key = normalise_dungeon_name(row.dungeon_name)
        rows[row_key] = {
            "name": row.dungeon_name,
            "key_level": row.key_level,
            "text": mplus_metric_display_text(row.best_percent, row.median_percent, row.run_count),
            "colour": row.colour,
            "best_percent": row.best_percent,
            "median_percent": row.median_percent,
            "run_count": row.run_count,
        }
    return rows


def mplus_display_summary(
    applicant: Applicant, listing: Listing | None
) -> MPlusPerformanceSummary:
    if wcl_scope_status(applicant, "mplus") != "ready":
        return MPlusPerformanceSummary()
    return mplus_performance_summary(applicant, listing)


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
    boss_kills_by_difficulty: dict[str, dict[int, int]] = {}
    for key in difficulty_keys:
        progress = applicant.rio_raid_progress.get(key, {})
        boss_kills = progress.get("boss_kills") if isinstance(progress, dict) else None
        encounter_ids = progress.get("boss_encounter_ids") if isinstance(progress, dict) else None
        mapped: dict[int, int] = {}
        if (isinstance(encounter_ids, list) and isinstance(boss_kills, list)
                and len(encounter_ids) == len(boss_kills)):
            for boss_id, count in zip(encounter_ids, boss_kills):
                if (type(boss_id) is not int or boss_id < 0
                        or type(count) is not int or count < 0 or count.bit_length() > 63
                        or boss_id > 0 and boss_id in mapped):
                    mapped = {}
                    break
                if boss_id > 0:
                    mapped[boss_id] = count
        boss_kills_by_difficulty[key] = mapped
    rows: list[dict[str, object]] = []
    for _alias, encounter_id, name in CURRENT_RAID_ENCOUNTERS:
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
            kills = boss_kills.get(encounter_id, 0)
            if kills > 0:
                kill_parts.append(f"{difficulty}×{kills}")
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
    if applicant.raid_boss_availability.get(difficulty) in UNUSABLE_AVAILABILITY_STATES:
        return rows
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
    # Paired-value placeholder keeps the ASCII hyphen: it renders inside
    # committed overlay fixtures, so unifying it needs a baseline refresh.
    left = str(int(overall)) if overall is not None else "-"
    right = str(int(ilvl)) if ilvl is not None else "-"
    return f"{left} / {right}"


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
        return MISSING_DATA_TEXT
    return mplus_metric_display_text(
        entry.get("parse_percent"),
        entry.get("median_percent"),
        entry.get("run_count"),
    )


def mplus_metric_display_text(
    best: object, median: object, run_count: object, *, headline: bool = False
) -> str:
    """Keep summary cells numeric; show sample counts in the dungeon details."""
    # Flooring keeps the displayed percentile in the same band as its raw colour.
    best_pct, median_pct = safe_percent(best), safe_percent(median)
    text = mplus_metric_text(
        int(best_pct) if best_pct is not None else None,
        int(median_pct) if median_pct is not None else None,
        run_count,
    )
    return text.replace(" N=1", "" if headline else " 1 run")


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
