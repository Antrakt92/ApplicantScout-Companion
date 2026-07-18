"""Context-aware applicant fit scoring for the current LFG listing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import math

from .constants import (
    MPLUS_ENCOUNTERS,
    mplus_dungeon_name_for_activity_id,
    percentile_colour,
)
from .state import Applicant, Listing


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
MPLUS_SCORE_COMPLETION_MAX = 4.0
MPLUS_SCORE_WCL_POSITIVE_MAX = 36.0
MPLUS_SCORE_WCL_GRAY_COMPLETION_MAX = 18.0
MPLUS_SCORE_WCL_GRAY_MAX = 4.0
MPLUS_SCORE_WCL_BAD_MAX = 42.0
MPLUS_SCORE_LOW_KEY_SIGNAL_WEIGHT = 0.12

FIT_LABEL_BUCKETS: list[tuple[float, str]] = [
    (85.0, "TOP"),
    (70.0, "FIT"),
    (50.0, "OK"),
    (0.0, "RISK"),
]


@dataclass(frozen=True)
class CandidateFit:
    context: str = CONTEXT_UNKNOWN
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
    same_dungeon_score: float = 0.0
    has_same_dungeon_context: bool = False
    same_dungeon_rio_key: int = 0
    same_dungeon_wcl_key: int = 0
    same_dungeon_wcl_best: float | None = None
    same_dungeon_wcl_median: float | None = None
    same_dungeon_wcl_run_count: int = 0
    limit_reason: str = ""


@dataclass(frozen=True)
class PackageFit:
    context: str = CONTEXT_UNKNOWN
    score: float = 0.0
    label: str = ""
    display: str = ""
    colour: str | None = None
    size: int = 0
    confidence: float = 0.0
    high_score: float = 0.0
    best_score: float = 0.0
    average_score: float = 0.0
    low_score: float = 0.0
    worst_score: float = 0.0
    spread: float = 0.0
    member_scores: tuple[float, ...] = ()
    status_penalty: float = 0.0
    tank_count: int = 0
    healer_count: int = 0
    dps_count: int = 0
    unknown_role_count: int = 0
    loading_count: int = 0
    error_count: int = 0
    not_found_count: int = 0


@dataclass(frozen=True)
class _PackageParams:
    center: float
    scale: float
    carry_threshold: float
    carry_coeff: float
    carry_cap: float
    low_carry_floor: float


@dataclass(frozen=True)
class _MPlusWCLSignal:
    dungeon_name: str
    key_level: int
    percentile: float
    median_percent: float | None
    run_count: int
    same_dungeon: bool = False


@dataclass(frozen=True)
class MPlusDungeonFit:
    dungeon_name: str
    key_level: int
    score: float
    text: str
    colour: str


def detect_listing_context(listing: Listing | None) -> str:
    if listing is None:
        return CONTEXT_UNKNOWN
    if listing.key_level > 0:
        return CONTEXT_MPLUS
    if listing.difficulty_id in RAID_TARGET_BY_DIFFICULTY_ID:
        return CONTEXT_RAID
    return CONTEXT_UNKNOWN


def effective_rio_score(applicant: Applicant) -> int:
    """Score used for ranking/support: current character or better RaiderIO main."""
    return max(applicant.score, applicant.main_score)


def candidate_fit(applicant: Applicant, listing: Listing | None) -> CandidateFit:
    context = detect_listing_context(listing)
    if context == CONTEXT_MPLUS and listing is not None:
        if _is_terminal_fetch_status(applicant.fetch_status):
            rio_fit = _mplus_terminal_scorecard_candidate_fit(applicant, listing)
            if rio_fit is not None:
                return rio_fit
            return CandidateFit(
                context=CONTEXT_MPLUS,
                score=0.0,
                label=fit_label(0.0),
                source=applicant.fetch_status,
                display="",
                colour=fit_colour(0.0),
                target_key=listing.key_level,
                confidence=0.0,
            )
        return _mplus_candidate_fit(applicant, listing)
    if context == CONTEXT_RAID and listing is not None:
        if _is_terminal_fetch_status(applicant.fetch_status):
            return CandidateFit(
                context=CONTEXT_RAID,
                score=0.0,
                label=fit_label(0.0),
                source=applicant.fetch_status,
                display="",
                colour=fit_colour(0.0),
                target_raid=RAID_TARGET_BY_DIFFICULTY_ID.get(listing.difficulty_id, ""),
                confidence=0.0,
            )
        return _raid_candidate_fit(applicant, listing)
    return CandidateFit(context=CONTEXT_UNKNOWN)


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
            best_score=score,
            average_score=average,
            low_score=low,
            worst_score=low,
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
        )

    fits = [candidate_fit(member, listing) for member in members]
    scores = tuple(fit.score for fit in fits)
    high = max(scores)
    low = min(scores)
    average = sum(scores) / len(scores)
    spread = high - low
    confidence = _package_confidence(members, fits)
    status_penalty = _package_status_penalty(members)
    if size == 1:
        score = high
        display = ""
    else:
        score = _package_score_for_member_scores(scores, context, status_penalty)
        display = ""
    score = _clamp(score, 0.0, 100.0 if context == CONTEXT_MPLUS else 105.0)
    label = "" if context == CONTEXT_MPLUS else fit_label(score)
    if size > 1:
        if context == CONTEXT_MPLUS:
            display = f"G{size} {int(round(score))}"
        else:
            display = f"G{size} {label} {int(round(score))}"
    return PackageFit(
        context=context,
        score=score,
        label=label,
        display=display,
        colour=fit_colour(score),
        size=size,
        confidence=confidence,
        high_score=high,
        best_score=high,
        average_score=average,
        low_score=low,
        worst_score=low,
        spread=spread,
        member_scores=scores,
        status_penalty=status_penalty,
        tank_count=tank_count,
        healer_count=healer_count,
        dps_count=dps_count,
        unknown_role_count=unknown_role_count,
        loading_count=loading_count,
        error_count=error_count,
        not_found_count=not_found_count,
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


def fit_label(score: float) -> str:
    for threshold, label in FIT_LABEL_BUCKETS:
        if score >= threshold:
            return label
    return "RISK"


def fit_colour(score: float) -> str:
    return percentile_colour(score)


def _is_terminal_fetch_status(status: str) -> bool:
    return status in {"error", "not_found"}


def listing_dungeon_keys(listing: Listing | None) -> set[str]:
    if listing is None:
        return set()
    keys = {_normalise_name(listing.dungeon_name)}
    mapped_name = mplus_dungeon_name_for_activity_id(listing.activity_id)
    if mapped_name:
        keys.add(_normalise_name(mapped_name))
    return {key for key in keys if key}


def _has_specific_mplus_dungeon(listing: Listing) -> bool:
    if mplus_dungeon_name_for_activity_id(listing.activity_id):
        return True
    name = listing.dungeon_name.strip().casefold()
    return name not in {"", "?", "mythic+", "mythic plus"}


def _rio_same_dungeon_key(applicant: Applicant, listing: Listing) -> int:
    listing_keys = listing_dungeon_keys(listing)
    best_key = (
        positive_int(applicant.rio_best_dungeon_key)
        if _mplus_rio_summary_matches_target(applicant, listing.key_level)
        else 0
    )
    for entry in applicant.rio_dungeons:
        if not isinstance(entry, dict):
            continue
        row_key = _normalise_name(entry.get("name"))
        if row_key not in listing_keys:
            continue
        best_key = max(best_key, positive_int(entry.get("key_level")))
    return best_key


def _mplus_terminal_scorecard_candidate_fit(
    applicant: Applicant, listing: Listing
) -> CandidateFit | None:
    return _mplus_scorecard_candidate_fit(
        applicant, listing, ignore_wcl=True, allow_score_fallback=False
    )


def _mplus_candidate_fit(applicant: Applicant, listing: Listing) -> CandidateFit:
    fit = _mplus_scorecard_candidate_fit(
        applicant, listing, ignore_wcl=False, allow_score_fallback=True
    )
    if fit is not None:
        return fit
    return CandidateFit(
        context=CONTEXT_MPLUS,
        score=0.0,
        label="",
        source="mplus_scorecard",
        display="",
        colour=fit_colour(0.0),
        target_key=listing.key_level,
        confidence=0.0,
    )


def _mplus_scorecard_candidate_fit(
    applicant: Applicant,
    listing: Listing,
    *,
    ignore_wcl: bool,
    allow_score_fallback: bool,
) -> CandidateFit | None:
    target_key = listing.key_level
    if target_key <= 0:
        return None
    same_dungeon_key = _rio_same_dungeon_key(applicant, listing)
    rio_row_key_levels = _mplus_rio_row_key_levels(applicant)
    rio_key_levels = _mplus_rio_key_levels(applicant, target_key, same_dungeon_key)
    wcl_signals = [] if ignore_wcl else _mplus_wcl_signals(applicant, listing)

    if not rio_key_levels and not wcl_signals and not allow_score_fallback:
        return None
    if not rio_key_levels and not wcl_signals and effective_rio_score(applicant) <= 0:
        return None

    wcl_key_levels = _mplus_wcl_key_levels(wcl_signals, target_key)
    primary_key = _mplus_primary_key(
        rio_key_levels=rio_key_levels,
        rio_row_key_levels=rio_row_key_levels,
        wcl_signals=wcl_signals,
        target_key=target_key,
        same_dungeon_key=same_dungeon_key,
        summary_best_key=positive_int(applicant.rio_best_key)
        if _mplus_rio_summary_matches_target(applicant, target_key)
        else 0,
    )

    completion_key_levels = _mplus_completion_key_levels(
        rio_key_levels, wcl_key_levels
    )
    best_nearby_key = _mplus_best_nearby_evidence_key(
        applicant=applicant,
        rio_row_key_levels=rio_row_key_levels,
        wcl_signals=wcl_signals,
        target_key=target_key,
        same_dungeon_key=same_dungeon_key,
    )
    key_score = _mplus_key_readiness_score(completion_key_levels, target_key)
    has_clean_same_dungeon_wcl = any(
        signal.same_dungeon
        and signal.key_level >= target_key
        and _mplus_wcl_quality_percent(signal) >= 50.0
        for signal in wcl_signals
    )
    same_bonus = (
        MPLUS_SCORE_SAME_MAX
        if same_dungeon_key >= target_key or has_clean_same_dungeon_wcl
        else 0.0
    )
    consistency = _mplus_key_consistency_score(completion_key_levels, target_key)
    completion_experience = _mplus_completion_experience_score(applicant, target_key)
    carry = _mplus_carry_bonus(completion_key_levels, target_key)
    wcl_positive = _mplus_wcl_positive_score(wcl_signals, target_key)
    wcl_gray_completion = _mplus_wcl_gray_completion_score(
        wcl_signals, target_key, rio_key_levels
    )
    representative_wcl_signals = _mplus_representative_wcl_signals(
        wcl_signals, target_key
    )
    has_same_dungeon_context = _has_specific_mplus_dungeon(listing)
    same_dungeon_signal = (
        _mplus_same_dungeon_representative(representative_wcl_signals, target_key)
        if has_same_dungeon_context
        else None
    )
    wcl_gray_penalty = _mplus_wcl_gray_penalty(representative_wcl_signals, target_key)
    wcl_bad_penalty = _mplus_wcl_bad_penalty(representative_wcl_signals, target_key)
    readiness_score = (
        key_score + carry + same_bonus + consistency + completion_experience
    )
    unpenalized_score = readiness_score + wcl_positive + wcl_gray_completion
    raw_score = unpenalized_score - wcl_gray_penalty - wcl_bad_penalty
    raw_score = max(
        raw_score,
        _mplus_rio_completion_risk_floor(
            applicant, rio_key_levels, target_key, readiness_score
        ),
    )
    applied_wcl_penalty = max(0.0, unpenalized_score - raw_score)

    has_relevant_wcl = any(
        signal.key_level - target_key >= -1 for signal in wcl_signals
    )
    no_relevant_wcl_reduction = 0.0
    if not has_relevant_wcl:
        capped_score = min(
            raw_score, _mplus_no_relevant_wcl_cap(completion_key_levels, target_key)
        )
        no_relevant_wcl_reduction = max(0.0, raw_score - capped_score)
        raw_score = capped_score
    score_only_fallback = (
        not completion_key_levels and not wcl_signals and allow_score_fallback
    )
    if score_only_fallback:
        raw_score = min(
            _mplus_rio_fit(effective_rio_score(applicant), target_key) * 0.40,
            42.0,
        )
    score = _mplus_display_score(raw_score)

    clean_wcl_dungeon_count = len(_mplus_clean_wcl_dungeon_keys(wcl_signals))
    coverage = _clamp(
        max(
            len([level for level in rio_key_levels if level > 0]),
            clean_wcl_dungeon_count,
        )
        / max(MPLUS_DUNGEON_COUNT, 1),
        0.0,
        1.0,
    )
    total_runs = _mplus_wcl_total_runs(wcl_signals)
    confidence = _clamp(
        0.30 + 0.45 * coverage + 0.25 * min(total_runs / 16.0, 1.0),
        0.0,
        1.0,
    )
    has_same_dungeon_evidence = bool(
        has_same_dungeon_context and (same_dungeon_key > 0 or same_dungeon_signal)
    )
    limit_reason = _mplus_limit_reason(
        score_only_fallback=score_only_fallback,
        no_relevant_wcl_reduction=no_relevant_wcl_reduction,
        applied_wcl_penalty=applied_wcl_penalty,
        wcl_bad_penalty=wcl_bad_penalty,
        wcl_gray_penalty=wcl_gray_penalty,
        primary_key=primary_key,
        target_key=target_key,
        coverage=coverage,
        has_same_dungeon_context=has_same_dungeon_context,
        has_same_dungeon_evidence=has_same_dungeon_evidence,
    )
    same_dungeon_score = max(
        (
            _mplus_wcl_single_positive_score(signal, target_key)
            for signal in wcl_signals
            if signal.same_dungeon
        ),
        default=0.0,
    )
    display = str(int(round(score)))
    if primary_key > 0:
        display = f"{display} +{primary_key}"
    return CandidateFit(
        context=CONTEXT_MPLUS,
        score=score,
        label="",
        source="mplus_scorecard",
        display=display,
        colour=fit_colour(score),
        target_key=target_key,
        primary_key=primary_key,
        best_nearby_key=best_nearby_key,
        confidence=confidence,
        coverage=coverage,
        same_dungeon_score=same_dungeon_score,
        has_same_dungeon_context=has_same_dungeon_context,
        same_dungeon_rio_key=same_dungeon_key if has_same_dungeon_context else 0,
        same_dungeon_wcl_key=(
            same_dungeon_signal.key_level if same_dungeon_signal is not None else 0
        ),
        same_dungeon_wcl_best=(
            same_dungeon_signal.percentile if same_dungeon_signal is not None else None
        ),
        same_dungeon_wcl_median=(
            same_dungeon_signal.median_percent
            if same_dungeon_signal is not None
            else None
        ),
        same_dungeon_wcl_run_count=(
            same_dungeon_signal.run_count if same_dungeon_signal is not None else 0
        ),
        limit_reason=limit_reason,
    )


def _mplus_wcl_signals(applicant: Applicant, listing: Listing) -> list[_MPlusWCLSignal]:
    _metric_label, breakdown, _best, _median = role_mplus_view(applicant)
    target_dungeon_keys = listing_dungeon_keys(listing)
    signals: list[_MPlusWCLSignal] = []
    for entry in breakdown:
        if not isinstance(entry, dict):
            continue
        dungeon_name = str(entry.get("name") or "?")
        normalised_name = _normalise_name(dungeon_name)
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
                )
            )
    return signals


def _mplus_representative_wcl_signals(
    signals: list[_MPlusWCLSignal], target_key: int
) -> list[_MPlusWCLSignal]:
    by_dungeon: dict[str, list[_MPlusWCLSignal]] = {}
    for signal in signals:
        dungeon_key = _mplus_wcl_dungeon_key(signal)
        if dungeon_key is None:
            continue
        by_dungeon.setdefault(dungeon_key, []).append(signal)

    representatives: list[_MPlusWCLSignal] = []
    for dungeon_signals in by_dungeon.values():
        best_signal: tuple[float, _MPlusWCLSignal] | None = None
        window_signal: tuple[float, _MPlusWCLSignal] | None = None
        for signal in dungeon_signals:
            fit = _mplus_signal_fit(signal, target_key)
            if fit is None:
                continue
            candidate = (fit, signal)
            if best_signal is None or fit > best_signal[0]:
                best_signal = candidate
            if abs(signal.key_level - target_key) <= 2 and (
                window_signal is None or fit > window_signal[0]
            ):
                window_signal = candidate
        selected = window_signal or best_signal
        if selected is not None:
            representatives.append(selected[1])
    return representatives


def _mplus_same_dungeon_representative(
    signals: list[_MPlusWCLSignal], target_key: int
) -> _MPlusWCLSignal | None:
    candidates = [
        signal
        for signal in signals
        if signal.same_dungeon and _mplus_wcl_is_fit_evidence(signal, target_key)
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda signal: (
            _mplus_signal_fit(signal, target_key) or 0.0,
            signal.key_level,
        ),
    )


def _mplus_best_nearby_evidence_key(
    *,
    applicant: Applicant,
    rio_row_key_levels: list[int],
    wcl_signals: list[_MPlusWCLSignal],
    target_key: int,
    same_dungeon_key: int,
) -> int:
    actual_keys = list(rio_row_key_levels)
    if _mplus_rio_summary_matches_target(applicant, target_key):
        actual_keys.extend(
            (
                positive_int(applicant.rio_best_key),
                positive_int(same_dungeon_key),
            )
        )
    actual_keys.extend(
        signal.key_level
        for signal in wcl_signals
        if _mplus_wcl_is_fit_evidence(signal, target_key)
    )
    return max(
        (level for level in actual_keys if abs(level - target_key) <= 2),
        default=0,
    )


def _mplus_wcl_is_fit_evidence(
    signal: _MPlusWCLSignal, target_key: int
) -> bool:
    quality = _mplus_wcl_quality_percent(signal)
    delta = signal.key_level - target_key
    if quality >= 50.0:
        return True
    # Weak/low logs participate only in the near-target completion, cap, and
    # penalty paths. Far-below low parses are observations, not fit evidence.
    return delta >= -1


def _mplus_limit_reason(
    *,
    score_only_fallback: bool,
    no_relevant_wcl_reduction: float,
    applied_wcl_penalty: float,
    wcl_bad_penalty: float,
    wcl_gray_penalty: float,
    primary_key: int,
    target_key: int,
    coverage: float,
    has_same_dungeon_context: bool,
    has_same_dungeon_evidence: bool,
) -> str:
    if score_only_fallback:
        return MPLUS_LIMIT_SCORE_ONLY

    penalty_reason = (
        MPLUS_LIMIT_LOW_WCL
        if wcl_bad_penalty >= wcl_gray_penalty
        else MPLUS_LIMIT_WEAK_WCL
    )
    applied_limits = (
        (no_relevant_wcl_reduction, MPLUS_LIMIT_NO_RELEVANT_WCL),
        (applied_wcl_penalty, penalty_reason),
    )
    reduction, reason = max(applied_limits, key=lambda candidate: candidate[0])
    if reduction > 0.0:
        return reason
    if 0 < primary_key < target_key:
        return MPLUS_LIMIT_BELOW_TARGET
    if coverage < 1.0:
        return MPLUS_LIMIT_SPARSE_COVERAGE
    if has_same_dungeon_context and not has_same_dungeon_evidence:
        return MPLUS_LIMIT_NO_SAME_DUNGEON
    return ""


def _mplus_signal_fit(signal: _MPlusWCLSignal, target_key: int) -> float | None:
    return _mplus_bracket_fit(
        {
            "key_level": signal.key_level,
            "parse_percent": signal.percentile,
            "median_percent": signal.median_percent,
            "run_count": signal.run_count,
        },
        target_key,
    )


def _mplus_rio_completion_risk_floor(
    applicant: Applicant,
    rio_key_levels: list[int],
    target_key: int,
    readiness_score: float,
) -> float:
    if target_key <= 0 or readiness_score <= 0.0:
        return 0.0
    near_target_count = sum(
        1 for level in rio_key_levels[:MPLUS_DUNGEON_COUNT] if level >= target_key - 1
    )
    at_target_count = sum(
        1 for level in rio_key_levels[:MPLUS_DUNGEON_COUNT] if level >= target_key
    )
    if near_target_count < 6 or at_target_count < 1:
        return 0.0
    if applicant.role == "HEALER":
        return min(38.0, readiness_score * 0.75)
    return min(30.0, readiness_score * 0.55)


def _mplus_primary_key(
    *,
    rio_key_levels: list[int],
    rio_row_key_levels: list[int],
    wcl_signals: list[_MPlusWCLSignal],
    target_key: int,
    same_dungeon_key: int,
    summary_best_key: int,
) -> int:
    positive_wcl_keys = [
        signal.key_level
        for signal in wcl_signals
        if _mplus_wcl_single_positive_score(signal, target_key) > 0.0
    ]
    if positive_wcl_keys:
        concrete_rio_key = max(max(rio_row_key_levels, default=0), same_dungeon_key, 0)
        if concrete_rio_key > 0:
            return max(concrete_rio_key, max(positive_wcl_keys))
        return max(positive_wcl_keys)
    completion_wcl_keys = [
        _mplus_wcl_effective_key_level(signal, target_key)
        for signal in wcl_signals
        if _mplus_wcl_counts_as_completion_evidence(signal, target_key)
    ]
    return max(
        max(rio_key_levels, default=0),
        summary_best_key,
        same_dungeon_key,
        max(completion_wcl_keys, default=0),
        0,
    )


def _mplus_rio_row_key_levels(applicant: Applicant) -> list[int]:
    by_dungeon: dict[str, int] = {}
    for entry in applicant.rio_dungeons:
        if not isinstance(entry, dict):
            continue
        dungeon_key = _normalise_name(str(entry.get("name") or ""))
        if not dungeon_key:
            continue
        level = positive_int(entry.get("key_level"))
        if level <= 0:
            continue
        by_dungeon[dungeon_key] = max(by_dungeon.get(dungeon_key, 0), level)
    return list(by_dungeon.values())


def _mplus_rio_key_levels(
    applicant: Applicant, target_key: int, same_dungeon_key: int
) -> list[int]:
    row_levels = _mplus_rio_row_key_levels(applicant)
    dungeon_count = positive_int(applicant.rio_dungeon_count)
    expected_rows = min(MPLUS_DUNGEON_COUNT, dungeon_count or MPLUS_DUNGEON_COUNT)
    if len(row_levels) >= expected_rows:
        return sorted(row_levels, reverse=True)[:MPLUS_DUNGEON_COUNT]

    synthetic = (
        _mplus_synthetic_rio_key_levels(
            applicant,
            target_key,
            same_dungeon_key=same_dungeon_key,
            include_named_keys=not row_levels,
        )
        if _mplus_rio_summary_matches_target(applicant, target_key)
        else []
    )
    if not synthetic:
        return sorted(row_levels, reverse=True)[:MPLUS_DUNGEON_COUNT]
    if not row_levels:
        return synthetic

    merged = list(synthetic)
    for row_level in sorted(row_levels, reverse=True):
        replaceable = [
            idx for idx, synthetic_level in enumerate(merged) if row_level > synthetic_level
        ]
        if not replaceable:
            continue
        best_index = max(replaceable, key=lambda idx: merged[idx])
        merged[best_index] = row_level
    return sorted(merged, reverse=True)[:MPLUS_DUNGEON_COUNT]


def _mplus_rio_summary_matches_target(applicant: Applicant, target_key: int) -> bool:
    return target_key > 0 and positive_int(applicant.rio_summary_target_key) == target_key


def _mplus_completion_key_levels(
    rio_key_levels: list[int], wcl_key_levels: list[int]
) -> list[int]:
    merged = sorted((level for level in rio_key_levels if level > 0), reverse=True)[
        :MPLUS_DUNGEON_COUNT
    ]
    for level in sorted((level for level in wcl_key_levels if level > 0), reverse=True):
        if len(merged) < MPLUS_DUNGEON_COUNT:
            merged.append(level)
            continue
        weakest_index = min(range(len(merged)), key=lambda idx: merged[idx])
        if level > merged[weakest_index]:
            merged[weakest_index] = level
    return sorted(merged, reverse=True)[:MPLUS_DUNGEON_COUNT]


def _mplus_synthetic_rio_key_levels(
    applicant: Applicant,
    target_key: int,
    *,
    same_dungeon_key: int,
    include_named_keys: bool = True,
) -> list[int]:
    if not applicant.rio_profile or target_key <= 0:
        return []
    dungeon_count = max(
        1,
        min(
            positive_int(applicant.rio_dungeon_count) or MPLUS_DUNGEON_COUNT,
            MPLUS_DUNGEON_COUNT,
        ),
    )
    timed_at = min(nonnegative_int(applicant.rio_timed_at_or_above), dungeon_count)
    timed_minus1 = min(
        max(nonnegative_int(applicant.rio_timed_at_or_above_minus1), timed_at),
        dungeon_count,
    )
    timed_minus2 = min(
        max(nonnegative_int(applicant.rio_timed_at_or_above_minus2), timed_minus1),
        dungeon_count,
    )
    levels: list[int] = []
    levels.extend([target_key] * timed_at)
    levels.extend([max(1, target_key - 1)] * (timed_minus1 - timed_at))
    levels.extend([max(1, target_key - 2)] * (timed_minus2 - timed_minus1))
    while len(levels) < dungeon_count:
        levels.append(0)

    if include_named_keys:
        for key in (
            positive_int(applicant.rio_best_key),
            positive_int(same_dungeon_key or applicant.rio_best_dungeon_key),
        ):
            if key <= 0:
                continue
            weakest_index = min(range(len(levels)), key=lambda idx: levels[idx])
            if key > levels[weakest_index]:
                levels[weakest_index] = key
    return sorted(levels, reverse=True)[:MPLUS_DUNGEON_COUNT]


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


def _mplus_completion_experience_score(applicant: Applicant, target_key: int) -> float:
    if not _mplus_rio_summary_matches_target(applicant, target_key):
        return 0.0
    dungeon_count = max(
        1,
        min(
            positive_int(applicant.rio_dungeon_count) or MPLUS_DUNGEON_COUNT,
            MPLUS_DUNGEON_COUNT,
        ),
    )
    timed_minus1 = min(
        nonnegative_int(applicant.rio_timed_at_or_above_minus1),
        dungeon_count,
    )
    completed_minus1 = min(
        max(nonnegative_int(applicant.rio_completed_at_or_above_minus1), timed_minus1),
        dungeon_count,
    )
    completed_only = max(0, completed_minus1 - timed_minus1)
    if completed_only <= 0:
        return 0.0
    return MPLUS_SCORE_COMPLETION_MAX * _clamp(
        completed_only / max(dungeon_count, 1), 0.0, 1.0
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


def _mplus_wcl_positive_score(signals: list[_MPlusWCLSignal], target_key: int) -> float:
    by_dungeon: dict[str, float] = {}
    for signal in signals:
        dungeon_key = _mplus_wcl_dungeon_key(signal)
        if dungeon_key is None:
            continue
        score = _mplus_wcl_single_positive_score(signal, target_key)
        by_dungeon[dungeon_key] = max(by_dungeon.get(dungeon_key, 0.0), score)
    values = sorted(by_dungeon.values(), reverse=True)
    if not values:
        return 0.0
    return min(
        MPLUS_SCORE_WCL_POSITIVE_MAX,
        _weighted_sum_top(values, [0.34, 0.25, 0.18, 0.13, 0.10]),
    )


def _mplus_wcl_single_positive_score(signal: _MPlusWCLSignal, target_key: int) -> float:
    quality = _mplus_wcl_quality_percent(signal)
    if quality < 50.0 or target_key <= 0:
        return 0.0
    delta = signal.key_level - target_key
    key_weight = _mplus_wcl_key_weight(delta)
    run_weight = 0.70 + 0.30 * min(max(signal.run_count, 1), 3) / 3.0
    return (
        ((quality - 50.0) / 50.0)
        * MPLUS_SCORE_WCL_POSITIVE_MAX
        * key_weight
        * run_weight
    )


def _mplus_wcl_key_weight(delta: int) -> float:
    if delta >= 0:
        return 1.0
    if delta == -1:
        return 0.82
    if delta == -2:
        return MPLUS_SCORE_LOW_KEY_SIGNAL_WEIGHT
    if delta == -3:
        return 0.04
    return 0.0


def _mplus_wcl_quality_percent(signal: _MPlusWCLSignal) -> float:
    return _mplus_performance_score(
        signal.percentile, signal.median_percent, signal.run_count
    )


def _mplus_wcl_gray_penalty(
    signals: list[_MPlusWCLSignal], target_key: int
) -> float:
    by_dungeon: dict[str, float] = {}
    for signal in signals:
        dungeon_key = _mplus_wcl_dungeon_key(signal)
        if dungeon_key is None:
            continue
        delta = signal.key_level - target_key
        quality = _mplus_wcl_quality_percent(signal)
        if quality < 25.0 or quality >= 50.0 or delta < -1:
            continue
        if delta >= 0:
            severity = 8.0 / (1.0 + 0.25 * delta)
        else:
            severity = 5.0
        run_weight = 0.75 + 0.25 * min(max(signal.run_count, 1), 3) / 3.0
        penalty = ((50.0 - quality) / 25.0) * severity * run_weight
        by_dungeon[dungeon_key] = max(by_dungeon.get(dungeon_key, 0.0), penalty)
    penalties = sorted(by_dungeon.values(), reverse=True)
    if not penalties:
        return 0.0
    return min(
        MPLUS_SCORE_WCL_GRAY_MAX,
        _weighted_sum_top(penalties, [1.0, 0.70, 0.45, 0.25]),
    )


def _mplus_wcl_gray_completion_score(
    signals: list[_MPlusWCLSignal], target_key: int, rio_key_levels: list[int]
) -> float:
    by_dungeon: dict[str, float] = {}
    for signal in signals:
        dungeon_key = _mplus_wcl_dungeon_key(signal)
        if dungeon_key is None:
            continue
        delta = signal.key_level - target_key
        quality = _mplus_wcl_quality_percent(signal)
        if quality < 25.0 or quality >= 50.0 or delta < -1 or delta >= 3:
            continue
        key_weight = 0.70 if delta == -1 else 1.0 + max(0, delta) * 0.10
        value = ((quality - 25.0) / 25.0) * key_weight
        by_dungeon[dungeon_key] = max(by_dungeon.get(dungeon_key, 0.0), value)
    if not by_dungeon:
        return 0.0
    missing_rio_coverage = _clamp(
        (MPLUS_DUNGEON_COUNT - len([level for level in rio_key_levels if level > 0]))
        / max(MPLUS_DUNGEON_COUNT, 1),
        0.0,
        1.0,
    )
    if missing_rio_coverage <= 0.0:
        return 0.0
    values = list(by_dungeon.values())
    coverage = _clamp(len(values) / max(MPLUS_DUNGEON_COUNT, 1), 0.0, 1.0)
    average = sum(values) / len(values)
    return (
        MPLUS_SCORE_WCL_GRAY_COMPLETION_MAX
        * missing_rio_coverage
        * coverage
        * average
    )


def _mplus_wcl_bad_penalty(signals: list[_MPlusWCLSignal], target_key: int) -> float:
    by_dungeon: dict[str, float] = {}
    for signal in signals:
        dungeon_key = _mplus_wcl_dungeon_key(signal)
        if dungeon_key is None:
            continue
        delta = signal.key_level - target_key
        quality = _mplus_wcl_quality_percent(signal)
        if quality >= 25.0 or delta < -1:
            continue
        severity = 28.0 if delta >= 0 else 18.0
        run_weight = 0.90 + 0.35 * min(max(signal.run_count, 1), 3) / 3.0
        penalty = ((25.0 - quality) / 25.0) * severity * run_weight
        by_dungeon[dungeon_key] = max(by_dungeon.get(dungeon_key, 0.0), penalty)
    penalties = sorted(by_dungeon.values(), reverse=True)
    if not penalties:
        return 0.0
    return min(
        MPLUS_SCORE_WCL_BAD_MAX,
        _weighted_sum_top(
            penalties, [1.0, 0.82, 0.62, 0.42, 0.25]
        ),
    )


def _mplus_wcl_key_levels(
    signals: list[_MPlusWCLSignal], target_key: int
) -> list[int]:
    by_dungeon: dict[str, int] = {}
    for signal in signals:
        if not _mplus_wcl_counts_as_completion_evidence(signal, target_key):
            continue
        dungeon_key = _mplus_wcl_dungeon_key(signal)
        if dungeon_key is None:
            continue
        by_dungeon[dungeon_key] = max(
            by_dungeon.get(dungeon_key, 0),
            _mplus_wcl_effective_key_level(signal, target_key),
        )
    return list(by_dungeon.values())


def _mplus_wcl_counts_as_completion_evidence(
    signal: _MPlusWCLSignal, target_key: int
) -> bool:
    quality = _mplus_wcl_quality_percent(signal)
    if quality >= 50.0:
        return True
    # Grey logs are weak completion evidence, not clean readiness. Near-target
    # grey proves the player has seen the key range; very high grey also proves
    # experience above the listing, but neither should count like a clean timed
    # target key.
    return quality >= 25.0 and signal.key_level - target_key >= -1


def _mplus_wcl_effective_key_level(
    signal: _MPlusWCLSignal, target_key: int
) -> int:
    quality = _mplus_wcl_quality_percent(signal)
    if quality >= 50.0:
        return signal.key_level
    if signal.key_level - target_key >= 3:
        return min(signal.key_level, target_key)
    return min(signal.key_level, max(1, target_key - 2))


def _mplus_clean_wcl_dungeon_keys(signals: Iterable[_MPlusWCLSignal]) -> set[str]:
    keys: set[str] = set()
    for signal in signals:
        if _mplus_wcl_quality_percent(signal) < 50.0:
            continue
        dungeon_key = _mplus_wcl_dungeon_key(signal)
        if dungeon_key is not None:
            keys.add(dungeon_key)
    return keys


def _mplus_wcl_total_runs(signals: list[_MPlusWCLSignal]) -> int:
    by_dungeon: dict[str, int] = {}
    for signal in signals:
        if _mplus_wcl_quality_percent(signal) < 50.0:
            continue
        dungeon_key = _mplus_wcl_dungeon_key(signal)
        if dungeon_key is None:
            continue
        by_dungeon[dungeon_key] = max(
            by_dungeon.get(dungeon_key, 0), max(signal.run_count, 1)
        )
    return sum(by_dungeon.values())


def _mplus_wcl_dungeon_key(signal: _MPlusWCLSignal) -> str | None:
    dungeon_key = _normalise_name(signal.dungeon_name)
    if not dungeon_key or signal.key_level <= 0:
        return None
    return dungeon_key


def _mplus_no_relevant_wcl_cap(key_levels: list[int], target_key: int) -> float:
    if target_key <= 0:
        return 42.0
    deltas = sorted((level - target_key for level in key_levels), reverse=True)
    top3 = deltas[:3]
    top3_avg = sum(top3) / len(top3) if top3 else -999.0
    at_target = sum(1 for delta in deltas if delta >= 0)
    if top3_avg >= 5.0:
        return 90.0
    if top3_avg >= 4.0:
        return 86.0
    if top3_avg >= 3.0:
        return 80.0
    if top3_avg >= 2.0:
        return 72.0
    if top3_avg >= 1.0:
        return 62.0
    if at_target >= 6:
        return 56.0
    if at_target >= 3:
        return 48.0
    return 42.0


def _mplus_display_score(raw_score: float) -> float:
    clean = _clamp(raw_score, 0.0, 160.0)
    if clean <= 92.0:
        return clean
    return _clamp(100.0 - 8.0 * math.exp(-(clean - 92.0) / 20.0), 0.0, 100.0)


def _weighted_sum_top(values: list[float], weights: list[float]) -> float:
    if not values:
        return 0.0
    used_values = values[: len(weights)]
    used_weights = weights[: len(used_values)]
    return sum(
        value * weight for value, weight in zip(used_values, used_weights, strict=True)
    )


def _raid_candidate_fit(applicant: Applicant, listing: Listing) -> CandidateFit:
    target = RAID_TARGET_BY_DIFFICULTY_ID.get(listing.difficulty_id, "")
    raid = {
        "N": _raid_perf(applicant.raid_normal, applicant.raid_normal_median),
        "H": _raid_perf(applicant.raid_heroic, applicant.raid_heroic_median),
        "M": _raid_perf(applicant.raid_mythic, applicant.raid_mythic_median),
    }
    order = ["N", "H", "M"]
    target_idx = order.index(target) if target in order else -1

    if target_idx >= 0:
        exact = raid[target]
        higher = _present_scores(raid[key] for key in order[target_idx + 1 :])
        lower = _present_scores(raid[key] for key in order[:target_idx])
    else:
        exact = None
        higher = _present_scores(raid.values())
        lower = []

    target_score = exact
    source = "raid_exact" if exact is not None else "raid_missing_exact"
    if target_score is None and higher:
        target_score = max(higher) * 0.88
        source = "raid_higher_fallback"
    if target_score is None and lower:
        target_score = max(lower) * 0.55
        source = "raid_lower_fallback"
    if target_score is None:
        target_score = 0.0
        source = "support_fallback"

    higher_bonus = max(higher) if higher else 0.0
    mplus_support = _mplus_support_percent(applicant)
    rio_support = _clamp(effective_rio_score(applicant) / 35.0, 0.0, 100.0)
    score = (
        0.84 * target_score
        + 0.08 * higher_bonus
        + 0.04 * mplus_support
        + 0.04 * rio_support
    )
    score = _clamp(score, 0.0, 105.0)
    label = fit_label(score)
    target_label = target or "?"
    return CandidateFit(
        context=CONTEXT_RAID,
        score=score,
        label=label,
        source=source,
        display=f"{target_label} {label} {int(round(score))}",
        colour=fit_colour(score),
        target_raid=target_label,
        confidence=1.0
        if source == "raid_exact"
        else (0.65 if target_score > 0 else 0.2),
    )


def role_mplus_view(
    applicant: Applicant,
) -> tuple[str, list[dict], float | None, float | None]:
    if applicant.role == "HEALER":
        return (
            "HPS",
            applicant.mplus_hps_breakdown,
            safe_percent(applicant.mplus_hps),
            safe_percent(applicant.mplus_hps_median),
        )
    return (
        "DPS",
        applicant.mplus_dps_breakdown,
        safe_percent(applicant.mplus_dps),
        safe_percent(applicant.mplus_dps_median),
    )


def _present_scores(values: Iterable[float | None]) -> list[float]:
    return [value for value in values if value is not None]


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
            carry_threshold=82.0,
            carry_coeff=0.035,
            carry_cap=4.0,
            low_carry_floor=42.0,
        )
    return _PackageParams(
        center=58.0,
        scale=11.0,
        carry_threshold=86.0,
        carry_coeff=0.06,
        carry_cap=7.0,
        low_carry_floor=48.0,
    )


def _package_score_for_member_scores(
    scores: tuple[float, ...], context: str, status_penalty: float
) -> float:
    if not scores:
        return 0.0
    if len(scores) == 1:
        return scores[0]
    params = _package_params(context)
    # Group applications are accepted as one package. Geometric mean in
    # probability space behaves like a soft weak-link: it rewards several
    # solid players without letting one superstar hide a risky friend.
    probabilities = [
        _sigmoid((score - params.center) / params.scale) for score in scores
    ]
    group_probability = _geometric_mean_probability(probabilities)
    base = params.center + params.scale * _logit(group_probability)
    carry_credit = min(
        params.carry_cap,
        sum(max(0.0, score - params.carry_threshold) for score in scores)
        * params.carry_coeff,
    )
    carry_credit *= _package_low_carry_multiplier(
        min(scores), params.low_carry_floor
    )
    return base + carry_credit - status_penalty


def _package_low_carry_multiplier(low_score: float, floor: float) -> float:
    if low_score >= floor:
        return 1.0
    ramp_width = 12.0
    return 0.35 + 0.65 * _clamp((low_score - (floor - ramp_width)) / ramp_width, 0.0, 1.0)


def _package_status_penalty(members: Iterable[Applicant]) -> float:
    penalties = {
        "error": 4.0,
        "not_found": 6.0,
    }
    return min(12.0, sum(penalties.get(member.fetch_status, 0.0) for member in members))


def _package_confidence(
    members: Iterable[Applicant], fits: Iterable[CandidateFit]
) -> float:
    factors = {
        "ready": 1.0,
        "loading": 0.65,
        "pending": 0.65,
        "error": 0.45,
        "not_found": 0.35,
    }
    values = [
        fit.confidence * factors.get(member.fetch_status, 0.5)
        for member, fit in zip(members, fits, strict=True)
    ]
    return _clamp(min(values, default=0.0), 0.0, 1.0)


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


def _mplus_rio_fit(score: int, target_key: int) -> float:
    return _clamp(55.0 + (score - (1700.0 + target_key * 100.0)) / 18.0, 0.0, 105.0)


def _raid_perf(best: float | None, median: float | None) -> float | None:
    clean_best = safe_percent(best)
    clean_median = safe_percent(median)
    if clean_best is None and clean_median is None:
        return None
    if clean_best is None:
        return clean_median * 0.85 if clean_median is not None else None
    if clean_median is None:
        return clean_best * 0.90
    return clean_best * 0.55 + clean_median * 0.45


def _mplus_support_percent(applicant: Applicant) -> float:
    _metric_label, _breakdown, best, median = role_mplus_view(applicant)
    if best is None and median is None:
        return 0.0
    if best is None:
        return median or 0.0
    if median is None:
        return best * 0.90
    return best * 0.60 + median * 0.40


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
    except (TypeError, ValueError):
        return None
    if not math.isfinite(pct) or pct < 0.0 or pct > 100.0:
        return None
    return pct


def nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


def positive_int(value: object) -> int:
    parsed = nonnegative_int(value)
    return parsed if parsed > 0 else 0


def _normalise_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
