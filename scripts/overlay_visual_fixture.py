"""Shared overlay visual fixture used by tests and screenshot rendering."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from applicant_scout.state import (
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    AppState,
    Applicant,
    Listing,
)

if TYPE_CHECKING:
    from applicant_scout.overlay import OverlayWindow


VISUAL_FIXTURE_PINNED_ID = "10:2"


def _app(**overrides) -> Applicant:
    base = Applicant(
        applicant_id="20:1",
        name="Verylongapplicantname-Twisting Nether",
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
        activity_id=401,
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
            name="Stonewall-Area 52",
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
            name="Bloomwell-Area 52",
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
            name="Cinderbolt-Area 52",
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
            name="Queueingtank-Illidan",
            cls="DEATHKNIGHT",
            spec_id=250,
            ilvl=651,
            score=2520,
            role="TANK",
            fetch_status="loading",
        ),
        _app(
            applicant_id="40:1",
            name="Apiwobble-Tichondrius",
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
            name="Freshalt-Stormrage",
            cls="ROGUE",
            spec_id=261,
            ilvl=642,
            score=2310,
            role="DAMAGER",
            fetch_status="not_found",
        ),
        _app(
            applicant_id="60:1",
            name="Nodatahealer-Dalaran",
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


def prepare_overlay_visual_window(window: OverlayWindow) -> None:
    window.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
    window._refresh_table()
    window._update_title()
    # Fixture stability: real overlay hover intentionally wins over pin, but
    # screenshot/tests should not depend on the OS cursor position.
    window._table.setMouseTracking(False)
    viewport = window._table.viewport()
    if viewport is not None:
        viewport.setMouseTracking(False)
    for role in ("TANK", "HEALER", "DAMAGER"):
        window._role_filter_bar._buttons[role].setChecked(True)
    window._pinned_id = VISUAL_FIXTURE_PINNED_ID
    window._sync_delegate_and_panel()
