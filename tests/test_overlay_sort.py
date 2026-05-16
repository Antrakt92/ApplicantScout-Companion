"""Unit tests for sort_applicants_grouped (overlay.py) — multi-member group
adjacency that preserves prior solo sort semantics.

Pure data tests; no QApplication required (sort fn touches no Qt symbols)."""

from __future__ import annotations

from applicant_scout.constants import group_id_colour
from applicant_scout.overlay import (
    _SUNK_STATES,
    _build_group_markers,
    _split_composite,
    sort_applicants_grouped,
)
from applicant_scout.state import Applicant, Listing


def _app(
    *,
    aid: int,
    m: int = 1,
    score: int,
    main_score: int = 0,
    name: str = "X-R",
    spec_id: int = 71,
    cls: str = "MAGE",
    ilvl: int = 480,
    role: str = "DAMAGER",
    fetch_status: str = "ready",
    dps_breakdown: list[dict] | None = None,
    rio_profile: bool = False,
    rio_best_key: int = 0,
    rio_best_dungeon_key: int = 0,
    rio_timed_at_or_above: int = 0,
    rio_timed_at_or_above_minus1: int = 0,
    rio_timed_at_or_above_minus2: int = 0,
    rio_completed_at_or_above_minus1: int = 0,
    rio_dungeon_count: int = 0,
) -> Applicant:
    return Applicant(
        applicant_id=f"{aid}:{m}",
        name=name,
        cls=cls,
        spec_id=spec_id,
        ilvl=ilvl,
        score=score,
        main_score=main_score,
        role=role,
        fetch_status=fetch_status,
        mplus_dps_breakdown=dps_breakdown or [],
        mplus_dps=80.0 if dps_breakdown else None,
        mplus_dps_median=60.0 if dps_breakdown else None,
        rio_profile=rio_profile,
        rio_best_key=rio_best_key,
        rio_best_dungeon_key=rio_best_dungeon_key,
        rio_timed_at_or_above=rio_timed_at_or_above,
        rio_timed_at_or_above_minus1=rio_timed_at_or_above_minus1,
        rio_timed_at_or_above_minus2=rio_timed_at_or_above_minus2,
        rio_completed_at_or_above_minus1=rio_completed_at_or_above_minus1,
        rio_dungeon_count=rio_dungeon_count,
    )


def test_build_group_markers_adds_caps_for_visible_groups():
    markers = _build_group_markers(
        [
            (0, "10:1"),
            (1, "10:2"),
            (2, "20:1"),
            (4, "30:1"),
            (5, "30:2"),
            (6, "30:3"),
        ]
    )

    assert set(markers) == {0, 1, 4, 5, 6}
    assert markers[0].colour == group_id_colour("10")
    assert markers[0].first_visible
    assert not markers[0].last_visible
    assert markers[0].position == 1
    assert markers[0].size == 2
    assert markers[1].last_visible
    assert markers[1].position == 2
    assert markers[1].size == 2
    assert markers[4].first_visible
    assert not markers[5].first_visible
    assert not markers[5].last_visible
    assert markers[5].position == 2
    assert markers[5].size == 3
    assert markers[6].last_visible


# ─── _split_composite ───────────────────────────────────────────────────────


def test_split_composite_normal():
    assert _split_composite("42:1") == ("42", 1)
    assert _split_composite("99:5") == ("99", 5)


def test_split_composite_missing_colon_defaults_to_one():
    """Defensive: pre-v2 ids without ':m' suffix should fall back to member 1.
    Current code never produces such ids; the fallback keeps the helper total."""
    assert _split_composite("42") == ("42", 1)


def test_split_composite_malformed_member_idx():
    assert _split_composite("42:bogus") == ("42", 1)


def test_split_composite_empty_member_idx():
    """Trailing colon with no member_idx number."""
    assert _split_composite("42:") == ("42", 1)


def test_split_composite_empty_input():
    """Empty composite id (defensive — should never happen but no raise)."""
    assert _split_composite("") == ("", 1)


# ─── _SUNK_STATES sentinel ──────────────────────────────────────────────────


def test_sunk_states_contents():
    """Sentinel: pin the set contents. Anti-drift guard — adding a new fetch
    status to state.py without considering its sort behaviour should be a
    deliberate decision that updates this test in lockstep."""
    assert _SUNK_STATES == frozenset({"error", "not_found"})


# ─── empty input ────────────────────────────────────────────────────────────


def test_sort_empty_returns_empty_list():
    assert sort_applicants_grouped([]) == []


# ─── solo behaviour preserved (regression guards) ───────────────────────────


def test_solo_applicants_sort_by_rio_desc_unchanged():
    apps = [_app(aid=1, score=1500), _app(aid=2, score=2200), _app(aid=3, score=800)]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == ["2:1", "1:1", "3:1"]


def test_zero_rio_solo_sinks_to_bottom():
    apps = [_app(aid=1, score=0), _app(aid=2, score=2200)]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == ["2:1", "1:1"]


def test_solo_sort_uses_effective_rio_score():
    apps = [
        _app(aid=1, score=1500, main_score=3400),
        _app(aid=2, score=3000),
        _app(aid=3, score=3600, main_score=1200),
    ]

    sorted_apps = sort_applicants_grouped(apps)

    assert [a.applicant_id for a in sorted_apps] == ["3:1", "1:1", "2:1"]


def test_two_solos_same_rio_ready_before_sunk():
    """[B-1] Regression guard: equal-RIO solos must sort ready BEFORE sunk
    status, regardless of raw_aid. Without the all_group_sunk axis, raw_aid
    string compare wins on tie and arbitrary aid ordering swaps ready/sunk."""
    apps = [
        _app(aid=99, score=2000, fetch_status="ready"),
        _app(aid=42, score=2000, fetch_status="error"),
    ]
    sorted_apps = sort_applicants_grouped(apps)
    # Without the fix, raw_aid "42" < "99" puts the error-row first. With it,
    # all_group_sunk True > False sinks the error-row regardless of aid.
    assert [a.applicant_id for a in sorted_apps] == ["99:1", "42:1"]


def test_two_solos_same_rio_both_ready_aid_tiebreak():
    """When sunk status is equal (both ready), raw_aid string sort glues
    determinism across renders."""
    apps = [
        _app(aid=99, score=2000, fetch_status="ready"),
        _app(aid=42, score=2000, fetch_status="ready"),
    ]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == ["42:1", "99:1"]


def test_two_solos_same_rio_both_sunk_aid_tiebreak():
    apps = [
        _app(aid=99, score=2000, fetch_status="error"),
        _app(aid=42, score=2000, fetch_status="not_found"),
    ]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == ["42:1", "99:1"]


def test_loading_status_treated_as_ready_for_sort():
    """`loading` is NOT in _SUNK_STATES — in-flight fetches don't get pushed
    down vs `ready` rows of the same RIO. Pin this contract."""
    apps = [
        _app(aid=42, score=2000, fetch_status="loading"),
        _app(aid=99, score=2000, fetch_status="error"),
    ]
    sorted_apps = sort_applicants_grouped(apps)
    # loading > error on all_group_sunk axis → loading first
    assert [a.applicant_id for a in sorted_apps] == ["42:1", "99:1"]


# ─── group adjacency ────────────────────────────────────────────────────────


def test_two_member_group_sorts_adjacent_at_max_rio_position():
    """Group of 2 (max=2200) outranks solo (1500) but loses to solo (2400).
    Within group: leader (m=1, score=800) FIRST despite lower RIO."""
    apps = [
        _app(aid=10, score=2400),  # solo, top
        _app(aid=20, m=1, score=800),  # group leader (low RIO)
        _app(aid=20, m=2, score=2200),  # group follower (high RIO)
        _app(aid=30, score=1500),  # solo, below group
    ]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == [
        "10:1",
        "20:1",
        "20:2",
        "30:1",
    ]


def test_group_max_uses_effective_rio_without_splitting_group():
    apps = [
        _app(aid=10, score=3300),  # solo, top
        _app(aid=20, m=1, score=800, main_score=3200),
        _app(aid=20, m=2, score=2200),
        _app(aid=30, score=3000),  # below group because group effective max=3200
    ]

    sorted_apps = sort_applicants_grouped(apps)

    assert [a.applicant_id for a in sorted_apps] == [
        "10:1",
        "20:1",
        "20:2",
        "30:1",
    ]


def test_two_groups_sort_adjacent_within_their_buckets():
    apps = [
        _app(aid=11, m=1, score=2500),
        _app(aid=11, m=2, score=1200),
        _app(aid=22, m=1, score=1800),
        _app(aid=22, m=2, score=900),
    ]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == [
        "11:1",
        "11:2",
        "22:1",
        "22:2",
    ]


def test_groups_with_identical_max_tiebreak_on_aid():
    """Deterministic across renders: identical gmax+all_sunk groups tiebreak
    by raw_aid string sort."""
    apps = [
        _app(aid=99, m=1, score=2000),
        _app(aid=99, m=2, score=1500),
        _app(aid=42, m=1, score=2000),
        _app(aid=42, m=2, score=1800),
    ]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == [
        "42:1",
        "42:2",
        "99:1",
        "99:2",
    ]


def test_five_member_group_orders_by_member_idx():
    """Max LFG group size: all 5 sort adjacent in member_idx order regardless
    of the score variation across members."""
    apps = [_app(aid=7, m=m, score=1000 + m * 100) for m in (3, 1, 5, 2, 4)]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == [
        "7:1",
        "7:2",
        "7:3",
        "7:4",
        "7:5",
    ]


def test_non_contiguous_member_idx_sorts_correctly():
    """Edge case — addon could emit members 1 and 3 with member 2 nil (one-frame
    transient). Member ordering must still be by member_idx ascending."""
    apps = [_app(aid=7, m=3, score=2000), _app(aid=7, m=1, score=1500)]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == ["7:1", "7:3"]


def test_mixed_solo_and_group_interleaved_by_group_max():
    apps = [
        _app(aid=1, score=2800),  # solo top
        _app(aid=2, m=1, score=1200),
        _app(aid=2, m=2, score=2400),  # group max=2400
        _app(aid=3, score=2000),  # solo mid
        _app(aid=4, m=1, score=1900),
        _app(aid=4, m=2, score=600),
        _app(aid=4, m=3, score=1500),  # group max=1900
    ]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == [
        "1:1",
        "2:1",
        "2:2",
        "3:1",
        "4:1",
        "4:2",
        "4:3",
    ]


# ─── group_all_sunk axis ────────────────────────────────────────────────────


def test_mixed_status_group_stays_adjacent():
    """[B-2] Group with one ready + one error member at same gmax must NOT
    be split apart by an external solo applicant of the same RIO. The
    group_all_sunk flag is False (group has a ready member) — this prevents
    sunk-axis from sinking the errored member below external rows."""
    apps = [
        _app(aid=10, m=1, score=2000, fetch_status="ready"),
        _app(aid=10, m=2, score=2000, fetch_status="error"),
        _app(aid=20, score=2000, fetch_status="ready"),
    ]
    sorted_apps = sort_applicants_grouped(apps)
    # Group A members adjacent (10:1 ready, 10:2 error), then solo B.
    # raw_aid "10" < "20" wins the deterministic tiebreak.
    assert [a.applicant_id for a in sorted_apps] == ["10:1", "10:2", "20:1"]


def test_all_sunk_group_sinks_below_ready_solo_at_same_gmax():
    """Group with all members in error/not_found state should sink BELOW a
    same-RIO ready solo (group_all_sunk=True > False)."""
    apps = [
        _app(aid=10, m=1, score=2000, fetch_status="error"),
        _app(aid=10, m=2, score=2000, fetch_status="error"),
        _app(aid=20, score=2000, fetch_status="ready"),
    ]
    sorted_apps = sort_applicants_grouped(apps)
    # Solo B (ready) first, then group A's members adjacent.
    assert [a.applicant_id for a in sorted_apps] == ["20:1", "10:1", "10:2"]


def test_all_sunk_group_keeps_members_adjacent():
    """Even when sinking, group members stay adjacent (ordered by member_idx)."""
    apps = [
        _app(aid=10, m=2, score=2000, fetch_status="error"),
        _app(aid=10, m=1, score=2000, fetch_status="not_found"),
    ]
    sorted_apps = sort_applicants_grouped(apps)
    assert [a.applicant_id for a in sorted_apps] == ["10:1", "10:2"]


def test_mplus_listing_sorts_by_context_fit_before_rio():
    listing = Listing(
        activity_id=401,
        dungeon_name="Skyreach",
        listing_name="+16 Skyreach",
        comment="",
        key_level=16,
        category_id=2,
        difficulty_id=8,
    )
    relevant = _app(
        aid=1,
        score=3300,
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 20,
                "parse_percent": 31,
                "median_percent": 31,
                "run_count": 1,
                "brackets": [
                    {
                        "key_level": 16,
                        "parse_percent": 88,
                        "median_percent": 78,
                        "run_count": 2,
                    },
                    {
                        "key_level": 20,
                        "parse_percent": 31,
                        "median_percent": 31,
                        "run_count": 1,
                    },
                ],
            }
        ],
    )
    higher_rio_farm = _app(
        aid=2,
        score=3600,
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 10,
                "parse_percent": 99,
                "median_percent": 95,
                "run_count": 3,
            }
        ],
    )

    sorted_apps = sort_applicants_grouped([higher_rio_farm, relevant], listing)

    assert [a.applicant_id for a in sorted_apps] == ["1:1", "2:1"]


def test_mplus_group_sort_uses_package_fit_not_best_member_only():
    listing = Listing(
        activity_id=401,
        dungeon_name="Skyreach",
        listing_name="+16 Skyreach",
        comment="",
        key_level=16,
        category_id=2,
        difficulty_id=8,
    )
    strong_group_member = _app(
        aid=10,
        m=1,
        score=3600,
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 20,
                "parse_percent": 92,
                "median_percent": 88,
                "run_count": 3,
            }
        ],
    )
    weak_group_member = _app(
        aid=10,
        m=2,
        score=3300,
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 10,
                "parse_percent": 99,
                "median_percent": 95,
                "run_count": 3,
            }
        ],
    )
    solid_solo = _app(
        aid=20,
        score=3300,
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 16,
                "parse_percent": 82,
                "median_percent": 74,
                "run_count": 3,
            }
        ],
    )

    sorted_apps = sort_applicants_grouped(
        [strong_group_member, weak_group_member, solid_solo], listing
    )

    assert [a.applicant_id for a in sorted_apps] == ["20:1", "10:1", "10:2"]


def test_mplus_all_sunk_group_sinks_below_ready_fit_group():
    listing = Listing(
        activity_id=401,
        dungeon_name="Skyreach",
        listing_name="+16 Skyreach",
        comment="",
        key_level=16,
        category_id=2,
        difficulty_id=8,
    )
    sunk_high_fit = _app(
        aid=10,
        score=3600,
        fetch_status="error",
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 20,
                "parse_percent": 99,
                "median_percent": 95,
                "run_count": 3,
            }
        ],
    )
    ready_lower_fit = _app(
        aid=20,
        score=2500,
        fetch_status="ready",
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 16,
                "parse_percent": 60,
                "median_percent": 55,
                "run_count": 2,
            }
        ],
    )

    sorted_apps = sort_applicants_grouped([sunk_high_fit, ready_lower_fit], listing)

    assert [a.applicant_id for a in sorted_apps] == ["20:1", "10:1"]


def test_mplus_not_found_with_strong_rio_completion_sorts_by_fit_not_status_bucket():
    listing = Listing(
        activity_id=401,
        dungeon_name="Skyreach",
        listing_name="+16 Skyreach",
        comment="",
        key_level=16,
        category_id=2,
        difficulty_id=8,
    )
    no_wcl_strong_rio = _app(
        aid=10,
        score=3200,
        fetch_status="not_found",
        rio_profile=True,
        rio_best_key=17,
        rio_best_dungeon_key=15,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
    )
    ready_lower_fit = _app(
        aid=20,
        score=2500,
        fetch_status="ready",
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 16,
                "parse_percent": 55,
                "median_percent": 50,
                "run_count": 2,
            }
        ],
    )

    sorted_apps = sort_applicants_grouped([ready_lower_fit, no_wcl_strong_rio], listing)

    assert [a.applicant_id for a in sorted_apps] == ["10:1", "20:1"]


def test_mplus_zero_fit_all_sunk_still_sinks_below_ready_zero_fit():
    listing = Listing(
        activity_id=401,
        dungeon_name="Skyreach",
        listing_name="+16 Skyreach",
        comment="",
        key_level=16,
        category_id=2,
        difficulty_id=8,
    )
    all_sunk_no_fit = _app(aid=10, score=3600, fetch_status="error")
    ready_no_fit = _app(aid=20, score=0, fetch_status="ready")

    sorted_apps = sort_applicants_grouped([all_sunk_no_fit, ready_no_fit], listing)

    assert [a.applicant_id for a in sorted_apps] == ["20:1", "10:1"]


def test_mplus_unknown_key_sorts_by_visible_mplus_headline_before_rio():
    listing = Listing(
        activity_id=401,
        dungeon_name="Mythic+",
        listing_name="Mythic+",
        comment="",
        key_level=0,
        category_id=2,
        difficulty_id=8,
    )
    better_wcl_lower_rio = _app(
        aid=20,
        score=3291,
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 14,
                "parse_percent": 47,
                "median_percent": 22,
                "run_count": 2,
            }
        ],
    )
    better_wcl_lower_rio.mplus_dps = 47.0
    better_wcl_lower_rio.mplus_dps_median = 22.0
    weaker_wcl_higher_rio = _app(
        aid=10,
        score=3413,
        dps_breakdown=[
            {
                "name": "Skyreach",
                "key_level": 14,
                "parse_percent": 40,
                "median_percent": 39,
                "run_count": 2,
            }
        ],
    )
    weaker_wcl_higher_rio.mplus_dps = 40.0
    weaker_wcl_higher_rio.mplus_dps_median = 39.0
    wcl_error_higher_rio = _app(aid=30, score=3399, fetch_status="error")

    sorted_apps = sort_applicants_grouped(
        [weaker_wcl_higher_rio, wcl_error_higher_rio, better_wcl_lower_rio],
        listing,
    )

    assert [a.applicant_id for a in sorted_apps] == ["20:1", "10:1", "30:1"]


def test_mplus_unknown_key_prioritises_highest_key_before_percentile():
    listing = Listing(
        activity_id=401,
        dungeon_name="Mythic+",
        listing_name="Mythic+",
        comment="",
        key_level=0,
        category_id=2,
        difficulty_id=8,
    )
    low_key_high_percent = _app(
        aid=10,
        score=3098,
        dps_breakdown=[
            {
                "name": "Magisters' Terrace",
                "key_level": 12,
                "parse_percent": 91,
                "median_percent": 91,
                "run_count": 1,
            },
            {
                "name": "Windrunner Spire",
                "key_level": 10,
                "parse_percent": 81,
                "median_percent": 81,
                "run_count": 1,
            },
        ],
    )
    low_key_high_percent.mplus_dps = 84.0
    low_key_high_percent.mplus_dps_median = None
    higher_key_lower_percent = _app(
        aid=20,
        score=3231,
        dps_breakdown=[
            {
                "name": "Maisara Caverns",
                "key_level": 14,
                "parse_percent": 78,
                "median_percent": 78,
                "run_count": 1,
            }
        ],
    )
    higher_key_lower_percent.mplus_dps = 78.0
    higher_key_lower_percent.mplus_dps_median = None

    sorted_apps = sort_applicants_grouped(
        [low_key_high_percent, higher_key_lower_percent],
        listing,
    )

    assert [a.applicant_id for a in sorted_apps] == ["20:1", "10:1"]
