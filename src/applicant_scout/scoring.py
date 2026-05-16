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

FIT_LABEL_BUCKETS: list[tuple[float, str]] = [
    (85.0, "TOP"),
    (70.0, "FIT"),
    (50.0, "OK"),
    (0.0, "RISK"),
]
MPLUS_BEST_WEIGHT = 0.55
MPLUS_TOP3_WEIGHT = 0.22
MPLUS_AVERAGE_WEIGHT = 0.15
MPLUS_SAME_DUNGEON_WEIGHT = 0.08
MPLUS_SPARSE_PENALTY = 20.0
MPLUS_SAME_DUNGEON_SPARSE_PENALTY = 10.0
MPLUS_OVERQUALIFIED_SPARSE_PENALTY = 12.0
MPLUS_RIO_NUDGE_GATE = 55.0
MPLUS_RIO_NUDGE_WEIGHT = 0.04
MPLUS_RIO_NUDGE_CAP = 2.0
MPLUS_RIO_FALLBACK_WEIGHT = 0.62
MPLUS_RIO_FALLBACK_CAP = 58.0
MPLUS_RIO_COMPLETION_FALLBACK_CAP = 82.0


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
class _BracketFit:
    dungeon_name: str
    key_level: int
    fit: float
    run_count: int


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
            rio_fit = _mplus_rio_completion_candidate_fit(applicant, listing)
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
    score = _clamp(score, 0.0, 105.0)
    label = fit_label(score)
    if size > 1:
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


def _mplus_rio_completion_candidate_fit(
    applicant: Applicant, listing: Listing
) -> CandidateFit | None:
    target_key = listing.key_level
    same_dungeon_key = _rio_same_dungeon_key(applicant, listing)
    rio_completion_fit = _mplus_rio_completion_fit(
        applicant, target_key, same_dungeon_key=same_dungeon_key
    )
    if rio_completion_fit <= 0.0:
        return None
    score = _clamp(rio_completion_fit, 0.0, MPLUS_RIO_COMPLETION_FALLBACK_CAP)
    label = fit_label(score)
    primary_key = (
        same_dungeon_key or applicant.rio_best_dungeon_key or applicant.rio_best_key
    )
    display = f"{label} {int(round(score))} RIO"
    if primary_key > 0:
        display = f"{label} {int(round(score))} +{primary_key} RIO"
    return CandidateFit(
        context=CONTEXT_MPLUS,
        score=score,
        label=label,
        source="rio_completion",
        display=display,
        colour=fit_colour(score),
        target_key=target_key,
        primary_key=primary_key,
        confidence=_mplus_rio_completion_confidence(
            applicant, target_key, same_dungeon_key=same_dungeon_key
        ),
        coverage=_mplus_rio_timed_minus1_coverage(applicant),
    )


def _mplus_candidate_fit(applicant: Applicant, listing: Listing) -> CandidateFit:
    target_key = listing.key_level
    metric_label, breakdown, _best, _median = _role_mplus_view(applicant)
    bracket_fits: list[_BracketFit] = []
    best_by_dungeon: dict[str, _BracketFit] = {}
    same_dungeon_score = 0.0
    total_runs = 0
    max_key_delta = -999
    raw_best_percent = 0.0
    listing_dungeon_keys = _listing_dungeon_keys(listing)

    for entry in breakdown:
        if not isinstance(entry, dict):
            continue
        dungeon_name = str(entry.get("name") or "?")
        dungeon_best: _BracketFit | None = None
        for bracket in _iter_mplus_brackets(entry):
            fit = _mplus_bracket_fit(bracket, target_key)
            if fit is None:
                continue
            row = _BracketFit(
                dungeon_name=dungeon_name,
                key_level=_positive_int(bracket.get("key_level")),
                fit=fit,
                run_count=_nonnegative_int(bracket.get("run_count")),
            )
            bracket_fits.append(row)
            total_runs += row.run_count
            max_key_delta = max(max_key_delta, row.key_level - target_key)
            raw_best_percent = max(
                raw_best_percent, _safe_percent(bracket.get("parse_percent")) or 0.0
            )
            if dungeon_best is None or row.fit > dungeon_best.fit:
                dungeon_best = row
        if dungeon_best is not None:
            best_by_dungeon[_normalise_name(dungeon_name)] = dungeon_best
            if _normalise_name(dungeon_name) in listing_dungeon_keys:
                same_dungeon_score = dungeon_best.fit

    rio_score = effective_rio_score(applicant)
    rio_fit = _mplus_rio_fit(rio_score, target_key)
    same_dungeon_rio_key = _rio_same_dungeon_key(applicant, listing)
    rio_completion_fit = _mplus_rio_completion_fit(
        applicant, target_key, same_dungeon_key=same_dungeon_rio_key
    )
    if not bracket_fits:
        rio_completion_candidate = _mplus_rio_completion_candidate_fit(applicant, listing)
        if rio_completion_candidate is not None:
            return rio_completion_candidate
        score = _clamp(
            rio_fit * MPLUS_RIO_FALLBACK_WEIGHT, 0.0, MPLUS_RIO_FALLBACK_CAP
        )
        label = fit_label(score)
        return CandidateFit(
            context=CONTEXT_MPLUS,
            score=score,
            label=label,
            source="rio_fallback",
            display=f"RIO {int(round(score))}",
            colour=fit_colour(score),
            target_key=target_key,
            confidence=0.25 if rio_score > 0 else 0.0,
        )

    primary_row = max(bracket_fits, key=lambda row: row.fit)
    top_dungeon_rows = sorted(best_by_dungeon.values(), key=lambda row: row.fit, reverse=True)
    top3 = _weighted_top(top_dungeon_rows[:3])
    average = sum(row.fit for row in top_dungeon_rows) / len(top_dungeon_rows)
    coverage = _clamp(len(best_by_dungeon) / max(MPLUS_DUNGEON_COUNT, 1), 0.0, 1.0)
    quality_coverage = _mplus_quality_coverage(top_dungeon_rows)
    score = (
        MPLUS_BEST_WEIGHT * primary_row.fit
        + MPLUS_TOP3_WEIGHT * top3
        + MPLUS_AVERAGE_WEIGHT * average
        + MPLUS_SAME_DUNGEON_WEIGHT * same_dungeon_score
    )
    score -= _mplus_sparse_evidence_penalty(
        coverage=quality_coverage,
        max_key_delta=max_key_delta,
        has_same_dungeon=same_dungeon_score > 0.0,
    )
    if score >= MPLUS_RIO_NUDGE_GATE and raw_best_percent >= 50.0:
        score += min(
            MPLUS_RIO_NUDGE_CAP,
            max(0.0, rio_fit - score) * MPLUS_RIO_NUDGE_WEIGHT,
        )
    score = _mplus_raw_quality_cap(score, raw_best_percent, max_key_delta)
    display_key = primary_row.key_level
    if rio_completion_fit > score:
        rio_floor = _mplus_rio_completion_floor_with_wcl(
            rio_completion_fit,
            raw_best_percent=raw_best_percent,
            max_key_delta=max_key_delta,
            target_key=target_key,
            rio_same_dungeon_key=same_dungeon_rio_key,
            rio_best_key=applicant.rio_best_key,
            rio_timed_at_or_above=applicant.rio_timed_at_or_above,
        )
        if rio_floor > score:
            score = rio_floor
            display_key = (
                same_dungeon_rio_key
                or applicant.rio_best_dungeon_key
                or applicant.rio_best_key
            )
    score = _clamp(score, 0.0, 105.0)
    label = fit_label(score)
    confidence = _clamp(0.35 + 0.45 * coverage + 0.20 * min(total_runs / 16.0, 1.0), 0.0, 1.0)
    return CandidateFit(
        context=CONTEXT_MPLUS,
        score=score,
        label=label,
        source="wcl_mplus",
        display=f"{label} {int(round(score))} +{display_key}",
        colour=fit_colour(score),
        target_key=target_key,
        primary_key=display_key,
        confidence=confidence,
        coverage=coverage,
        same_dungeon_score=same_dungeon_score,
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


def _mplus_sparse_evidence_penalty(
    *, coverage: float, max_key_delta: int, has_same_dungeon: bool
) -> float:
    if has_same_dungeon:
        penalty = MPLUS_SAME_DUNGEON_SPARSE_PENALTY
    elif max_key_delta >= 4:
        penalty = MPLUS_OVERQUALIFIED_SPARSE_PENALTY
    else:
        penalty = MPLUS_SPARSE_PENALTY
    return penalty * (1.0 - coverage)


def _mplus_quality_coverage(rows: Iterable[_BracketFit]) -> float:
    if MPLUS_DUNGEON_COUNT <= 0:
        return 0.0
    covered = sum(_clamp(row.fit / 55.0, 0.0, 1.0) for row in rows)
    return _clamp(covered / MPLUS_DUNGEON_COUNT, 0.0, 1.0)


def _mplus_raw_quality_cap(
    score: float, raw_best_percent: float, max_key_delta: int
) -> float:
    if raw_best_percent < 25.0 and max_key_delta <= 1:
        return min(score, 24.0)
    if raw_best_percent < 50.0 and max_key_delta <= 1:
        return min(score, 49.0)
    if raw_best_percent >= 50.0:
        return score
    if max_key_delta >= 8:
        return min(score, 78.0)
    if max_key_delta >= 6:
        return min(score, 70.0)
    if max_key_delta >= 4:
        return min(score, 64.0)
    return score


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


def _weighted_top(rows: list[_BracketFit]) -> float:
    if not rows:
        return 0.0
    weights = [0.50, 0.30, 0.20]
    used_weights = weights[: len(rows)]
    total_weight = sum(used_weights)
    return sum(row.fit * weight for row, weight in zip(rows, used_weights, strict=True)) / total_weight


def _mplus_rio_fit(score: int, target_key: int) -> float:
    return _clamp(55.0 + (score - (1700.0 + target_key * 100.0)) / 18.0, 0.0, 105.0)


def _mplus_key_proximity_score(key_level: int, target_key: int) -> float:
    if key_level <= 0 or target_key <= 0:
        return 0.0
    delta = key_level - target_key
    if delta >= 1:
        return 94.0
    if delta == 0:
        return 88.0
    if delta == -1:
        return 78.0
    if delta == -2:
        return 64.0
    if delta == -3:
        return 50.0
    return _clamp(50.0 + delta * 8.0, 0.0, 45.0)


def _mplus_rio_timed_minus1_coverage(applicant: Applicant) -> float:
    dungeon_count = _positive_int(applicant.rio_dungeon_count)
    if dungeon_count <= 0:
        return 0.0
    return _clamp(applicant.rio_timed_at_or_above_minus1 / dungeon_count, 0.0, 1.0)


def _mplus_rio_completion_fit(
    applicant: Applicant, target_key: int, *, same_dungeon_key: int = 0
) -> float:
    if not applicant.rio_profile or target_key <= 0:
        return 0.0
    dungeon_count = max(1, min(_positive_int(applicant.rio_dungeon_count), MPLUS_DUNGEON_COUNT))
    timed_minus1 = _clamp(applicant.rio_timed_at_or_above_minus1 / dungeon_count, 0.0, 1.0)
    timed_minus2 = _clamp(applicant.rio_timed_at_or_above_minus2 / dungeon_count, 0.0, 1.0)
    completed_minus1 = _clamp(
        applicant.rio_completed_at_or_above_minus1 / dungeon_count, 0.0, 1.0
    )
    same_dungeon = _mplus_key_proximity_score(
        same_dungeon_key or applicant.rio_best_dungeon_key, target_key
    )
    best_overall = _mplus_key_proximity_score(applicant.rio_best_key, target_key)
    score_fit = _mplus_rio_fit(effective_rio_score(applicant), target_key)
    score = (
        0.36 * same_dungeon
        + 0.30 * (timed_minus1 * 100.0)
        + 0.14 * (timed_minus2 * 100.0)
        + 0.10 * (completed_minus1 * 100.0)
        + 0.07 * best_overall
        + 0.03 * score_fit
    )
    if applicant.rio_timed_at_or_above <= 0 and applicant.rio_best_key < target_key:
        score -= 4.0
    return _clamp(score, 0.0, 92.0)


def _mplus_rio_completion_confidence(
    applicant: Applicant, target_key: int, *, same_dungeon_key: int = 0
) -> float:
    if not applicant.rio_profile:
        return 0.0
    timed_minus1 = _mplus_rio_timed_minus1_coverage(applicant)
    same_near = (
        1.0
        if (same_dungeon_key or applicant.rio_best_dungeon_key) >= max(2, target_key - 1)
        else 0.0
    )
    best_near = 1.0 if applicant.rio_best_key >= max(2, target_key - 1) else 0.0
    return _clamp(0.25 + 0.45 * timed_minus1 + 0.15 * same_near + 0.15 * best_near, 0.0, 1.0)


def _mplus_rio_completion_floor_with_wcl(
    rio_completion_fit: float,
    *,
    raw_best_percent: float,
    max_key_delta: int,
    target_key: int,
    rio_same_dungeon_key: int,
    rio_best_key: int,
    rio_timed_at_or_above: int,
) -> float:
    if rio_completion_fit <= 0.0:
        return 0.0
    rio_key = rio_same_dungeon_key or rio_best_key
    rio_key_delta = rio_key - target_key if rio_key > 0 and target_key > 0 else -999
    # WHY: This is only a RaiderIO completion floor under partial WCL evidence.
    # Let it rescue stale/missing logs into strong FIT, but reserve TOP for WCL-earned scores.
    cap = 84.0
    if rio_timed_at_or_above <= 0 and rio_key_delta < 0:
        cap = 78.0
    if rio_timed_at_or_above <= 0 and rio_key_delta <= -2:
        cap = 68.0
    if max_key_delta >= -1 and raw_best_percent < 25.0:
        return min(rio_completion_fit, cap, 61.0)
    if max_key_delta >= -2 and raw_best_percent < 40.0:
        return min(rio_completion_fit, cap, 68.0)
    if raw_best_percent < 50.0:
        return min(rio_completion_fit, cap, 78.0)
    return min(rio_completion_fit, cap)


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
