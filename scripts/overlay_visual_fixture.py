"""Shared overlay visual fixture used by tests and screenshot rendering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from applicant_scout.metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences
from applicant_scout.state import (
    DEFAULT_WINDOW_HEIGHT,
    AppState,
    Applicant,
    Listing,
    RosterMember,
)
from scripts.visual_fixture_checks import VisualFixtureDiff, compare_visual_images

if TYPE_CHECKING:
    from PyQt6.QtGui import QImage, QPixmap
    from applicant_scout.overlay import OverlayWindow
    from applicant_scout.wcl import WCLClient


OVERLAY_VISUAL_BASELINE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "visual"
    / "overlay-polish-fixture.png"
)
VISUAL_FIXTURE_PINNED_ID = "10:2"
VISUAL_FIXTURE_REGEN_COMMAND = (
    r".\.venv\Scripts\python scripts\render_overlay_fixture.py --all"
)
VISUAL_DIFF_CHANNEL_TOLERANCE = 12
VISUAL_DIFF_MAX_PIXEL_RATIO = 0.005
VISUAL_DIFF_SCALE_RATIO_TOLERANCE = 0.01
DEFAULT_VISUAL_FIXTURE_SCENARIO = "applicants-default"


@dataclass(frozen=True)
class VisualFixtureScenario:
    name: str
    baseline_path: Path
    build_state: Callable[[], AppState]
    metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES
    prepare_window: Callable[["OverlayWindow"], None] | None = None


def _app(**overrides) -> Applicant:
    base = Applicant(
        applicant_id="20:1",
        name="ScoutDps-Example",
        cls="WARRIOR",
        spec_id=71,
        ilvl=664,
        score=2843,
        role="DAMAGER",
        raid_normal=88.0,
        raid_normal_median=72.0,
        raid_heroic=91.0,
        raid_heroic_median=78.0,
        raid_mythic=34.0,
        raid_mythic_median=34.0,
        mplus_dps=80.0,
        mplus_dps_median=62.0,
        mplus_hps=None,
        mplus_hps_median=None,
        mplus_dps_breakdown=[
            {
                "name": "Pit of Saron",
                "parse_percent": 100.0,
                "median_percent": 80.0,
                "key_level": 14,
                "run_count": 3,
            },
            {
                "name": "Skyreach",
                "parse_percent": 70.0,
                "median_percent": None,
                "key_level": 16,
                "run_count": 1,
            },
        ],
        mplus_hps_breakdown=[],
        fetch_status="ready",
    )
    return replace(base, **overrides)


def _healer_breakdown() -> list[dict]:
    return [
        {
            "name": "Skyreach",
            "parse_percent": 92.0,
            "median_percent": 84.0,
            "key_level": 16,
            "run_count": 5,
        },
        {
            "name": "Pit of Saron",
            "parse_percent": 63.0,
            "median_percent": 58.0,
            "key_level": 13,
            "run_count": 2,
        },
    ]


def build_overlay_visual_state() -> AppState:
    state = AppState()
    state.listing = Listing(
        activity_id=182,
        dungeon_name="Skyreach",
        listing_name="+16 Skyreach - visual QA",
        comment="Representative overlay fixture for UI polish review.",
        key_level=16,
        category_id=2,
        difficulty_id=8,
    )

    applicants = [
        _app(
            applicant_id="10:1",
            name="ScoutTank-Example",
            cls="PALADIN",
            spec_id=66,
            ilvl=662,
            score=2794,
            main_score=3468,
            role="TANK",
            raid_mythic=62.0,
            raid_mythic_median=55.0,
            mplus_dps=78.0,
            mplus_dps_median=66.0,
        ),
        _app(
            applicant_id="10:2",
            name="ScoutHealer-Example",
            cls="DRUID",
            spec_id=105,
            ilvl=660,
            score=2740,
            role="HEALER",
            raid_normal=94.0,
            raid_normal_median=89.0,
            raid_heroic=86.0,
            raid_heroic_median=77.0,
            raid_mythic=42.0,
            raid_mythic_median=38.0,
            mplus_dps=None,
            mplus_dps_median=None,
            mplus_dps_breakdown=[],
            mplus_hps=92.0,
            mplus_hps_median=84.0,
            mplus_hps_breakdown=_healer_breakdown(),
        ),
        _app(
            applicant_id="10:3",
            name="ScoutMage-Example",
            cls="MAGE",
            spec_id=63,
            ilvl=658,
            score=2685,
            role="DAMAGER",
            raid_mythic=73.0,
            raid_mythic_median=65.0,
            mplus_dps=88.0,
            mplus_dps_median=75.0,
        ),
        _app(),
        _app(
            applicant_id="30:1",
            name="ScoutQueued-Example",
            cls="DEATHKNIGHT",
            spec_id=250,
            ilvl=651,
            score=2520,
            role="TANK",
            fetch_status="loading",
        ),
        _app(
            applicant_id="40:1",
            name="ScoutRetry-Example",
            cls="PRIEST",
            spec_id=257,
            ilvl=648,
            score=2460,
            role="HEALER",
            fetch_status="error",
            error_message="rate limited",
        ),
        _app(
            applicant_id="50:1",
            name="ScoutNoLogs-Example",
            cls="ROGUE",
            spec_id=261,
            ilvl=642,
            score=2310,
            role="DAMAGER",
            fetch_status="not_found",
        ),
        _app(
            applicant_id="60:1",
            name="ScoutEmpty-Example",
            cls="SHAMAN",
            spec_id=264,
            ilvl=646,
            score=2388,
            role="HEALER",
            raid_normal=None,
            raid_normal_median=None,
            raid_heroic=None,
            raid_heroic_median=None,
            raid_mythic=None,
            raid_mythic_median=None,
            mplus_dps=None,
            mplus_dps_median=None,
            mplus_dps_breakdown=[],
            mplus_hps=None,
            mplus_hps_median=None,
            mplus_hps_breakdown=[],
        ),
    ]
    for applicant in applicants:
        state.add_or_update(applicant)
    return state


def _raid_boss_parses() -> dict[str, list[dict]]:
    return {
        "H": [
            {
                "encounter_id": 3176,
                "name": "Imperator Averzian",
                "overall": 83.0,
                "ilvl": 63.0,
            },
            {
                "encounter_id": 3177,
                "name": "Vorasius",
                "overall": 72.0,
                "ilvl": 58.0,
            },
        ],
        "M": [
            {
                "encounter_id": 3176,
                "name": "Imperator Averzian",
                "overall": 46.0,
                "ilvl": 68.0,
            },
            {
                "encounter_id": 3177,
                "name": "Vorasius",
                "overall": 39.0,
                "ilvl": 52.0,
            },
        ],
    }


def _raid_progress() -> dict[str, dict]:
    return {
        "H": {
            "killed": 4,
            "total": 9,
            "boss_kills": [2, 1, 1, 0, 0, 0, 0, 0, 0],
        },
        "M": {
            "killed": 1,
            "total": 9,
            "boss_kills": [2, 0, 0, 0, 0, 0, 0, 0, 0],
        },
    }


def build_raid_listing_visual_state() -> AppState:
    state = AppState()
    state.listing = Listing(
        activity_id=0,
        dungeon_name="Manaforge Omega",
        listing_name="Heroic Manaforge Omega - visual QA",
        comment="Raid-context overlay fixture for table and detail panel review.",
        key_level=0,
        category_id=3,
        difficulty_id=15,
    )
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=True,
    )
    applicants = [
        _app(
            applicant_id="10:1",
            name="ScoutTank-Example",
            cls="PALADIN",
            spec_id=66,
            ilvl=662,
            score=2794,
            main_score=3468,
            role="TANK",
            raid_heroic=74.0,
            raid_heroic_median=67.0,
            raid_mythic=41.0,
            raid_mythic_median=36.0,
            mplus_dps=78.0,
            mplus_dps_median=66.0,
            raid_boss_parses=_raid_boss_parses(),
            rio_raid_progress=_raid_progress(),
            wcl_metric_preferences=prefs,
        ),
        _app(
            applicant_id=VISUAL_FIXTURE_PINNED_ID,
            name="ScoutHealer-Example",
            cls="DRUID",
            spec_id=105,
            ilvl=660,
            score=2740,
            role="HEALER",
            raid_normal=None,
            raid_normal_median=None,
            raid_heroic=86.0,
            raid_heroic_median=77.0,
            raid_mythic=42.0,
            raid_mythic_median=38.0,
            mplus_dps=None,
            mplus_dps_median=None,
            mplus_dps_breakdown=[],
            mplus_hps=92.0,
            mplus_hps_median=84.0,
            mplus_hps_breakdown=_healer_breakdown(),
            raid_boss_parses=_raid_boss_parses(),
            rio_raid_progress=_raid_progress(),
            wcl_metric_preferences=prefs,
        ),
        _app(
            applicant_id="10:3",
            name="ScoutMage-Example",
            cls="MAGE",
            spec_id=63,
            ilvl=658,
            score=2685,
            role="DAMAGER",
            raid_heroic=61.0,
            raid_heroic_median=57.0,
            raid_mythic=73.0,
            raid_mythic_median=65.0,
            mplus_dps=88.0,
            mplus_dps_median=75.0,
            raid_boss_parses=_raid_boss_parses(),
            rio_raid_progress=_raid_progress(),
            wcl_metric_preferences=prefs,
        ),
    ]
    for applicant in applicants:
        state.add_or_update(applicant)
    return state


def _party_member(unit_index: int, **overrides) -> RosterMember:
    base = _app(**overrides)
    values = base.__dict__.copy()
    values.update(unit_index=unit_index, subgroup=1)
    return RosterMember(**values)


def build_party_visual_state(*, include_listing: bool) -> AppState:
    state = AppState()
    if include_listing:
        state.listing = Listing(
            activity_id=182,
            dungeon_name="Skyreach",
            listing_name="+15 Skyreach - party visual QA",
            comment="",
            key_level=15,
            category_id=2,
            difficulty_id=8,
        )
    for member in (
        _party_member(
            1,
            applicant_id="party:tank",
            name="PartyTank-Example",
            cls="PALADIN",
            spec_id=66,
            role="TANK",
            score=2760,
            main_score=3340,
            rio_profile=True,
            rio_best_key=16,
            rio_best_dungeon_key=15,
            rio_summary_target_key=16,
        ),
        _party_member(
            2,
            applicant_id="party:healer",
            name="ScoutHealer-Example",
            cls="DRUID",
            spec_id=105,
            role="HEALER",
            score=2740,
            mplus_dps=None,
            mplus_dps_median=None,
            mplus_dps_breakdown=[],
            mplus_hps=92.0,
            mplus_hps_median=84.0,
            mplus_hps_breakdown=_healer_breakdown(),
        ),
        _party_member(
            3,
            applicant_id="party:dps",
            name="ScoutMage-Example",
            cls="MAGE",
            spec_id=63,
            role="DAMAGER",
            score=2685,
            mplus_dps=88.0,
            mplus_dps_median=75.0,
        ),
    ):
        state.add_or_update_party_member(member)
    return state


def _prepare_common_visual_window(window: OverlayWindow) -> None:
    from applicant_scout.overlay import _minimum_window_width_for_metrics

    window.resize(window.minimumWidth(), DEFAULT_WINDOW_HEIGHT)
    window._refresh_table()
    content_safe_width = _minimum_window_width_for_metrics(
        window._metric_preferences,
        name_width=getattr(window, "_max_name_width_px", 0),
    )
    if window.width() < content_safe_width:
        window.resize(content_safe_width, DEFAULT_WINDOW_HEIGHT)
        window._refresh_table()
    window._update_title()
    # Fixture stability: real overlay hover intentionally wins over pin, but
    # screenshot/tests should not depend on the OS cursor position.
    window._table.setMouseTracking(False)
    viewport = window._table.viewport()
    if viewport is not None:
        viewport.setMouseTracking(False)


def _pin_visual_row(window: OverlayWindow, applicant_id: str) -> None:
    window._pinned_id = VISUAL_FIXTURE_PINNED_ID
    if applicant_id != VISUAL_FIXTURE_PINNED_ID:
        window._pinned_id = applicant_id
    window._sync_delegate_and_panel()


def prepare_overlay_visual_window(
    window: OverlayWindow,
    scenario: str | VisualFixtureScenario = DEFAULT_VISUAL_FIXTURE_SCENARIO,
) -> None:
    resolved = resolve_visual_fixture_scenario(scenario)
    _prepare_common_visual_window(window)
    if resolved.prepare_window is not None:
        resolved.prepare_window(window)
    else:
        _pin_visual_row(window, VISUAL_FIXTURE_PINNED_ID)


def _prepare_party_manual_key_window(window: OverlayWindow) -> None:
    window._tab_bar.set_active("party")
    window._tab_bar._key_spin.setValue(16)
    _pin_visual_row(window, "party:healer")


def _prepare_party_no_listing_manual_key_window(window: OverlayWindow) -> None:
    window._tab_bar.set_active("party")
    window._tab_bar._key_spin.setValue(14)
    _pin_visual_row(window, "party:healer")


def _prepare_metrics_raid_only_window(window: OverlayWindow) -> None:
    _pin_visual_row(window, VISUAL_FIXTURE_PINNED_ID)


def _prepare_wcl_retry_window(window: OverlayWindow) -> None:
    from applicant_scout.wcl import WCL_ERROR_GRAPHQL

    applicant = window._state.applicants.get("40:1")
    if applicant is not None:
        applicant.error_message = "GraphQL error: proxy unavailable"
        applicant.wcl_error_kind = WCL_ERROR_GRAPHQL
    _pin_visual_row(window, "40:1")


def _baseline_path(name: str) -> Path:
    if name == DEFAULT_VISUAL_FIXTURE_SCENARIO:
        return OVERLAY_VISUAL_BASELINE_PATH
    return OVERLAY_VISUAL_BASELINE_PATH.with_name(f"overlay-polish-fixture-{name}.png")


OVERLAY_VISUAL_SCENARIOS: dict[str, VisualFixtureScenario] = {
    DEFAULT_VISUAL_FIXTURE_SCENARIO: VisualFixtureScenario(
        name=DEFAULT_VISUAL_FIXTURE_SCENARIO,
        baseline_path=_baseline_path(DEFAULT_VISUAL_FIXTURE_SCENARIO),
        build_state=build_overlay_visual_state,
    ),
    "party-manual-key": VisualFixtureScenario(
        name="party-manual-key",
        baseline_path=_baseline_path("party-manual-key"),
        build_state=lambda: build_party_visual_state(include_listing=True),
        prepare_window=_prepare_party_manual_key_window,
    ),
    "party-no-listing-manual-key": VisualFixtureScenario(
        name="party-no-listing-manual-key",
        baseline_path=_baseline_path("party-no-listing-manual-key"),
        build_state=lambda: build_party_visual_state(include_listing=False),
        prepare_window=_prepare_party_no_listing_manual_key_window,
    ),
    "metrics-raid-only": VisualFixtureScenario(
        name="metrics-raid-only",
        baseline_path=_baseline_path("metrics-raid-only"),
        build_state=build_overlay_visual_state,
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=True,
            raid_heroic=False,
            raid_mythic=True,
        ),
        prepare_window=_prepare_metrics_raid_only_window,
    ),
    "raid-listing": VisualFixtureScenario(
        name="raid-listing",
        baseline_path=_baseline_path("raid-listing"),
        build_state=build_raid_listing_visual_state,
        metric_preferences=MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=True,
        ),
    ),
    "wcl-retry": VisualFixtureScenario(
        name="wcl-retry",
        baseline_path=_baseline_path("wcl-retry"),
        build_state=build_overlay_visual_state,
        prepare_window=_prepare_wcl_retry_window,
    ),
}


def resolve_visual_fixture_scenario(
    scenario: str | VisualFixtureScenario = DEFAULT_VISUAL_FIXTURE_SCENARIO,
) -> VisualFixtureScenario:
    if isinstance(scenario, VisualFixtureScenario):
        return scenario
    try:
        return OVERLAY_VISUAL_SCENARIOS[scenario]
    except KeyError as exc:
        names = ", ".join(sorted(OVERLAY_VISUAL_SCENARIOS))
        raise ValueError(f"unknown overlay visual fixture scenario {scenario!r}; choices: {names}") from exc


def create_overlay_visual_window(
    work_dir: Path,
    scenario: str | VisualFixtureScenario = DEFAULT_VISUAL_FIXTURE_SCENARIO,
) -> tuple[AppState, "OverlayWindow", "WCLClient"]:
    from applicant_scout.overlay import OverlayWindow
    from applicant_scout.wcl import CharacterCache, WCLAuth, WCLClient

    resolved = resolve_visual_fixture_scenario(scenario)
    state = resolved.build_state()
    auth = WCLAuth("visual-fixture-client", "visual-fixture-secret", work_dir)
    client = WCLClient(auth, metric_preferences=resolved.metric_preferences)
    client.reconfigure_auth(auth, validated=True)
    cache = CharacterCache(work_dir)
    window = OverlayWindow(
        state,
        client,
        cache,
        work_dir,
        metric_preferences=resolved.metric_preferences,
    )
    # WHY: visual fixtures are static snapshots; they must not enqueue real WCL
    # workers during pytest or standalone screenshot rendering.
    window._launch_fetch = lambda applicant: None
    window._launch_raid_boss_fetch_if_needed = lambda applicant: False
    return state, window, client


def show_overlay_visual_window(
    window: "OverlayWindow",
    scenario: str | VisualFixtureScenario = DEFAULT_VISUAL_FIXTURE_SCENARIO,
    *,
    process_events: Callable[[], None],
) -> None:
    prepare_overlay_visual_window(window, scenario)
    window.show()
    for _ in range(8):
        process_events()
        viewport = window._table.viewport()
        if (
            viewport is not None
            and viewport.width() > 0
            and window._panel.height() == window._panel.target_height()
        ):
            return
    raise RuntimeError("Overlay visual fixture did not settle before screenshot")


def grab_overlay_visual_image(window: "OverlayWindow") -> "QPixmap":
    return window.grab()


def compare_overlay_visual_images(
    expected: "QImage",
    actual: "QImage",
) -> VisualFixtureDiff:
    return compare_visual_images(
        expected,
        actual,
        label="overlay visual fixture",
        regen_command=VISUAL_FIXTURE_REGEN_COMMAND,
        channel_tolerance=VISUAL_DIFF_CHANNEL_TOLERANCE,
        max_pixel_ratio=VISUAL_DIFF_MAX_PIXEL_RATIO,
        scale_ratio_tolerance=VISUAL_DIFF_SCALE_RATIO_TOLERANCE,
    )
