"""Context-aware applicant fit scoring for the current LFG listing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
import math

from .constants import (
    MPLUS_ENCOUNTERS,
    SPEC_ID_TO_WCL_NAME,
    mplus_dungeon_name_for_activity_id,
    percentile_colour,
)
from .state import Applicant, Listing
from .wcl_cache_store import sanitize_raid_evidence
from .wcl_availability import METRIC_SCOPES, UNUSABLE_AVAILABILITY_STATES


CONTEXT_MPLUS = "mplus"
CONTEXT_RAID = "raid"
CONTEXT_UNKNOWN = "unknown"

MPLUS_LIMIT_SCORE_ONLY = "score_only"
MPLUS_LIMIT_NO_RELEVANT_WCL = "no_relevant_wcl"
MPLUS_LIMIT_LOW_WCL = "low_wcl"
MPLUS_LIMIT_WEAK_WCL = "weak_wcl"
MPLUS_LIMIT_BELOW_TARGET = "below_target"
MPLUS_LIMIT_SPARSE_COVERAGE = "sparse_coverage"
MPLUS_LIMIT_NO_SAME_DUNGEON = "no_same_dungeon"

RAID_TARGET_BY_DIFFICULTY_ID = {
    14: "N",
    15: "H",
    16: "M",
}
MPLUS_DUNGEON_COUNT = len(MPLUS_ENCOUNTERS)

MPLUS_SCORE_KEY_MAX = 48.0
MPLUS_SCORE_CARRY_MAX = 34.0
MPLUS_SCORE_SAME_MAX = 3.0
MPLUS_SCORE_CONSISTENCY_MAX = 4.0
MPLUS_TIMED_INDEX_MAX = (
    MPLUS_SCORE_KEY_MAX + MPLUS_SCORE_CARRY_MAX
    + MPLUS_SCORE_SAME_MAX + MPLUS_SCORE_CONSISTENCY_MAX
)
# Logged output remains primary; timed experience corroborates at most 20%
# of the remaining score headroom. This is a heuristic, not a probability.
MPLUS_TIMED_CORROBORATION_MAX = 0.20
MPLUS_LIMIT_NO_TIMED_HISTORY = "no_timed_history"

FIT_LABEL_BUCKETS: list[tuple[float, str]] = [
    (85.0, "TOP"),
    (70.0, "FIT"),
    (50.0, "OK"),
    (0.0, "RISK"),
]


@dataclass(frozen=True)
class CandidateFit:
    context: str = CONTEXT_UNKNOWN
    is_available: bool = False
    score: float = 0.0
    label: str = ""
    source: str = ""
    display: str = ""
    colour: str | None = None
    target_key: int = 0
    primary_key: int = 0
    best_nearby_key: int = 0
    target_raid: str = ""
    confidence: float = 0.0
    coverage: float = 0.0
    has_same_dungeon_context: bool = False
    same_dungeon_rio_key: int = 0
    same_dungeon_wcl_key: int = 0
    same_dungeon_wcl_best: float | None = None
    same_dungeon_wcl_median: float | None = None
    same_dungeon_wcl_run_count: int = 0
    limit_reason: str = ""
    evidence_count: int = 0
    dungeon_count: int = 0
    relevant_dungeon_count: int = 0
    raid_boss_count: int = 0
    raid_total_bosses: int = 0
    raid_kill_count: int = 0
    evidence_note: str = ""
    evidence_source: str = ""
    wcl_score: float | None = None
    timed_experience_score: float | None = None
    wcl_dungeon_count: int = 0
    wcl_key_min: int = 0
    wcl_key_max: int = 0
    wcl_run_count: int | None = None
    wcl_all_single_run: bool = False


@dataclass(frozen=True)
class PackageFit:
    context: str = CONTEXT_UNKNOWN
    is_complete: bool = False
    available_count: int = 0
    unavailable_count: int = 0
    score: float = 0.0
    label: str = ""
    display: str = ""
    colour: str | None = None
    size: int = 0
    confidence: float = 0.0
    high_score: float = 0.0
    average_score: float = 0.0
    low_score: float = 0.0
    spread: float = 0.0
    member_scores: tuple[float, ...] = ()
    member_fits: tuple[CandidateFit, ...] = ()
    status_penalty: float = 0.0
    tank_count: int = 0
    healer_count: int = 0
    dps_count: int = 0
    unknown_role_count: int = 0
    loading_count: int = 0
    error_count: int = 0
    not_found_count: int = 0
    restricted_count: int = 0
    parse_percentile: float | None = None


@dataclass(frozen=True)
class _PackageParams:
    center: float
    scale: float


@dataclass(frozen=True)
class _MPlusWCLSignal:
    dungeon_name: str
    key_level: int
    percentile: float
    median_percent: float | None
    run_count: int
    same_dungeon: bool = False
    normalized_dungeon: tuple[str, str] | None = field(
        default=None, compare=False, repr=False
    )


@dataclass(frozen=True)
class MPlusDungeonFit:
    dungeon_name: str
    key_level: int
    score: float
    text: str
    colour: str
    best_percent: float | None = None
    median_percent: float | None = None
    run_count: int = 0


@dataclass(frozen=True)
class MPlusPerformanceSummary:
    best: float | None = None
    median: float | None = None
    key_min: int = 0
    key_max: int = 0
    all_single_run: bool = False
    target_key: int = 0
    is_contextual: bool = False


def detect_listing_context(listing: Listing | None) -> str:
    if listing is None:
        return CONTEXT_UNKNOWN
    if type(listing.key_level) is int and 2 <= listing.key_level <= 50:
        return CONTEXT_MPLUS
    if listing.difficulty_id in RAID_TARGET_BY_DIFFICULTY_ID:
        return CONTEXT_RAID
    return CONTEXT_UNKNOWN


def effective_rio_score(applicant: Applicant) -> int:
    """Score used for ranking/support: current character or better RaiderIO main."""
    return max(nonnegative_int(applicant.score), nonnegative_int(applicant.main_score))


def candidate_fit(applicant: Applicant, listing: Listing | None) -> CandidateFit:
    context = detect_listing_context(listing)
    if context == CONTEXT_MPLUS and listing is not None:
        return _mplus_candidate_fit(applicant, listing)
    if context == CONTEXT_RAID and listing is not None:
        if applicant.fetch_status != "ready":
            return CandidateFit(
                context=context,
                source=applicant.fetch_status,
                target_raid=RAID_TARGET_BY_DIFFICULTY_ID[listing.difficulty_id],
            )
        return _raid_candidate_fit(applicant, listing)
    return CandidateFit(context=CONTEXT_UNKNOWN)


def wcl_scope_usable(applicant: Applicant, scope: str) -> bool:
    """Apply the same source eligibility to estimates, raw cells and sort keys."""
    canonical = "mplus" if scope in {"mplus_dps", "mplus_hps"} else scope
    if canonical not in METRIC_SCOPES:
        return False
    if any(
        applicant.wcl_availability.get(key) in UNUSABLE_AVAILABILITY_STATES
        for key in {scope, canonical}
    ):
        return False
    preferences = applicant.wcl_metric_preferences
    return preferences is None or getattr(preferences, canonical)


def listing_parse_percentile(
    applicant: Applicant, listing: Listing | None
) -> float | None:
    """Visible best percentile for the listing; unavailable rows never rank from stale data."""
    if applicant.fetch_status != "ready" or listing is None:
        return None
    context = detect_listing_context(listing)
    if context == CONTEXT_MPLUS or (
        context == CONTEXT_UNKNOWN and listing.category_id == 2
    ):
        return (
            mplus_performance_summary(applicant, listing).best
            if wcl_scope_usable(applicant, "mplus")
            else None
        )
    if context == CONTEXT_RAID:
        target = RAID_TARGET_BY_DIFFICULTY_ID.get(listing.difficulty_id, "")
        scope = "raid_" + {"N": "normal", "H": "heroic", "M": "mythic"}[target]
        if not wcl_scope_usable(applicant, scope):
            return None
        return safe_percent(
            {
                "N": applicant.raid_normal,
                "H": applicant.raid_heroic,
                "M": applicant.raid_mythic,
            }.get(target)
        )
    return None


def package_fit(applicants: Iterable[Applicant], listing: Listing | None) -> PackageFit:
    members = list(applicants)
    if not members:
        return PackageFit()
    context = detect_listing_context(listing)
    size = len(members)
    tank_count = sum(member.role == "TANK" for member in members)
    healer_count = sum(member.role == "HEALER" for member in members)
    dps_count = sum(member.role == "DAMAGER" for member in members)
    unknown_role_count = size - tank_count - healer_count - dps_count
    loading_count = sum(
        member.fetch_status in {"pending", "loading"} for member in members
    )
    error_count = sum(member.fetch_status == "error" for member in members)
    not_found_count = sum(member.fetch_status == "not_found" for member in members)
    restricted_count = sum(member.fetch_status == "restricted" for member in members)
    parses = [listing_parse_percentile(member, listing) for member in members]
    # Keep an application together without letting one strong member hide missing
    # or weaker logs. Zero is a real parse; None means incomplete evidence.
    parse_percentile = (
        min(value for value in parses if value is not None)
        if all(value is not None for value in parses)
        else None
    )
    if context == CONTEXT_UNKNOWN:
        member_scores = tuple(float(effective_rio_score(a)) for a in members)
        score = float(max(member_scores, default=0.0))
        low = float(min(member_scores, default=0.0))
        average = sum(member_scores) / len(member_scores)
        return PackageFit(
            context=context,
            score=score,
            label="",
            display="",
            colour=None,
            size=size,
            confidence=0.0,
            high_score=score,
            average_score=average,
            low_score=low,
            spread=score - low,
            member_scores=member_scores,
            status_penalty=0.0,
            tank_count=tank_count,
            healer_count=healer_count,
            dps_count=dps_count,
            unknown_role_count=unknown_role_count,
            loading_count=loading_count,
            error_count=error_count,
            not_found_count=not_found_count,
            restricted_count=restricted_count,
            parse_percentile=parse_percentile,
        )

    fits = tuple(candidate_fit(member, listing) for member in members)
    known = tuple(fit.score for fit in fits if fit.is_available)
    complete = len(known) == size
    scores = tuple(fit.score for fit in fits)
    high = max(known, default=0.0)
    low = min(known, default=0.0)
    average = sum(known) / len(known) if known else 0.0
    # Unknown members have no performance value. Never score a partial package
    # as though they were observed zeros, or subtract an API/privacy penalty.
    score = _package_score_for_member_scores(known, context, 0.0) if complete else 0.0
    score = _clamp(score, 0.0, 100.0)
    display = ""
    if size > 1:
        display = (
            f"G{size} ~{int(round(score))}"
            if complete
            else f"G{size} partial"
            if known
            else f"G{size} unknown"
        )
    return PackageFit(
        context=context,
        is_complete=complete,
        available_count=len(known),
        unavailable_count=size - len(known),
        score=score,
        display=display,
        colour=fit_colour(score) if complete else None,
        size=size,
        confidence=_package_confidence(members, fits),
        high_score=high,
        low_score=low,
        average_score=average,
        spread=high - low,
        member_scores=scores,
        member_fits=fits,
        status_penalty=0.0,
        tank_count=tank_count,
        healer_count=healer_count,
        dps_count=dps_count,
        unknown_role_count=unknown_role_count,
        loading_count=loading_count,
        error_count=error_count,
        not_found_count=not_found_count,
        restricted_count=restricted_count,
        parse_percentile=parse_percentile,
    )


def mplus_dungeon_fit_rows(
    applicant: Applicant, listing: Listing | None
) -> list[MPlusDungeonFit]:
    if detect_listing_context(listing) != CONTEXT_MPLUS or listing is None:
        return []
    _metric_label, breakdown, _best, _median = role_mplus_view(applicant)
    rows: list[MPlusDungeonFit] = []
    for entry in breakdown:
        if not isinstance(entry, dict):
            continue
        dungeon_name = str(entry.get("name") or "?")
        best_row: MPlusDungeonFit | None = None
        window_row: MPlusDungeonFit | None = None
        for bracket in _iter_mplus_brackets(entry):
            fit = _mplus_bracket_fit(bracket, listing.key_level)
            if fit is None:
                continue
            key_level = positive_int(bracket.get("key_level"))
            text = _bracket_metric_text(bracket)
            best_percent = safe_percent(bracket.get("parse_percent"))
            row = MPlusDungeonFit(
                dungeon_name=dungeon_name,
                key_level=key_level,
                score=fit,
                text=text,
                # The row is ordered by context fit, but the badge text is the
                # raw WCL percentile. Keep the colour tied to the printed value.
                colour=percentile_colour(best_percent),
                best_percent=safe_percent(bracket.get("parse_percent")),
                median_percent=safe_percent(bracket.get("median_percent")),
                run_count=nonnegative_int(bracket.get("run_count")),
            )
            if best_row is None or row.score > best_row.score:
                best_row = row
            if abs(key_level - listing.key_level) <= 2 and (
                window_row is None or row.score > window_row.score
            ):
                window_row = row
        if window_row is not None:
            rows.append(window_row)
        elif best_row is not None:
            rows.append(best_row)
    rows.sort(key=lambda row: (-row.score, -row.key_level, row.dungeon_name))
    return rows


def mplus_performance_rows(
    applicant: Applicant, listing: Listing | None
) -> list[MPlusDungeonFit]:
    """One canonical logged bracket per displayed dungeon, shared by all views."""
    if applicant.fetch_status != "ready" or not wcl_scope_usable(applicant, "mplus"):
        return []
    rows = mplus_dungeon_fit_rows(applicant, listing)
    if not rows:
        _label, breakdown, _best, _median = role_mplus_view(applicant)
        entries = sorted(
            [entry for entry in breakdown if isinstance(entry, dict)],
            key=lambda entry: (-positive_int(entry.get("key_level")), str(entry.get("name") or "")),
        )
        for entry in entries:
            name = str(entry.get("name") or "").strip()
            best = safe_percent(entry.get("parse_percent"))
            rows.append(MPlusDungeonFit(
                dungeon_name=name,
                key_level=positive_int(entry.get("key_level")),
                score=best if best is not None else 0.0,
                text=_bracket_metric_text(entry),
                colour=percentile_colour(best) if best is not None else "#2a2a33",
                best_percent=best,
                median_percent=safe_percent(entry.get("median_percent")),
                run_count=nonnegative_int(entry.get("run_count")),
            ))
    # Match the detail table's canonical names and duplicate resolution exactly.
    canonical: dict[str, MPlusDungeonFit] = {}
    for row in rows:
        key = normalise_dungeon_name(row.dungeon_name)
        if key:
            canonical[key] = row
    return list(canonical.values())


def mplus_performance_summary(
    applicant: Applicant, listing: Listing | None
) -> MPlusPerformanceSummary:
    """Contextual damage averages use exactly the displayed dungeon brackets."""
    if applicant.fetch_status != "ready" or not wcl_scope_usable(applicant, "mplus"):
        return MPlusPerformanceSummary()
    rows = [row for row in mplus_performance_rows(applicant, listing)
            if row.best_percent is not None]
    contextual = (
        detect_listing_context(listing) == CONTEXT_MPLUS
        and listing is not None
        and bool(rows)
        and any(row.key_level > 0 for row in rows)
    )
    if not contextual:
        _label, breakdown, best, median = role_mplus_view(applicant)
        highest = max(
            (positive_int(entry.get("key_level")) for entry in breakdown if isinstance(entry, dict)),
            default=0,
        )
        return MPlusPerformanceSummary(
            best=best, median=median, key_min=highest, key_max=highest,
            all_single_run=bool(rows) and all(row.run_count == 1 for row in rows),
        )
    bests = [row.best_percent for row in rows if row.best_percent is not None]
    median = None
    if all(row.run_count >= 2 and row.median_percent is not None for row in rows):
        medians = [row.median_percent for row in rows if row.median_percent is not None]
        median = sum(medians) / len(medians)
    keys = [row.key_level for row in rows if row.key_level > 0]
    return MPlusPerformanceSummary(
        best=sum(bests) / len(bests), median=median,
        key_min=min(keys), key_max=max(keys),
        all_single_run=all(row.run_count == 1 for row in rows),
        target_key=listing.key_level if listing is not None else 0, is_contextual=True,
    )


def fit_label(score: float) -> str:
    for threshold, label in FIT_LABEL_BUCKETS:
        if score >= threshold:
            return label
    return "RISK"


def fit_colour(score: float) -> str:
    """Colour the rounded displayed estimate using the shared rarity bands."""
    if not math.isfinite(score):
        return percentile_colour(None)
    return percentile_colour(round(score))


def listing_dungeon_keys(listing: Listing | None) -> set[str]:
    if listing is None:
        return set()
    return _activity_dungeon_keys(listing.activity_id, listing.dungeon_name)


def _activity_dungeon_keys(activity_id: int, dungeon_name: object) -> set[str]:
    # A known activity owns its dungeon identity; stale/localized display text
    # cannot turn a different dungeon's timed history into target experience.
    mapped_name = mplus_dungeon_name_for_activity_id(activity_id)
    key = normalise_dungeon_name(mapped_name or dungeon_name)
    keys = {key} if key else set()
    # The common canonical-name case needs no scan of unrelated encounters.
    # Only a distinct localized alias requires the conflicting-name check.
    if not mapped_name:
        return keys
    raw_key = normalise_dungeon_name(dungeon_name)
    if raw_key == key:
        return keys
    known_names = {normalise_dungeon_name(name) for _alias, _eid, name in MPLUS_ENCOUNTERS}
    # Keep a localized alias, but never a conflicting known dungeon name.
    if (mapped_name and raw_key not in {"", "mythic", "mythicplus", "unknown", "unknowndungeon"}
            and raw_key not in known_names):
        keys.add(raw_key)
    return keys


def _has_specific_mplus_dungeon(listing: Listing) -> bool:
    if mplus_dungeon_name_for_activity_id(listing.activity_id):
        return True
    name = listing.dungeon_name.strip().casefold()
    return bool(normalise_dungeon_name(name)) and name not in {"mythic+", "mythic plus"}


def _rio_summary_key_level(value: object) -> int:
    # The addon normalizes summary and captured target keys to this domain.
    level = positive_int(value)
    return level if 2 <= level <= 50 else 0


def _rio_summary_profile_available(applicant: Applicant) -> bool:
    # Local enrichment proves its own rows, never an absent raw QR summary.
    raw_flag = applicant.rio_transport_profile
    if (applicant.rio_profile if raw_flag is None else raw_flag) is not True:
        return False
    best = _rio_summary_key_level(applicant.rio_best_key)
    if (best and _rio_captured_dungeon_identity(applicant)
            and _rio_summary_key_level(applicant.rio_best_dungeon_key) > best):
        return False
    captured_target = _rio_summary_key_level(applicant.rio_summary_target_key)
    if best and captured_target:
        floors = (
            (applicant.rio_timed_at_or_above, captured_target),
            (applicant.rio_timed_at_or_above_minus1, max(2, captured_target - 1)),
            (applicant.rio_timed_at_or_above_minus2, max(2, captured_target - 2)),
        )
        # An explicit maximum cannot be below the same capture's timed floors.
        # Missing maxima remain unknown; independent named history stays usable.
        if any(nonnegative_int(count) > 0 and best < floor for count, floor in floors):
            return False
    return True


def _rio_captured_dungeon_key(applicant: Applicant) -> str:
    if not _rio_summary_profile_available(applicant):
        return ""
    return _rio_captured_dungeon_identity(applicant)


def _rio_captured_dungeon_identity(applicant: Applicant) -> str:
    name = (
        mplus_dungeon_name_for_activity_id(
            positive_int(applicant.rio_summary_activity_id)
        )
        or applicant.rio_summary_dungeon_name
    )
    key = normalise_dungeon_name(name)
    return (
        "" if key in {"", "mythic", "mythicplus", "unknown", "unknowndungeon"} else key
    )


def _rio_timed_row_key_level(entry: dict) -> int:
    # Local RIO's six-bit key field is independent of the addon's summary cap.
    if "timed" in entry and entry["timed"] is not True:
        return 0
    for field_name in ("chests", "upgrades"):
        if field_name in entry and positive_int(entry[field_name]) <= 0:
            return 0
    level = positive_int(entry.get("key_level"))
    return level if 2 <= level <= 63 else 0


def _rio_same_dungeon_key(
    applicant: Applicant, listing: Listing, *, rows: dict[str, int] | None = None
) -> int:
    rows = _mplus_rio_dungeon_key_levels(applicant, listing) if rows is None else rows
    return max((rows.get(key, 0) for key in listing_dungeon_keys(listing)), default=0)


def _mplus_candidate_fit(applicant: Applicant, listing: Listing) -> CandidateFit:
    fit = _mplus_scorecard_candidate_fit(applicant, listing)
    return fit or CandidateFit(
        context=CONTEXT_MPLUS,
        target_key=listing.key_level,
        source="rio",
        evidence_note="No comparable applying-spec WCL output or verified in-time key history available.",
    )


def _mplus_scorecard_candidate_fit(
    applicant: Applicant, listing: Listing
) -> CandidateFit | None:
    """Combine comparable applying-spec output with separate timed experience."""
    timed = _mplus_timed_experience_fit(applicant, listing)
    rows = (
        [row for row in mplus_performance_rows(applicant, listing)
         if row.best_percent is not None
         and 2 <= row.key_level <= 63
         and abs(row.key_level - listing.key_level) <= 2]
        if positive_int(applicant.spec_id) in SPEC_ID_TO_WCL_NAME
        else []
    )
    if not rows:
        if timed is None:
            return None
        return replace(
            timed,
            source="mplus_timed_only",
            timed_experience_score=timed.score,
            limit_reason=MPLUS_LIMIT_NO_RELEVANT_WCL,
            evidence_note=(
                "No comparable applying-spec WCL output within two key levels. "
                "This estimate uses verified in-time key experience only."
            ),
        )

    performance = sum(_clamp(row.score, 0.0, 100.0) for row in rows) / len(rows)
    experience = timed.score if timed is not None else None
    score = performance
    if experience is not None:
        score += (
            MPLUS_TIMED_CORROBORATION_MAX * (experience / MPLUS_TIMED_INDEX_MAX)
            * (100.0 - performance)
        )
    base = timed or CandidateFit(context=CONTEXT_MPLUS, target_key=listing.key_level)
    target_dungeons = listing_dungeon_keys(listing)
    same_row = next(
        (row for row in rows if normalise_dungeon_name(row.dungeon_name) in target_dungeons),
        None,
    )
    run_count = sum(row.run_count for row in rows) if all(row.run_count > 0 for row in rows) else None
    display = str(int(round(score)))
    if base.primary_key:
        display += f" +{base.primary_key}"
    return replace(
        base,
        is_available=True,
        score=score,
        source="mplus_combined" if timed is not None else "mplus_wcl_only",
        display=display,
        colour=fit_colour(score),
        wcl_score=performance,
        timed_experience_score=experience,
        wcl_dungeon_count=len(rows),
        wcl_key_min=min(row.key_level for row in rows),
        wcl_key_max=max(row.key_level for row in rows),
        wcl_run_count=run_count,
        wcl_all_single_run=all(row.run_count == 1 for row in rows),
        has_same_dungeon_context=_has_specific_mplus_dungeon(listing),
        same_dungeon_wcl_key=same_row.key_level if same_row is not None else 0,
        same_dungeon_wcl_best=same_row.best_percent if same_row is not None else None,
        same_dungeon_wcl_median=same_row.median_percent if same_row is not None else None,
        same_dungeon_wcl_run_count=same_row.run_count if same_row is not None else 0,
        limit_reason=base.limit_reason if timed is not None else MPLUS_LIMIT_NO_TIMED_HISTORY,
        evidence_source="WCL DPS + RIO in-time keys" if timed is not None else "WCL DPS",
        evidence_note=(
            "Applying-spec WCL output within two key levels is primary; verified in-time "
            "experience corroborates at most 20% of remaining score headroom."
            if timed is not None else
            "Applying-spec WCL output within two key levels; timed-key history is unknown."
        ),
    )


def _mplus_timed_experience_fit(
    applicant: Applicant, listing: Listing
) -> CandidateFit | None:
    """Estimate target readiness from the character's verified in-time RIO keys.

    Logged WCL brackets and general rating cannot establish timer success.
    Anonymous timed summaries are merged as bounds with unknown row overlap.
    """
    target_key = listing.key_level
    if target_key <= 0:
        return None
    rio_dungeons = _mplus_rio_dungeon_key_levels(applicant, listing)
    same_dungeon_key = _rio_same_dungeon_key(applicant, listing, rows=rio_dungeons)
    rio_rows = list(rio_dungeons.values())
    levels = _mplus_rio_key_levels(
        applicant, target_key, rio_row_key_levels=rio_rows, rio_dungeon_key_levels=rio_dungeons
    )
    if not levels:
        return None
    specific = _has_specific_mplus_dungeon(listing)
    primary_key = max(
        max(rio_rows, default=0),
        same_dungeon_key,
        _rio_summary_key_level(applicant.rio_best_key)
        if _rio_summary_profile_available(applicant)
        else 0,
    )
    same_bonus = MPLUS_SCORE_SAME_MAX if same_dungeon_key >= target_key else 0.0
    raw = (
        _mplus_key_readiness_score(levels, target_key)
        + _mplus_carry_bonus(levels, target_key)
        + same_bonus
        + _mplus_key_consistency_score(levels, target_key)
    )
    count = min(MPLUS_DUNGEON_COUNT, len(levels))
    relevant = sum(level >= max(2, target_key - 1) for level in levels)
    coverage = count / max(MPLUS_DUNGEON_COUNT, 1)
    reason = (
        MPLUS_LIMIT_BELOW_TARGET
        if 0 < primary_key < target_key
        else MPLUS_LIMIT_SPARSE_COVERAGE
        if coverage < 1.0
        else MPLUS_LIMIT_NO_SAME_DUNGEON
        if specific and not same_dungeon_key
        else ""
    )
    score = _mplus_display_score(raw)
    return CandidateFit(
        context=CONTEXT_MPLUS,
        is_available=True,
        score=score,
        source="mplus_scorecard",
        display=str(int(round(score))) + (f" +{primary_key}" if primary_key else ""),
        colour=fit_colour(score),
        target_key=target_key,
        primary_key=primary_key,
        best_nearby_key=_mplus_best_nearby_evidence_key(
            applicant=applicant,
            rio_row_key_levels=rio_rows,
            target_key=target_key,
            same_dungeon_key=same_dungeon_key,
        ),
        confidence=coverage,
        coverage=coverage,
        has_same_dungeon_context=specific,
        same_dungeon_rio_key=same_dungeon_key if specific else 0,
        limit_reason=reason,
        dungeon_count=count,
        relevant_dungeon_count=relevant,
        evidence_source="RIO in-time keys",
        evidence_note=(
            "Target-key readiness estimate from verified in-time runs only. "
            "Untimed completions, WCL logs and general rating do not affect Fit."
        ),
    )


def _mplus_wcl_signals(applicant: Applicant, listing: Listing) -> list[_MPlusWCLSignal]:
    _metric_label, breakdown, _best, _median = role_mplus_view(applicant)
    target_dungeon_keys = listing_dungeon_keys(listing)
    signals: list[_MPlusWCLSignal] = []
    for entry in breakdown:
        if not isinstance(entry, dict):
            continue
        dungeon_name = entry.get("name")
        if not isinstance(dungeon_name, str):
            continue
        normalised_name = normalise_dungeon_name(dungeon_name)
        if not normalised_name:
            continue
        normalized_dungeon = (dungeon_name, normalised_name)
        for bracket in _iter_mplus_brackets(entry):
            key_level = positive_int(bracket.get("key_level"))
            percentile = safe_percent(bracket.get("parse_percent"))
            if key_level <= 0 or percentile is None:
                continue
            signals.append(
                _MPlusWCLSignal(
                    dungeon_name=dungeon_name,
                    key_level=key_level,
                    percentile=percentile,
                    median_percent=safe_percent(bracket.get("median_percent")),
                    run_count=nonnegative_int(bracket.get("run_count")),
                    same_dungeon=normalised_name in target_dungeon_keys,
                    normalized_dungeon=normalized_dungeon,
                )
            )
    return signals


def _mplus_representative_wcl_signals(
    signals: list[_MPlusWCLSignal],
    _target_key: int,
) -> list[_MPlusWCLSignal]:
    """Show strongest logged key per dungeon without changing experience by DPS."""
    by_dungeon: dict[str, _MPlusWCLSignal] = {}
    for signal in signals:
        dungeon = _mplus_wcl_dungeon_key(signal)
        if dungeon is None:
            continue
        old = by_dungeon.get(dungeon)
        if old is None or (signal.key_level, signal.run_count, signal.percentile) > (
            old.key_level,
            old.run_count,
            old.percentile,
        ):
            by_dungeon[dungeon] = signal
    return list(by_dungeon.values())


def _mplus_best_nearby_evidence_key(
    *,
    applicant: Applicant,
    rio_row_key_levels: list[int],
    target_key: int,
    same_dungeon_key: int,
) -> int:
    actual_keys = list(rio_row_key_levels)
    if _rio_summary_profile_available(applicant):
        actual_keys.extend(
            (
                _rio_summary_key_level(applicant.rio_best_key),
                positive_int(same_dungeon_key),
            )
        )
    return max(
        (level for level in actual_keys if abs(level - target_key) <= 2),
        default=0,
    )


def _mplus_rio_dungeon_key_levels(
    applicant: Applicant, listing: Listing | None
) -> dict[str, int]:
    by_dungeon: dict[str, int] = {}
    for entry in applicant.rio_dungeons:
        if not isinstance(entry, dict):
            continue
        dungeon_key = normalise_dungeon_name(entry.get("name"))
        if not dungeon_key:
            continue
        level = _rio_timed_row_key_level(entry)
        if level <= 0:
            continue
        by_dungeon[dungeon_key] = max(by_dungeon.get(dungeon_key, 0), level)
    captured_key = _rio_captured_dungeon_key(applicant)
    captured_level = _rio_summary_key_level(applicant.rio_best_dungeon_key)
    if captured_key:
        captured_aliases = _activity_dungeon_keys(
            positive_int(applicant.rio_summary_activity_id), applicant.rio_summary_dungeon_name,
        ) - {captured_key}
        for alias in captured_aliases:
            if alias in by_dungeon:
                by_dungeon[captured_key] = max(
                    by_dungeon.get(captured_key, 0), by_dungeon.pop(alias),
                )
    if captured_key and captured_level:
        by_dungeon[captured_key] = max(by_dungeon.get(captured_key, 0), captured_level)
    if listing is not None and _has_specific_mplus_dungeon(listing):
        target_name = (
            mplus_dungeon_name_for_activity_id(listing.activity_id)
            or listing.dungeon_name
        )
        target_key = normalise_dungeon_name(target_name)
        for alias in listing_dungeon_keys(listing) - {target_key}:
            if alias in by_dungeon:
                by_dungeon[target_key] = max(
                    by_dungeon.get(target_key, 0), by_dungeon.pop(alias)
                )
    return by_dungeon


def _mplus_rio_key_levels(
    applicant: Applicant,
    target_key: int,
    *,
    rio_row_key_levels: list[int],
    rio_dungeon_key_levels: dict[str, int] | None = None,
) -> list[int]:
    synthetic = (
        _mplus_synthetic_rio_key_levels(
            applicant,
            target_key,
            rio_dungeon_key_levels=rio_dungeon_key_levels,
        )
        if _rio_summary_profile_available(applicant)
        else []
    )
    if not synthetic:
        return sorted(rio_row_key_levels, reverse=True)[:MPLUS_DUNGEON_COUNT]
    if not rio_row_key_levels:
        return synthetic

    return _mplus_merge_anonymous_key_bounds(synthetic, rio_row_key_levels)


def _mplus_merge_anonymous_key_bounds(left: list[int], right: list[int]) -> list[int]:
    """Keep each strongest guaranteed slot without assuming disjoint identities."""
    left = sorted((level for level in left if level > 0), reverse=True)
    right = sorted((level for level in right if level > 0), reverse=True)
    count = min(MPLUS_DUNGEON_COUNT, max(len(left), len(right)))
    return [
        max(
            left[index] if index < len(left) else 0,
            right[index] if index < len(right) else 0,
        )
        for index in range(count)
    ]


def _mplus_synthetic_rio_key_levels(
    applicant: Applicant,
    target_key: int,
    *,
    rio_dungeon_key_levels: dict[str, int] | None = None,
) -> list[int]:
    if not _rio_summary_profile_available(applicant) or target_key <= 0:
        return []
    captured_target = _rio_summary_key_level(applicant.rio_summary_target_key)
    dungeon_count = min(
        positive_int(applicant.rio_dungeon_count) or MPLUS_DUNGEON_COUNT,
        MPLUS_DUNGEON_COUNT,
    )
    levels: list[int] = []
    if captured_target:
        timed_at = min(nonnegative_int(applicant.rio_timed_at_or_above), dungeon_count)
        timed_minus1 = min(
            max(nonnegative_int(applicant.rio_timed_at_or_above_minus1), timed_at),
            dungeon_count,
        )
        timed_minus2 = min(
            max(nonnegative_int(applicant.rio_timed_at_or_above_minus2), timed_minus1),
            dungeon_count,
        )
        # The floors belong to the recorded target, never the requested one.
        levels = [captured_target] * timed_at
        levels.extend([max(2, captured_target - 1)] * (timed_minus1 - timed_at))
        levels.extend([max(2, captured_target - 2)] * (timed_minus2 - timed_minus1))

    best_key = _rio_summary_key_level(applicant.rio_best_key)
    captured_dungeon = _rio_captured_dungeon_key(applicant)
    captured_same = (
        _rio_summary_key_level(applicant.rio_best_dungeon_key)
        if captured_dungeon
        else 0
    )
    named_rows = (
        _mplus_rio_dungeon_key_levels(applicant, None)
        if rio_dungeon_key_levels is None else rio_dungeon_key_levels
    )
    same_bound = (
        max(captured_same, named_rows.get(captured_dungeon, 0))
        if captured_dungeon
        else 0
    )
    # A higher global best in the ORIGINAL capture proves a different dungeon.
    # Updating that captured dungeon locally must not erase the other run.
    named_bounds = (
        [best_key, same_bound]
        if best_key > captured_same > 0
        else [max(best_key, same_bound)]
    )
    return _mplus_merge_anonymous_key_bounds(levels, named_bounds)


def _mplus_key_readiness_score(key_levels: list[int], target_key: int) -> float:
    if target_key <= 0:
        return 0.0
    values = [
        _mplus_key_delta_value(level - target_key)
        for level in sorted(key_levels, reverse=True)[:MPLUS_DUNGEON_COUNT]
        if level > 0
    ]
    if len(values) < MPLUS_DUNGEON_COUNT:
        values.extend([0.0] * (MPLUS_DUNGEON_COUNT - len(values)))
    return MPLUS_SCORE_KEY_MAX * (sum(values) / max(MPLUS_DUNGEON_COUNT, 1))


def _mplus_key_delta_value(delta: int) -> float:
    if delta >= 0:
        return 1.0
    if delta == -1:
        return 0.80
    if delta == -2:
        return 0.45
    if delta == -3:
        return 0.22
    if delta == -4:
        return 0.10
    return 0.0


def _mplus_key_consistency_score(key_levels: list[int], target_key: int) -> float:
    if target_key <= 0 or not key_levels:
        return 0.0
    count = sum(1 for level in key_levels[:MPLUS_DUNGEON_COUNT] if level >= target_key)
    return MPLUS_SCORE_CONSISTENCY_MAX * _clamp(
        count / max(MPLUS_DUNGEON_COUNT, 1), 0.0, 1.0
    )


def _mplus_carry_bonus(key_levels: list[int], target_key: int) -> float:
    if target_key <= 0 or not key_levels:
        return 0.0
    deltas = sorted((level - target_key for level in key_levels), reverse=True)
    top3 = deltas[:3]
    if not top3:
        return 0.0
    top3_strength = sum(_clamp(delta / 4.0, 0.0, 1.0) for delta in top3) / 3.0
    near_coverage = sum(1 for level in key_levels if level >= target_key - 1) / max(
        MPLUS_DUNGEON_COUNT, 1
    )
    return MPLUS_SCORE_CARRY_MAX * top3_strength * _clamp(near_coverage, 0.0, 1.0)


def _mplus_wcl_dungeon_key_levels(
    signals: list[_MPlusWCLSignal],
    _target_key: int,
) -> dict[str, int]:
    by_dungeon: dict[str, int] = {}
    for signal in signals:
        dungeon = _mplus_wcl_dungeon_key(signal)
        if dungeon is not None:
            by_dungeon[dungeon] = max(by_dungeon.get(dungeon, 0), signal.key_level)
    return by_dungeon


def _mplus_wcl_dungeon_key(signal: _MPlusWCLSignal) -> str | None:
    normalized_dungeon = signal.normalized_dungeon
    if normalized_dungeon is None or normalized_dungeon[0] != signal.dungeon_name:
        dungeon_key = normalise_dungeon_name(signal.dungeon_name)
    else:
        dungeon_key = normalized_dungeon[1]
    if not dungeon_key or signal.key_level <= 0:
        return None
    return dungeon_key


def _mplus_display_score(raw_score: float) -> float:
    clean = _clamp(raw_score, 0.0, 160.0)
    if clean <= 92.0:
        return clean
    return _clamp(100.0 - 8.0 * math.exp(-(clean - 92.0) / 20.0), 0.0, 100.0)


def _raid_candidate_fit(applicant: Applicant, listing: Listing) -> CandidateFit:
    target = RAID_TARGET_BY_DIFFICULTY_ID[listing.difficulty_id]
    if positive_int(applicant.spec_id) not in SPEC_ID_TO_WCL_NAME:
        return CandidateFit(
            context=CONTEXT_RAID,
            target_raid=target,
            evidence_note="Applying specialization is unknown.",
        )
    raid = {
        "N": raid_performance_score(
            applicant.raid_normal, applicant.raid_normal_median
        ),
        "H": raid_performance_score(
            applicant.raid_heroic, applicant.raid_heroic_median
        ),
        "M": raid_performance_score(
            applicant.raid_mythic, applicant.raid_mythic_median
        ),
    }
    order = ["N", "H", "M"]
    index = order.index(target)
    candidates: list[tuple[float, str, str]] = []
    for difficulty, value in raid.items():
        scope = "raid_" + {"N": "normal", "H": "heroic", "M": "mythic"}[difficulty]
        if value is None or not wcl_scope_usable(applicant, scope):
            continue
        position = order.index(difficulty)
        factor = 1.0 if position == index else (0.88 if position > index else 0.55)
        source = (
            "raid_exact"
            if position == index
            else ("raid_higher_fallback" if position > index else "raid_lower_fallback")
        )
        candidates.append((value * factor, difficulty, source))
    if not candidates:
        return CandidateFit(
            context=CONTEXT_RAID,
            target_raid=target,
            source="raid_missing",
            evidence_note="No raid performance evidence available.",
        )
    # A small exact sample cannot discard stronger evidence from another
    # difficulty. Output and observed coverage are separate, not a skill verdict.
    score, difficulty, source = max(
        candidates, key=lambda row: (row[0], row[1] == target)
    )
    evidence = sanitize_raid_evidence(applicant.raid_evidence).get(difficulty, {})
    boss_count = nonnegative_int(evidence.get("boss_count"))
    total = nonnegative_int(evidence.get("total_bosses"))
    kills = (
        nonnegative_int(evidence.get("kill_count"))
        if evidence.get("sample_known") is True
        else 0
    )
    coverage = _clamp(boss_count / total, 0.0, 1.0) if total else 0.0
    confidence = (
        coverage * min(kills / max(boss_count * 3, 1), 1.0) if kills else coverage * 0.5
    )
    note = "Raid output estimate; mechanics and survival are not measured."
    if not total:
        note += " Boss coverage and repeat sample size are unknown."
    elif not kills:
        note += " Repeat sample size is unknown."
    return CandidateFit(
        context=CONTEXT_RAID,
        is_available=True,
        score=score,
        source=source,
        display=f"{target} ~{int(round(score))}",
        colour=fit_colour(score),
        target_raid=target,
        confidence=confidence,
        coverage=coverage,
        raid_boss_count=boss_count,
        raid_total_bosses=total,
        raid_kill_count=kills,
        evidence_source=difficulty,
        evidence_note=note,
    )


def role_mplus_view(
    applicant: Applicant,
) -> tuple[str, list[dict], float | None, float | None]:
    return (
        "DPS",
        applicant.mplus_dps_breakdown,
        safe_percent(applicant.mplus_dps),
        safe_percent(applicant.mplus_dps_median),
    )


def _iter_mplus_brackets(entry: dict) -> Iterable[dict]:
    raw_brackets = entry.get("brackets")
    if isinstance(raw_brackets, list) and raw_brackets:
        for raw in raw_brackets:
            if isinstance(raw, dict):
                yield raw
        return
    yield entry


def _mplus_bracket_fit(bracket: dict, target_key: int) -> float | None:
    key_level = positive_int(bracket.get("key_level"))
    best = safe_percent(bracket.get("parse_percent"))
    if key_level <= 0 or target_key <= 0 or best is None:
        return None
    median = safe_percent(bracket.get("median_percent"))
    run_count = nonnegative_int(bracket.get("run_count"))
    delta = key_level - target_key
    score = _mplus_performance_score(best, median, run_count) + _key_delta_bonus(delta)
    return _clamp(score, 0.0, _lower_key_fit_cap(delta))


def _mplus_performance_score(
    best: float, median: float | None, run_count: int
) -> float:
    score = best if median is None else best * 0.60 + median * 0.40
    if score >= 50.0:
        if run_count <= 1:
            score -= min(8.0, (score - 50.0) * 0.12)
        elif run_count == 2:
            score -= min(4.0, (score - 50.0) * 0.06)
    return score


def _key_delta_bonus(delta: int) -> float:
    if delta <= -6:
        return -70.0
    if delta == -5:
        return -55.0
    bonuses = {
        -4: -42.0,
        -3: -30.0,
        -2: -18.0,
        -1: -8.0,
        0: 0.0,
        1: 7.0,
        2: 14.0,
        3: 20.0,
        4: 26.0,
        5: 32.0,
    }
    if delta in bonuses:
        return bonuses[delta]
    return 32.0 + min(28.0, (delta - 5) * 6.0)


def _lower_key_fit_cap(delta: int) -> float:
    if delta <= -6:
        return 18.0
    caps = {
        -5: 25.0,
        -4: 32.0,
        -3: 40.0,
        -2: 50.0,
        -1: 72.0,
    }
    return caps.get(delta, 105.0)


def _package_params(context: str) -> _PackageParams:
    if context == CONTEXT_RAID:
        return _PackageParams(
            center=52.0,
            scale=15.0,
        )
    return _PackageParams(
        center=58.0,
        scale=11.0,
    )


def _package_score_for_member_scores(
    scores: tuple[float, ...], context: str, status_penalty: float
) -> float:
    if not scores:
        return 0.0
    if len(scores) == 1:
        return scores[0]
    # Preserve equal inputs exactly at display/colour rounding boundaries.
    if all(score == scores[0] for score in scores[1:]):
        return scores[0] - status_penalty
    params = _package_params(context)
    # A nonlinear mean preserves weak-member influence. The transformed
    # values describe the aggregation shape; they are not calibrated success
    # probabilities or a model of party coordination.
    probabilities = [
        _sigmoid((score - params.center) / params.scale) for score in scores
    ]
    group_probability = _geometric_mean_probability(probabilities)
    base = params.center + params.scale * _logit(group_probability)
    # The members' experience is the evidence. Package size alone does not
    # establish extra capability or coordination, so identical scores stay
    # identical rather than earning a speculative carry bonus.
    return base - status_penalty


def _package_confidence(
    members: Iterable[Applicant], fits: Iterable[CandidateFit]
) -> float:
    pairs = list(zip(members, fits, strict=True))
    if any(not fit.is_available for _, fit in pairs):
        return 0.0
    return min((fit.confidence for _, fit in pairs), default=0.0)


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _logit(probability: float) -> float:
    clean = _clamp(probability, 1e-6, 1.0 - 1e-6)
    return math.log(clean / (1.0 - clean))


def _geometric_mean_probability(probabilities: Iterable[float]) -> float:
    values = [_clamp(probability, 1e-6, 1.0 - 1e-6) for probability in probabilities]
    if not values:
        return 1e-6
    return math.exp(sum(math.log(value) for value in values) / len(values))


def raid_performance_score(best: float | None, median: float | None) -> float | None:
    """Weight raid evidence consistently for fit scoring and its explanation."""
    clean_best = safe_percent(best)
    clean_median = safe_percent(median)
    if clean_best is None and clean_median is None:
        return None
    if clean_best is None:
        return clean_median * 0.85 if clean_median is not None else None
    if clean_median is None:
        return clean_best * 0.90
    return clean_best * 0.55 + clean_median * 0.45


def mplus_metric_text(best: object, median: object, run_count: object) -> str:
    best_pct = safe_percent(best)
    if best_pct is None:
        return "—"
    median_pct = safe_percent(median)
    count = nonnegative_int(run_count)
    best_text = f"{int(round(best_pct))}"
    if count >= 2 and median_pct is not None:
        return f"{best_text}/{int(round(median_pct))}"
    if count == 1:
        return f"{best_text} N=1"
    return best_text


def _bracket_metric_text(bracket: dict) -> str:
    return mplus_metric_text(
        bracket.get("parse_percent"),
        bracket.get("median_percent"),
        bracket.get("run_count"),
    )


def safe_percent(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        pct = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(pct) or pct < 0.0 or pct > 100.0:
        return None
    return pct


def nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value >= 0 and value.bit_length() <= 63 else 0
    if isinstance(value, str) and len(value) <= 19 and value.isdecimal():
        try:
            parsed = int(value)
        except (ValueError, OverflowError):
            return 0
        return parsed if parsed.bit_length() <= 63 else 0
    return 0


def positive_int(value: object) -> int:
    parsed = nonnegative_int(value)
    return parsed if parsed > 0 else 0


def normalise_dungeon_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _clamp(value: float, low: float, high: float) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))
