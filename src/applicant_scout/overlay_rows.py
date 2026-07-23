"""Pure row grouping, sorting, and render-key helpers for the overlay."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass

from .constants import group_id_colour
from .scoring import (
    CONTEXT_MPLUS,
    CONTEXT_RAID,
    CandidateFit,
    PackageFit,
    detect_listing_context,
    effective_rio_score,
    package_fit,
    positive_int,
    role_mplus_view,
)
from .state import Applicant, Listing


_SUNK_STATES: frozenset[str] = frozenset({"error", "not_found"})
_PROVISIONAL_STATES: frozenset[str] = frozenset({"loading", "pending"})
_MPLUS_CATEGORY_ID = 2


@dataclass(frozen=True)
class GroupMarker:
    colour: str
    first_visible: bool
    last_visible: bool
    position: int
    size: int


def split_composite(composite_id: str) -> tuple[str, int]:
    """Parse a rendered member id without letting malformed input escape."""
    if ":" not in composite_id:
        return composite_id, 1
    raw, member = composite_id.split(":", 1)
    try:
        return raw, int(member)
    except ValueError:
        return raw, 1


def application_count(applicant_ids: Iterable[str]) -> int:
    """Count distinct Blizzard LFG applications, not rendered member rows."""
    return len(
        {
            raw_aid
            for applicant_id in applicant_ids
            if (raw_aid := split_composite(applicant_id)[0])
        }
    )


def freeze_render_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return tuple(
            (field.name, freeze_render_value(getattr(value, field.name)))
            for field in fields(value)
        )
    if isinstance(value, Mapping):
        return tuple(
            sorted(
                (
                    (
                        freeze_render_value(key),
                        freeze_render_value(item_value),
                    )
                    for key, item_value in value.items()
                ),
                key=repr,
            )
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_render_value(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((freeze_render_value(item) for item in value), key=repr))
    return value


def listing_render_key(listing: Listing | None) -> object:
    return freeze_render_value(listing)


def mplus_key_level(entry: object) -> int:
    if not isinstance(entry, dict):
        return 0
    return positive_int(entry.get("key_level"))


def highest_mplus_key_level(breakdown: Iterable[object]) -> int:
    highest = 0
    for entry in breakdown:
        highest = max(highest, mplus_key_level(entry))
    return highest


def mplus_headline_sort_score(applicant: Applicant) -> tuple[int, float]:
    if applicant.fetch_status in _SUNK_STATES:
        return (0, 0.0)
    _metric_label, breakdown, best, _median = role_mplus_view(applicant)
    return (highest_mplus_key_level(breakdown), float(best or 0.0))


def sort_applicants_grouped_with_package_fits(
    applicants: Iterable[Applicant],
    listing: Listing | None = None,
    *,
    package_fit_cache: dict[str, tuple[object, PackageFit]] | None = None,
    fit_cache_context: object = None,
    package_fit_fn: Callable[[Iterable[Applicant], Listing | None], PackageFit] = package_fit,
) -> tuple[list[Applicant], dict[str, PackageFit], dict[str, CandidateFit]]:
    """Sort groups atomically while retaining package and member fit results."""
    apps = list(applicants)
    package_fits: dict[str, PackageFit] = {}
    candidate_fits: dict[str, CandidateFit] = {}
    group_max: dict[str, int] = {}
    group_fit: dict[str, float] = {}
    group_confidence: dict[str, float] = {}
    group_mplus_headline: dict[str, tuple[int, float]] = {}
    group_has_ready: dict[str, bool] = {}
    group_has_provisional: dict[str, bool] = {}
    use_fit = detect_listing_context(listing) in (CONTEXT_MPLUS, CONTEXT_RAID)
    use_mplus_headline = (
        not use_fit
        and listing is not None
        and listing.category_id == _MPLUS_CATEGORY_ID
    )
    group_members: dict[str, list[Applicant]] = {}
    for applicant in apps:
        raw_aid, _ = split_composite(applicant.applicant_id)
        group_members.setdefault(raw_aid, []).append(applicant)
        rio_score = effective_rio_score(applicant)
        if rio_score > group_max.get(raw_aid, -1):
            group_max[raw_aid] = rio_score
        if applicant.fetch_status not in _SUNK_STATES:
            group_has_ready[raw_aid] = True
        if applicant.fetch_status in _PROVISIONAL_STATES:
            group_has_provisional[raw_aid] = True
    if package_fit_cache is not None:
        for stale_raw_aid in set(package_fit_cache) - set(group_members):
            package_fit_cache.pop(stale_raw_aid, None)
    if use_fit:
        for raw_aid, members in group_members.items():
            fit_cache_key = (
                fit_cache_context,
                listing_render_key(listing),
                tuple(freeze_render_value(member) for member in members),
            )
            cached_fit = (
                package_fit_cache.get(raw_aid)
                if package_fit_cache is not None
                else None
            )
            if cached_fit is not None and cached_fit[0] == fit_cache_key:
                fit = cached_fit[1]
            else:
                fit = package_fit_fn(members, listing)
                if package_fit_cache is not None:
                    package_fit_cache[raw_aid] = (fit_cache_key, fit)
            package_fits[raw_aid] = fit
            for member, member_fit in zip(members, fit.member_fits, strict=True):
                candidate_fits[member.applicant_id] = member_fit
            group_fit[raw_aid] = fit.score
            group_confidence[raw_aid] = fit.confidence
    elif use_mplus_headline:
        for raw_aid, members in group_members.items():
            group_mplus_headline[raw_aid] = min(
                (mplus_headline_sort_score(member) for member in members),
                default=(0, 0.0),
            )

    def _key(applicant: Applicant):
        raw_aid, member_idx = split_composite(applicant.applicant_id)
        group_rio = group_max.get(raw_aid, 0)
        group_score = group_fit.get(raw_aid, 0.0)
        group_confidence_score = group_confidence.get(raw_aid, 0.0)
        headline_key, headline_percent = group_mplus_headline.get(raw_aid, (0, 0.0))
        all_sunk = not group_has_ready.get(raw_aid, False)
        provisional = group_has_provisional.get(raw_aid, False)
        sunk = applicant.fetch_status in _SUNK_STATES
        if use_fit:
            no_fit = group_score <= 0.0
            return (
                no_fit,
                provisional if not no_fit else False,
                all_sunk if no_fit else False,
                -int(round(group_score)),
                -group_confidence_score,
                -group_score,
                -group_rio,
                raw_aid,
                member_idx,
                sunk,
            )
        if use_mplus_headline:
            return (
                all_sunk,
                headline_key <= 0,
                -headline_key,
                -headline_percent,
                -group_rio,
                raw_aid,
                member_idx,
                sunk,
            )
        return (group_rio == 0, -group_rio, all_sunk, raw_aid, member_idx, sunk)

    return sorted(apps, key=_key), package_fits, candidate_fits


def sort_roster_members(members: Iterable[Applicant]) -> list[Applicant]:
    role_order = {"TANK": 0, "HEALER": 1, "DAMAGER": 2}
    return sorted(
        members,
        key=lambda member: (
            role_order.get(member.role, 3),
            -effective_rio_score(member),
            member.name.lower(),
        ),
    )


def build_group_markers(
    visible_id_by_row: Iterable[tuple[int, str]],
) -> dict[int, GroupMarker]:
    """Build bracket metadata from currently visible rows."""
    members_by_raw: dict[str, list[tuple[int, str]]] = {}
    for row, applicant_id in visible_id_by_row:
        raw_aid, _member_idx = split_composite(applicant_id)
        members_by_raw.setdefault(raw_aid, []).append((row, applicant_id))

    markers: dict[int, GroupMarker] = {}
    for raw_aid, members in members_by_raw.items():
        size = len(members)
        if size < 2:
            continue
        colour = group_id_colour(raw_aid)
        for position, (row, _applicant_id) in enumerate(members, start=1):
            markers[row] = GroupMarker(
                colour=colour,
                first_visible=position == 1,
                last_visible=position == size,
                position=position,
                size=size,
            )
    return markers
