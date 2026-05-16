"""Applicant + Listing data models, state machine, persisted window geometry."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from .atomic_io import atomic_write_text
from .metric_preferences import MetricPreferences


_log = logging.getLogger("applicant_scout.state")


WINDOW_GEOMETRY_LAYOUT_VERSION = 5
DEFAULT_WINDOW_WIDTH = 572
DEFAULT_WINDOW_HEIGHT = 440


@dataclass
class Applicant:
    """One row in the overlay table."""

    applicant_id: str
    name: str  # raw "Charname" or "Charname-Realm" as emitted
    cls: str  # locale-stable English token e.g. "MONK"
    spec_id: int
    ilvl: int
    score: int
    role: str  # TANK / HEALER / DAMAGER
    main_score: int = 0
    # Compact target-relative RaiderIO completion summary from addon wire v5.
    # Counts are computed by the addon against the active listing key at
    # screenshot time, so they are evidence for this listing snapshot rather
    # than generic season totals.
    rio_profile: bool = False
    rio_best_key: int = 0
    rio_best_dungeon_key: int = 0
    rio_timed_at_or_above: int = 0
    rio_timed_at_or_above_minus1: int = 0
    rio_timed_at_or_above_minus2: int = 0
    rio_completed_at_or_above_minus1: int = 0
    rio_dungeon_count: int = 0

    # Filled by WCL fetcher. None = not yet fetched OR fetched-but-no-data
    # (we never use NaN; `fetch_status` field disambiguates loading vs done).
    # Each raid difficulty stores BOTH best and median per-encounter avg parse %.
    # Overlay displays as "best/median" pair so user can spot consistency
    # (best 99 / median 50 = lucky pulls; best 90 / median 85 = stable pumper).
    raid_normal: Optional[float] = None
    raid_heroic: Optional[float] = None
    raid_mythic: Optional[float] = None
    raid_normal_median: Optional[float] = None
    raid_heroic_median: Optional[float] = None
    raid_mythic_median: Optional[float] = None
    # M+ headline best/median avg across the 8 dungeons. Only the role-relevant
    # metric is populated — DPS for tank+damager, HPS for healer (the OTHER
    # metric stays None to save WCL quota: 8 unneeded encounter queries).
    # Overlay displays as "best/median" pair from the populated metric.
    mplus_dps: Optional[float] = None
    mplus_hps: Optional[float] = None
    mplus_dps_median: Optional[float] = None
    mplus_hps_median: Optional[float] = None
    # Per-dungeon detail behind each headline — feeds the M+ cell tooltip.
    # list[dict] (not list[DungeonPerf]) to avoid wcl.py import cycle here;
    # each entry: {"name": str, "parse_percent": float|None,
    # "median_percent": float|None, "key_level": int, "run_count": int.
    # Optional "brackets" holds per-key summaries with the same metric fields.
    # run_count critical for confidence display: N=1 means single-run data
    # (lucky/unlucky risk); N>=2 enables median signal.
    mplus_dps_breakdown: list[dict] = field(default_factory=list)
    mplus_hps_breakdown: list[dict] = field(default_factory=list)

    # Scope that the currently populated WCL fields are allowed to represent.
    # None means unknown/no usable WCL data, so runtime scope changes must refetch
    # before relying on enabled metrics.
    wcl_metric_preferences: Optional[MetricPreferences] = None
    fetch_status: str = "pending"  # pending / loading / ready / error / not_found
    error_message: str = ""
    wcl_error_kind: str = ""

    def clear_wcl_data(self, *, fetch_status: str = "pending") -> None:
        self.fetch_status = fetch_status
        self.error_message = ""
        self.wcl_error_kind = ""
        self.raid_normal = None
        self.raid_heroic = None
        self.raid_mythic = None
        self.raid_normal_median = None
        self.raid_heroic_median = None
        self.raid_mythic_median = None
        self.mplus_dps = None
        self.mplus_hps = None
        self.mplus_dps_median = None
        self.mplus_hps_median = None
        self.mplus_dps_breakdown = []
        self.mplus_hps_breakdown = []
        self.wcl_metric_preferences = None

    def wcl_data_covers(self, metric_preferences: MetricPreferences) -> bool:
        return (
            self.fetch_status == "ready"
            and self.wcl_metric_preferences is not None
            and self.wcl_metric_preferences.covers(metric_preferences)
        )

    def project_wcl_data_to_preferences(
        self, metric_preferences: MetricPreferences
    ) -> None:
        """Drop disabled WCL fields so hidden metrics cannot affect scoring."""
        previous = self.wcl_metric_preferences
        if not metric_preferences.raid_normal:
            self.raid_normal = None
            self.raid_normal_median = None
        if not metric_preferences.raid_heroic:
            self.raid_heroic = None
            self.raid_heroic_median = None
        if not metric_preferences.raid_mythic:
            self.raid_mythic = None
            self.raid_mythic_median = None
        if not metric_preferences.mplus:
            self.mplus_dps = None
            self.mplus_hps = None
            self.mplus_dps_median = None
            self.mplus_hps_median = None
            self.mplus_dps_breakdown = []
            self.mplus_hps_breakdown = []
        self.wcl_metric_preferences = (
            metric_preferences
            if previous is not None and previous.covers(metric_preferences)
            else None
        )


@dataclass
class Listing:
    activity_id: int
    dungeon_name: str
    listing_name: str
    comment: str
    key_level: int = 0  # M+ keystone level (0 for non-M+ listings)
    category_id: int = 0  # WoW LFG category, 0 for legacy/unknown snapshots
    difficulty_id: int = 0  # WoW difficultyID, 0 for legacy/unknown snapshots


@dataclass
class WoWPlayer:
    """Info about the player from [APSCOUT|VERSION] line."""

    addon_version: str = ""
    game_version: str = ""
    region_id: int = 0  # 1=NA 2=KR 3=EU 4=TW 5=CN
    full_name: str = (
        ""  # Player's own Charname-Realm (used to fill realm for same-realm applicants)
    )


class AppState:
    """Mutable in-memory state — applicants set, listing, player info."""

    def __init__(self) -> None:
        self.applicants: dict[str, Applicant] = {}
        self.listing: Optional[Listing] = None
        self.player: WoWPlayer = WoWPlayer()

    def add_or_update(self, app: Applicant) -> None:
        self.applicants[app.applicant_id] = app

    def remove(self, applicant_id: str) -> None:
        self.applicants.pop(applicant_id, None)

    def clear_all(self) -> None:
        self.applicants.clear()

    def count(self) -> int:
        return len(self.applicants)


@dataclass
class WindowGeometry:
    x: int = 100
    y: int = 100
    # Compact QWidget scout-card layout default. OverlayWindow minimum uses
    # DEFAULT_WINDOW_WIDTH x 370; default height keeps table headroom on launch.
    w: int = DEFAULT_WINDOW_WIDTH
    h: int = DEFAULT_WINDOW_HEIGHT
    layout_version: int = WINDOW_GEOMETRY_LAYOUT_VERSION


def _coerce_geometry_int(
    value,
    *,
    default: int,
    min_value: int | None = None,
) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        signless = text[1:] if text[0] in ("+", "-") else text
        if not signless.isdecimal():
            return default
        try:
            parsed = int(text, 10)
        except ValueError:
            return default
    else:
        return default
    if min_value is not None and parsed < min_value:
        return default
    return parsed


def _geometry_from_dict(data: dict) -> WindowGeometry:
    defaults = WindowGeometry()
    layout_default = (
        1 if "layout_version" not in data else WINDOW_GEOMETRY_LAYOUT_VERSION
    )
    return WindowGeometry(
        x=_coerce_geometry_int(data.get("x"), default=defaults.x),
        y=_coerce_geometry_int(data.get("y"), default=defaults.y),
        w=_coerce_geometry_int(data.get("w"), default=defaults.w, min_value=1),
        h=_coerce_geometry_int(data.get("h"), default=defaults.h, min_value=1),
        layout_version=_coerce_geometry_int(
            data.get("layout_version"),
            default=layout_default,
            min_value=1,
        ),
    )


def load_geometry(config_dir: Path) -> WindowGeometry:
    path = config_dir / "window.json"
    if not path.exists():
        return WindowGeometry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return WindowGeometry()
        return _geometry_from_dict(data)
    except (json.JSONDecodeError, OSError):
        return WindowGeometry()


def save_geometry(config_dir: Path, geo: WindowGeometry) -> None:
    """Persist window geometry. Failure is non-fatal — caller is the debounced
    QTimer slot in OverlayWindow, an uncaught raise there propagates into Qt's
    event loop and ends the process. Disk-full / read-only fs / antivirus
    quarantine of our config_dir all become silent (logged-only) so the user's
    in-flight scout session continues."""
    path = config_dir / "window.json"
    try:
        atomic_write_text(path, json.dumps(asdict(geo)))
    except OSError as e:
        _log.warning("Failed to save window geometry to %s: %s", path, e)
