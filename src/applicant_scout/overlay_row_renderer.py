"""Per-row cell builders for the overlay applicant table (P2 extraction).

Render half of ``OverlayWindow._render_row``: spec/name/iLvl/RIO/raid/M+/
fit cells plus the accessible-description tail — no window state. The
window method stays a thin controller that resolves the effective listing,
builds a :class:`RowRenderEnv` from its table helpers, and calls
:func:`render_row_cells`.

Qt item factories (``*_dual_cell``/``*_group_cell``/``fit_cell``) and the
tiny foreground/background/font helpers stay defined in ``overlay.py``
(the info panel and column-width paths share them); they are passed in via
``RowRenderEnv`` so this module never imports the window. Column indices
and item-data roles likewise arrive via the env (defined once in
``overlay.py``), as do the row's package/group maps and pin/keyboard state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

from . import overlay_rows as _overlay_rows
from . import overlay_presenters as _presenters
from .constants import CLASS_COLOURS, SPEC_SHORT_NAMES, rio_score_colour
from .scoring import (
    CONTEXT_MPLUS,
    CONTEXT_RAID,
    CandidateFit,
    PackageFit,
    detect_listing_context,
    effective_rio_score,
)
from .state import Applicant, Listing


@dataclass(slots=True)
class RowRenderEnv:
    """Everything ``render_row_cells`` needs from the window, passed plainly."""

    listing: Listing | None
    package_fit_by_raw: Mapping[str, PackageFit]
    group_size_by_raw: Mapping[str, int]
    group_position_by_id: Mapping[str, int]
    group_ready_by_raw: Mapping[str, bool]
    pinned_id: str | None = None
    keyboard_id: str | None = None
    keyboard_preview_active: bool = False
    col_spec: int = 0
    col_name: int = 1
    col_ilvl: int = 2
    col_rio: int = 3
    col_n: int = 4
    col_h: int = 5
    col_m: int = 6
    col_mplus: int = 7
    col_fit: int = 8
    package_text_role: int = 0
    individual_text_role: int = 0
    row_base_role: int = 0
    reuse_item: Callable[[int, int, str], QTableWidgetItem] | None = None
    set_data: (
        Callable[[QTableWidgetItem, Qt.ItemDataRole | int, object], None] | None
    ) = None
    set_row_height: Callable[[int, int], None] | None = None
    rio_cell_height: Callable[[QTableWidgetItem], int] | None = None
    table_font: Callable[[], QFont] | None = None
    role_icon: Callable[[str], QIcon | None] | None = None
    bold_font: Callable[[QFont | None], QFont] | None = None
    metric_font: Callable[[QFont | None, bool], QFont] | None = None
    text_colour_for_bg: Callable[[str | None], str | None] | None = None
    set_cell_foreground: Callable[[QTableWidgetItem, str | None], None] | None = None
    set_cell_background: Callable[[QTableWidgetItem, str | None], None] | None = None
    stamp_cell_font_sig: Callable[[QTableWidgetItem], None] | None = None
    raid_dual_cell: Callable[..., QTableWidgetItem] | None = None
    mplus_dual_cell: Callable[..., QTableWidgetItem] | None = None
    mplus_group_cell: Callable[..., QTableWidgetItem] | None = None
    fit_cell: Callable[..., QTableWidgetItem] | None = None


def render_row_cells(
    row: int,
    applicant: Applicant,
    fit: CandidateFit | None,
    *,
    table: QTableWidget,
    env: RowRenderEnv,
) -> None:
    """Write applicant data into an existing table row. Caller manages
    row creation / position — used by _refresh_table after sort.

    Per-cell tooltips removed: applicant data now lives in the top
    ApplicantInfoPanel which is row-hover/pin driven. Cell items are
    plain text + colour only.

    Items are updated in place when the row already exists: re-rendering
    a changed row rewrites text/roles without reallocating 9 items and
    without re-querying the system font per cell (see the cached cell
    fonts). Accessible roles are rewritten only when their value
    changed — they fire accessibility events on every write.
    """
    assert env.reuse_item is not None
    assert env.set_data is not None
    assert env.set_row_height is not None
    assert env.rio_cell_height is not None
    assert env.table_font is not None
    assert env.role_icon is not None
    assert env.bold_font is not None
    assert env.metric_font is not None
    assert env.text_colour_for_bg is not None
    assert env.set_cell_foreground is not None
    assert env.set_cell_background is not None
    assert env.stamp_cell_font_sig is not None
    assert env.raid_dual_cell is not None
    assert env.mplus_dual_cell is not None
    assert env.mplus_group_cell is not None
    assert env.fit_cell is not None
    set_data = env.set_data
    spec_text = SPEC_SHORT_NAMES.get(applicant.spec_id, f"#{applicant.spec_id}")
    spec_item = env.reuse_item(row, env.col_spec, spec_text)
    spec_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    set_data(spec_item, Qt.ItemDataRole.UserRole, applicant.role)
    icon = env.role_icon(applicant.role)
    if icon is not None:
        if spec_item.icon().cacheKey() != icon.cacheKey():
            spec_item.setIcon(icon)
    # Class-coloured cell background mirrors the panel's `_class_pill` so
    # the table and info-panel use the same visual language for class
    # identity. Foreground follows the same contrast helper as the panel so
    # dark DK/DH colours remain readable while pale class colours stay dark.
    # Hover/pin stripes are painted by `_HoverHighlightDelegate` on top.
    cls_hex = CLASS_COLOURS.get(applicant.cls, "#888888")
    env.set_cell_background(spec_item, cls_hex)
    env.set_cell_foreground(spec_item, env.text_colour_for_bg(cls_hex))
    spec_item.setFont(env.bold_font(env.table_font()))
    env.stamp_cell_font_sig(spec_item)

    # Display Charname only (without -Realm) for compactness; full in panel
    display_name = applicant.name.split("-", 1)[0]
    name_item = env.reuse_item(row, env.col_name, display_name)
    env.set_cell_foreground(
        name_item, CLASS_COLOURS.get(applicant.cls, "#FFFFFF")
    )
    name_item.setFont(env.bold_font(env.table_font()))
    env.stamp_cell_font_sig(name_item)

    # iLvl + RIO numeric cells. RIO shows the applying character's score,
    # plus a higher RaiderIO main score in brackets when available.
    ilvl_item = env.reuse_item(
        row, env.col_ilvl, str(applicant.ilvl) if applicant.ilvl else "—"
    )
    ilvl_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    ilvl_item.setFont(env.metric_font(env.table_font(), False))
    env.stamp_cell_font_sig(ilvl_item)

    rio_score = effective_rio_score(applicant)
    rio_item = env.reuse_item(
        row, env.col_rio, _presenters.rio_table_text(applicant)
    )
    rio_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    # bold — primary scouting signal alongside name
    rio_item.setFont(env.metric_font(env.table_font(), True))
    env.stamp_cell_font_sig(rio_item)
    # Tier-band foreground colour (gold/purple/blue/green/white) matches
    # RaiderIO addon's score-tier visual language, mirrors raid percentile
    # cell palette so eye-tracking across columns reads consistently.
    env.set_cell_foreground(rio_item, rio_score_colour(rio_score))
    history_text = _presenters.rio_history_text(applicant)
    history_tip = f"Raider.IO · {history_text}" if history_text else ""
    if rio_item.toolTip() != history_tip:
        rio_item.setToolTip(history_tip)
    env.set_row_height(row, env.rio_cell_height(rio_item))

    raw_aid, _ = _overlay_rows.split_composite(applicant.applicant_id)
    listing = env.listing
    listing_context = detect_listing_context(listing)
    package = env.package_fit_by_raw.get(raw_aid)

    # Raw WCL evidence keeps the same meaning and palette in every listing.
    # Contextual recommendations have a separate cell.
    raid_cells = [
        ("N", env.col_n, applicant.raid_normal, applicant.raid_normal_median),
        ("H", env.col_h, applicant.raid_heroic, applicant.raid_heroic_median),
        ("M", env.col_m, applicant.raid_mythic, applicant.raid_mythic_median),
    ]
    for _raid_key, col, best, median in raid_cells:
        existing = table.item(row, col)
        filled = env.raid_dual_cell(
            best,
            median,
            applicant.fetch_status,
            item=existing,
        )
        if existing is None:
            table.setItem(row, col, filled)
    existing = table.item(row, env.col_mplus)
    filled = env.mplus_dual_cell(
        applicant,
        listing,
        fit=fit,
        item=existing,
    )
    if existing is None:
        table.setItem(row, env.col_mplus, filled)
    if (
        listing_context in {CONTEXT_MPLUS, CONTEXT_RAID}
        and package is not None
        and package.display
        and env.group_size_by_raw.get(raw_aid, 1) >= 2
        and (
            listing_context == CONTEXT_MPLUS
            or env.group_ready_by_raw.get(raw_aid, False)
        )
    ):
        existing = table.item(row, env.col_fit)
        filled = env.mplus_group_cell(
            package,
            applicant,
            listing,
            fit=fit,
            item=existing,
        )
        if existing is None:
            table.setItem(row, env.col_fit, filled)
    else:
        existing = table.item(row, env.col_fit)
        filled = env.fit_cell(
            applicant,
            listing,
            fit=fit,
            item=existing,
        )
        if existing is None:
            table.setItem(row, env.col_fit, filled)

    accessible_headers = (
        "Specialization",
        "Name",
        "Item level",
        "RaiderIO score",
        "Normal raid",
        "Heroic raid",
        "Mythic raid",
        "Mythic Plus DPS percentiles",
        "Contextual fit estimate",
    )
    role_name = {
        "TANK": "Tank",
        "HEALER": "Healer",
        "DAMAGER": "Damage dealer",
    }.get(applicant.role, "Unknown")
    raw_aid, _member_index = _overlay_rows.split_composite(applicant.applicant_id)
    group_size = env.group_size_by_raw.get(raw_aid, 1)
    description = (
        f"Role: {role_name}. Specialization: {spec_text}. "
        f"Class: {applicant.cls.title()}."
    )
    if group_size > 1:
        group_position = env.group_position_by_id.get(applicant.applicant_id, 1)
        description += (
            f" Group application member {group_position} of {group_size}."
        )
    # Interaction suffix mirrors _sync_row_accessible_descriptions so a
    # re-rendered pinned/keyboard row keeps its full description without
    # waiting for the next sync pass.
    interaction_state = ""
    if applicant.applicant_id == env.pinned_id:
        interaction_state = "Pinned."
    if (
        env.keyboard_preview_active
        and applicant.applicant_id == env.keyboard_id
    ):
        interaction_state = f"{interaction_state} Keyboard preview.".strip()
    expected_description = (
        f"{description} {interaction_state}"
        if interaction_state
        else description
    )
    for column, header in enumerate(accessible_headers):
        item = table.item(row, column)
        if item is None:
            continue
        if column == env.col_name:
            value = applicant.name
        elif column == env.col_fit and isinstance(
            item.data(env.package_text_role), str
        ):
            package_text = str(item.data(env.package_text_role) or "No data")
            individual_text = str(
                item.data(env.individual_text_role) or "No data"
            )
            value = f"Group package {package_text}; individual {individual_text}"
        else:
            value = item.text() or "No data"
        # Accessible writes fire OS accessibility events: only rewrite on
        # actual value change, not on every row refresh.
        set_data(
            item,
            Qt.ItemDataRole.AccessibleTextRole,
            f"{header}: {value}",
        )
        set_data(item, env.row_base_role, description)
        set_data(
            item, Qt.ItemDataRole.AccessibleDescriptionRole, expected_description
        )


__all__ = ["RowRenderEnv", "render_row_cells"]
