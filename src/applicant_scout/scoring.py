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
MPLUS_SCORE_WCL_POSITIVE_MAX = 36.0
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
    target_raid: str = ""
    confidence: float = 0.0
    coverage: float = 0.0
    same_dungeon_score: float = 0.0


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


@dataclass(frozen=True)
class _PackageParams:
    center: float
    scale: float
    carry_threshold: float
    carry_coeff: float
    carry_cap: float
    spread_threshold: float
    spread_coeff: float
    spread_cap: float
    low_carry_floor: float


@dataclass(frozen=True)
class _MPlusWCLSignal:
    dungeon_name: str
    key_level: int
    percentile: float
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
        if low < params.low_carry_floor:
            carry_credit *= 0.35
        spread_penalty = min(
            params.spread_cap,
            max(0.0, spread - params.spread_threshold) * params.spread_coeff,
        )
        score = base + carry_credit - spread_penalty - status_penalty
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
    )


def mplus_dungeon_fit_rows(
    applicant: Applicant, listing: Listing | None
) -> list[MPlusDungeonFit]:
    if detect_listing_context(listing) != CONTEXT_MPLUS or listing is None:
        return []
    _metric_label, breakdown, _best, _median = _role_mplus_view(applicant)
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
            key_level = _positive_int(bracket.get("key_level"))
            text = _bracket_metric_text(bracket)
            best_percent = _safe_percent(bracket.get("parse_percent"))
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
            if abs(key_level - listing.key_level) <= 2:
                if window_row is None or row.score > window_row.score:
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


def _listing_dungeon_keys(listing: Listing) -> set[str]:
    keys = {_normalise_name(listing.dungeon_name)}
    mapped_name = mplus_dungeon_name_for_activity_id(listing.activity_id)
    if mapped_name:
        keys.add(_normalise_name(mapped_name))
    return {key for key in keys if key}


def _rio_same_dungeon_key(applicant: Applicant, listing: Listing) -> int:
    listing_keys = _listing_dungeon_keys(listing)
    best_key = _positive_int(applicant.rio_best_dungeon_key)
    for entry in applicant.rio_dungeons:
        if not isinstance(entry, dict):
            continue
        row_key = _normalise_name(entry.get("name"))
        if row_key not in listing_keys:
            continue
        best_key = max(best_key, _positive_int(entry.get("key_level")))
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

    wcl_key_levels = [
        signal.key_level for signal in wcl_signals if signal.percentile >= 25.0
    ]
    primary_key = _mplus_primary_key(
        rio_key_levels=rio_key_levels,
        rio_row_key_levels=rio_row_key_levels,
        wcl_signals=wcl_signals,
        target_key=target_key,
        same_dungeon_key=same_dungeon_key,
    )

    completion_key_levels = rio_key_levels if rio_key_levels else wcl_key_levels
    key_score = _mplus_key_readiness_score(completion_key_levels, target_key)
    same_bonus = (
        MPLUS_SCORE_SAME_MAX
        if max(
            same_dungeon_key,
            max(
                (signal.key_level for signal in wcl_signals if signal.same_dungeon),
                default=0,
            ),
        )
        >= target_key
        else 0.0
    )
    consistency = _mplus_key_consistency_score(completion_key_levels, target_key)
    carry = _mplus_carry_bonus(completion_key_levels, target_key)
    wcl_positive = _mplus_wcl_positive_score(wcl_signals, target_key)
    wcl_bad_penalty = _mplus_wcl_bad_penalty(wcl_signals, target_key)
    raw_score = key_score + carry + same_bonus + consistency + wcl_positive - wcl_bad_penalty

    has_relevant_wcl = any(signal.key_level - target_key >= -1 for signal in wcl_signals)
    if not has_relevant_wcl:
        raw_score = min(
            raw_score,
            _mplus_no_relevant_wcl_cap(completion_key_levels, target_key),
        )
    if not completion_key_levels and not wcl_signals and allow_score_fallback:
        raw_score = min(
            _mplus_rio_fit(effective_rio_score(applicant), target_key) * 0.40,
            42.0,
        )
    score = _mplus_display_score(raw_score)

    coverage = _clamp(
        max(
            len([level for level in rio_key_levels if level > 0]),
            len({_normalise_name(signal.dungeon_name) for signal in wcl_signals}),
        )
        / max(MPLUS_DUNGEON_COUNT, 1),
        0.0,
        1.0,
    )
    total_runs = sum(max(signal.run_count, 1) for signal in wcl_signals)
    confidence = _clamp(
        0.30 + 0.45 * coverage + 0.25 * min(total_runs / 16.0, 1.0),
        0.0,
        1.0,
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
        confidence=confidence,
        coverage=coverage,
        same_dungeon_score=same_dungeon_score,
    )


def _mplus_wcl_signals(applicant: Applicant, listing: Listing) -> list[_MPlusWCLSignal]:
    _metric_label, breakdown, _best, _median = _role_mplus_view(applicant)
    listing_dungeon_keys = _listing_dungeon_keys(listing)
    signals: list[_MPlusWCLSignal] = []
    for entry in breakdown:
        if not isinstance(entry, dict):
            continue
        dungeon_name = str(entry.get("name") or "?")
        normalised_name = _normalise_name(dungeon_name)
        for bracket in _iter_mplus_brackets(entry):
            key_level = _positive_int(bracket.get("key_level"))
            percentile = _safe_percent(bracket.get("parse_percent"))
            if key_level <= 0 or percentile is None:
                continue
            signals.append(
                _MPlusWCLSignal(
                    dungeon_name=dungeon_name,
                    key_level=key_level,
                    percentile=percentile,
                    run_count=_nonnegative_int(bracket.get("run_count")),
                    same_dungeon=normalised_name in listing_dungeon_keys,
                )
            )
    return signals


def _mplus_primary_key(
    *,
    rio_key_levels: list[int],
    rio_row_key_levels: list[int],
    wcl_signals: list[_MPlusWCLSignal],
    target_key: int,
    same_dungeon_key: int,
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
    return max(
        max(rio_key_levels, default=0),
        same_dungeon_key,
        0,
    )


def _mplus_rio_row_key_levels(applicant: Applicant) -> list[int]:
    return [
        level
        for level in (
            _positive_int(entry.get("key_level"))
            for entry in applicant.rio_dungeons
            if isinstance(entry, dict)
        )
        if level > 0
    ]


def _mplus_rio_key_levels(
    applicant: Applicant, target_key: int, same_dungeon_key: int
) -> list[int]:
    row_levels = _mplus_rio_row_key_levels(applicant)
    dungeon_count = _positive_int(applicant.rio_dungeon_count)
    expected_rows = min(MPLUS_DUNGEON_COUNT, dungeon_count or MPLUS_DUNGEON_COUNT)
    if len(row_levels) >= expected_rows:
        return sorted(row_levels, reverse=True)[:MPLUS_DUNGEON_COUNT]

    synthetic = _mplus_synthetic_rio_key_levels(
        applicant, target_key, same_dungeon_key=same_dungeon_key
    )
    if not synthetic:
        return sorted(row_levels, reverse=True)[:MPLUS_DUNGEON_COUNT]
    if not row_levels:
        return synthetic

    merged = list(synthetic)
    for row_level in sorted(row_levels, reverse=True):
        weakest_index = min(range(len(merged)), key=lambda idx: merged[idx])
        if row_level > merged[weakest_index]:
            merged[weakest_index] = row_level
    return sorted(merged, reverse=True)[:MPLUS_DUNGEON_COUNT]


def _mplus_synthetic_rio_key_levels(
    applicant: Applicant, target_key: int, *, same_dungeon_key: int
) -> list[int]:
    if not applicant.rio_profile or target_key <= 0:
        return []
    dungeon_count = max(
        1,
        min(
            _positive_int(applicant.rio_dungeon_count) or MPLUS_DUNGEON_COUNT,
            MPLUS_DUNGEON_COUNT,
        ),
    )
    timed_at = min(_nonnegative_int(applicant.rio_timed_at_or_above), dungeon_count)
    timed_minus1 = min(
        max(_nonnegative_int(applicant.rio_timed_at_or_above_minus1), timed_at),
        dungeon_count,
    )
    timed_minus2 = min(
        max(_nonnegative_int(applicant.rio_timed_at_or_above_minus2), timed_minus1),
        dungeon_count,
    )
    levels: list[int] = []
    levels.extend([target_key] * timed_at)
    levels.extend([max(1, target_key - 1)] * (timed_minus1 - timed_at))
    levels.extend([max(1, target_key - 2)] * (timed_minus2 - timed_minus1))
    while len(levels) < dungeon_count:
        levels.append(0)

    for key in (
        _positive_int(applicant.rio_best_key),
        _positive_int(same_dungeon_key or applicant.rio_best_dungeon_key),
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
    values = sorted(
        (
            _mplus_wcl_single_positive_score(signal, target_key)
            for signal in signals
        ),
        reverse=True,
    )
    if not values:
        return 0.0
    return min(MPLUS_SCORE_WCL_POSITIVE_MAX, _weighted_sum_top(values, [0.34, 0.25, 0.18, 0.13, 0.10]))


def _mplus_wcl_single_positive_score(
    signal: _MPlusWCLSignal, target_key: int
) -> float:
    if signal.percentile < 50.0 or target_key <= 0:
        return 0.0
    delta = signal.key_level - target_key
    key_weight = _mplus_wcl_key_weight(delta)
    run_weight = 0.70 + 0.30 * min(max(signal.run_count, 1), 3) / 3.0
    return ((signal.percentile - 50.0) / 50.0) * MPLUS_SCORE_WCL_POSITIVE_MAX * key_weight * run_weight


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


def _mplus_wcl_bad_penalty(signals: list[_MPlusWCLSignal], target_key: int) -> float:
    penalties: list[float] = []
    for signal in signals:
        delta = signal.key_level - target_key
        if signal.percentile >= 25.0 or delta < -1:
            continue
        severity = 28.0 if delta >= 0 else 18.0
        run_weight = 0.90 + 0.35 * min(max(signal.run_count, 1), 3) / 3.0
        penalties.append(((25.0 - signal.percentile) / 25.0) * severity * run_weight)
    if not penalties:
        return 0.0
    return min(
        MPLUS_SCORE_WCL_BAD_MAX,
        _weighted_sum_top(sorted(penalties, reverse=True), [1.0, 0.82, 0.62, 0.42, 0.25]),
    )


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
        value * weight
        for value, weight in zip(used_values, used_weights, strict=True)
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
        confidence=1.0 if source == "raid_exact" else (0.65 if target_score > 0 else 0.2),
    )


def _role_mplus_view(applicant: Applicant) -> tuple[str, list[dict], float | None, float | None]:
    if applicant.role == "HEALER":
        return (
            "HPS",
            applicant.mplus_hps_breakdown,
            _safe_percent(applicant.mplus_hps),
            _safe_percent(applicant.mplus_hps_median),
        )
    return (
        "DPS",
        applicant.mplus_dps_breakdown,
        _safe_percent(applicant.mplus_dps),
        _safe_percent(applicant.mplus_dps_median),
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
    key_level = _positive_int(bracket.get("key_level"))
    best = _safe_percent(bracket.get("parse_percent"))
    if key_level <= 0 or target_key <= 0 or best is None:
        return None
    median = _safe_percent(bracket.get("median_percent"))
    run_count = _nonnegative_int(bracket.get("run_count"))
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
            spread_threshold=38.0,
            spread_coeff=0.06,
            spread_cap=6.0,
            low_carry_floor=42.0,
        )
    return _PackageParams(
        center=58.0,
        scale=11.0,
        carry_threshold=86.0,
        carry_coeff=0.06,
        carry_cap=7.0,
        spread_threshold=30.0,
        spread_coeff=0.12,
        spread_cap=10.0,
        low_carry_floor=48.0,
    )


def _package_status_penalty(members: Iterable[Applicant]) -> float:
    penalties = {
        "error": 4.0,
        "not_found": 6.0,
    }
    return min(12.0, sum(penalties.get(member.fetch_status, 0.0) for member in members))


def _package_confidence(members: Iterable[Applicant], fits: Iterable[CandidateFit]) -> float:
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
    clean_best = _safe_percent(best)
    clean_median = _safe_percent(median)
    if clean_best is None and clean_median is None:
        return None
    if clean_best is None:
        return clean_median * 0.85 if clean_median is not None else None
    if clean_median is None:
        return clean_best * 0.90
    return clean_best * 0.55 + clean_median * 0.45


def _mplus_support_percent(applicant: Applicant) -> float:
    _metric_label, _breakdown, best, median = _role_mplus_view(applicant)
    if best is None and median is None:
        return 0.0
    if best is None:
        return median or 0.0
    if median is None:
        return best * 0.90
    return best * 0.60 + median * 0.40


def _bracket_metric_text(bracket: dict) -> str:
    best = _safe_percent(bracket.get("parse_percent"))
    if best is None:
        return "—"
    median = _safe_percent(bracket.get("median_percent"))
    run_count = _nonnegative_int(bracket.get("run_count"))
    if run_count >= 2 and median is not None:
        return f"{int(round(best))}/{int(round(median))}"
    return f"{int(round(best))}"


def _safe_percent(value: object) -> float | None:
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


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


def _positive_int(value: object) -> int:
    parsed = _nonnegative_int(value)
    return parsed if parsed > 0 else 0


def _normalise_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
