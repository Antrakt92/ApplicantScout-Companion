"""Shared overlay visual fixture used by tests and screenshot rendering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt

from applicant_scout.state import DEFAULT_WINDOW_HEIGHT, AppState, Applicant, Listing

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
    r".\.venv\Scripts\python scripts\render_overlay_fixture.py"
)
VISUAL_DIFF_CHANNEL_TOLERANCE = 12
VISUAL_DIFF_MAX_PIXEL_RATIO = 0.005
VISUAL_DIFF_SCALE_RATIO_TOLERANCE = 0.01


@dataclass(frozen=True)
class VisualFixtureDiff:
    passed: bool
    message: str
    changed_pixels: int
    total_pixels: int
    max_channel_delta: int


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
    window.resize(window.minimumWidth(), DEFAULT_WINDOW_HEIGHT)
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


def create_overlay_visual_window(
    work_dir: Path,
) -> tuple[AppState, "OverlayWindow", "WCLClient"]:
    from applicant_scout.overlay import OverlayWindow
    from applicant_scout.wcl import CharacterCache, WCLAuth, WCLClient

    state = build_overlay_visual_state()
    auth = WCLAuth("visual-fixture-client", "visual-fixture-secret", work_dir)
    client = WCLClient(auth)
    cache = CharacterCache(work_dir)
    window = OverlayWindow(state, client, cache, work_dir)
    return state, window, client


def show_overlay_visual_window(
    window: "OverlayWindow",
    *,
    process_events: Callable[[], None],
) -> None:
    prepare_overlay_visual_window(window)
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


def _channel_delta(expected: "QImage", actual: "QImage", x: int, y: int) -> int:
    expected_color = expected.pixelColor(x, y)
    actual_color = actual.pixelColor(x, y)
    return max(
        abs(expected_color.red() - actual_color.red()),
        abs(expected_color.green() - actual_color.green()),
        abs(expected_color.blue() - actual_color.blue()),
        abs(expected_color.alpha() - actual_color.alpha()),
    )


def compare_overlay_visual_images(
    expected: "QImage",
    actual: "QImage",
) -> VisualFixtureDiff:
    if expected.isNull() or actual.isNull():
        return VisualFixtureDiff(
            passed=False,
            message=(
                "overlay visual fixture comparison received a null image; "
                f"regenerate with {VISUAL_FIXTURE_REGEN_COMMAND}"
            ),
            changed_pixels=0,
            total_pixels=0,
            max_channel_delta=0,
        )
    if expected.size() != actual.size():
        expected_width = expected.width()
        expected_height = expected.height()
        actual_width = actual.width()
        actual_height = actual.height()
        scale_x = expected_width / actual_width if actual_width else 0.0
        scale_y = expected_height / actual_height if actual_height else 0.0
        if (
            expected_width > 0
            and expected_height > 0
            and actual_width > 0
            and actual_height > 0
            and abs(scale_x - scale_y) <= VISUAL_DIFF_SCALE_RATIO_TOLERANCE
        ):
            actual = actual.scaled(
                expected.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            return VisualFixtureDiff(
                passed=False,
                message=(
                    "overlay visual fixture dimension mismatch: "
                    f"expected {expected_width}x{expected_height}, "
                    f"got {actual_width}x{actual_height}; regenerate with "
                    f"{VISUAL_FIXTURE_REGEN_COMMAND} after intentional UI changes"
                ),
                changed_pixels=0,
                total_pixels=max(actual_width * actual_height, 0),
                max_channel_delta=0,
            )

    changed_pixels = 0
    max_channel_delta = 0
    total_pixels = expected.width() * expected.height()
    for x in range(expected.width()):
        for y in range(expected.height()):
            delta = _channel_delta(expected, actual, x, y)
            max_channel_delta = max(max_channel_delta, delta)
            if delta > VISUAL_DIFF_CHANNEL_TOLERANCE:
                changed_pixels += 1

    changed_ratio = changed_pixels / total_pixels if total_pixels else 0.0
    passed = changed_ratio <= VISUAL_DIFF_MAX_PIXEL_RATIO
    if passed:
        return VisualFixtureDiff(
            passed=True,
            message=(
                "overlay visual fixture matched committed baseline "
                f"({changed_pixels}/{total_pixels} changed pixels over tolerance)"
            ),
            changed_pixels=changed_pixels,
            total_pixels=total_pixels,
            max_channel_delta=max_channel_delta,
        )
    return VisualFixtureDiff(
        passed=False,
        message=(
            "overlay visual fixture drift exceeded tolerance: "
            f"{changed_pixels}/{total_pixels} changed pixels "
            f"({changed_ratio:.2%}) over channel delta "
            f"{VISUAL_DIFF_CHANNEL_TOLERANCE}, max channel delta "
            f"{max_channel_delta}; regenerate with "
            f"{VISUAL_FIXTURE_REGEN_COMMAND} after intentional UI changes"
        ),
        changed_pixels=changed_pixels,
        total_pixels=total_pixels,
        max_channel_delta=max_channel_delta,
    )
