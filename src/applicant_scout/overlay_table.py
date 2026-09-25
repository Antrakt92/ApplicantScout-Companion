"""Pure table model for the overlay applicant table (P2 extraction).

Model half of ``OverlayWindow._refresh_table``: sorting, per-row fit
prefetch, and group bookkeeping — no Qt. The window method stays a thin
controller that calls :func:`refresh_table_model`, diffs row identity /
render keys, owns ``QTableWidgetItem`` lifetime, and re-applies hover, pin,
filter, and delegate state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import overlay_rows as _overlay_rows
from .metric_preferences import MetricPreferences
from .scoring import (
    CONTEXT_MPLUS,
    CONTEXT_RAID,
    CandidateFit,
    PackageFit,
    candidate_fit,
    detect_listing_context,
    package_fit,
)
from .state import Applicant, AppState, Listing


@dataclass(slots=True)
class TableModel:
    """Sorted rows plus the group/fit maps the view needs to render them.

    Group maps are filled by :func:`index_group_maps` from the final
    (post-manual-sort) order — mirroring ``OverlayWindow._refresh_table``,
    which enumerates group positions after the manual column sort so pinned
    descriptions and package lanes follow the displayed order.
    """

    sorted_applicants: list[Applicant] = field(default_factory=list)
    package_fit_by_raw: dict[str, PackageFit] = field(default_factory=dict)
    candidate_fit_by_id: dict[str, CandidateFit] = field(default_factory=dict)
    group_size_by_raw: dict[str, int] = field(default_factory=dict)
    group_position_by_id: dict[str, int] = field(default_factory=dict)
    group_ready_by_raw: dict[str, bool] = field(default_factory=dict)
    #: Raw helper fits from the grouped sort (including non-display ones).
    #: Indexing source for :func:`index_group_maps`; the manual FIT sort
    #: reads ``package_fit_by_raw`` instead (equivalent — it checks
    #: ``.display`` itself, so filtered-out groups fall through identically).
    sort_package_fits_by_raw: dict[str, PackageFit] = field(default_factory=dict)


def refresh_table_model(
    state: AppState,
    listing: Listing | None,
    prefs: MetricPreferences,
    *,
    active_tab: str,
    package_fit_cache: dict[str, tuple[object, PackageFit]] | None = None,
    compute_party_fits: bool = False,
    package_fit_fn: Callable[
        [Iterable[Applicant], Listing | None], PackageFit
    ]
    | None = None,
    candidate_fit_fn: Callable[[Applicant, Listing | None], CandidateFit]
    | None = None,
) -> TableModel:
    """Build the sorted applicant model for one table refresh.

    ``state`` supplies ``applicants``/``party_members``; ``listing`` is the
    effective listing; ``prefs`` provides the fit-cache context.
    ``compute_party_fits`` mirrors the window's FIT-visibility/manual-sort
    gate (party tab only): precompute each member fit once per refresh and
    share it between manual FIT sorting and cell rendering.
    ``package_fit_fn``/``candidate_fit_fn`` arrive from the window's module
    namespace so test doubles patched onto ``overlay`` keep working exactly
    as when the calls were inlined in ``OverlayWindow._refresh_table``.
    """
    package_fit_fn = package_fit_fn or package_fit
    candidate_fit_fn = candidate_fit_fn or candidate_fit
    model = TableModel()
    if active_tab == "party":
        model.sorted_applicants = _overlay_rows.sort_roster_members(
            state.party_members.values()
        )
        # M8: compute each member fit ONCE per refresh and share it
        # between manual FIT sorting and cell rendering, instead of one
        # candidate_fit per sort probe plus one per cell.
        if compute_party_fits:
            for member in model.sorted_applicants:
                if (
                    member.applicant_id not in model.candidate_fit_by_id
                    and member.fetch_status not in {"loading", "pending"}
                ):
                    model.candidate_fit_by_id[member.applicant_id] = (
                        candidate_fit_fn(member, listing)
                    )
        return model

    (
        model.sorted_applicants,
        model.sort_package_fits_by_raw,
        model.candidate_fit_by_id,
    ) = _overlay_rows.sort_applicants_grouped_with_package_fits(
        state.applicants.values(),
        listing,
        package_fit_cache=package_fit_cache,
        fit_cache_context=prefs.cache_key(),
        package_fit_fn=package_fit_fn,
    )
    index_group_maps(model, listing, package_fit_fn)
    return model


def index_group_maps(
    model: TableModel,
    listing: Listing | None,
    package_fit_fn: Callable[
        [Iterable[Applicant], Listing | None], PackageFit
    ]
    | None = None,
) -> TableModel:
    """(Re)build group maps from ``model.sorted_applicants`` in place.

    Runs after the final row order is known (the window applies its manual
    column sort between :func:`refresh_table_model` and this call), so group
    positions and package lanes follow the displayed order. Idempotent:
    re-running on an unchanged order rebuilds identical maps.
    """
    resolve_fit = package_fit_fn or package_fit
    package_fits_by_raw = model.sort_package_fits_by_raw
    model.group_size_by_raw = {}
    model.group_position_by_id = {}
    model.group_ready_by_raw = {}
    model.package_fit_by_raw = {}
    group_members: dict[str, list[Applicant]] = {}
    for applicant in model.sorted_applicants:
        raw_aid, _ = _overlay_rows.split_composite(applicant.applicant_id)
        model.group_size_by_raw[raw_aid] = model.group_size_by_raw.get(raw_aid, 0) + 1
        group_members.setdefault(raw_aid, []).append(applicant)
    for members in group_members.values():
        for position, member in enumerate(members, start=1):
            model.group_position_by_id[member.applicant_id] = position
    if detect_listing_context(listing) in (
        CONTEXT_MPLUS,
        CONTEXT_RAID,
    ):
        for raw_aid, members in group_members.items():
            model.group_ready_by_raw[raw_aid] = all(
                member.fetch_status == "ready" for member in members
            )
            if len(members) < 2:
                continue
            fit = package_fits_by_raw.get(raw_aid)
            if fit is None:
                fit = resolve_fit(members, listing)
            if fit.display:
                model.package_fit_by_raw[raw_aid] = fit
    return model
