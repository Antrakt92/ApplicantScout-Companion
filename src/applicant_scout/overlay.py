"""PyQt6 frameless always-on-top overlay window with applicant table."""

from __future__ import annotations


import html
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
from PyQt6.QtCore import (
    Qt,
    QSize,
    pyqtSignal,
    QTimer,
    QRunnable,
    QThreadPool,
    QObject,
    QPoint,
    QRect,
)
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QMouseEvent,
    QPalette,
)
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizeGrip,
    QSpinBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QStyle,
    QStyleOptionViewItem,
)


from .constants import (
    ALL_ROLES,
    CLASS_COLOURS,
    ROLE_COLOURS,
    ROLE_GLYPHS,
    ROLE_LABELS,
    SPEC_SHORT_NAMES,
    MPLUS_ENCOUNTERS,
    group_id_colour,
    mplus_dungeon_name_for_activity_id,
    percentile_colour,
    rio_score_colour,
)
from .metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences
from .scoring import (
    CONTEXT_MPLUS,
    CONTEXT_RAID,
    PackageFit,
    candidate_fit,
    detect_listing_context,
    effective_rio_score,
    listing_dungeon_keys,
    mplus_dungeon_fit_rows,
    package_fit,
    nonnegative_int,
    positive_int,
    role_mplus_view,
    safe_percent,
)
from .state import (
    Applicant,
    AppState,
    Listing,
    LauncherPosition,
    WindowGeometry,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    WINDOW_GEOMETRY_LAYOUT_VERSION,
    save_geometry,
    load_geometry,
    load_launcher_position,
    save_launcher_position,
)
from .wcl import (
    WCLClient,
    WCLApiError,
    WCLAuthError,
    WCL_ERROR_AUTH,
    WCL_ERROR_NETWORK,
    WCL_ERROR_QUOTA_GUARD,
    WCL_ERROR_RATE_LIMITED,
    WCL_ERROR_SERVER,
    CharacterCache,
    CharacterRanks,
    derive_server_slug,
    default_realm_from_player,
    split_name_realm,
    wcl_metric_role,
)


_log = logging.getLogger("applicant_scout.overlay")


# Compact column layout. Name can grow a little for real applicants, but is
# capped so one long character name cannot force the whole overlay wide.
COLUMN_HEADERS = ["Spec", "Name", "iLvl", "RIO", "N", "H", "M", "M+"]
COLUMN_WIDTHS = [74, 112, 44, 84, 50, 50, 50, 88]
NAME_COLUMN_MAX_WIDTH = 126
DUNGEON_NAME_WIDTH = 148
DUNGEON_KEY_WIDTH = 72
DUNGEON_WCL_KEY_WIDTH = 72
DUNGEON_METRIC_WIDTH = 58
COL_SPEC, COL_NAME, COL_ILVL, COL_RIO, COL_N, COL_H, COL_M, COL_MPLUS = range(8)
WINDOW_CHROME_WIDTH = DEFAULT_WINDOW_WIDTH - sum(COLUMN_WIDTHS)
MIN_VISIBLE_WINDOW_WIDTH = 420
USER_MIN_WINDOW_WIDTH = 300
USER_MIN_WINDOW_HEIGHT = 220
INFO_PANEL_MIN_HEIGHT = 80
INFO_PANEL_PREFERRED_HEIGHT = 220
LAUNCHER_SIZE = 42
GAME_FOREGROUND_POLL_MS = 500
MPLUS_GROUP_COLUMN_WIDTH = 188
MPLUS_PACKAGE_TEXT_ROLE = Qt.ItemDataRole.UserRole + 20
MPLUS_PACKAGE_BG_ROLE = Qt.ItemDataRole.UserRole + 21
MPLUS_INDIVIDUAL_TEXT_ROLE = Qt.ItemDataRole.UserRole + 22
MPLUS_INDIVIDUAL_FG_ROLE = Qt.ItemDataRole.UserRole + 23
MPLUS_INDIVIDUAL_BG_ROLE = Qt.ItemDataRole.UserRole + 24
MPLUS_GROUP_LANE_MAX_WIDTH = 72
MPLUS_GROUP_LANE_MIN_WIDTH = 42
MPLUS_INDIVIDUAL_LANE_MIN_WIDTH = 56


# Auto-hide delay (seconds) when applicants drain to zero but listing is still
# active. M+ keystone listings don't auto-delist after the group fills, so the
# addon keeps emitting snapshots with `applicants=[]` indefinitely. Hiding
# immediately on the first empty snapshot would flicker the window when
# applicants come and go in churn — the timer rate-limits to one hide per
# quiet period.
EMPTY_HIDE_DELAY_S = 5
MAX_WCL_RETRY_BATCH = 3
WCL_RETRY_CUSHION_MS = 250
WCL_RETRY_BATCH_DELAY_MS = 1500
LEGACY_COMPACT_WINDOW_WIDTH = 526
_RETRYABLE_WCL_ERROR_KINDS = frozenset(
    {WCL_ERROR_QUOTA_GUARD, WCL_ERROR_RATE_LIMITED, WCL_ERROR_SERVER, WCL_ERROR_NETWORK}
)
ROLE_ICON_SIZE = QSize(16, 16)
ROLE_ICON_FILES = {
    "TANK": "role_tank.png",
    "HEALER": "role_healer.png",
    "DAMAGER": "role_dps.png",
}
_ROLE_ICON_CACHE: dict[str, QIcon | None] = {}


def _role_icon(role: str) -> QIcon | None:
    if role not in _ROLE_ICON_CACHE:
        icon_name = ROLE_ICON_FILES.get(role)
        icon_path = (
            Path(__file__).with_name("assets") / "role_icons" / icon_name
            if icon_name
            else None
        )
        icon = QIcon(str(icon_path)) if icon_path and icon_path.is_file() else QIcon()
        _ROLE_ICON_CACHE[role] = icon if not icon.isNull() else None
    return _ROLE_ICON_CACHE[role]


# Per-column legend shown when the user hovers a header cell. Surfaces the
# meaning of each column without requiring docs / a separate help screen,
# and explains the raid pair + context-fit conventions used by score cells.
# Indexed by column INDEX (matches COLUMN_HEADERS positions) so refactors
# of header text don't desync the lookup.
HEADER_TOOLTIPS: list[str] = [
    # Spec
    "Applicant's spec at the time they applied to your group.\n\n"
    "Compact name (e.g. 'Brm', 'Holy', 'Resto'). '#NNNN' means the\n"
    "spec_id is unknown to the companion — likely a new spec that needs\n"
    "to be added to constants.py SPEC_SHORT_NAMES.",
    # Name
    "Applicant's character name (Charname-Realm in the hover panel).\n\n"
    "Coloured by class (warrior brown, mage cyan, rogue yellow, etc.).\n"
    "Hover the row for the full scout summary in the top panel.",
    # iLvl
    "Equipped item level reported by Blizzard's LFG API.\n\n"
    "Higher = better gear ceiling, but doesn't tell skill on its own —\n"
    "use alongside RIO + raid/M+ percentiles.",
    # RIO
    "RaiderIO M+ score for this character. If RaiderIO is installed and exposes\n"
    "a higher main score for an alt, the cell shows current [main] and sorting\n"
    "uses the higher score.\n\n"
    "Coloured by tier band (gold ≥3200, purple ≥2700, blue ≥2200,\n"
    "green ≥1700, white below). Mid-Midnight-S1 thresholds.",
    # N
    "Raid Normal — best/median per-encounter parse percentile.\n\n"
    "Format: 'best/median' (e.g. '88/72'). Best = ceiling on a single boss.\n"
    "Median = consistency across encounters at this difficulty.\n"
    "Single value (no slash) = only one encounter logged → no median signal.\n\n"
    "Background colour = best percentile tier (WCL ranking palette:\n"
    "tan 100, pink 99, orange 95-98, purple 75-94,\n"
    "blue 50-74, green 25-49, gray 0-24).",
    # H
    "Raid Heroic — best/median per-encounter parse percentile.\n\n"
    "Same format and colour scheme as the Normal column. Use Heroic as\n"
    "the primary raid signal — most pugs that care about parses run\n"
    "Heroic, fewer have meaningful Mythic data.",
    # M
    "Raid Mythic — best/median per-encounter parse percentile.\n\n"
    "Same format as Normal/Heroic. Few pug applicants will have data\n"
    "here; '—' means no Mythic logs in current spec.",
    # M+
    "Mythic+ fit for the current listing when the companion knows your\n"
    "hosted key level; otherwise falls back to the old best/median headline.\n\n"
    "Metric: DPS for tank / damage applicants, HPS for healers.\n"
    "Fit labels are driven primarily by relevant WCL bracket performance,\n"
    "then adjusted for key-level context, same-dungeon evidence, and profile\n"
    "consistency. Sparse coverage penalizes the fit and lowers confidence\n"
    "instead of giving free score; RIO support is only a small nudge or\n"
    "fallback when WCL data is missing. Group rows show a package rating on\n"
    "the leader row because group applicants are accepted together.\n\n"
    "Background colour follows the numeric fit score with the WCL ranking\n"
    "palette, after those context guards are applied.\n\n"
    "Hover the row for the per-dungeon breakdown in the top panel.",
]


# ───────────────────────────────────────────────────────────────────
# WCL fetch worker (off main thread)


class _FetchSignals(QObject):
    done = pyqtSignal(object, object)  # _FetchIdentity, CharacterRanks


@dataclass(frozen=True)
class _FetchIdentity:
    applicant_id: str
    charname_key: str
    server_slug: str
    region: str
    spec_id: int
    metric_role: str
    row_source: str = "applicants"
    runtime_generation: int = 0
    metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES
    listing_session_generation: int = 0

    @property
    def storage_key(self) -> str:
        if self.row_source == "applicants":
            return self.applicant_id
        return f"{self.row_source}:{self.applicant_id}"


@dataclass(frozen=True)
class _GroupMarker:
    colour: str
    first_visible: bool
    last_visible: bool
    position: int
    size: int


def _fetch_identity_for_applicant(
    applicant: Applicant,
    player_full_name: str,
    region: str,
    metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
    runtime_generation: int = 0,
    listing_session_generation: int = 0,
    row_source: str = "applicants",
) -> tuple[_FetchIdentity, str] | None:
    charname, realm = split_name_realm(
        applicant.name, default_realm_from_player(player_full_name)
    )
    server_slug = derive_server_slug(realm)
    if not charname or not server_slug:
        return None
    return (
        _FetchIdentity(
            applicant_id=applicant.applicant_id,
            charname_key=charname.lower(),
            server_slug=server_slug,
            region=region,
            spec_id=applicant.spec_id,
            metric_role=wcl_metric_role(applicant.role),
            row_source=row_source,
            runtime_generation=runtime_generation,
            metric_preferences=metric_preferences,
            listing_session_generation=listing_session_generation,
        ),
        charname,
    )


def _same_fetch_target_except_preferences(
    left: _FetchIdentity, right: _FetchIdentity
) -> bool:
    return (
        left.applicant_id == right.applicant_id
        and left.row_source == right.row_source
        and left.charname_key == right.charname_key
        and left.server_slug == right.server_slug
        and left.region == right.region
        and left.spec_id == right.spec_id
        and left.metric_role == right.metric_role
        and left.runtime_generation == right.runtime_generation
        and left.listing_session_generation == right.listing_session_generation
    )


class _FetchTask(QRunnable):
    def __init__(
        self,
        identity: _FetchIdentity,
        name: str,
        client: WCLClient,
        cache: CharacterCache,
    ):
        super().__init__()
        self.signals = _FetchSignals()
        self._identity = identity
        self._name = name
        self._client = client
        self._cache = cache
        self._cache_generation = cache.generation

    def run(self) -> None:
        identity = self._identity
        started_at = time.perf_counter()
        _log.info(
            "WCL fetch started: %s-%s region=%s spec=%s role=%s prefs=%s",
            self._name,
            identity.server_slug,
            identity.region,
            identity.spec_id,
            identity.metric_role,
            identity.metric_preferences.cache_key(),
        )
        cached = self._cache.get(
            self._name,
            identity.server_slug,
            identity.region,
            identity.spec_id,
            identity.metric_role,
            identity.metric_preferences,
        )
        if cached is not None:
            _log.info(
                "WCL fetch cache hit: %s-%s in %.2fs",
                self._name,
                identity.server_slug,
                time.perf_counter() - started_at,
            )
            self.signals.done.emit(identity, cached)
            return
        try:
            ranks = self._client.fetch_character_ranks(
                self._name,
                identity.server_slug,
                identity.spec_id,
                identity.metric_role,
                region=identity.region,
                metric_preferences=identity.metric_preferences,
            )
        except WCLApiError as e:
            ranks = CharacterRanks.empty(error=str(e), error_kind=e.error_kind)
        except WCLAuthError as e:
            ranks = CharacterRanks.empty(error=str(e), error_kind=WCL_ERROR_AUTH)
        except (httpx.TimeoutException, httpx.RequestError) as e:
            ranks = CharacterRanks.empty(error=str(e), error_kind=WCL_ERROR_NETWORK)
        except Exception as e:  # noqa: BLE001 — pass error string to UI for surfacing
            # MUST use .empty() factory — raw `CharacterRanks(None, None, ...)`
            # silently TypeErrors (8 required positional args), kills QRunnable,
            # applicant row stays on 'loading' forever.
            ranks = CharacterRanks.empty(error=str(e))
        elapsed = time.perf_counter() - started_at
        if ranks.error:
            _log.info(
                "WCL fetch finished with error: %s-%s kind=%s in %.2fs",
                self._name,
                identity.server_slug,
                ranks.error_kind or "unknown",
                elapsed,
            )
        elif ranks.not_found:
            _log.info(
                "WCL fetch finished not_found: %s-%s in %.2fs",
                self._name,
                identity.server_slug,
                elapsed,
            )
        else:
            _log.info(
                "WCL fetch finished: %s-%s in %.2fs",
                self._name,
                identity.server_slug,
                elapsed,
            )
        if not ranks.error and not ranks.not_found:
            self._cache.put(
                self._name,
                identity.server_slug,
                identity.region,
                identity.spec_id,
                ranks,
                identity.metric_role,
                identity.metric_preferences,
                expected_generation=self._cache_generation,
            )
        self.signals.done.emit(identity, ranks)


# ───────────────────────────────────────────────────────────────────
# Tooltip-rendering subclass (Qt-on-Windows translucent-overlay workaround)


def _render_tooltip(parent_widget, tip: str, global_pos) -> bool:
    """Renders or hides the tooltip via QToolTip.showText() bypass.

    Used by OverlayWindow.eventFilter for header-viewport (column legends)
    and title-label (listing tooltip) branches. Both bypasses are needed
    because Qt's default tooltip path silently fails on this overlay's
    flag combo (FramelessWindowHint + Tool + WA_TranslucentBackground +
    WindowStaysOnTopHint) — default QToolTip widget inherits the
    translucent attribute and paints invisible. showText routes through
    Qt's global tooltip widget (screen-parented) which paints reliably.

    Returns True so callers can `return _render_tooltip(...)` to consume."""
    from PyQt6.QtWidgets import QToolTip

    if tip:
        QToolTip.showText(global_pos, tip, parent_widget)
    else:
        QToolTip.hideText()
    return True


# ───────────────────────────────────────────────────────────────────
# Row sort + group-marker support (pure data; no Qt). Group-aware sort lives
# here so the delegate (next class down) can stay focused on paint.


# Fetch states that count as terminal-bad (used as a sort tiebreak so
# unfetchable applicants sink within their RIO bucket). Module-level frozenset
# so the pure sort fn can see it without rebuilding per state change.
_SUNK_STATES: frozenset[str] = frozenset({"error", "not_found"})
_PROVISIONAL_STATES: frozenset[str] = frozenset({"loading", "pending"})
_MPLUS_CATEGORY_ID = 2


def _split_composite(composite_id: str) -> tuple[str, int]:
    """Parse 'aid:m' → ('aid', m). Inverse of the composite-key construction
    in __main__.StateMachine.apply_snapshot.

    Defensive fallbacks on every edge — missing colon, non-numeric m, empty
    parts, empty input — never raises. Lives in overlay.py because the only
    consumers are the sort fn and the delegate-marker build (both overlay-
    internal). state.py treats applicant_id as opaque; __main__ only constructs
    it. Single parse point keeps the composite-format invariant local."""
    if ":" not in composite_id:
        return composite_id, 1
    raw, m = composite_id.split(":", 1)
    try:
        return raw, int(m)
    except ValueError:
        return raw, 1


def _rio_display_text(applicant: Applicant) -> str:
    if applicant.main_score > applicant.score and applicant.score:
        return f"{applicant.score} [{applicant.main_score}]"
    if applicant.main_score > applicant.score:
        return f"[{applicant.main_score}]"
    return str(applicant.score) if applicant.score else "—"


def _rio_panel_text(applicant: Applicant) -> str:
    if applicant.main_score > applicant.score and applicant.score:
        return f"RIO {applicant.score} · main {applicant.main_score}"
    if applicant.main_score > applicant.score:
        return f"RIO main {applicant.main_score}"
    return f"RIO {applicant.score}" if applicant.score else ""


def _mplus_fit_source_text(applicant: Applicant) -> str:
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


def sort_applicants_grouped(
    applicants: Iterable[Applicant], listing: Listing | None = None
) -> list[Applicant]:
    """Sort applicants with multi-member group adjacency.

    Unknown/no listing preserves the original max-RIO ordering. Known M+/raid
    listings rank groups by package fit, not best-member fit, while keeping all
    members adjacent and leader/member order stable.
    """
    apps = list(applicants)
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
    for a in apps:
        raw_aid, _ = _split_composite(a.applicant_id)
        group_members.setdefault(raw_aid, []).append(a)
        rio_score = effective_rio_score(a)
        if rio_score > group_max.get(raw_aid, -1):
            group_max[raw_aid] = rio_score
        if a.fetch_status not in _SUNK_STATES:
            group_has_ready[raw_aid] = True
        if a.fetch_status in _PROVISIONAL_STATES:
            group_has_provisional[raw_aid] = True
    if use_fit:
        for raw_aid, members in group_members.items():
            fit = package_fit(members, listing)
            group_fit[raw_aid] = fit.score
            group_confidence[raw_aid] = fit.confidence
    elif use_mplus_headline:
        for raw_aid, members in group_members.items():
            group_mplus_headline[raw_aid] = min(
                (_mplus_headline_sort_score(member) for member in members),
                default=(0, 0.0),
            )

    def _key(a: Applicant):
        raw_aid, member_idx = _split_composite(a.applicant_id)
        gmax = group_max.get(raw_aid, 0)
        gfit = group_fit.get(raw_aid, 0.0)
        gconfidence = group_confidence.get(raw_aid, 0.0)
        gheadline_key, gheadline_percent = group_mplus_headline.get(raw_aid, (0, 0.0))
        all_sunk = not group_has_ready.get(raw_aid, False)
        provisional = group_has_provisional.get(raw_aid, False)
        sunk = a.fetch_status in _SUNK_STATES
        if use_fit:
            no_fit = gfit <= 0.0
            return (
                no_fit,
                provisional if not no_fit else False,
                all_sunk if no_fit else False,
                -int(round(gfit)),
                -gconfidence,
                -gfit,
                -gmax,
                raw_aid,
                member_idx,
                sunk,
            )
        if use_mplus_headline:
            return (
                all_sunk,
                gheadline_key <= 0,
                -gheadline_key,
                -gheadline_percent,
                -gmax,
                raw_aid,
                member_idx,
                sunk,
            )
        return (gmax == 0, -gmax, all_sunk, raw_aid, member_idx, sunk)

    return sorted(apps, key=_key)


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


def _mplus_headline_sort_score(applicant: Applicant) -> tuple[int, float]:
    if applicant.fetch_status in _SUNK_STATES:
        return (0, 0.0)
    _metric_label, breakdown, best, _median = role_mplus_view(applicant)
    return (_highest_mplus_key_level(breakdown), float(best or 0.0))


def _build_group_markers(
    visible_id_by_row: Iterable[tuple[int, str]],
) -> dict[int, _GroupMarker]:
    """Build visible-row grouping metadata for the table paint delegate.

    Only rows whose raw applicant id appears at least twice get markers. The
    caller passes visible rows so role filters can keep the bracket shape
    aligned to what the user actually sees.
    """
    members_by_raw: dict[str, list[tuple[int, str]]] = {}
    for row, applicant_id in visible_id_by_row:
        raw_aid, _member_idx = _split_composite(applicant_id)
        members_by_raw.setdefault(raw_aid, []).append((row, applicant_id))

    markers: dict[int, _GroupMarker] = {}
    for raw_aid, members in members_by_raw.items():
        size = len(members)
        if size < 2:
            continue
        colour = group_id_colour(raw_aid)
        for position, (row, _applicant_id) in enumerate(members, start=1):
            markers[row] = _GroupMarker(
                colour=colour,
                first_visible=position == 1,
                last_visible=position == size,
                position=position,
                size=size,
            )
    return markers


def _minimum_window_width_for_metrics(
    metric_preferences: MetricPreferences,
    *,
    name_width: int = COLUMN_WIDTHS[COL_NAME],
) -> int:
    mplus_width = (
        MPLUS_GROUP_COLUMN_WIDTH
        if metric_preferences.mplus and not metric_preferences.raid_enabled
        else COLUMN_WIDTHS[COL_MPLUS]
    )
    width = (
        COLUMN_WIDTHS[COL_SPEC]
        + max(COLUMN_WIDTHS[COL_NAME], name_width)
        + COLUMN_WIDTHS[COL_ILVL]
        + COLUMN_WIDTHS[COL_RIO]
        + (COLUMN_WIDTHS[COL_N] if metric_preferences.raid_normal else 0)
        + (COLUMN_WIDTHS[COL_H] if metric_preferences.raid_heroic else 0)
        + (COLUMN_WIDTHS[COL_M] if metric_preferences.raid_mythic else 0)
        + (mplus_width if metric_preferences.mplus else 0)
        + WINDOW_CHROME_WIDTH
    )
    return max(MIN_VISIBLE_WINDOW_WIDTH, width)


def _should_draw_group_package_text(group_marker: _GroupMarker | None) -> bool:
    if group_marker is None:
        return True
    return group_marker.position == (group_marker.size // 2) + 1


class _HoverHighlightDelegate(QStyledItemDelegate):
    """Paints hover/pin row stripes and stronger multi-member group markers.

    Group rows get a wide coloured bracket in the spec column. This stays
    visible beside saturated WCL cells and still works when the M+ package cell
    is absent or filtered.

    Stripe colours intentionally match the existing percentile-tier visual
    language: gold #e5cc80 (matches gold percentile cells, unambiguous "this
    is selected"), white #ffffff (high contrast on any background, neutral
    "this is just hovered")."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover_row = -1
        self._pinned_row = -1
        self._group_marker_by_row: dict[int, _GroupMarker] = {}

    def set_rows(self, hover_row: int, pinned_row: int) -> None:
        self._hover_row = hover_row
        self._pinned_row = pinned_row

    def set_group_markers(self, markers: dict[int, _GroupMarker]) -> None:
        """row index → group paint metadata; solo rows are absent."""
        if markers != self._group_marker_by_row:
            self._group_marker_by_row = markers

    def paint(self, painter, option, index):  # type: ignore[override]
        # Item paints first (preserves QTableWidgetItem.setBackground colours
        # for raid/M+ percentile cells). Stripe overlays after — visible over
        # any background, never desaturates the text behind it.
        group_marker = self._group_marker_by_row.get(index.row())
        if index.column() == COL_SPEC:
            role = index.data(Qt.ItemDataRole.UserRole)
            icon = _role_icon(role) if isinstance(role, str) else None
            if (icon is not None or group_marker is not None) and painter is not None:
                opt = QStyleOptionViewItem(option)
                self.initStyleOption(opt, index)
                text = opt.text
                opt.text = ""
                opt.icon = QIcon()
                widget = opt.widget
                style = widget.style() if widget is not None else QApplication.style()
                if style is None:
                    super().paint(painter, option, index)
                    return
                style.drawControl(
                    QStyle.ControlElement.CE_ItemViewItem,
                    opt,
                    painter,
                    widget,
                )

                if icon is not None:
                    icon_rect = QRect(
                        opt.rect.left() + 8,
                        opt.rect.top()
                        + max(0, (opt.rect.height() - ROLE_ICON_SIZE.height()) // 2),
                        ROLE_ICON_SIZE.width(),
                        ROLE_ICON_SIZE.height(),
                    )
                    text_rect = opt.rect.adjusted(28, 0, -3, 0)
                    icon.paint(painter, icon_rect)
                else:
                    text_rect = opt.rect.adjusted(8, 0, -3, 0)
                painter.save()
                painter.setFont(opt.font)
                painter.setPen(opt.palette.color(QPalette.ColorRole.Text))
                painter.drawText(
                    text_rect,
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    text,
                )
                painter.restore()
            else:
                super().paint(painter, option, index)
        elif (
            index.column() == COL_MPLUS
            and isinstance(index.data(MPLUS_PACKAGE_TEXT_ROLE), str)
            and painter is not None
        ):
            self._paint_group_mplus_cell(painter, option, index, group_marker)
        else:
            super().paint(painter, option, index)
        if painter is None:
            return
        r = option.rect
        # Per-cell hover/pin stripe at x=0..2 — paints in EVERY column
        # deliberately: interaction state needs constant visual reinforcement
        # (cursor moves, eye scans columns). Pinned wins on same row.
        if index.row() == self._pinned_row:
            painter.fillRect(QRect(r.left(), r.top(), 3, r.height()), QColor("#e5cc80"))
        elif index.row() == self._hover_row:
            painter.fillRect(QRect(r.left(), r.top(), 3, r.height()), QColor("#ffffff"))
        # Group bracket at x=3..9, COLUMN 0 ONLY. The wider rail + caps answer
        # "these adjacent rows are one application" without adding table text.
        if index.column() == COL_SPEC and group_marker is not None:
            colour = QColor(group_marker.colour)
            rail_x = r.left() + 3
            painter.fillRect(QRect(rail_x, r.top(), 7, r.height()), colour)
            cap_width = 42
            if group_marker.first_visible:
                painter.fillRect(QRect(rail_x, r.top(), cap_width, 3), colour)
            if group_marker.last_visible:
                painter.fillRect(QRect(rail_x, r.bottom() - 2, cap_width, 3), colour)

    def _paint_group_mplus_cell(self, painter, option, index, group_marker) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        rect = opt.rect
        width = max(1, rect.width())
        package_text = str(index.data(MPLUS_PACKAGE_TEXT_ROLE) or "")
        package_bg = str(index.data(MPLUS_PACKAGE_BG_ROLE) or "#2a2a33")
        individual_text = str(index.data(MPLUS_INDIVIDUAL_TEXT_ROLE) or "")
        individual_fg = index.data(MPLUS_INDIVIDUAL_FG_ROLE)
        individual_bg = index.data(MPLUS_INDIVIDUAL_BG_ROLE)
        package_text_width = opt.fontMetrics.horizontalAdvance(package_text) + 12
        if width <= MPLUS_GROUP_LANE_MIN_WIDTH + 1:
            package_width = max(1, width // 2)
        else:
            package_width = min(
                MPLUS_GROUP_LANE_MAX_WIDTH,
                max(MPLUS_GROUP_LANE_MIN_WIDTH, package_text_width),
            )
            if width - package_width < MPLUS_INDIVIDUAL_LANE_MIN_WIDTH:
                package_width = max(
                    MPLUS_GROUP_LANE_MIN_WIDTH,
                    width - MPLUS_INDIVIDUAL_LANE_MIN_WIDTH,
                )
            package_width = min(package_width, width - 1)

        package_rect = QRect(rect.left(), rect.top(), package_width, rect.height())
        separator_rect = QRect(
            rect.left() + package_width,
            rect.top(),
            1,
            rect.height(),
        )
        individual_left = separator_rect.right() + 1
        individual_rect = QRect(
            individual_left,
            rect.top(),
            max(1, rect.right() - individual_left + 1),
            rect.height(),
        )

        painter.save()
        painter.setFont(opt.font)
        painter.fillRect(package_rect, QColor(package_bg))
        painter.fillRect(separator_rect, QColor("#09090d"))
        if isinstance(individual_bg, str) and individual_bg:
            painter.fillRect(individual_rect, QColor(individual_bg))
            individual_text_colour = _text_colour_for_bg(individual_bg)
        else:
            painter.fillRect(individual_rect, QColor(28, 28, 38, 240))
            individual_text_colour = (
                individual_fg if isinstance(individual_fg, str) else "#e0e0e0"
            )

        if _should_draw_group_package_text(group_marker):
            text_rect = package_rect
            if group_marker is not None and group_marker.size % 2 == 0:
                text_rect = QRect(
                    package_rect.left(),
                    package_rect.top() - package_rect.height(),
                    package_rect.width(),
                    package_rect.height() * 2,
                )
            painter.setPen(QColor(_text_colour_for_bg(package_bg)))
            painter.drawText(
                text_rect.adjusted(4, 0, -4, 0),
                Qt.AlignmentFlag.AlignCenter,
                opt.fontMetrics.elidedText(
                    package_text,
                    Qt.TextElideMode.ElideRight,
                    max(1, text_rect.width() - 8),
                ),
            )
        painter.setPen(QColor(individual_text_colour))
        painter.drawText(
            individual_rect.adjusted(4, 0, -4, 0),
            Qt.AlignmentFlag.AlignCenter,
            opt.fontMetrics.elidedText(
                individual_text,
                Qt.TextElideMode.ElideRight,
                max(1, individual_rect.width() - 8),
            ),
        )
        painter.restore()


class _TooltipTableWidget(QTableWidget):
    """QTableWidget with a viewportEvent override kept as the documented
    tooltip-bypass anchor.

    Post-refactor (custom row-hover panel replaced per-cell tooltips on
    applicant rows), the viewportEvent body is INERT for cell tooltips —
    `item.toolTip()` returns "" for every applicant cell, so _render_tooltip
    just calls QToolTip.hideText() (no-op). The subclass and override stay
    as a stable type and the documented anchor; if cell tooltips ever come
    back here, the bypass machinery is already wired.

    Header tooltips (column legends) and title-label tooltip route through
    OverlayWindow.eventFilter, NOT through this override."""

    def viewportEvent(self, e):  # type: ignore[override]
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QHelpEvent

        if (
            e is not None
            and e.type() == QEvent.Type.ToolTip
            and isinstance(e, QHelpEvent)
        ):
            tip = ""
            idx = self.indexAt(e.pos())
            if idx.isValid():
                item = self.item(idx.row(), idx.column())
                if item is not None:
                    tip = item.toolTip()
            return _render_tooltip(self, tip, e.globalPos())
        return super().viewportEvent(e)


# ───────────────────────────────────────────────────────────────────
# Custom title bar (frameless windows must implement their own drag)


class TitleBar(QWidget):
    hideClicked = pyqtSignal()
    settingsClicked = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setObjectName("titleBar")
        self._drag_offset = None  # type: ignore[assignment]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(4)

        self.title_label = QLabel("M+ Applicants")
        self.title_label.setObjectName("titleLabel")
        layout.addWidget(self.title_label, stretch=1)

        self.settings_button = QPushButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setFixedSize(20, 20)
        self.settings_button.setToolTip("Settings")
        style = self.style()
        if style is not None:
            self.settings_button.setIcon(
                style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
            )
        self.settings_button.clicked.connect(self.settingsClicked.emit)
        layout.addWidget(self.settings_button)

        self.hide_button = QPushButton("-")
        self.hide_button.setObjectName("hideButton")
        self.hide_button.setFixedSize(20, 20)
        self.hide_button.setToolTip("Hide overlay")
        self.hide_button.clicked.connect(self.hideClicked.emit)
        layout.addWidget(self.hide_button)

    def setTitleText(self, text: str) -> None:
        self.title_label.setText(text)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            if window is None:
                return
            self._drag_offset = (
                event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and self._drag_offset is not None
        ):
            window = self.window()
            if window is None:
                return
            window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        self._drag_offset = None


class OverlayLauncher(QFrame):
    clicked = pyqtSignal()
    positionChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setObjectName("overlayLauncher")
        self.setFixedSize(LAUNCHER_SIZE, LAUNCHER_SIZE)
        self.setToolTip("Show ApplicantScout overlay")
        self._press_global_pos: QPoint | None = None
        self._press_window_pos: QPoint | None = None
        self._dragged = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        label = QLabel("AS")
        label.setObjectName("overlayLauncherLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

    def show_at(self, pos: QPoint) -> None:
        self.move(pos)
        self.show()
        self.raise_()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global_pos = event.globalPosition().toPoint()
            self._press_window_pos = self.pos()
            self._dragged = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and self._press_global_pos is not None
            and self._press_window_pos is not None
        ):
            delta = event.globalPosition().toPoint() - self._press_global_pos
            if delta.manhattanLength() > 3:
                self._dragged = True
            self.move(self._press_window_pos + delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragged = self._dragged
            if not was_dragged:
                self.clicked.emit()
            else:
                self.positionChanged.emit()
            self._press_global_pos = None
            self._press_window_pos = None
            self._dragged = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ───────────────────────────────────────────────────────────────────
# Source tab bar (Applicants / Party)


class SourceTabBar(QWidget):
    tabChanged = pyqtSignal(str)
    keyChanged = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("sourceTabBar")
        self.setFixedHeight(30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(5)

        self._buttons: dict[str, QPushButton] = {}
        for key, label in (("applicants", "Applicants"), ("party", "Party")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked=False, k=key: self.set_active(k))
            self._buttons[key] = button
            layout.addWidget(button)
        self._key_label = QLabel("Key")
        self._key_label.setObjectName("targetKeyLabel")
        self._key_label.setToolTip("Manual Mythic+ key level for fit scoring.")
        key_label_font = self._key_label.font()
        key_label_font.setBold(True)
        self._key_label.setFont(key_label_font)
        layout.addWidget(self._key_label)
        self._key_control = QWidget(self)
        self._key_control.setObjectName("targetKeyControl")
        self._key_control.setFixedWidth(112)
        key_layout = QHBoxLayout(self._key_control)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(0)
        self._key_spin = QSpinBox()
        self._key_spin.setObjectName("targetKeySpin")
        self._key_spin.setRange(0, 30)
        self._key_spin.setSpecialValueText("—")
        self._key_spin.setPrefix("+")
        self._key_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._key_spin.setFixedWidth(64)
        self._key_spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._key_spin.setToolTip(
            "Set the current Mythic+ key when the addon cannot read it from your own listing."
        )
        key_spin_font = self._key_spin.font()
        key_spin_font.setBold(True)
        self._key_spin.setFont(key_spin_font)
        self._key_spin.valueChanged.connect(self.keyChanged.emit)
        key_layout.addWidget(self._key_spin)
        self._key_up_button = QPushButton("▲")
        self._key_up_button.setObjectName("targetKeyStepUp")
        self._key_up_button.setFixedSize(24, 22)
        self._key_up_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._key_up_button.setToolTip("Increase key level")
        self._key_up_button.clicked.connect(self._key_spin.stepUp)
        key_layout.addWidget(self._key_up_button)
        self._key_down_button = QPushButton("▼")
        self._key_down_button.setObjectName("targetKeyStepDown")
        self._key_down_button.setFixedSize(24, 22)
        self._key_down_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._key_down_button.setToolTip("Decrease key level")
        self._key_down_button.clicked.connect(self._key_spin.stepDown)
        key_layout.addWidget(self._key_down_button)
        layout.addWidget(self._key_control)
        layout.addStretch(1)
        self.set_counts(applicants=0, party=0)
        self.set_active("applicants", emit=False)

    def set_counts(self, *, applicants: int, party: int) -> None:
        self._buttons["applicants"].setText(f"Applicants ({applicants})")
        self._buttons["party"].setText(f"Party ({party})")

    def set_active(self, key: str, *, emit: bool = True) -> None:
        if key not in self._buttons:
            return
        for button_key, button in self._buttons.items():
            was_blocked = button.blockSignals(True)
            button.setChecked(button_key == key)
            button.blockSignals(was_blocked)
        if emit:
            self.tabChanged.emit(key)

    def set_target_key(self, key_level: int) -> None:
        key_level = max(0, min(30, int(key_level)))
        was_blocked = self._key_spin.blockSignals(True)
        self._key_spin.setValue(key_level)
        self._key_spin.blockSignals(was_blocked)


# ───────────────────────────────────────────────────────────────────
# Role filter bar (above the info panel, below the title bar)


ROLE_FILTER_TOOLTIPS: dict[str, str] = {
    "TANK": "Show only tank applicants",
    "HEALER": "Show only healer applicants",
    "DAMAGER": "Show only damage dealer applicants",
}
ROLE_FILTER_RESET_TEXT = "All"
ROLE_FILTER_RESET_TOOLTIP = "Show all roles"
ROLE_FILTER_RESET_SIZE = QSize(34, 20)


class RoleFilterBar(QWidget):
    """Top toolbar with 3 toggle buttons (TANK / HEAL / DPS) + reset/status.

    Empty filter set = show all (default). All-3-selected is semantically
    equivalent to empty for count-display purposes — `set_status()` checks
    against `ALL_ROLES`; OverlayWindow uses `_is_filter_active()` for the
    same check before deciding row visibility math.

    Each role toggle adds/removes its role from the active filter set. The reset
    button clears all toggles and emits a single empty filter set. Buttons have
    NoFocus policy — frameless overlay isn't a focus participant for
    keyboard nav, mouse-only interaction matches the rest of the overlay."""

    filterChanged = pyqtSignal(set)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("roleFilterBar")
        self.setFixedHeight(30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(5)

        self._buttons: dict[str, QPushButton] = {}
        self._active: set[str] = set()
        # Order: TANK | HEAL | DPS — matches Blizzard role-checkbox UI in
        # Group Finder for muscle-memory consistency.
        for role in ("TANK", "HEALER", "DAMAGER"):
            btn = QPushButton(ROLE_LABELS[role])
            btn.setCheckable(True)
            btn.setObjectName(f"roleBtn_{role}")
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setToolTip(ROLE_FILTER_TOOLTIPS[role])
            icon = _role_icon(role)
            if icon is not None:
                btn.setIcon(icon)
                btn.setIconSize(ROLE_ICON_SIZE)
            else:
                btn.setText(f"{ROLE_GLYPHS[role]} {ROLE_LABELS[role]}")
            btn.toggled.connect(lambda on, r=role: self._on_toggled(r, on))
            self._buttons[role] = btn
            layout.addWidget(btn)

        layout.addStretch(1)
        self._reset_btn = QPushButton(ROLE_FILTER_RESET_TEXT)
        self._reset_btn.setObjectName("roleFilterReset")
        self._reset_btn.setFixedSize(ROLE_FILTER_RESET_SIZE)
        self._reset_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._reset_btn.setToolTip(ROLE_FILTER_RESET_TOOLTIP)
        self._reset_btn.clicked.connect(lambda _checked=False: self._reset())
        self._reset_btn.hide()
        layout.addWidget(self._reset_btn)

        self._status = QLabel("")
        self._status.setObjectName("roleFilterStatus")
        layout.addWidget(self._status)

    def _on_toggled(self, role: str, on: bool) -> None:
        if on:
            self._active.add(role)
        else:
            self._active.discard(role)
        self._sync_reset_button()
        self.filterChanged.emit(set(self._active))

    def _reset(self) -> None:
        if not self._active:
            return
        for btn in self._buttons.values():
            was_blocked = btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(was_blocked)
        self._active.clear()
        self._status.setText("")
        self._sync_reset_button()
        self.filterChanged.emit(set())

    def _sync_reset_button(self) -> None:
        self._reset_btn.setVisible(bool(self._active))

    def tooltip_widgets(self) -> tuple[QWidget, ...]:
        return (
            self._buttons["TANK"],
            self._buttons["HEALER"],
            self._buttons["DAMAGER"],
            self._reset_btn,
        )

    def set_status(self, visible: int, total: int) -> None:
        """Show 'showing N / total' only when filter is active AND some rows
        are hidden. All-3-selected (== ALL_ROLES) treated as inactive for
        display purposes — empty status."""
        is_active = bool(self._active) and self._active != ALL_ROLES
        if not is_active or visible == total:
            self._status.setText("")
        else:
            self._status.setText(f"showing {visible} / {total}")


# ───────────────────────────────────────────────────────────────────
# Applicant info hover/pin panel (above the table, below the title bar)


class ApplicantInfoPanel(QFrame):
    """Compact QWidget scout card shown above the applicant table.

    The panel is always present so hover/pin changes never resize the table
    below it. Child widgets are created once and updated in-place on each
    hover; this keeps fast mouse movement cheap and avoids layout churn."""

    def __init__(
        self,
        parent: QWidget,
        metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
    ):
        super().__init__(parent)
        self._metric_preferences = metric_preferences
        self.setObjectName("infoPanel")
        # Three-layer translucency mitigation: panel is a child of rootContainer
        # which sits on the WA_TranslucentBackground top-level overlay window.
        # Without these, panel inherits transparent painting and renders
        # invisible — same bug that motivated the QToolTip.showText bypass.
        # (Third layer is QSS background-color in _STYLESHEET.)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAutoFillBackground(True)
        self.setMinimumHeight(INFO_PANEL_MIN_HEIGHT)
        self.setMaximumHeight(INFO_PANEL_MIN_HEIGHT)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(4)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(5)
        self._name_label = QLabel("")
        self._name_label.setObjectName("infoName")
        self._realm_label = QLabel("")
        self._realm_label.setObjectName("infoRealm")
        header_layout.addWidget(self._name_label)
        header_layout.addWidget(self._realm_label)
        header_layout.addStretch(1)
        outer.addWidget(header)

        identity = QWidget(self)
        identity_layout = QHBoxLayout(identity)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(4)
        self._spec_label = QLabel("")
        self._spec_label.setObjectName("infoSpecBadge")
        self._role_label = QLabel("")
        self._role_label.setObjectName("infoRoleBadge")
        self._ilvl_label = QLabel("")
        self._ilvl_label.setObjectName("infoMeta")
        self._rio_label = QLabel("")
        self._rio_label.setObjectName("infoMeta")
        for label in (
            self._spec_label,
            self._role_label,
            self._ilvl_label,
            self._rio_label,
        ):
            identity_layout.addWidget(label)
        identity_layout.addStretch(1)
        outer.addWidget(identity)

        metrics = QWidget(self)
        metrics_layout = QHBoxLayout(metrics)
        metrics_layout.setContentsMargins(0, 0, 0, 0)
        metrics_layout.setSpacing(4)
        self._metric_labels: dict[str, QLabel] = {
            key: QLabel("") for key in ("N", "H", "M", "M+")
        }
        for key in ("N", "H", "M", "M+"):
            label = self._metric_labels[key]
            label.setObjectName("infoMetricBadge")
            metrics_layout.addWidget(label)
        metrics_layout.addStretch(1)
        outer.addWidget(metrics)

        self._package_label = QLabel("")
        self._package_label.setObjectName("infoPackageBadge")
        outer.addWidget(self._package_label)

        self._status_label = QLabel("")
        self._status_label.setObjectName("infoPanelStatus")
        self._status_label.setWordWrap(True)
        outer.addWidget(self._status_label)

        self._dungeon_widget = QWidget(self)
        self._dungeon_grid = QGridLayout(self._dungeon_widget)
        self._dungeon_grid.setContentsMargins(0, 2, 0, 0)
        self._dungeon_grid.setHorizontalSpacing(6)
        self._dungeon_grid.setVerticalSpacing(1)
        self._dungeon_rows: list[tuple[QLabel, QLabel, QLabel, QLabel]] = []
        for row in range(8):
            name = QLabel("")
            name.setObjectName("infoDungeonName")
            rio_key = QLabel("")
            rio_key.setObjectName("infoDungeonKey")
            wcl_key = QLabel("")
            wcl_key.setObjectName("infoDungeonWclKey")
            value = QLabel("")
            value.setObjectName("infoDungeonMetric")
            name.setFixedWidth(DUNGEON_NAME_WIDTH)
            rio_key.setFixedWidth(DUNGEON_KEY_WIDTH)
            wcl_key.setFixedWidth(DUNGEON_WCL_KEY_WIDTH)
            value.setFixedWidth(DUNGEON_METRIC_WIDTH)
            rio_key.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            wcl_key.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            value.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self._dungeon_grid.addWidget(name, row, 0)
            self._dungeon_grid.addWidget(rio_key, row, 1)
            self._dungeon_grid.addWidget(wcl_key, row, 2)
            self._dungeon_grid.addWidget(value, row, 3)
            self._dungeon_rows.append((name, rio_key, wcl_key, value))
        self._dungeon_grid.setColumnStretch(4, 1)
        outer.addWidget(self._dungeon_widget)
        outer.addStretch(1)

        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.TextFormat.PlainText)

        self.setPlaceholder()

    def set_metric_preferences(self, metric_preferences: MetricPreferences) -> None:
        self._metric_preferences = metric_preferences

    def sizeHint(self) -> QSize:  # type: ignore[override]
        hint = super().sizeHint()
        return QSize(hint.width(), INFO_PANEL_PREFERRED_HEIGHT)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        hint = super().minimumSizeHint()
        return QSize(hint.width(), INFO_PANEL_MIN_HEIGHT)

    def target_height(self) -> int:
        return (
            INFO_PANEL_PREFERRED_HEIGHT
            if self._dungeon_widget.isVisible()
            else INFO_PANEL_MIN_HEIGHT
        )

    def setPlaceholder(self) -> None:
        """Show the compact hint when nothing is hovered/pinned."""
        self._hide_data_widgets()
        self._status_label.setText("Hover a row for applicant details.")
        self._status_label.setVisible(True)

    def setApplicantData(
        self,
        applicant: Applicant,
        listing: Listing | None = None,
        package: PackageFit | None = None,
    ) -> None:
        """Show full applicant scout data in the fixed widget layout."""
        self._set_identity(applicant)
        self._set_package(package, applicant, listing)
        self._set_status_or_data(applicant, listing)

    def _hide_data_widgets(self) -> None:
        for label in (
            self._name_label,
            self._realm_label,
            self._spec_label,
            self._role_label,
            self._ilvl_label,
            self._rio_label,
        ):
            label.setText("")
            label.setVisible(False)
        for label in self._metric_labels.values():
            label.setText("")
            label.setVisible(False)
        self._package_label.setText("")
        self._package_label.setVisible(False)
        for row in self._dungeon_rows:
            for label in row:
                label.setText("")
                label.setVisible(False)
        self._dungeon_widget.setVisible(False)

    def _set_identity(self, applicant: Applicant) -> None:
        raw_name, _, raw_realm = applicant.name.partition("-")
        class_hex = CLASS_COLOURS.get(applicant.cls, "#FFFFFF")
        self._name_label.setText(raw_name or "?")
        self._name_label.setStyleSheet(f"color: {class_hex};")
        self._name_label.setVisible(True)
        self._realm_label.setText(raw_realm)
        self._realm_label.setVisible(bool(raw_realm))

        spec = SPEC_SHORT_NAMES.get(applicant.spec_id, f"#{applicant.spec_id}")
        self._set_badge(
            self._spec_label, spec, class_hex, _text_colour_for_bg(class_hex)
        )

        role_bg = ROLE_COLOURS.get(applicant.role, "#555555")
        role_text = ROLE_LABELS.get(applicant.role, applicant.role[:4] or "?")
        self._set_badge(self._role_label, role_text, role_bg, "#ffffff")

        self._ilvl_label.setText(f"ilvl {applicant.ilvl}" if applicant.ilvl else "")
        self._ilvl_label.setVisible(bool(applicant.ilvl))
        rio_score = effective_rio_score(applicant)
        if rio_score:
            self._rio_label.setText(_rio_panel_text(applicant))
            self._rio_label.setStyleSheet(
                f"color: {rio_score_colour(rio_score)}; font-weight: bold;"
            )
            self._rio_label.setVisible(True)
        else:
            self._rio_label.setText("")
            self._rio_label.setVisible(False)

    def _set_package(
        self,
        package: PackageFit | None,
        applicant: Applicant | None = None,
        listing: Listing | None = None,
    ) -> None:
        if package is None or package.size < 2 or not package.display:
            self._package_label.setText("")
            self._package_label.setVisible(False)
            return
        member_score = (
            candidate_fit(applicant, listing).score
            if applicant is not None and listing is not None
            else None
        )
        member_note = ""
        if (
            member_score is not None
            and package.spread >= 1.0
            and abs(member_score - package.low_score) < 0.5
        ):
            member_note = " · this low"
        package_label = package.label or (
            "fit" if package.context == CONTEXT_MPLUS else "package"
        )
        if package.context == CONTEXT_MPLUS:
            text = (
                f"Group {package_label} {int(round(package.score))} · "
                f"hi/avg/low {int(round(package.high_score))}/"
                f"{int(round(package.average_score))}/"
                f"{int(round(package.low_score))} · "
                f"conf {int(round(package.confidence * 100))}%"
                f"{member_note}"
            )
        else:
            text = (
                f"Group {package_label} {int(round(package.score))} · "
                f"high {int(round(package.high_score))} · "
                f"avg {int(round(package.average_score))} · "
                f"low {int(round(package.low_score))} · "
                f"conf {int(round(package.confidence * 100))}%"
                f"{member_note}"
            )
        bg = package.colour or "#2a2a33"
        self._set_badge(self._package_label, text, bg, _text_colour_for_bg(bg))

    def _set_status_or_data(
        self, applicant: Applicant, listing: Listing | None = None
    ) -> None:
        status = applicant.fetch_status
        self._clear_metrics_and_dungeons()
        self._status_label.setVisible(False)
        if status in ("loading", "pending"):
            self._show_status("Fetching from Warcraft Logs…")
            self._set_dungeon_rows(applicant, listing)
            return
        if status == "error":
            msg = applicant.error_message or "unknown"
            fit = candidate_fit(applicant, listing)
            source_note = (
                " · RaiderIO only"
                if fit.context == CONTEXT_MPLUS and fit.score > 0.0
                else ""
            )
            self._show_status(f"WCL error: {msg}{source_note}", error=True)
            self._set_metric_badges(applicant, listing)
            self._set_dungeon_rows(applicant, listing)
            return
        if status == "not_found":
            fit = candidate_fit(applicant, listing)
            source_note = (
                " · RaiderIO only"
                if fit.context == CONTEXT_MPLUS and fit.score > 0.0
                else ""
            )
            self._show_status(f"Not found on Warcraft Logs{source_note}")
            self._set_metric_badges(applicant, listing)
            self._set_dungeon_rows(applicant, listing)
            return
        if status != "ready":
            self._show_status("")
            return

        visible_metrics = self._set_metric_badges(applicant, listing)
        visible_rows = self._set_dungeon_rows(applicant, listing)
        fit_status = self._mplus_fit_status_text(applicant, listing)
        if fit_status:
            self._show_status(fit_status)
        elif not visible_metrics and not visible_rows:
            self._show_status("No Warcraft Logs data")

    def _show_status(self, text: str, *, error: bool = False) -> None:
        self._status_label.setText(text)
        color = "#ff6666" if error else "#8d8d98"
        self._status_label.setStyleSheet(f"color: {color};")
        self._status_label.setVisible(bool(text))

    def _clear_metrics_and_dungeons(self) -> None:
        for label in self._metric_labels.values():
            label.setText("")
            label.setVisible(False)
        for row in self._dungeon_rows:
            for label in row:
                label.setText("")
                label.setVisible(False)
        self._dungeon_widget.setVisible(False)

    def _set_metric_badges(
        self, applicant: Applicant, listing: Listing | None = None
    ) -> int:
        shown = 0
        fit = candidate_fit(applicant, listing)
        raid_sources = [
            (
                "N",
                applicant.raid_normal,
                applicant.raid_normal_median,
                self._metric_preferences.raid_normal,
            ),
            (
                "H",
                applicant.raid_heroic,
                applicant.raid_heroic_median,
                self._metric_preferences.raid_heroic,
            ),
            (
                "M",
                applicant.raid_mythic,
                applicant.raid_mythic_median,
                self._metric_preferences.raid_mythic,
            ),
        ]
        for key, best, median, enabled in raid_sources:
            if not enabled:
                self._metric_labels[key].setVisible(False)
                continue
            text, _fg, bg = _raid_cell_visuals(best, median, applicant.fetch_status)
            if bg is None:
                self._metric_labels[key].setVisible(False)
                continue
            prefix = f"{key} "
            if fit.context == CONTEXT_RAID and fit.target_raid == key:
                prefix = f"{key} {fit.label} "
            self._set_badge(
                self._metric_labels[key],
                f"{prefix}{text}",
                bg,
                _text_colour_for_bg(bg),
            )
            shown += 1

        if self._metric_preferences.mplus:
            metric_label, _breakdown, _best, _median = role_mplus_view(applicant)
            text, _fg, bg = _mplus_cell_visuals(applicant, listing)
            if bg is not None:
                prefix = "M+ " if text.startswith("Fit ") else f"M+ {metric_label} "
                self._set_badge(
                    self._metric_labels["M+"],
                    f"{prefix}{text}",
                    bg,
                    _text_colour_for_bg(bg),
                )
                shown += 1
            else:
                self._metric_labels["M+"].setVisible(False)
        else:
            self._metric_labels["M+"].setVisible(False)
        return shown

    def _mplus_fit_status_text(
        self, applicant: Applicant, listing: Listing | None = None
    ) -> str:
        fit = candidate_fit(applicant, listing)
        if fit.context != CONTEXT_MPLUS or not fit.display or fit.score <= 0.0:
            return ""
        source = _mplus_fit_source_text(applicant)
        coverage = int(round(fit.coverage * max(len(MPLUS_ENCOUNTERS), 1)))
        return (
            f"M+ fit conf {int(round(fit.confidence * 100))}% · "
            f"cov {coverage}/{max(len(MPLUS_ENCOUNTERS), 1)} · {source}"
        )

    def _set_dungeon_rows(
        self, applicant: Applicant, listing: Listing | None = None
    ) -> int:
        if not self._metric_preferences.mplus:
            self._dungeon_widget.setVisible(False)
            return 0
        rio_rows = _rio_dungeon_rows_by_name(applicant, listing)
        wcl_rows = _wcl_dungeon_rows_by_name(applicant, listing)
        listing_keys = listing_dungeon_keys(listing)
        row_keys = sorted(
            set(rio_rows) | set(wcl_rows),
            key=lambda key: (
                0 if key and key in listing_keys else 1,
                -max(
                    positive_int(rio_rows.get(key, {}).get("key_level")),
                    positive_int(wcl_rows.get(key, {}).get("key_level")),
                ),
                str(
                    wcl_rows.get(key, {}).get("name")
                    or rio_rows.get(key, {}).get("name")
                    or ""
                ),
            ),
        )[:8]
        for row_idx, labels in enumerate(self._dungeon_rows):
            name_label, rio_label, wcl_key_label, value_label = labels
            if row_idx >= len(row_keys):
                for label in labels:
                    label.setText("")
                    label.setVisible(False)
                continue
            row_key = row_keys[row_idx]
            rio_row = rio_rows.get(row_key, {})
            wcl_row = wcl_rows.get(row_key, {})
            dungeon_name = str(wcl_row.get("name") or rio_row.get("name") or "?")
            name_label.setText(
                name_label.fontMetrics().elidedText(
                    dungeon_name,
                    Qt.TextElideMode.ElideRight,
                    DUNGEON_NAME_WIDTH,
                )
            )
            name_label.setToolTip(
                dungeon_name if name_label.text() != dungeon_name else ""
            )
            rio_key = positive_int(rio_row.get("key_level"))
            if rio_key > 0:
                rio_label.setText(f"RIO +{rio_key}")
                rio_label.setStyleSheet(
                    "background-color: #24242d; color: #e0e0e0; "
                    "border-radius: 2px; padding: 0 4px; font-weight: bold;"
                )
            else:
                rio_label.setText("")
                rio_label.setStyleSheet("")
            wcl_key = positive_int(wcl_row.get("key_level"))
            if wcl_key > 0:
                wcl_text = str(wcl_row.get("text") or "")
                wcl_key_label.setText(f"WCL +{wcl_key}")
                wcl_key_label.setStyleSheet(
                    "background-color: #202028; color: #f1f1f4; "
                    "border-radius: 2px; padding: 0 4px; font-weight: bold;"
                )
                value_label.setText(wcl_text)
                bg = str(wcl_row.get("colour") or "#2a2a33")
                fg = _text_colour_for_bg(bg)
                value_label.setStyleSheet(
                    f"background-color: {bg}; color: {fg}; border-radius: 2px; "
                    "padding: 0 4px; font-weight: bold;"
                )
            else:
                wcl_key_label.setText("")
                wcl_key_label.setStyleSheet("")
                value_label.setText("")
                value_label.setStyleSheet("")
            for label in labels:
                label.setVisible(True)
        visible = len(row_keys)
        self._dungeon_widget.setVisible(visible > 0)
        return visible

    def _set_badge(self, label: QLabel, text: str, bg: str, fg: str) -> None:
        label.setText(text)
        label.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border-radius: 2px; "
            "padding: 1px 5px; font-weight: bold;"
        )
        label.setVisible(True)


# ───────────────────────────────────────────────────────────────────
# Main window


class OverlayWindow(QMainWindow):
    def __init__(
        self,
        state: AppState,
        wcl_client: WCLClient,
        cache: CharacterCache,
        config_dir,
        metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
        show_settings: Callable[[], None] | None = None,
        game_foreground_probe: Callable[[], bool] | None = None,
    ):
        super().__init__()

        self._state = state
        self._wcl_client = wcl_client
        self._cache = cache
        self._config_dir = config_dir
        self._metric_preferences = metric_preferences
        self._show_settings = show_settings
        self._game_foreground_probe = game_foreground_probe or (lambda: True)
        self._game_foreground = self._is_game_foreground()
        self._collapsed_to_launcher = False
        self._launcher = OverlayLauncher()
        self._launcher.clicked.connect(self.restore_from_launcher)
        self._launcher.positionChanged.connect(self._persist_launcher_position)
        self._saved_launcher_position = load_launcher_position(self._config_dir)
        self._pool = QThreadPool.globalInstance()
        if self._pool is not None:
            self._pool.setMaxThreadCount(3)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMinimumSize(QSize(USER_MIN_WINDOW_WIDTH, USER_MIN_WINDOW_HEIGHT))

        # Central container with QSS-stylable background
        container = QWidget()
        container.setObjectName("rootContainer")
        self.setCentralWidget(container)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._title_bar = TitleBar(container)
        self._title_bar.hideClicked.connect(self.collapse_to_launcher)
        if self._show_settings is not None:
            self._title_bar.settingsClicked.connect(self._show_settings)
        layout.addWidget(self._title_bar)

        self._active_tab = "applicants"
        self._hover_by_tab: dict[str, str | None] = {
            "applicants": None,
            "party": None,
        }
        self._pinned_by_tab: dict[str, str | None] = {
            "applicants": None,
            "party": None,
        }
        self._manual_target_key: int | None = None
        self._tab_bar = SourceTabBar(container)
        self._tab_bar.tabChanged.connect(self._on_source_tab_changed)
        self._tab_bar.keyChanged.connect(self._on_target_key_changed)
        layout.addWidget(self._tab_bar)

        # Role filter bar — toggle TANK / HEAL / DPS to hide other-role rows.
        # Inserted between title bar and info panel. Filter state is session-
        # only (transient lens, not persisted setting). See RoleFilterBar
        # docstring + OverlayWindow._apply_role_filter.
        self._role_filter_bar = RoleFilterBar(container)
        self._role_filter_bar.filterChanged.connect(self._on_role_filter_changed)
        layout.addWidget(self._role_filter_bar)
        self._action_tooltip_widgets = (
            *self._role_filter_bar.tooltip_widgets(),
            self._title_bar.settings_button,
            self._title_bar.hide_button,
        )
        for action_widget in self._action_tooltip_widgets:
            action_widget.installEventFilter(self)

        # Applicant info panel — fixed-height QWidget scout card. It updates
        # existing labels on hover/pin so the table below never jolts.
        self._panel = ApplicantInfoPanel(container, metric_preferences)
        layout.addWidget(self._panel)

        # Table — _TooltipTableWidget overrides viewportEvent to render tooltips
        # via QToolTip.showText(). Required because Qt's default tooltip path
        # silently fails on this overlay's flag combo (Tool + WA_Translucent
        # Background + WindowStaysOnTopHint) — see _TooltipTableWidget docstring.
        self._table = _TooltipTableWidget(0, len(COLUMN_HEADERS), container)
        self._table.setHorizontalHeaderLabels(COLUMN_HEADERS)
        # Per-column legend tooltips. setHorizontalHeaderLabels creates default
        # QTableWidgetItem objects we can fetch + decorate; the actual tooltip
        # rendering routes through the OverlayWindow eventFilter installed on
        # the header viewport below — same QToolTip.showText() bypass.
        for col, tip_text in enumerate(HEADER_TOOLTIPS):
            header_item = self._table.horizontalHeaderItem(col)
            if header_item is not None:
                header_item.setToolTip(tip_text)
        # Header viewport doesn't go through _TooltipTableWidget.viewportEvent
        # (it's a separate child QHeaderView with its own viewport). Install an
        # event filter on the header viewport so QHelpEvents there route to
        # OverlayWindow.eventFilter, which renders the same QToolTip.showText
        # path. Without this, header tooltips silently fail on the translucent
        # overlay just like cell tooltips did before _TooltipTableWidget.
        header_widget = self._table.horizontalHeader()
        if header_widget is not None:
            header_vp = header_widget.viewport()
            if header_vp is not None:
                header_vp.installEventFilter(self)
        # Title-label tooltip — same bypass machinery, independent branch in
        # eventFilter. setToolTip on title_label is updated from _update_title;
        # only rendering goes through eventFilter → _render_tooltip.
        self._title_bar.title_label.installEventFilter(self)
        # Mouse-tracking + viewport event filter feed the row-hover panel.
        # Filter catches Leave (cursor exits viewport) and MouseMove over the
        # empty area below the last row (rowAt(y)<0) — both clear hover. The
        # filter also catches WindowDeactivate on `self` so Alt-Tab clears the
        # stale hover that would otherwise stick when window regains focus.
        self._table.setMouseTracking(True)
        table_vp = self._table.viewport()
        if table_vp is not None:
            table_vp.setMouseTracking(True)
            table_vp.installEventFilter(self)
        self.installEventFilter(self)
        # Install the row-highlight delegate (3 px left-edge stripe). Caches
        # current hover/pinned row indices; OverlayWindow updates via
        # self._delegate.set_rows on every state change.
        self._delegate = _HoverHighlightDelegate(self._table)
        self._table.setItemDelegate(self._delegate)
        # Hover & click signals — Qt built-ins do row math for us. cellClicked
        # is left-button-only by Qt convention (right/middle clicks don't fire
        # it), so no manual button filter is needed.
        self._table.cellEntered.connect(self._on_cell_entered)
        self._table.cellClicked.connect(self._on_cell_clicked)
        # Scroll changes the row under a stationary cursor without firing
        # cellEntered — re-resolve hover from cursor position on scroll.
        scrollbar = self._table.verticalScrollBar()
        if scrollbar is not None:
            scrollbar.valueChanged.connect(self._reresolve_hover_from_cursor)
        vertical_header = self._table.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setShowGrid(False)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setTextElideMode(Qt.TextElideMode.ElideRight)
        for i, w in enumerate(COLUMN_WIDTHS):
            self._table.setColumnWidth(i, w)
        h = self._table.horizontalHeader()
        if h is None:
            raise RuntimeError("QTableWidget horizontal header is unavailable")
        # Keep non-M+ columns at their compact widths; M+ is the fill column.
        # Without a stretched final section, wider saved geometries leave a
        # transparent strip after M+ that looks like broken unused UI.
        for col in range(self._table.columnCount()):
            if col != COL_MPLUS:
                h.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(COL_MPLUS, QHeaderView.ResizeMode.Stretch)
        h.setStretchLastSection(True)
        self._max_name_width_px = COLUMN_WIDTHS[COL_NAME]
        self._apply_metric_column_visibility()
        layout.addWidget(self._table, stretch=1)

        # Bottom row: status label + size grip
        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(8, 2, 0, 0)
        bottom_layout.setSpacing(8)
        self._status_label = QLabel("")
        self._status_label.setObjectName("statusLabel")
        bottom_layout.addWidget(self._status_label, stretch=1)
        # Health indicator — "shot Xs ago" lit by snapshotReceived signal slot
        # note_decode. Stale-pipeline detection: if the watcher Observer thread
        # silently dies, the timestamp stops advancing and the user sees the
        # delta climb. Right-aligned (no stretch) next to the size grip.
        self._health_label = QLabel("shot —")
        self._health_label.setObjectName("healthLabel")
        bottom_layout.addWidget(self._health_label)
        bottom_layout.addWidget(QSizeGrip(bottom))
        layout.addWidget(bottom)

        self.setStyleSheet(_STYLESHEET)
        self._launcher.setStyleSheet(_STYLESHEET)

        # Constructor-time Qt geometry events can fire synchronously during
        # setGeometry(), so every field read by moveEvent/resizeEvent/hover
        # re-resolution must exist before geometry restore.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._persist_geometry)
        self._suppress_geometry_persist = False
        self._panel_anchor_extra_height = 0
        self._panel_anchor_y_offset = 0

        # Map of applicant_id -> table row (kept in sync with _id_by_row).
        self._row_for_id: dict[str, int] = {}
        # Reverse lookup: row -> applicant_id. Rebuilt in _refresh_table.
        # Used by hover/click signal handlers to translate row index → id.
        self._id_by_row: list[str] = []
        self._group_size_by_raw: dict[str, int] = {}
        self._package_fit_by_raw: dict[str, PackageFit] = {}
        # Hover & pin tracking. Display priority: hover > pin > hidden.
        # Pin survives applicant churn (preserved by id across re-sort);
        # hover is preserved by id when prev row's id still exists, falls
        # back to cursor re-resolution otherwise.
        self._hover_id: str | None = None
        self._pinned_id: str | None = None
        # Role filter — empty set = show all (default). All-3-selected
        # (== ALL_ROLES) is semantically equivalent for count-display via
        # _is_filter_active(). Session-only — not persisted across launches.
        self._role_filter: set[str] = set()
        # Dedup WCL fetches by the full identity we intend to fetch, not just
        # applicant_id. Old listing-session workers can complete after the same
        # id has been reused by a new listing.
        self._fetches_in_flight: dict[str, _FetchIdentity] = {}
        self._wcl_runtime_generation = 0
        self._listing_session_generation = 0
        self._refresh_flush_pending = False
        self._refresh_needs_title = False
        self._refresh_needs_show = False
        self._restore_party_on_next_roster = False

        # Restore geometry, clamped to a visible screen so off-monitor positions
        # from a previous multi-monitor session don't render the window invisibly
        # off-screen. Picks first screen whose geometry intersects the saved
        # rect; falls back to centering on primary screen if none match.
        has_saved_geometry = (self._config_dir / "window.json").exists()
        geo = _normalize_loaded_geometry(load_geometry(self._config_dir))
        x, y, w, h = _clamp_geometry_to_screen(geo.x, geo.y, geo.w, geo.h)
        min_width = self.minimumWidth()
        if not has_saved_geometry and geo.w == DEFAULT_WINDOW_WIDTH:
            w = _minimum_window_width_for_metrics(metric_preferences)
        else:
            w = max(w, min_width)
        if (x, y) != (geo.x, geo.y):
            _log.info(
                "Window geometry off-screen (%d,%d) → clamped to (%d,%d)",
                geo.x,
                geo.y,
                x,
                y,
            )
        self.setGeometry(x, y, w, h)
        self.show_launcher_only()

        # Auto-hide timer for "listing active but applicants empty" case.
        # M+ keystone listings don't auto-delist when group fills — the host
        # has to manually click Delist. After all applicants are accepted
        # (count=0), listing remains active so the addon keeps emitting
        # snapshots with listing!=None and applicants=[]. on_applicant_removed
        # already hides on count==0, but if a NEW listing event re-shows the
        # window (e.g. comment edit, dungeon change), we need a safety net.
        # Timer: starts when applicants reach 0 with listing active; fires
        # after EMPTY_HIDE_DELAY_S to hide the window. Cancelled if a new
        # applicant arrives.
        self._empty_hide_timer = QTimer(self)
        self._empty_hide_timer.setSingleShot(True)
        self._empty_hide_timer.setInterval(EMPTY_HIDE_DELAY_S * 1000)
        self._empty_hide_timer.timeout.connect(self._on_empty_hide_timeout)

        # Last-decode timestamp populated by note_decode slot connected to
        # ScreenshotWatcher.snapshotReceived. Read by _refresh_health_label.
        # GUI-thread only (signal queues across thread → slot runs on GUI),
        # no lock needed.
        self._last_decode_time: float | None = None
        self._last_decode_failed_time: float | None = None
        self._last_decode_failed_path = ""
        self._last_decode_failed_reason = ""

        # Bottom status-row poller — fires both quota and health refreshes on
        # the same 1s cadence. 1Hz keeps "shot Xs ago" smooth (vs 3s jumps);
        # both refresh slots are O(1) string format. quota itself is updated
        # on every WCL fetch via rateLimitData GraphQL field — timer just
        # pushes the latest snapshot to the label.
        self._wcl_retry_timer = QTimer(self)
        self._wcl_retry_timer.setSingleShot(True)
        self._wcl_retry_timer.timeout.connect(self._retry_failed_wcl_fetches)
        self._quota_timer = QTimer(self)
        self._quota_timer.setInterval(1000)
        self._quota_timer.timeout.connect(self._refresh_status_row)
        self._quota_timer.start()
        self._foreground_timer = QTimer(self)
        self._foreground_timer.setInterval(GAME_FOREGROUND_POLL_MS)
        self._foreground_timer.timeout.connect(self._sync_game_foreground_visibility)
        self._foreground_timer.start()
        self._refresh_status_row()  # initial paint of "WCL: —/—" + "shot —"
        for applicant in self._state.applicants.values():
            applicant.project_wcl_data_to_preferences(self._metric_preferences)
        self._schedule_wcl_retry()

    # ─── public slots called from main app ─────

    def _schedule_overlay_refresh(
        self,
        *,
        update_title: bool = True,
        maybe_show: bool = False,
    ) -> None:
        self._refresh_needs_title = self._refresh_needs_title or update_title
        self._refresh_needs_show = self._refresh_needs_show or maybe_show
        if self._refresh_flush_pending:
            return
        self._refresh_flush_pending = True
        QTimer.singleShot(0, self._flush_overlay_refresh)

    def _flush_overlay_refresh(self) -> None:
        if (
            not self._refresh_flush_pending
            and not self._refresh_needs_title
            and not self._refresh_needs_show
        ):
            return
        update_title = self._refresh_needs_title
        maybe_show = self._refresh_needs_show
        self._refresh_flush_pending = False
        self._refresh_needs_title = False
        self._refresh_needs_show = False
        self._refresh_table()
        if update_title:
            self._update_title()
        if maybe_show and (
            self._state.count() > 0 or len(self._state.party_members) > 0
        ):
            self._maybe_show()

    def collapse_to_launcher(self) -> None:
        self.flush_geometry()
        self.show_launcher_only()

    def show_launcher_only(self) -> None:
        self._collapsed_to_launcher = True
        self.hide()
        if self._game_foreground:
            self._launcher.show_at(self._default_launcher_position())
        else:
            self._launcher.hide()

    def restore_from_launcher(self) -> None:
        self._collapsed_to_launcher = False
        self._launcher.hide()
        if self._game_foreground:
            self.show()
            self.raise_()
            self.activateWindow()

    def _is_game_foreground(self) -> bool:
        try:
            return bool(self._game_foreground_probe())
        except Exception as exc:  # noqa: BLE001
            _log.warning("Game foreground probe failed: %s", exc)
            return True

    def _sync_game_foreground_visibility(self) -> None:
        foreground = self._is_game_foreground()
        if foreground == self._game_foreground:
            if (
                not foreground
                and self.isVisible()
                and not self.isActiveWindow()
            ):
                self.hide()
            return
        self._game_foreground = foreground
        if not foreground:
            self._launcher.hide()
            if self.isVisible() and not self.isActiveWindow():
                self.hide()
            return
        if self._collapsed_to_launcher:
            self._launcher.show_at(self._default_launcher_position())
        else:
            self.show()
            self.raise_()

    def _on_source_tab_changed(self, key: str) -> None:
        if key == self._active_tab:
            return
        self._hover_by_tab[self._active_tab] = self._hover_id
        self._pinned_by_tab[self._active_tab] = self._pinned_id
        self._active_tab = key
        self._hover_id = self._hover_by_tab.get(key)
        self._pinned_id = self._pinned_by_tab.get(key)
        self._refresh_table()
        self._update_title()

    def _on_target_key_changed(self, key_level: int) -> None:
        new_key = key_level if key_level > 0 else None
        if new_key is not None and not self._can_apply_manual_target_key():
            self._manual_target_key = None
            self._sync_target_key_control()
            return
        if self._manual_target_key == new_key:
            self._sync_target_key_control()
            return
        self._manual_target_key = new_key
        self._refresh_table()
        self._update_title()
        self._sync_delegate_and_panel()

    def _clear_manual_target_key(self) -> None:
        if self._manual_target_key is None:
            return
        self._manual_target_key = None
        self._sync_target_key_control()

    def _can_apply_manual_target_key(self) -> bool:
        listing = self._state.listing
        if listing is None:
            return True
        if detect_listing_context(listing) == CONTEXT_RAID:
            return False
        return listing.category_id in (0, _MPLUS_CATEGORY_ID)

    def on_applicant_added(self, applicant: Applicant) -> None:
        self._restore_party_on_next_roster = False
        self._empty_hide_timer.stop()  # cancel pending auto-hide — fresh activity
        # Order matters: launch fetch FIRST so applicant.fetch_status flips to
        # "loading" before _refresh_table reads it. Otherwise the cell briefly
        # renders the default "pending" state (which displays as "no data") for
        # the fetch duration (50-500ms), then flips to "loading" then "ready".
        self._launch_fetch(applicant)
        self._schedule_overlay_refresh(maybe_show=True)

    def on_applicant_updated(self, applicant: Applicant) -> None:
        self._restore_party_on_next_roster = False
        self._empty_hide_timer.stop()  # cancel pending auto-hide — fresh activity
        # Re-fetch ONLY when fetch_status is "pending" — apply_snapshot resets
        # to pending on (a) newly seen applicant id and (b) spec_id change.
        # Other field updates (score, ilvl, role) don't invalidate WCL data, so
        # firing _launch_fetch on every update was wasted cache-hit + signal +
        # row-rebuild. Errors are NOT auto-retried here — they'd just re-error
        # under the same WCL state (rate limit, OAuth); manual `/apscout reset`
        # from addon side restarts the pipeline if user wants a retry cycle.
        if applicant.fetch_status == "pending":
            self._launch_fetch(applicant)
        # Edge case: if the very first event for an applicant arrives as APP=
        # (e.g., addon emits "=" because it cached state across /reload), the
        # state-machine emits applicantUpdated rather than applicantAdded.
        # Window must still pop up — check visibility here too.
        self._schedule_overlay_refresh(maybe_show=True)

    def on_applicant_removed(self, applicant_id: str) -> None:
        # Clear hover/pin if THIS removed applicant was the one we were
        # showing — _refresh_table also preserves-by-id (so an unrelated
        # remove doesn't clobber pin), but we still need to NULL the field
        # for the removed-id case so panel hides instead of orphaning.
        if applicant_id == self._hover_id:
            self._hover_id = None
        if applicant_id == self._pinned_id:
            self._pinned_id = None
        self._schedule_overlay_refresh()
        if self._state.count() == 0:
            has_party = len(self._state.party_members) > 0
            was_visible = self.isVisible() and not self._collapsed_to_launcher
            # No applicants left. Two reasons: (a) addon delisted (handled by
            # on_cleared), (b) host invited everyone, listing still active.
            # Defensive: clear both ids on hide so next show doesn't bring back
            # ghost panel content from a stale id.
            self._hover_id = None
            self._pinned_id = None
            self._hover_by_tab["applicants"] = None
            self._pinned_by_tab["applicants"] = None
            if has_party:
                self._restore_party_on_next_roster = False
                self._empty_hide_timer.stop()
                self._active_tab = "party"
                self._tab_bar.set_active("party", emit=False)
                self._schedule_overlay_refresh(update_title=True, maybe_show=True)
                return
            self._restore_party_on_next_roster = (
                was_visible and self._state.listing is not None
            )
            # Case (b): listing remains, snapshots keep arriving with apps=[].
            # Hide immediately AND start a guard timer — if a new listing event
            # re-shows the window (comment change, etc.), the timer hides it
            # again after a quiet period.
            self.show_launcher_only()
            if self._state.listing is not None:
                self._empty_hide_timer.start()

    def on_listing_changed(self) -> None:
        listing = self._state.listing
        if (
            self._manual_target_key is not None
            and listing is not None
            and listing.key_level > 0
        ):
            self._clear_manual_target_key()
        elif self._manual_target_key is not None and not self._can_apply_manual_target_key():
            self._clear_manual_target_key()
        self._schedule_overlay_refresh(
            update_title=True,
            maybe_show=self._state.count() > 0 or len(self._state.party_members) > 0,
        )
        # Pop the window when listing is created — but only if there's actual
        # work to look at (applicants present). After group fills (apps=[]),
        # listing comment edits would re-show the window otherwise.
        if self._state.listing is None:
            return
        if self._state.count() > 0:
            self._maybe_show()
        else:
            # Listing active but empty — start guard timer to auto-hide if no
            # new applicants arrive in EMPTY_HIDE_DELAY_S.
            if not self._empty_hide_timer.isActive():
                self._empty_hide_timer.start()

    def on_cleared(self) -> None:
        self._listing_session_generation += 1
        self._fetches_in_flight.clear()
        self._restore_party_on_next_roster = False
        self._refresh_flush_pending = False
        self._refresh_needs_title = False
        self._refresh_needs_show = False
        self._empty_hide_timer.stop()  # listing gone, no need for guard timer
        self._table.setRowCount(0)
        self._row_for_id.clear()
        self._id_by_row = []
        self._hover_id = None
        self._pinned_id = None
        self._hover_by_tab["applicants"] = None
        self._pinned_by_tab["applicants"] = None
        self._sync_delegate_and_panel()
        self._update_title()
        if self._state.party_members:
            self._active_tab = "party"
            self._tab_bar.set_active("party", emit=False)
            self._schedule_overlay_refresh(update_title=True, maybe_show=True)
            return
        # Only hide if there's also no active listing or Party roster. EMPTY
        # arrives transiently when applicants come and go between listing-active
        # periods; hiding on every EMPTY would flicker the window.
        if self._state.listing is None:
            self._clear_manual_target_key()
            self.show_launcher_only()

    def on_roster_changed(self) -> None:
        for member in self._state.party_members.values():
            if member.fetch_status == "pending":
                self._launch_fetch(member)
        if (
            len(self._state.party_members) == 0
            and self._state.count() == 0
            and self._state.listing is None
        ):
            self._clear_manual_target_key()
            self._schedule_overlay_refresh(update_title=True, maybe_show=False)
            self.show_launcher_only()
            return
        should_show_party = self._state.count() == 0 and len(self._state.party_members) > 0
        if should_show_party:
            self._active_tab = "party"
            self._tab_bar.set_active("party", emit=False)
            if self._restore_party_on_next_roster:
                self._collapsed_to_launcher = False
        self._restore_party_on_next_roster = False
        self._schedule_overlay_refresh(
            update_title=True,
            maybe_show=should_show_party,
        )

    def note_decode(self, _snap: object) -> None:
        """Slot for ScreenshotWatcher.snapshotReceived. Bumps the local last-
        decode timestamp; _refresh_health_label reads it on the next tick.
        Arg typed `object` so overlay.py stays Snapshot-import-free; under
        score-prefixed to signal intentional unused.

        Runs on GUI thread (Qt queues cross-thread emits from the watchdog
        Observer thread onto the GUI thread automatically). Read by the timer
        slot also runs on GUI thread — no lock needed for the float field."""
        self._last_decode_time = time.time()
        self._last_decode_failed_time = None
        self._last_decode_failed_path = ""
        self._last_decode_failed_reason = ""
        self._health_label.setToolTip("")

    def note_decode_failed(self, path: str, reason: str) -> None:
        """Slot for ScreenshotWatcher.decodeFailed. Keeps marker-bearing QR
        parse failures visible in the status row until the next good decode."""
        self._last_decode_failed_time = time.time()
        self._last_decode_failed_path = path
        self._last_decode_failed_reason = reason
        self._refresh_health_label()

    def _refresh_status_row(self) -> None:
        """One-shot update of both bottom-row labels. Driven by _quota_timer
        at 1Hz. Quota refresh is idempotent (just reads last_quota snapshot);
        health refresh advances the "Xs ago" counter."""
        self._refresh_quota_label()
        self._refresh_health_label()

    def _refresh_health_label(self) -> None:
        """Updates _health_label text from _last_decode_time. None → "shot —"
        (no decode yet). Otherwise formats `time.time() - last` via _format_age.

        max(0, delta) clamp guards against a system-clock backwards-jump (DST
        transition, manual change). No color escalation in v1 — idle listings
        legitimately have no decodes for minutes; coloring would false-alarm.
        Absolute "shot Xm ago" text is enough to spot a dead pipeline."""
        failed_at = self._last_decode_failed_time
        if failed_at is not None and (
            self._last_decode_time is None or failed_at >= self._last_decode_time
        ):
            delta = max(0.0, time.time() - failed_at)
            self._health_label.setText("shot failed")
            self._health_label.setToolTip(
                f"{self._last_decode_failed_path}\n"
                f"{self._last_decode_failed_reason}\n"
                f"{_format_age(delta)} ago"
            )
            return
        last = self._last_decode_time
        if last is None:
            self._health_label.setText("shot —")
            self._health_label.setToolTip("")
            return
        delta = max(0.0, time.time() - last)
        self._health_label.setText(f"shot {_format_age(delta)}")
        self._health_label.setToolTip("")

    def _refresh_quota_label(self) -> None:
        """Pull latest quota snapshot from wcl_client and format into status
        label. Format: "WCL: spent/limit (Xm to reset)" — e.g. "WCL: 245/3600
        (52m to reset)". Before the first quota-bearing WCL response, shows
        queued/running fetches when any are active.

        Visual urgency: turns yellow at 70% spent, red at 90%."""
        q = getattr(self._wcl_client, "last_quota", None)
        if q is None:
            in_flight = len(self._fetches_in_flight)
            if in_flight > 0:
                suffix = "fetch" if in_flight == 1 else "fetches"
                self._status_label.setText(
                    f"WCL: fetching {in_flight} {suffix} (quota pending)"
                )
            else:
                self._status_label.setText("WCL: — / — (idle, no quota yet)")
            self._status_label.setStyleSheet("")
            return
        spent = int(round(q.points_spent))
        limit = int(round(q.limit_per_hour))
        remaining = self._wcl_client.quota_reset_remaining_seconds()
        if remaining is None:
            remaining = q.reset_in_seconds
        reset_min = int(remaining / 60) if remaining > 0 else 0
        reset_str = f"{reset_min}m to reset" if reset_min > 0 else "resetting"
        self._status_label.setText(f"WCL: {spent} / {limit}  ({reset_str})")
        # Color escalation
        ratio = q.points_spent / q.limit_per_hour if q.limit_per_hour > 0 else 0
        if ratio >= 0.9:
            self._status_label.setStyleSheet("color: #ff5555;")
        elif ratio >= 0.7:
            self._status_label.setStyleSheet("color: #ffcc55;")
        else:
            self._status_label.setStyleSheet("")

    def _retryable_wcl_error_rows(self) -> list[Applicant]:
        return [
            applicant
            for applicant in self._fetch_rows()
            if applicant.fetch_status == "error"
            and applicant.wcl_error_kind in _RETRYABLE_WCL_ERROR_KINDS
        ]

    def _schedule_wcl_retry(self, delay_ms: int | None = None) -> None:
        if not self._metric_preferences.any_enabled:
            return
        if self._wcl_retry_timer.isActive():
            return
        if not self._retryable_wcl_error_rows():
            return
        if delay_ms is None:
            remaining = self._wcl_client.retry_block_remaining_seconds()
            delay_ms = max(
                WCL_RETRY_CUSHION_MS,
                int(remaining * 1000) + WCL_RETRY_CUSHION_MS,
            )
        self._wcl_retry_timer.start(delay_ms)

    def _retry_failed_wcl_fetches(self) -> int:
        if not self._metric_preferences.any_enabled:
            return 0
        remaining = self._wcl_client.retry_block_remaining_seconds()
        if remaining > 0:
            self._schedule_wcl_retry(int(remaining * 1000) + WCL_RETRY_CUSHION_MS)
            return 0
        launched = 0
        for applicant in self._retryable_wcl_error_rows():
            if launched >= MAX_WCL_RETRY_BATCH:
                break
            current_identity = self._current_fetch_identity_for(applicant)
            if current_identity is not None and self._is_fetch_in_flight_for(
                current_identity
            ):
                continue
            applicant.clear_wcl_data()
            self._launch_fetch(applicant)
            launched += 1
        if launched:
            self._refresh_table()
            self._update_title()
        if any(
            not (
                (identity := self._current_fetch_identity_for(applicant)) is not None
                and self._is_fetch_in_flight_for(identity)
            )
            for applicant in self._retryable_wcl_error_rows()
        ):
            self._schedule_wcl_retry(WCL_RETRY_BATCH_DELAY_MS)
        return launched

    def _on_empty_hide_timeout(self) -> None:
        """Fired EMPTY_HIDE_DELAY_S after applicants reached 0 with listing
        still active. Auto-hide as a safety net for cases where M+ keystone
        listing isn't auto-delisted by Blizzard after group fills."""
        if self._state.count() == 0 and len(self._state.party_members) == 0:
            self.show_launcher_only()

    # ─── hover/pin panel orchestration ─────

    def _active_row_map(self) -> Mapping[str, Applicant]:
        if self._active_tab == "party":
            return self._state.party_members
        return self._state.applicants

    def _effective_listing(self) -> Listing | None:
        if self._manual_target_key is None or not self._can_apply_manual_target_key():
            return self._state.listing
        if self._state.listing is not None:
            return replace(self._state.listing, key_level=self._manual_target_key)
        return Listing(
            activity_id=0,
            dungeon_name="Mythic+",
            listing_name="",
            comment="",
            key_level=self._manual_target_key,
            category_id=2,
        )

    def _sync_target_key_control(self) -> None:
        if self._manual_target_key is not None:
            self._tab_bar.set_target_key(self._manual_target_key)
            return
        listing = self._state.listing
        self._tab_bar.set_target_key(listing.key_level if listing is not None else 0)

    def _active_sorted_rows(self) -> list[Applicant]:
        if self._active_tab == "party":
            return sort_roster_members(self._state.party_members.values())
        return sort_applicants_grouped(
            self._state.applicants.values(), self._effective_listing()
        )

    def _resolve_visible_id(self) -> str | None:
        """Hover wins over pin; both must reference an applicant currently in
        state. Returns None when nothing should display."""
        rows = self._active_row_map()
        if (
            self._hover_id
            and self._hover_id in rows
            and self._is_row_visible_for_applicant(self._hover_id)
        ):
            return self._hover_id
        if (
            self._pinned_id
            and self._pinned_id in rows
            and self._is_row_visible_for_applicant(self._pinned_id)
        ):
            return self._pinned_id
        return None

    def _is_row_visible_for_applicant(self, applicant_id: str) -> bool:
        row = self._row_for_id.get(applicant_id)
        return row is not None and not self._table.isRowHidden(row)

    def _refresh_panel(self) -> None:
        """Push current visible applicant into the panel, OR show the
        placeholder when nothing is hovered/pinned. Centralized — called from
        _sync_delegate_and_panel only, never independently, so delegate cache
        and panel content can never desync.

        Panel is always-visible (fixed-height layout, no oscillation/jolt on
        hover transitions). Visibility toggle removed; only content swaps
        between the dim placeholder and an applicant's scout card."""
        visible_id = self._resolve_visible_id()
        if visible_id is None:
            self._panel.setPlaceholder()
            return
        applicant = self._active_row_map().get(visible_id)
        if applicant is None:
            self._panel.setPlaceholder()
            return
        raw_aid, _ = _split_composite(applicant.applicant_id)
        self._panel.setApplicantData(
            applicant,
            self._effective_listing(),
            self._package_fit_by_raw.get(raw_aid) if self._active_tab == "applicants" else None,
        )

    def _apply_panel_height_above_table(self) -> None:
        if not self.isVisible():
            self._panel_anchor_extra_height = 0
            self._panel_anchor_y_offset = 0
            return

        target_height = self._panel.target_height()
        current_height = self._panel.height() or INFO_PANEL_MIN_HEIGHT
        if (
            current_height == target_height
            and self._panel.minimumHeight() == target_height
            and self._panel.maximumHeight() == target_height
        ):
            return

        previous_extra = self._panel_anchor_extra_height
        previous_y_offset = self._panel_anchor_y_offset
        new_extra = max(0, target_height - INFO_PANEL_MIN_HEIGHT)
        geom = self.geometry()
        base_y = geom.y() + previous_y_offset
        base_height = max(self.minimumHeight(), geom.height() - previous_extra)

        self._panel.setMinimumHeight(target_height)
        self._panel.setMaximumHeight(target_height)

        new_y = base_y - new_extra
        screen = self.screen()
        if screen is not None:
            new_y = max(screen.availableGeometry().top(), new_y)
        self._panel_anchor_extra_height = new_extra
        self._panel_anchor_y_offset = max(0, base_y - new_y)
        new_height = max(self.minimumHeight(), base_height + new_extra)
        if (new_y, new_height) == (geom.y(), geom.height()):
            return
        self._set_geometry_without_persist(
            geom.x(),
            new_y,
            geom.width(),
            new_height,
        )

    def _set_geometry_without_persist(self, x: int, y: int, w: int, h: int) -> None:
        self._suppress_geometry_persist = True
        try:
            self.setGeometry(x, y, w, h)
        finally:
            self._suppress_geometry_persist = False

    def _sync_delegate_and_panel(self) -> None:
        """Single bookkeeping point for (delegate hover/pin row caches →
        viewport repaint → panel content). Called from EVERY mutation site
        (cell entered/clicked/closed, leave, scroll, resize, fetch-done,
        applicant removed, on_cleared, etc). Avoids drift if any of the
        three pieces (hover, pin, panel) get out of sync."""
        hover_row = self._row_for_id.get(self._hover_id, -1) if self._hover_id else -1
        pinned_row = (
            self._row_for_id.get(self._pinned_id, -1) if self._pinned_id else -1
        )
        self._delegate.set_rows(hover_row, pinned_row)
        vp = self._table.viewport()
        if vp is not None:
            vp.update()
        self._refresh_panel()
        self._apply_panel_height_above_table()

    def _on_cell_entered(self, row: int, _col: int) -> None:
        # Bounds check guards against (a) ever-zero state during init,
        # (b) any conceivable race between setRowCount and _id_by_row rebuild.
        if not (0 <= row < len(self._id_by_row)):
            return
        new_id = self._id_by_row[row]
        if new_id == self._hover_id:
            return  # de-dup same-row entries
        self._hover_id = new_id
        self._hover_by_tab[self._active_tab] = new_id
        self._sync_delegate_and_panel()

    def _on_cell_clicked(self, row: int, _col: int) -> None:
        # cellClicked is left-button-only by Qt convention. REPLACE pin every
        # click — never toggle. The X close button is the only un-pin path
        # (avoids "click pinned row to confirm" surprising the user with
        # silent un-pin).
        if not (0 <= row < len(self._id_by_row)):
            return
        self._pinned_id = self._id_by_row[row]
        self._pinned_by_tab[self._active_tab] = self._pinned_id
        self._sync_delegate_and_panel()

    def _resolve_hover_from_cursor(self) -> str | None:
        """Map global cursor position to an applicant_id under it (or None
        if cursor is outside the table viewport / over an empty area)."""
        if not self._table.hasMouseTracking():
            return None
        vp = self._table.viewport()
        if vp is None or not vp.hasMouseTracking():
            return None
        local = vp.mapFromGlobal(QCursor.pos())
        if not vp.rect().contains(local):
            return None
        row = self._table.rowAt(local.y())
        if 0 <= row < len(self._id_by_row):
            return self._id_by_row[row]
        return None

    def _reresolve_hover_from_cursor(self) -> None:
        """Called when scrolling or window resize shifts cells under the
        stationary cursor without firing cellEntered. Re-derive hover_id
        from current cursor position; sync if changed."""
        new_id = self._resolve_hover_from_cursor()
        if new_id != self._hover_id:
            self._hover_id = new_id
            self._sync_delegate_and_panel()

    # ─── internals ─────

    def _default_launcher_position(self) -> QPoint:
        if self._saved_launcher_position is not None:
            x, y, _w, _h = _clamp_geometry_to_screen(
                self._saved_launcher_position.x,
                self._saved_launcher_position.y,
                LAUNCHER_SIZE,
                LAUNCHER_SIZE,
                # WHY: a taskbar/available-geometry change can leave a saved
                # launcher position barely visible at the screen edge. Clamp
                # that back onto the same screen instead of treating it like a
                # disconnected monitor and recentering the launcher.
                min_visible_px=1,
            )
            return QPoint(x, y)
        g = self.geometry()
        x = g.x() + max(0, g.width() - LAUNCHER_SIZE)
        y = g.y()
        x, y, _w, _h = _clamp_geometry_to_screen(
            x,
            y,
            LAUNCHER_SIZE,
            LAUNCHER_SIZE,
            min_visible_px=20,
        )
        return QPoint(x, y)

    def _persist_launcher_position(self) -> None:
        pos = self._launcher.pos()
        launcher_position = LauncherPosition(pos.x(), pos.y())
        self._saved_launcher_position = launcher_position
        save_launcher_position(self._config_dir, launcher_position)

    def _maybe_show(self) -> None:
        if not self._game_foreground:
            self._launcher.hide()
            if not self.isActiveWindow():
                self.hide()
            return
        if self._collapsed_to_launcher:
            self._launcher.show_at(self._default_launcher_position())
            return
        if not self.isVisible():
            g = self.geometry()
            _log.info(
                "Showing overlay at (%d,%d) %dx%d",
                g.x(),
                g.y(),
                g.width(),
                g.height(),
            )
            self.show()
            self.raise_()

    def _render_row(self, row: int, applicant: Applicant) -> None:
        """Write applicant data into an existing table row. Caller manages
        row creation / position — used by _refresh_table after sort.

        Per-cell tooltips removed: applicant data now lives in the top
        ApplicantInfoPanel which is row-hover/pin driven. Cell items are
        plain text + colour only."""
        spec_text = SPEC_SHORT_NAMES.get(applicant.spec_id, f"#{applicant.spec_id}")
        spec_item = QTableWidgetItem(spec_text)
        spec_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        spec_item.setData(Qt.ItemDataRole.UserRole, applicant.role)
        icon = _role_icon(applicant.role)
        if icon is not None:
            spec_item.setIcon(icon)
        # Class-coloured cell background mirrors the panel's `_class_pill` so
        # the table and info-panel use the same visual language for class
        # identity. Black foreground + bold reads cleanly across all 13
        # saturated class colours (PRIEST #FFFFFF is high-contrast WCAG AAA;
        # darker classes like DK red still pass at this weight). Hover/pin
        # stripe is painted by `_HoverHighlightDelegate` ON TOP of this cell
        # background (delegate paints last in cell-rect render order), so the
        # 3 px left-edge stripe still wins where they overlap.
        cls_hex = CLASS_COLOURS.get(applicant.cls, "#888888")
        spec_item.setBackground(QColor(cls_hex))
        spec_item.setForeground(QColor("#000000"))
        spec_bold = QFont()
        spec_bold.setBold(True)
        spec_item.setFont(spec_bold)
        self._table.setItem(row, COL_SPEC, spec_item)

        # Display Charname only (without -Realm) for compactness; full in panel
        display_name = applicant.name.split("-", 1)[0]
        name_item = QTableWidgetItem(display_name)
        name_item.setForeground(QColor(CLASS_COLOURS.get(applicant.cls, "#FFFFFF")))
        f = QFont()
        f.setBold(True)
        name_item.setFont(f)
        self._table.setItem(row, COL_NAME, name_item)

        # iLvl + RIO numeric cells. RIO shows the applying character's score,
        # plus a higher RaiderIO main score in brackets when available.
        ilvl_item = QTableWidgetItem(str(applicant.ilvl) if applicant.ilvl else "—")
        ilvl_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, COL_ILVL, ilvl_item)

        rio_score = effective_rio_score(applicant)
        rio_item = QTableWidgetItem(_rio_display_text(applicant))
        rio_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        rio_item.setFont(f)  # bold — primary scouting signal alongside name
        # Tier-band foreground colour (gold/purple/blue/green/white) matches
        # RaiderIO addon's score-tier visual language, mirrors raid percentile
        # cell palette so eye-tracking across columns reads consistently.
        rio_item.setForeground(QColor(rio_score_colour(rio_score)))
        self._table.setItem(row, COL_RIO, rio_item)

        # Raid percentile cells: dual "best/median" display.
        raid_cells = [
            (COL_N, applicant.raid_normal, applicant.raid_normal_median),
            (COL_H, applicant.raid_heroic, applicant.raid_heroic_median),
            (COL_M, applicant.raid_mythic, applicant.raid_mythic_median),
        ]
        for col, best, median in raid_cells:
            self._table.setItem(
                row, col, _raid_dual_cell(best, median, applicant.fetch_status)
            )
        raw_aid, _ = _split_composite(applicant.applicant_id)
        listing = self._effective_listing()
        package = self._package_fit_by_raw.get(raw_aid)
        if (
            package is not None
            and package.display
            and self._group_size_by_raw.get(raw_aid, 1) >= 2
        ):
            self._table.setItem(
                row,
                COL_MPLUS,
                _mplus_group_cell(package, applicant, listing),
            )
        else:
            # M+ cell: context-fit display for M+ listings, legacy headline otherwise.
            self._table.setItem(
                row, COL_MPLUS, _mplus_dual_cell(applicant, listing)
            )

    def _refresh_table(self) -> None:
        """Rebuild table sorted by effective RIO score DESC.
        Called on every state change — full rebuild is trivial at our applicant
        counts (≤30) and avoids row-bookkeeping on insert/sort.

        Hover & pin preservation:
        - Capture prev_hover_id / prev_pinned_id BEFORE rebuild.
        - Reset delegate row caches to (-1, -1) BEFORE setRowCount so any
          intra-rebuild repaint can't paint stripe at a stale row index.
        - After rebuild: if prev_hover_id is gone, fall back to cursor-resolved;
          if prev_pinned_id is gone, drop pin (no fallback — pin is intentional
          state, don't auto-replace).
        - End with _sync_delegate_and_panel — single bookkeeping point that
          re-applies correct stripe rows + refreshes panel content.

        Sort: see `sort_applicants_grouped` docstring. Known M+/raid listings
        use context package fit; unknown listings preserve prior max-RIO order
        with higher RaiderIO main score folded into the max when available.

        Group markers: multi-member-group rows get a per-group hashed-hue
        bracket in the spec column. Solo rows get nothing — chrome reserved for
        actual grouping signal."""
        prev_hover = self._hover_id
        prev_pinned = self._pinned_id
        active_rows = self._active_row_map()
        self._tab_bar.set_counts(
            applicants=len(self._state.applicants),
            party=len(self._state.party_members),
        )

        # Reset delegate to "no highlight anywhere" BEFORE we tear down items.
        # Avoids a stripe / band paint at a row index that no longer maps if Qt
        # issues an intermediate paint event between setRowCount and the post-
        # rebuild marker reset. Symmetric with set_group_markers({}) below.
        self._delegate.set_rows(-1, -1)
        self._delegate.set_group_markers({})

        sorted_applicants = self._active_sorted_rows()
        listing = self._effective_listing()
        self._group_size_by_raw = {}
        self._package_fit_by_raw = {}
        if self._active_tab == "applicants":
            group_members: dict[str, list[Applicant]] = {}
            for applicant in sorted_applicants:
                raw_aid, _ = _split_composite(applicant.applicant_id)
                self._group_size_by_raw[raw_aid] = (
                    self._group_size_by_raw.get(raw_aid, 0) + 1
                )
                group_members.setdefault(raw_aid, []).append(applicant)
            if detect_listing_context(listing) in (
                CONTEXT_MPLUS,
                CONTEXT_RAID,
            ):
                for raw_aid, members in group_members.items():
                    if len(members) < 2:
                        continue
                    fit = package_fit(members, listing)
                    if fit.display:
                        self._package_fit_by_raw[raw_aid] = fit

        self._table.setRowCount(len(sorted_applicants))
        self._row_for_id.clear()
        self._id_by_row = [a.applicant_id for a in sorted_applicants]
        for row, applicant in enumerate(sorted_applicants):
            self._row_for_id[applicant.applicant_id] = row
            self._render_row(row, applicant)
        self._maybe_grow_name_column(sorted_applicants)

        if self._active_tab == "applicants":
            self._delegate.set_group_markers(
                _build_group_markers(
                    (row_idx, a.applicant_id)
                    for row_idx, a in enumerate(sorted_applicants)
                )
            )

        # Preserve hover/pin BY ID; cursor fallback only when prev id is gone.
        if prev_hover not in active_rows:
            self._hover_id = self._resolve_hover_from_cursor()
        if prev_pinned not in active_rows:
            self._pinned_id = None
            self._pinned_by_tab[self._active_tab] = None

        # Apply current role filter to the freshly-rebuilt rows so newly-
        # arrived applicants in a filtered-out role come in pre-hidden.
        # Also re-feeds visible-only group markers — must run AFTER the
        # set_group_markers call above so it's the final word on markers.
        self._apply_role_filter()
        # Re-apply correct stripe rows + refresh panel content (single point).
        self._sync_delegate_and_panel()

    def _is_filter_active(self) -> bool:
        """True iff the role filter actually hides anything. Empty set OR
        all-3-selected (== ALL_ROLES) both mean 'show all' — count display
        and row visibility math both branch on this single helper."""
        return bool(self._role_filter) and self._role_filter != ALL_ROLES

    def _on_role_filter_changed(self, active: set) -> None:
        """RoleFilterBar.filterChanged slot. Mutates _role_filter, possibly
        clears the pin if pinned applicant is now filtered out, then re-
        applies filter + re-resolves hover + refreshes panel + updates title."""
        self._role_filter = active
        if self._pinned_id is not None and self._is_filter_active():
            pinned = self._active_row_map().get(self._pinned_id)
            if self._active_tab == "party":
                keep_pin = pinned is not None and pinned.role in self._role_filter
            else:
                raw_aid, _ = _split_composite(self._pinned_id)
                keep_pin = raw_aid in self._role_filter_visible_raw_ids()
            if not keep_pin:
                self._pinned_id = None
                self._pinned_by_tab[self._active_tab] = None
        self._apply_role_filter()
        # Hovered row may now be hidden — re-resolve from cursor position.
        self._reresolve_hover_from_cursor()
        self._sync_delegate_and_panel()
        self._update_title()

    def _role_filter_visible_raw_ids(self) -> set[str]:
        """Raw applicant groups visible under the role filter.

        Group applications are package decisions: if one member matches the
        selected role lens, every member must stay visible so the package risk
        can still be judged honestly.
        """
        raw_ids: set[str] = set()
        if not self._is_filter_active():
            for applicant_id in self._state.applicants:
                raw_aid, _ = _split_composite(applicant_id)
                raw_ids.add(raw_aid)
            return raw_ids
        for applicant in self._state.applicants.values():
            if applicant.role in self._role_filter:
                raw_aid, _ = _split_composite(applicant.applicant_id)
                raw_ids.add(raw_aid)
        return raw_ids

    def _apply_role_filter(self) -> None:
        """Apply role lens while preserving accepted-together group packages."""
        is_active = self._is_filter_active()
        visible_raw_ids = self._role_filter_visible_raw_ids()
        active_rows = self._active_row_map()
        visible_count = 0
        visible_id_by_row: list[tuple[int, str]] = []
        for row, applicant_id in enumerate(self._id_by_row):
            applicant = active_rows.get(applicant_id)
            if applicant is None:
                continue
            if self._active_tab == "party":
                is_visible = (not is_active) or applicant.role in self._role_filter
            else:
                raw_aid, _ = _split_composite(applicant_id)
                is_visible = (not is_active) or raw_aid in visible_raw_ids
            self._table.setRowHidden(row, not is_visible)
            if is_visible:
                visible_count += 1
                visible_id_by_row.append((row, applicant_id))

        self._role_filter_bar.set_status(visible_count, len(self._id_by_row))

        # Rebuild applicant group markers from visible-only rows so filtered
        # views keep the bracket shape aligned to what is actually on screen.
        if self._active_tab == "applicants":
            self._delegate.set_group_markers(_build_group_markers(visible_id_by_row))
        else:
            self._delegate.set_group_markers({})

    def _maybe_grow_name_column(self, applicants: list[Applicant]) -> None:
        """Grow Name column a little, capped to preserve compact overlay width."""
        if not applicants:
            return
        fm = self._table.fontMetrics()
        widest = max(fm.horizontalAdvance(a.name.split("-", 1)[0]) for a in applicants)
        target = max(COLUMN_WIDTHS[COL_NAME], min(NAME_COLUMN_MAX_WIDTH, widest + 18))
        if target > self._max_name_width_px:
            self._max_name_width_px = target
            self._table.setColumnWidth(COL_NAME, target)
            self._apply_metric_minimum_width()

    def _apply_metric_column_visibility(self) -> None:
        prefs = self._metric_preferences
        self._table.setColumnHidden(COL_N, not prefs.raid_normal)
        self._table.setColumnHidden(COL_H, not prefs.raid_heroic)
        self._table.setColumnHidden(COL_M, not prefs.raid_mythic)
        self._table.setColumnHidden(COL_MPLUS, not prefs.mplus)
        self._apply_metric_minimum_width()

    def _apply_metric_minimum_width(self) -> None:
        self.setMinimumWidth(USER_MIN_WINDOW_WIDTH)

    def apply_metric_preferences(
        self,
        metric_preferences: MetricPreferences,
        *,
        refetch_missing: bool = True,
    ) -> None:
        self._metric_preferences = metric_preferences
        self._wcl_client.metric_preferences = metric_preferences
        self._panel.set_metric_preferences(metric_preferences)
        self._apply_metric_column_visibility()
        self._apply_metric_minimum_width()
        for applicant in self._fetch_rows():
            if not metric_preferences.any_enabled:
                applicant.clear_wcl_data(fetch_status="ready")
                continue
            applicant.project_wcl_data_to_preferences(metric_preferences)
            if (
                refetch_missing
                and applicant.fetch_status == "ready"
                and not applicant.wcl_data_covers(metric_preferences)
            ):
                applicant.clear_wcl_data()
                self._launch_fetch(applicant)
            elif applicant.fetch_status in ("pending", "loading"):
                self._launch_fetch(applicant)
        self._refresh_table()
        self._update_title()

    def bump_wcl_runtime_generation(self) -> None:
        self._wcl_runtime_generation += 1
        for applicant in self._fetch_rows():
            applicant.clear_wcl_data()
        self._refresh_table()
        self._update_title()
        for applicant in self._fetch_rows():
            self._launch_fetch(applicant)

    def _fetch_rows(self) -> Iterable[Applicant]:
        yield from self._state.applicants.values()
        yield from self._state.party_members.values()

    def _in_flight_identity(self, applicant_id: str) -> _FetchIdentity | None:
        return self._fetches_in_flight.get(applicant_id)

    def _mark_fetch_in_flight(self, identity: _FetchIdentity) -> None:
        self._fetches_in_flight[identity.storage_key] = identity

    def _discard_fetch_if_current(self, identity: _FetchIdentity) -> None:
        if self._fetches_in_flight.get(identity.storage_key) == identity:
            self._fetches_in_flight.pop(identity.storage_key, None)

    def _is_fetch_in_flight_for(self, identity: _FetchIdentity) -> bool:
        current = self._fetches_in_flight.get(identity.storage_key)
        return (
            current is not None
            and _same_fetch_target_except_preferences(current, identity)
            and current.metric_preferences.covers(identity.metric_preferences)
        )

    def _row_source_for(self, applicant: Applicant) -> str:
        if self._state.party_members.get(applicant.applicant_id) is applicant:
            return "party"
        return "applicants"

    def _row_for_fetch_identity(self, identity: _FetchIdentity) -> Applicant | None:
        if identity.row_source == "party":
            return self._state.party_members.get(identity.applicant_id)
        return self._state.applicants.get(identity.applicant_id)

    def _current_fetch_identity_for(
        self, applicant: Applicant
    ) -> _FetchIdentity | None:
        resolved = _fetch_identity_for_applicant(
            applicant,
            self._state.player.full_name,
            self._wcl_client.region,
            self._metric_preferences,
            self._wcl_runtime_generation,
            self._listing_session_generation,
            row_source=self._row_source_for(applicant),
        )
        if resolved is None:
            return None
        identity, _ = resolved
        return identity

    def _launch_fetch(self, applicant: Applicant) -> None:
        if not self._metric_preferences.any_enabled:
            applicant.clear_wcl_data(fetch_status="ready")
            return
        resolved = _fetch_identity_for_applicant(
            applicant,
            self._state.player.full_name,
            self._wcl_client.region,
            self._metric_preferences,
            self._wcl_runtime_generation,
            self._listing_session_generation,
            row_source=self._row_source_for(applicant),
        )
        if resolved is None:
            applicant.fetch_status = "error"
            applicant.error_message = "missing realm"
            applicant.wcl_error_kind = ""
            return  # caller (on_applicant_added/_updated) refreshes table after
        identity, charname = resolved
        if self._is_fetch_in_flight_for(identity):
            return  # avoid duplicate concurrent fetches for the same WCL scope
        applicant.fetch_status = "loading"
        applicant.error_message = ""
        applicant.wcl_error_kind = ""
        self._mark_fetch_in_flight(identity)
        task = _FetchTask(identity, charname, self._wcl_client, self._cache)
        task.signals.done.connect(self._on_fetch_done)
        if self._pool is not None:
            _log.info(
                "WCL fetch queued: %s-%s region=%s spec=%s role=%s prefs=%s "
                "in_flight=%d",
                charname,
                identity.server_slug,
                identity.region,
                identity.spec_id,
                identity.metric_role,
                identity.metric_preferences.cache_key(),
                len(self._fetches_in_flight),
            )
            self._pool.start(task)
            self._refresh_quota_label()

    def _on_fetch_done(
        self,
        fetched_identity: _FetchIdentity,
        ranks: CharacterRanks,
    ) -> None:
        self._discard_fetch_if_current(fetched_identity)
        self._refresh_quota_label()
        applicant = self._row_for_fetch_identity(fetched_identity)
        if applicant is None:
            return
        current = _fetch_identity_for_applicant(
            applicant,
            self._state.player.full_name,
            self._wcl_client.region,
            self._metric_preferences,
            self._wcl_runtime_generation,
            self._listing_session_generation,
            row_source=fetched_identity.row_source,
        )
        if current is None:
            applicant.clear_wcl_data(fetch_status="error")
            applicant.error_message = "missing realm"
            applicant.wcl_error_kind = ""
            self._sync_delegate_and_panel()
            return
        current_identity, _ = current
        if not _same_fetch_target_except_preferences(
            current_identity, fetched_identity
        ) or not fetched_identity.metric_preferences.covers(
            current_identity.metric_preferences
        ):
            if not self._is_fetch_in_flight_for(
                current_identity
            ) and not applicant.wcl_data_covers(self._metric_preferences):
                applicant.clear_wcl_data()
                self._launch_fetch(applicant)
            self._sync_delegate_and_panel()
            return
        if ranks.not_found:
            applicant.clear_wcl_data(fetch_status="not_found")
            applicant.error_message = ""
            applicant.wcl_error_kind = ""
        elif ranks.error:
            applicant.clear_wcl_data(fetch_status="error")
            applicant.error_message = ranks.error
            applicant.wcl_error_kind = ranks.error_kind
            self._schedule_wcl_retry()
        else:
            applicant.fetch_status = "ready"
            applicant.error_message = ""
            applicant.wcl_error_kind = ""
            applicant.wcl_metric_preferences = fetched_identity.metric_preferences
            applicant.raid_normal = ranks.raid_normal
            applicant.raid_heroic = ranks.raid_heroic
            applicant.raid_mythic = ranks.raid_mythic
            applicant.raid_normal_median = ranks.raid_normal_median
            applicant.raid_heroic_median = ranks.raid_heroic_median
            applicant.raid_mythic_median = ranks.raid_mythic_median
            applicant.mplus_dps = ranks.mplus_dps
            applicant.mplus_hps = ranks.mplus_hps
            applicant.mplus_dps_median = ranks.mplus_dps_median
            applicant.mplus_hps_median = ranks.mplus_hps_median

            # Convert DungeonPerf → list[dict] for cross-module storage
            # (Applicant lives in state.py without WCL dependency).
            def _dungeon_perf_dict(d) -> dict:
                return {
                    "name": d.name,
                    "parse_percent": d.parse_percent,
                    "median_percent": d.median_percent,
                    "key_level": d.key_level,
                    "run_count": d.run_count,
                    "brackets": [
                        {
                            "key_level": b.key_level,
                            "parse_percent": b.parse_percent,
                            "median_percent": b.median_percent,
                            "run_count": b.run_count,
                        }
                        for b in d.brackets
                    ],
                }

            applicant.mplus_dps_breakdown = [
                _dungeon_perf_dict(d) for d in ranks.mplus_dps_breakdown
            ]
            applicant.mplus_hps_breakdown = [
                _dungeon_perf_dict(d) for d in ranks.mplus_hps_breakdown
            ]
            applicant.project_wcl_data_to_preferences(self._metric_preferences)
        # Re-sort: this fetch may have produced a new M+ value that changes the
        # applicant's row position. _refresh_table ends with sync — so a pinned
        # panel showing this applicant rebuilds its HTML automatically here.
        # Don't add another _refresh_panel call — single bookkeeping point.
        self._schedule_overlay_refresh(update_title=False)

    def _update_title(self) -> None:
        self._tab_bar.set_counts(
            applicants=len(self._state.applicants),
            party=len(self._state.party_members),
        )
        self._sync_target_key_control()
        if self._active_tab == "party":
            n = len(self._state.party_members)
            listing = self._effective_listing()
            if listing is not None:
                level = f" +{listing.key_level}" if listing.key_level > 0 else ""
                dn = listing.dungeon_name
                generic = (not dn) or dn == "?" or dn.lower() in (
                    "mythic+",
                    "mythic plus",
                )
                if generic:
                    self._title_bar.setTitleText(f"Party{level} ({n})")
                else:
                    self._title_bar.setTitleText(f"Party — {dn}{level} ({n})")
            else:
                self._title_bar.setTitleText(f"Party ({n})")
            self._title_bar.title_label.setToolTip(_format_listing_tooltip(listing))
            return
        listing = self._effective_listing()
        n = self._state.count()
        # Filter-aware count: show (visible / total) when filter actually
        # hides rows; plain (total) otherwise. Single helper avoids the
        # ambiguity of "(20)" when 15 are hidden.
        if self._is_filter_active():
            visible_raw_ids = self._role_filter_visible_raw_ids()
            n_visible = sum(
                1
                for applicant_id in self._state.applicants
                if _split_composite(applicant_id)[0] in visible_raw_ids
            )
            count_str = f"({n_visible} / {n})"
        else:
            count_str = f"({n})"
        if listing is not None:
            level = f" +{listing.key_level}" if listing.key_level > 0 else ""
            # Skip the dungeon-name segment when it's just the generic LFG
            # activity name "Mythic+" (host listed "any keystone" rather than a
            # specific dungeon). "M+ Applicants — Mythic+ (12)" is redundant —
            # collapse to "M+ Applicants (12)" or "M+ Applicants +12 (3)".
            dn = listing.dungeon_name
            generic = (not dn) or dn == "?" or dn.lower() in ("mythic+", "mythic plus")
            if generic:
                self._title_bar.setTitleText(f"M+ Applicants{level} {count_str}")
            else:
                self._title_bar.setTitleText(f"M+ Applicants — {dn}{level} {count_str}")
        else:
            self._title_bar.setTitleText(f"M+ Applicants {count_str}")
        # Listing tooltip — host's listing_name + comment from in-game LFG UI.
        # Read by eventFilter title-label branch on hover.
        self._title_bar.title_label.setToolTip(_format_listing_tooltip(listing))

    def _persist_geometry(self) -> None:
        g = self.geometry()
        extra = self._panel_anchor_extra_height
        y_offset = self._panel_anchor_y_offset
        save_geometry(
            self._config_dir,
            WindowGeometry(
                g.x(),
                g.y() + y_offset,
                g.width(),
                max(self.minimumHeight(), g.height() - extra),
            ),
        )

    def flush_geometry(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
        self._persist_geometry()

    # ─── overrides ─────

    def eventFilter(self, obj, event):  # type: ignore[override]
        """Routes tooltip events through QToolTip.showText(), AND handles
        hover-bookkeeping events on the table viewport + WindowDeactivate on self.

        Branches A/B/E handle ToolTip rendering (header column legends, title-
        bar listing tooltip, role-filter buttons) — kept after refactor since
        Qt's standard tooltip path silently fails on this overlay's translucent
        flag combo. Tooltip branches return True to consume the event.

        Branches C/D handle row-hover panel state:
        - C: viewport Leave → clear hover; viewport MouseMove over empty area
          (rowAt(y) < 0) → clear hover.
        - D: WindowDeactivate (Alt-Tab away) → clear hover, KEEP pin.

        Hover/window branches return False so Qt continues normal processing
        after our bookkeeping runs."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QHelpEvent

        if (
            event is not None
            and event.type() == QEvent.Type.ToolTip
            and isinstance(event, QHelpEvent)
        ):
            # Branch A — header viewport (column header tooltips).
            header = self._table.horizontalHeader()
            if header is not None and obj is header.viewport():
                col = header.logicalIndexAt(event.pos())
                tip = ""
                if 0 <= col < self._table.columnCount():
                    item = self._table.horizontalHeaderItem(col)
                    if item is not None:
                        tip = item.toolTip()
                return _render_tooltip(obj, tip, event.globalPos())
            # Branch B — title-bar label (listing tooltip). Independent from
            # Branch A: only one matches per call by `obj is` identity.
            if obj is self._title_bar.title_label:
                return _render_tooltip(
                    obj, self._title_bar.title_label.toolTip(), event.globalPos()
                )
            # Branch E — action buttons (role filter/reset/title hide tooltips).
            # Identity match only: objectName/text can change without affecting
            # which child widgets need the translucent-overlay tooltip bypass.
            if any(obj is widget for widget in self._action_tooltip_widgets):
                return _render_tooltip(obj, obj.toolTip(), event.globalPos())
        # Branch C — table viewport Leave / MouseMove (hover bookkeeping).
        table_vp = self._table.viewport()
        if event is not None and table_vp is not None and obj is table_vp:
            if event.type() == QEvent.Type.Leave:
                # Qt can emit Leave while the overlay is resizing upward to
                # make room for the expanded info panel. Re-resolve from the
                # global cursor before clearing, otherwise hover can oscillate
                # between detailed and placeholder states.
                new_id = self._resolve_hover_from_cursor()
                if new_id != self._hover_id:
                    self._hover_id = new_id
                    self._sync_delegate_and_panel()
            elif event.type() == QEvent.Type.MouseMove:
                # Cursor over the empty area below the last row → clear hover.
                # event has position() in Qt6 (returns QPointF).
                pos = event.position().toPoint()  # type: ignore[attr-defined]
                if self._table.rowAt(pos.y()) < 0 and self._hover_id is not None:
                    self._hover_id = None
                    self._sync_delegate_and_panel()
            return False  # never consume — let Qt continue normal processing
        # Branch D — Alt-Tab away from window.
        if (
            event is not None
            and obj is self
            and event.type() == QEvent.Type.WindowDeactivate
        ):
            if self._hover_id is not None:
                self._hover_id = None
                self._sync_delegate_and_panel()
            return False
        return super().eventFilter(obj, event)

    def showEvent(self, event):  # type: ignore[override]
        self._collapsed_to_launcher = False
        self._launcher.hide()
        super().showEvent(event)
        # Mouse may have moved while window was hidden — drop stale hover.
        # Pin survives intentionally (it's persistent user state).
        if self._hover_id is not None:
            self._hover_id = None
            self._sync_delegate_and_panel()
        QTimer.singleShot(0, self._apply_panel_height_above_table)
        # Defer cursor-resolve to next event-loop tick: showEvent fires BEFORE
        # the show actually completes (Qt hasn't laid out the table viewport
        # yet, so viewport().rect() is zero-sized). singleShot(0, ...) runs
        # after Qt's layout pass, so rowAt(y) returns the row under the cursor
        # if the window pops up under a stationary mouse — no wiggle required.
        QTimer.singleShot(0, self._reresolve_hover_from_cursor)

    def moveEvent(self, event):
        super().moveEvent(event)
        if not self._suppress_geometry_persist:
            self._save_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._suppress_geometry_persist:
            return
        self._save_timer.start()
        # Geometry change can shift cells under a stationary cursor without
        # firing cellEntered — re-resolve hover from cursor position.
        self._reresolve_hover_from_cursor()

    def closeEvent(self, event):
        self.flush_geometry()
        super().closeEvent(event)


# ───────────────────────────────────────────────────────────────────
# Geometry helper


def _normalize_loaded_geometry(geo: WindowGeometry) -> WindowGeometry:
    """Migrate saved geometry that came from wider pre-compact layouts."""
    if geo.layout_version >= WINDOW_GEOMETRY_LAYOUT_VERSION:
        return geo
    if LEGACY_COMPACT_WINDOW_WIDTH < geo.w <= 780:
        height = DEFAULT_WINDOW_HEIGHT if 420 <= geo.h <= 580 else geo.h
        return WindowGeometry(
            geo.x,
            geo.y,
            DEFAULT_WINDOW_WIDTH,
            height,
            WINDOW_GEOMETRY_LAYOUT_VERSION,
        )
    return WindowGeometry(geo.x, geo.y, geo.w, geo.h, WINDOW_GEOMETRY_LAYOUT_VERSION)


def _clamp_rect_to_bounds(
    x: int,
    y: int,
    w: int,
    h: int,
    bounds: QRect,
) -> tuple[int, int, int, int]:
    if bounds.width() <= 0 or bounds.height() <= 0:
        return (x, y, w, h)
    cw = min(max(1, w), bounds.width())
    ch = min(max(1, h), bounds.height())
    min_x = bounds.x()
    min_y = bounds.y()
    max_x = bounds.x() + bounds.width() - cw
    max_y = bounds.y() + bounds.height() - ch
    cx = min(max(x, min_x), max_x)
    cy = min(max(y, min_y), max_y)
    return (cx, cy, cw, ch)


def _clamp_geometry_to_screen(
    x: int, y: int, w: int, h: int, *, min_visible_px: int = 80
) -> tuple[int, int, int, int]:
    """Clamp window rect to a visible screen. Picks first screen whose
    geometry intersects the saved rect by ≥80px on each axis (ensures the
    title bar is visibly grabbable, not just a 1-pixel sliver). Falls back
    to centering on primary screen.

    Why: Qt's setGeometry happily places windows at any coordinates including
    far off the visible desktop (e.g. (3000, 0) when a previous monitor at
    that position is now disconnected). The window would render but be
    invisible — looks identical to "overlay broken" from user's POV."""
    screens = QGuiApplication.screens()
    if not screens:
        return (x, y, w, h)
    for s in screens:
        sg = s.availableGeometry()
        # Visible overlap on each axis
        ox = max(0, min(x + w, sg.x() + sg.width()) - max(x, sg.x()))
        oy = max(0, min(y + h, sg.y() + sg.height()) - max(y, sg.y()))
        if ox >= min_visible_px and oy >= min_visible_px:
            return _clamp_rect_to_bounds(x, y, w, h, sg)
    # No good intersection — center on primary
    primary = QGuiApplication.primaryScreen()
    if primary is None:
        return (x, y, w, h)
    pg = primary.availableGeometry()
    cw = min(w, pg.width())
    ch = min(h, pg.height())
    cx = pg.x() + (pg.width() - cw) // 2
    cy = pg.y() + (pg.height() - ch) // 2
    return _clamp_rect_to_bounds(cx, cy, cw, ch, pg)


# ───────────────────────────────────────────────────────────────────
# Cell rendering helper


def _text_colour_for_bg(bg_hex: str | None) -> str:
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


def _raid_dual_cell(
    best: float | None,
    median: float | None,
    fetch_status: str,
) -> QTableWidgetItem:
    """Raid difficulty cell — shows "best/median" pair (WCL UI's "Best Perf.
    Avg." vs "Median Perf. Avg."). Best is the headline (skill ceiling);
    median is consistency signal. Background colour from BEST since that's
    the primary scouting signal — gold cell with low median tells "lucky
    pulls" story; same gold with high median tells "stable pumper".

    Per-cell tooltip removed: full context now lives in the row-hover panel."""
    text, fg, bg = _raid_cell_visuals(best, median, fetch_status)
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    if fg is not None:
        item.setForeground(QColor(fg))
    if bg is not None:
        item.setBackground(QColor(bg))
        f = QFont()
        f.setBold(True)
        item.setFont(f)
    return item


def _raid_cell_visuals(
    best: float | None,
    median: float | None,
    fetch_status: str,
) -> tuple[str, str | None, str | None]:
    """Returns (text, foreground_hex|None, background_hex|None).
    Pure function — no Qt dependency — keeps cell-rendering logic testable
    and deterministic. None on fg/bg means "leave at default"."""
    if fetch_status == "loading":
        return "…", "#888", None
    if fetch_status == "error":
        return "?", "#ff5555", None
    if fetch_status == "not_found":
        return "—", "#5d5d5d", None
    if best is None and median is None:
        return "—", "#5d5d5d", None

    best_str = f"{int(round(best))}" if best is not None else "—"
    # When median is missing (single-run, or only one encounter logged at this
    # difficulty), the literal "/—" added visual noise without information
    # ("89/—" reads like a broken value). Suppress the slash and second value
    # entirely; row-hover panel carries the explanation.
    if median is None:
        text = best_str if best is not None else "—"
    else:
        text = f"{best_str}/{int(round(median))}"
    bg = percentile_colour(best) if best is not None else None
    fg = _text_colour_for_bg(bg) if bg is not None else None
    return text, fg, bg


def _mplus_key_level(entry: object) -> int:
    if not isinstance(entry, dict):
        return 0
    return positive_int(entry.get("key_level"))


def _mplus_run_count(entry: object) -> int:
    if not isinstance(entry, dict):
        return 0
    return nonnegative_int(entry.get("run_count"))


def _mplus_sort_key(entry: dict) -> tuple[int, str]:
    name = entry.get("name")
    return (-_mplus_key_level(entry), str(name or ""))


def _normalise_dungeon_name(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _rio_dungeon_row_key(name: str, listing: Listing | None) -> str:
    row_key = _normalise_dungeon_name(name)
    if listing is None:
        return row_key
    mapped_name = mplus_dungeon_name_for_activity_id(listing.activity_id)
    mapped_key = _normalise_dungeon_name(mapped_name)
    if mapped_key and row_key == _normalise_dungeon_name(listing.dungeon_name):
        return mapped_key
    return row_key


def _rio_dungeon_rows_by_name(
    applicant: Applicant, listing: Listing | None
) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for entry in applicant.rio_dungeons:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        key = positive_int(entry.get("key_level"))
        row_key = _rio_dungeon_row_key(name, listing)
        if not name or not row_key or key <= 0:
            continue
        existing = rows.get(row_key)
        if existing is None or key > positive_int(existing.get("key_level")):
            rows[row_key] = {"name": name, "key_level": key}
    return rows


def _wcl_dungeon_rows_by_name(
    applicant: Applicant, listing: Listing | None
) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    if applicant.fetch_status != "ready":
        return rows
    fit_rows = mplus_dungeon_fit_rows(applicant, listing)
    if fit_rows:
        for row in fit_rows:
            row_key = _normalise_dungeon_name(row.dungeon_name)
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
        key=_mplus_sort_key,
    ):
        name = str(entry.get("name") or "").strip()
        row_key = _normalise_dungeon_name(name)
        if not name or not row_key:
            continue
        best = safe_percent(entry.get("parse_percent"))
        rows[row_key] = {
            "name": name,
            "key_level": _mplus_key_level(entry),
            "text": _mplus_dungeon_metric_text(entry),
            "colour": percentile_colour(best) if best is not None else "#2a2a33",
        }
    return rows


def _highest_mplus_key_level(breakdown: Iterable[object]) -> int:
    highest = 0
    for entry in breakdown:
        highest = max(highest, _mplus_key_level(entry))
    return highest


def _mplus_cell_visuals(
    applicant: Applicant, listing: Listing | None = None
) -> tuple[str, str | None, str | None]:
    """Returns table text/colours for the role-relevant M+ headline cell."""
    status = applicant.fetch_status
    if status in {"loading", "pending"}:
        return "…", "#888", None

    fit = candidate_fit(applicant, listing)
    if fit.context == CONTEXT_MPLUS and fit.display:
        bg = fit.colour
        fg = _text_colour_for_bg(bg) if bg is not None else None
        return f"Fit {fit.display}", fg, bg

    if status == "error":
        return "?", "#ff5555", None
    if status == "not_found":
        return "—", "#5d5d5d", None

    _metric_label, breakdown, best, median = role_mplus_view(applicant)
    if best is None and median is None:
        return "—", "#5d5d5d", None

    best_str = f"{int(round(best))}" if best is not None else "—"
    if median is None:
        text = best_str if best is not None else "—"
    else:
        text = f"{best_str}/{int(round(median))}"

    highest_key = _highest_mplus_key_level(breakdown)
    if highest_key > 0:
        text = f"{text} +{highest_key}"

    bg = percentile_colour(best) if best is not None else None
    fg = _text_colour_for_bg(bg) if bg is not None else None
    return text, fg, bg


def _mplus_dungeon_metric_text(entry: object) -> str:
    if not isinstance(entry, dict):
        return "—"
    best = safe_percent(entry.get("parse_percent"))
    median = safe_percent(entry.get("median_percent"))
    run_count = _mplus_run_count(entry)
    if best is None:
        return "—"
    if run_count >= 2 and median is not None:
        return _metric_text(best, median)
    return _metric_text(best, None)


def _mplus_dual_cell(
    applicant: Applicant, listing: Listing | None = None
) -> QTableWidgetItem:
    """Qt adapter over the pure M+ headline render boundary."""
    text, fg, bg = _mplus_cell_visuals(applicant, listing)
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    if fg is not None:
        item.setForeground(QColor(fg))
    if bg is not None:
        item.setBackground(QColor(bg))
        f = QFont()
        f.setBold(True)
        item.setFont(f)
    return item


def _mplus_group_cell(
    package: PackageFit,
    applicant: Applicant,
    listing: Listing | None = None,
) -> QTableWidgetItem:
    package_text = package.display
    package_bg = package.colour or "#2a2a33"
    individual_text, individual_fg, individual_bg = _mplus_cell_visuals(
        applicant, listing
    )
    item = QTableWidgetItem(f"{package_text} | {individual_text}")
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    item.setData(MPLUS_PACKAGE_TEXT_ROLE, package_text)
    item.setData(MPLUS_PACKAGE_BG_ROLE, package_bg)
    item.setData(MPLUS_INDIVIDUAL_TEXT_ROLE, individual_text)
    item.setData(MPLUS_INDIVIDUAL_FG_ROLE, individual_fg or "")
    item.setData(MPLUS_INDIVIDUAL_BG_ROLE, individual_bg or "")
    f = QFont()
    f.setBold(True)
    item.setFont(f)
    return item


def _format_age(delta_sec: float) -> str:
    """Formats "X ago" relative-time strings for the health indicator.

    24h cap returns "—" so a forgotten companion left running overnight
    doesn't display "27h ago" garbage — at that age the indicator is
    meaningless. Bands are coarse on purpose: the goal is failure-mode
    spotting (decode pipeline alive vs dead), not millisecond accuracy."""
    if delta_sec >= 86400.0:  # 24h
        return "—"
    if delta_sec >= 3600.0:
        return f"{int(delta_sec // 3600)}h ago"
    if delta_sec >= 60.0:
        return f"{int(delta_sec // 60)}m ago"
    return f"{int(delta_sec)}s ago"


def _format_listing_tooltip(listing: Listing | None) -> str:
    """Composes the title-bar tooltip text from listing_name + comment.

    Both fields come from the in-game LFG UI as user-typed strings — `<3`,
    `<-->`, etc. are legitimate content. Qt's tooltip widget auto-detects
    HTML mode by `<` presence, which would mangle plain-text comments.
    html.escape forces plain-text rendering; literal `\\n` line breaks are
    preserved (not converted to entities) so the two-line layout still works.

    Dedup safety: some addons mirror comment into listing_name. When both
    fields strip-equal we emit one line, not two."""
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


def _percent_text(value: object) -> str:
    pct = safe_percent(value)
    return "—" if pct is None else str(int(round(pct)))


def _metric_text(best: object, median: object) -> str:
    best_pct = safe_percent(best)
    median_pct = safe_percent(median)
    if best_pct is None and median_pct is None:
        return "—"
    best_text = _percent_text(best_pct)
    if median_pct is None:
        return best_text
    return f"{best_text}/{_percent_text(median_pct)}"


# ───────────────────────────────────────────────────────────────────
# Stylesheet


_STYLESHEET = """
#rootContainer {
    background-color: rgba(10, 10, 14, 220);
    border: 1px solid rgba(80, 80, 100, 200);
    border-radius: 4px;
}
#titleBar {
    background-color: rgba(20, 20, 28, 240);
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}
#titleLabel {
    color: #d8d8e0;
    font-size: 12px;
    font-weight: bold;
    padding: 4px;
}
#settingsButton,
#hideButton {
    background-color: transparent;
    color: #d8d8e0;
    border: none;
    font-size: 16px;
    font-weight: bold;
}
#settingsButton:hover,
#hideButton:hover {
    background-color: rgba(52, 52, 66, 205);
    border-radius: 2px;
}
#statusLabel {
    color: #888;
    font-size: 10px;
}
#healthLabel {
    color: #888;
    font-size: 10px;
}
#overlayLauncher {
    background-color: rgba(12, 12, 18, 235);
    border: 1px solid rgba(240, 120, 90, 210);
    border-radius: 6px;
}
#overlayLauncher:hover {
    background-color: rgba(34, 34, 44, 240);
}
#overlayLauncherLabel {
    color: #ff8a65;
    font-size: 14px;
    font-weight: bold;
}
#targetKeyLabel {
    color: #f0d27a;
    font-size: 11px;
    font-weight: bold;
}
#targetKeyControl {
    background-color: rgba(42, 42, 54, 225);
    border: 1px solid rgba(240, 120, 90, 230);
    border-radius: 3px;
}
#targetKeySpin {
    color: #ffffff;
    background-color: transparent;
    border: none;
    font-weight: bold;
    padding-left: 6px;
}
#targetKeyStepUp, #targetKeyStepDown {
    color: #f5f5fb;
    background-color: rgba(62, 62, 78, 235);
    border: none;
    border-left: 1px solid rgba(160, 160, 180, 125);
    font-size: 12px;
    font-weight: bold;
    padding: 0;
}
#targetKeyStepUp {
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
}
#targetKeyStepDown {
    border-left-color: rgba(100, 100, 120, 170);
    border-top-right-radius: 2px;
    border-bottom-right-radius: 2px;
}
#targetKeyStepUp:hover, #targetKeyStepDown:hover {
    background-color: rgba(240, 120, 90, 150);
}
#targetKeyStepUp:pressed, #targetKeyStepDown:pressed {
    background-color: rgba(240, 120, 90, 210);
}
/* Hover/pin info panel — opaque QWidget scout card. */
#infoPanel {
    background-color: rgba(12, 12, 18, 248);
    border: 1px solid rgba(70, 70, 92, 200);
    border-radius: 4px;
}
#infoPanel QLabel {
    background-color: transparent;
    font-family: 'Segoe UI', 'Cantarell', 'Helvetica Neue', sans-serif;
}
#infoName {
    color: #e8e8f0;
    font-size: 16px;
    font-weight: bold;
}
#infoRealm, #infoMeta, #infoDungeonKey {
    color: #8d8d98;
    font-size: 11px;
}
#infoPanelStatus {
    color: #8d8d98;
    font-size: 12px;
    padding-top: 2px;
}
#infoDungeonName {
    color: #d2d2dc;
    font-size: 11px;
}
#infoDungeonMetric {
    font-size: 11px;
}
/* Role filter bar — toggle buttons checked-state lights up in the role
   colour for at-a-glance "what's filtered" reading. Per-role :checked
   selectors via objectName for distinct active colours. */
#roleFilterBar {
    background-color: rgba(18, 18, 26, 205);
    border-bottom: 1px solid rgba(66, 66, 86, 130);
}
#roleFilterBar QPushButton {
    color: #c9c9d4;
    background-color: rgba(34, 34, 44, 165);
    border: 1px solid rgba(78, 78, 98, 135);
    border-radius: 3px;
    padding: 2px 8px;
    font-weight: bold;
    font-size: 12px;
}
#roleFilterBar QPushButton:hover {
    background-color: rgba(52, 52, 66, 205);
}
#roleFilterBar QPushButton#roleBtn_DAMAGER:checked {
    background-color: #b04545;
    color: #ffffff;
    border-color: #d06060;
}
#roleFilterBar QPushButton#roleBtn_HEALER:checked {
    background-color: #2f9450;
    color: #ffffff;
    border-color: #50b070;
}
#roleFilterBar QPushButton#roleBtn_TANK:checked {
    background-color: #3a6fb0;
    color: #ffffff;
    border-color: #5a8fd0;
}
#roleFilterBar QPushButton#roleFilterReset {
    padding: 0 6px;
    font-size: 11px;
    color: #b8b8c8;
    background-color: rgba(48, 48, 60, 190);
    border-color: rgba(100, 100, 120, 170);
}
#roleFilterBar QPushButton#roleFilterReset:hover {
    color: #ffffff;
    background-color: rgba(78, 78, 96, 230);
}
#roleFilterStatus {
    color: #888;
    font-size: 11px;
}
QTableWidget {
    background-color: transparent;
    color: #e0e0e0;
    gridline-color: transparent;
    selection-background-color: transparent;
    font-size: 11px;
}
QTableWidget::item {
    padding: 1px 3px;
}
QHeaderView::section {
    background-color: rgba(28, 28, 38, 240);
    color: #b8b8c0;
    padding: 2px;
    border: none;
    font-size: 10px;
}
/* Explicit QToolTip styling so tooltips render reliably on this overlay. */
/* WHY: Qt.Tool + WA_TranslucentBackground + WindowStaysOnTopHint hits a Qt-on-Windows */
/* corner where the default platform tooltip widget paints with a transparent backing */
/* and becomes invisible (the text is set, but the user never sees it). Forcing an */
/* explicit background + opaque colours via QSS makes Qt route the tooltip through */
/* the styled-widget path, which paints reliably. */
QToolTip {
    background-color: #14141a;
    color: #e8e8f0;
    border: 1px solid #555;
    padding: 6px;
    font-size: 11px;
    /* opacity must be 255 — translucency on tooltip in this overlay setup hides it */
    opacity: 255;
}
"""
