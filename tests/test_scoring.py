from __future__ import annotations

import pytest

from applicant_scout.constants import percentile_colour
from applicant_scout.scoring import (
    CONTEXT_MPLUS,
    CONTEXT_RAID,
    CONTEXT_UNKNOWN,
    candidate_fit,
    detect_listing_context,
    effective_rio_score,
    fit_label,
    fit_colour,
    mplus_dungeon_fit_rows,
    package_fit,
)
from applicant_scout.state import Applicant, Listing


def _listing(
    *,
    key_level: int = 0,
    dungeon_name: str = "Skyreach",
    category_id: int = 0,
    difficulty_id: int = 0,
) -> Listing:
    return Listing(
        activity_id=401,
        dungeon_name=dungeon_name,
        listing_name=f"+{key_level} {dungeon_name}" if key_level else dungeon_name,
        comment="",
        key_level=key_level,
        category_id=category_id,
        difficulty_id=difficulty_id,
    )


def _app(
    *,
    role: str = "DAMAGER",
    score: int = 3300,
    main_score: int = 0,
    rio_profile: bool = False,
    rio_best_key: int = 0,
    rio_best_dungeon_key: int = 0,
    rio_timed_at_or_above: int = 0,
    rio_timed_at_or_above_minus1: int = 0,
    rio_timed_at_or_above_minus2: int = 0,
    rio_completed_at_or_above_minus1: int = 0,
    rio_dungeon_count: int = 0,
    dps_breakdown: list[dict] | None = None,
    hps_breakdown: list[dict] | None = None,
    raid_normal: float | None = None,
    raid_normal_median: float | None = None,
    raid_heroic: float | None = None,
    raid_heroic_median: float | None = None,
    raid_mythic: float | None = None,
    raid_mythic_median: float | None = None,
    fetch_status: str = "ready",
) -> Applicant:
    return Applicant(
        applicant_id="1:1",
        name="Tester-Realm",
        cls="MAGE",
        spec_id=63,
        ilvl=280,
        score=score,
        main_score=main_score,
        rio_profile=rio_profile,
        rio_best_key=rio_best_key,
        rio_best_dungeon_key=rio_best_dungeon_key,
        rio_timed_at_or_above=rio_timed_at_or_above,
        rio_timed_at_or_above_minus1=rio_timed_at_or_above_minus1,
        rio_timed_at_or_above_minus2=rio_timed_at_or_above_minus2,
        rio_completed_at_or_above_minus1=rio_completed_at_or_above_minus1,
        rio_dungeon_count=rio_dungeon_count,
        role=role,
        raid_normal=raid_normal,
        raid_normal_median=raid_normal_median,
        raid_heroic=raid_heroic,
        raid_heroic_median=raid_heroic_median,
        raid_mythic=raid_mythic,
        raid_mythic_median=raid_mythic_median,
        mplus_dps_breakdown=dps_breakdown or [],
        mplus_hps_breakdown=hps_breakdown or [],
        mplus_dps=80.0 if dps_breakdown else None,
        mplus_dps_median=65.0 if dps_breakdown else None,
        mplus_hps=80.0 if hps_breakdown else None,
        mplus_hps_median=65.0 if hps_breakdown else None,
        fetch_status=fetch_status,
    )


def _dungeon(name: str, brackets: list[tuple[int, float, float, int]]) -> dict:
    top = max(brackets, key=lambda row: row[0])
    return {
        "name": name,
        "key_level": top[0],
        "parse_percent": top[1],
        "median_percent": top[2],
        "run_count": top[3],
        "brackets": [
            {
                "key_level": key,
                "parse_percent": best,
                "median_percent": median,
                "run_count": runs,
            }
            for key, best, median, runs in brackets
        ],
    }


def test_detect_listing_context_prefers_key_level_for_mplus():
    assert detect_listing_context(_listing(key_level=16, category_id=2)) == CONTEXT_MPLUS
    assert (
        detect_listing_context(_listing(category_id=3, difficulty_id=15))
        == CONTEXT_RAID
    )
    assert detect_listing_context(_listing()) == CONTEXT_UNKNOWN


def test_unknown_raid_difficulty_does_not_get_authoritative_fit_label():
    target = _listing(category_id=3, difficulty_id=0)
    applicant = _app(raid_heroic=90.0, raid_heroic_median=80.0, score=2500)

    fit = candidate_fit(applicant, target)
    group = package_fit([applicant], target)

    assert detect_listing_context(target) == CONTEXT_UNKNOWN
    assert fit.context == CONTEXT_UNKNOWN
    assert fit.display == ""
    assert group.context == CONTEXT_UNKNOWN
    assert group.display == ""


def test_unsupported_raid_difficulty_does_not_get_question_mark_fit_label():
    target = _listing(category_id=3, difficulty_id=99)
    applicant = _app(raid_mythic=95.0, raid_mythic_median=90.0, score=2500)

    fit = candidate_fit(applicant, target)

    assert detect_listing_context(target) == CONTEXT_UNKNOWN
    assert fit.context == CONTEXT_UNKNOWN
    assert fit.display == ""


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (24.0, "#666666"),
        (54.0, "#0070ff"),
        (73.0, "#0070ff"),
        (84.0, "#a335ee"),
        (96.0, "#ff8000"),
        (99.0, "#e268a8"),
        (100.0, "#e5cc80"),
    ],
)
def test_fit_colour_tracks_wcl_palette_for_numeric_score(score, expected):
    assert fit_colour(score) == expected


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (49.9, "RISK"),
        (50.0, "OK"),
        (69.9, "OK"),
        (70.0, "FIT"),
        (85.0, "TOP"),
    ],
)
def test_fit_label_tracks_visible_score_colour_bands(score, expected):
    assert fit_label(score) == expected


def test_effective_rio_score_uses_higher_of_current_and_main():
    assert effective_rio_score(_app(score=2443, main_score=3468)) == 3468
    assert effective_rio_score(_app(score=3600, main_score=3300)) == 3600
    assert effective_rio_score(_app(score=2200, main_score=0)) == 2200


def test_mplus_rio_fallback_uses_effective_rio_score():
    target = _listing(key_level=16)
    current_only = _app(score=1200, main_score=0)
    alt_with_main = _app(score=1200, main_score=3400)

    assert candidate_fit(alt_with_main, target).score > candidate_fit(
        current_only, target
    ).score


def test_mplus_rio_fallback_ignores_lower_main_score():
    target = _listing(key_level=16)

    assert candidate_fit(_app(score=3400, main_score=1200), target).score == candidate_fit(
        _app(score=3400, main_score=0), target
    ).score


def test_mplus_all_grey_near_target_profile_stays_low_risk():
    target = _listing(key_level=16)
    applicant = _app(
        score=3328,
        dps_breakdown=[
            _dungeon("Algeth'ar Academy", [(16, 15.0, 15.0, 1)]),
            _dungeon("Skyreach", [(15, 14.0, 14.0, 1)]),
            _dungeon("Nexus-Point Xenas", [(15, 10.0, 10.0, 1)]),
            _dungeon("Seat of the Triumvirate", [(15, 10.0, 10.0, 1)]),
            _dungeon("Magisters' Terrace", [(15, 6.0, 6.0, 1)]),
            _dungeon("Pit of Saron", [(15, 4.0, 4.0, 1)]),
            _dungeon("Windrunner Spire", [(16, 1.0, 1.0, 1)]),
            _dungeon("Maisara Caverns", [(14, 7.0, 7.0, 1)]),
        ],
    )

    fit = candidate_fit(applicant, target)

    assert fit.label == "RISK"
    assert fit.score < 25.0
    assert fit.colour == percentile_colour(24.0)


def test_mplus_mostly_grey_healer_is_not_rescued_by_main_rio():
    target = _listing(key_level=16, dungeon_name="Windrunner Spire")
    applicant = _app(
        role="HEALER",
        score=3381,
        main_score=3596,
        hps_breakdown=[
            _dungeon("Windrunner Spire", [(16, 47.0, 47.0, 1)]),
            _dungeon("Nexus-Point Xenas", [(16, 24.0, 24.0, 1)]),
            _dungeon("Maisara Caverns", [(16, 10.0, 10.0, 1)]),
            _dungeon("Seat of the Triumvirate", [(16, 8.0, 8.0, 1)]),
            _dungeon("Algeth'ar Academy", [(15, 28.0, 14.0, 2)]),
            _dungeon("Pit of Saron", [(16, 6.0, 6.0, 1)]),
            _dungeon("Skyreach", [(16, 1.0, 1.0, 1)]),
        ],
    )

    fit = candidate_fit(applicant, target)

    assert fit.label == "RISK"
    assert fit.score < 50.0


def test_mplus_overqualified_key_and_same_key_orange_are_good_for_low_listing():
    target = _listing(key_level=10)
    overqualified = _app(
        dps_breakdown=[_dungeon("Skyreach", [(20, 31.0, 31.0, 1)])],
        score=3400,
    )
    farm_parse = _app(
        dps_breakdown=[_dungeon("Skyreach", [(10, 99.0, 95.0, 3)])],
        score=2600,
    )

    overqualified_fit = candidate_fit(overqualified, target)
    farm_fit = candidate_fit(farm_parse, target)

    assert overqualified_fit.score >= 70.0
    assert farm_fit.score >= 70.0


def test_mplus_low_key_orange_does_not_beat_relevant_high_key_evidence():
    target = _listing(key_level=16)
    farm_parse = _app(
        dps_breakdown=[_dungeon("Skyreach", [(10, 99.0, 95.0, 3)])],
        score=3300,
    )
    relevant = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 88.0, 78.0, 2), (20, 31.0, 31.0, 1)])],
        score=3300,
    )

    farm_fit = candidate_fit(farm_parse, target)
    relevant_fit = candidate_fit(relevant, target)

    assert farm_fit.score < 25.0
    assert relevant_fit.score > farm_fit.score
    assert relevant_fit.primary_key == 16


def test_mplus_extra_bad_dungeons_do_not_reduce_sparse_penalty():
    target = _listing(key_level=16)
    strong_with_some_bad = _app(
        dps_breakdown=[
            _dungeon("Skyreach", [(16, 90.0, 80.0, 3)]),
            _dungeon("Nexus-Point Xenas", [(16, 1.0, 1.0, 1)]),
            _dungeon("Seat of the Triumvirate", [(16, 1.0, 1.0, 1)]),
            _dungeon("Maisara Caverns", [(16, 1.0, 1.0, 1)]),
        ],
    )
    strong_with_more_bad = _app(
        dps_breakdown=[
            _dungeon("Skyreach", [(16, 90.0, 80.0, 3)]),
            _dungeon("Nexus-Point Xenas", [(16, 1.0, 1.0, 1)]),
            _dungeon("Seat of the Triumvirate", [(16, 1.0, 1.0, 1)]),
            _dungeon("Maisara Caverns", [(16, 1.0, 1.0, 1)]),
            _dungeon("Pit of Saron", [(16, 1.0, 1.0, 1)]),
            _dungeon("Algeth'ar Academy", [(16, 1.0, 1.0, 1)]),
            _dungeon("Magisters' Terrace", [(16, 1.0, 1.0, 1)]),
            _dungeon("Windrunner Spire", [(16, 1.0, 1.0, 1)]),
        ],
    )

    assert candidate_fit(strong_with_more_bad, target).score <= candidate_fit(
        strong_with_some_bad, target
    ).score


def test_mplus_fit_rows_use_relevant_lower_bracket_when_top_key_is_grey():
    target = _listing(key_level=16)
    applicant = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 88.0, 78.0, 2), (20, 31.0, 31.0, 1)])],
        score=3300,
    )

    rows = mplus_dungeon_fit_rows(applicant, target)

    assert rows[0].dungeon_name == "Skyreach"
    assert rows[0].key_level == 16
    assert rows[0].text == "88/78"
    assert rows[0].colour == percentile_colour(88.0)


def test_mplus_fit_rows_colour_printed_percentile_not_context_fit():
    target = _listing(key_level=17)
    applicant = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 64.0, 64.0, 1)])],
        score=3300,
    )

    rows = mplus_dungeon_fit_rows(applicant, target)

    assert rows[0].text == "64"
    assert rows[0].score < 55.0
    assert rows[0].colour == percentile_colour(64.0)


def test_mplus_broad_target_minus_one_profile_is_ok_not_risk():
    target = _listing(key_level=17, dungeon_name="Algeth'ar Academy")
    applicant = _app(
        score=3409,
        dps_breakdown=[
            _dungeon("Nexus-Point Xenas", [(16, 96.0, 96.0, 1)]),
            _dungeon("Pit of Saron", [(16, 91.0, 91.0, 1)]),
            _dungeon("Magisters' Terrace", [(16, 83.0, 83.0, 1)]),
            _dungeon("Seat of the Triumvirate", [(16, 81.0, 81.0, 1)]),
            _dungeon("Skyreach", [(16, 64.0, 64.0, 1)]),
            _dungeon("Maisara Caverns", [(16, 43.0, 43.0, 1)]),
            _dungeon("Windrunner Spire", [(16, 18.0, 18.0, 1)]),
            _dungeon("Algeth'ar Academy", [(15, 91.0, 91.0, 1)]),
        ],
    )

    fit = candidate_fit(applicant, target)

    assert fit.label == "OK"
    assert fit.score >= 55.0


def test_mplus_single_target_minus_one_parse_does_not_overpromote():
    target = _listing(key_level=17, dungeon_name="Other Dungeon")
    applicant = _app(
        score=3409,
        dps_breakdown=[_dungeon("Skyreach", [(16, 96.0, 96.0, 1)])],
    )

    fit = candidate_fit(applicant, target)

    assert fit.score < 55.0


def test_mplus_healer_uses_hps_breakdown_and_ignores_dps():
    target = _listing(key_level=16)
    healer = _app(
        role="HEALER",
        dps_breakdown=[_dungeon("Skyreach", [(20, 99.0, 99.0, 3)])],
        hps_breakdown=[_dungeon("Skyreach", [(16, 60.0, 50.0, 2)])],
        score=3300,
    )

    fit = candidate_fit(healer, target)

    assert fit.primary_key == 16
    assert fit.score < 80


def test_mplus_rio_completion_profile_rescues_missing_wcl_without_top_rating():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    applicant = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=17,
        rio_best_dungeon_key=15,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
    )

    fit = candidate_fit(applicant, target)

    assert fit.source == "rio_completion"
    assert 68.0 <= fit.score < 85.0
    assert fit.confidence >= 0.55


def test_mplus_rio_completion_beats_low_key_parse_spike_for_target_key():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    low_key_parse_spike = _app(
        score=3098,
        dps_breakdown=[
            _dungeon("Skyreach", [(10, 96.0, 92.0, 2)]),
            _dungeon("Other", [(12, 91.0, 88.0, 2)]),
        ],
    )
    experienced_low_log = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=17,
        rio_best_dungeon_key=15,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        dps_breakdown=[_dungeon("Skyreach", [(12, 42.0, 38.0, 2)])],
    )

    assert candidate_fit(experienced_low_log, target).score > candidate_fit(
        low_key_parse_spike, target
    ).score


def test_mplus_bad_relevant_wcl_caps_strong_rio_completion():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    applicant = _app(
        score=3300,
        rio_profile=True,
        rio_best_key=18,
        rio_best_dungeon_key=16,
        rio_timed_at_or_above=4,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        dps_breakdown=[_dungeon("Skyreach", [(16, 18.0, 16.0, 3)])],
    )

    fit = candidate_fit(applicant, target)

    assert fit.score < 62.0
    assert fit.label in {"OK", "RISK"}


def test_package_fit_penalizes_weak_member_in_multi_member_group():
    target = _listing(key_level=16)
    strong = _app(
        dps_breakdown=[_dungeon("Skyreach", [(20, 92.0, 88.0, 3)])],
        score=3600,
    )
    weak = _app(
        dps_breakdown=[_dungeon("Skyreach", [(10, 99.0, 95.0, 3)])],
        score=3300,
    )

    group = package_fit([strong, weak], target)
    solo = package_fit([strong], target)

    assert group.score < solo.score
    assert group.low_score < group.high_score
    assert group.worst_score == group.low_score
    assert group.best_score == group.high_score
    assert group.display.startswith("G2 ")


def test_package_fit_solo_uses_individual_score_without_group_display():
    target = _listing(key_level=16)
    applicant = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
    )

    solo = package_fit([applicant], target)
    fit = candidate_fit(applicant, target)

    assert solo.score == fit.score
    assert solo.display == ""
    assert solo.member_scores == (fit.score,)


def test_package_fit_unknown_context_keeps_member_stats_consistent():
    group = package_fit(
        [
            _app(score=3600, main_score=1200),
            _app(score=2400, main_score=3100),
            _app(score=3000),
        ],
        _listing(),
    )

    assert group.score == 3600.0
    assert group.high_score == 3600.0
    assert group.average_score == 3233.3333333333335
    assert group.low_score == 3000.0
    assert group.spread == 600.0
    assert group.display == ""


def test_package_fit_solid_group_has_no_flat_size_penalty():
    target = _listing(key_level=16)
    solid = [
        _app(
            dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
            score=3300,
        )
        for _ in range(3)
    ]

    group = package_fit(solid, target)
    solo = package_fit([solid[0]], target)

    assert group.score >= solo.score - 1.0
    assert group.status_penalty == 0.0
    assert group.display.startswith("G3 ")


def test_package_fit_superstar_with_weak_friend_loses_to_solid_solo():
    target = _listing(key_level=16)
    superstar = _app(
        dps_breakdown=[_dungeon("Skyreach", [(22, 99.0, 95.0, 3)])],
        score=3900,
    )
    weak_friend = _app(
        dps_breakdown=[_dungeon("Skyreach", [(10, 99.0, 95.0, 3)])],
        score=3300,
    )
    solid_solo = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
    )

    group = package_fit([superstar, weak_friend], target)
    solo = package_fit([solid_solo], target)

    assert group.score < solo.score
    assert group.spread > 30


def test_package_fit_overqualified_member_helps_but_cannot_hide_deadweight():
    target = _listing(key_level=10)
    overqualified = _app(
        dps_breakdown=[_dungeon("Skyreach", [(22, 95.0, 90.0, 3)])],
        score=3900,
    )
    normal_friend = _app(
        dps_breakdown=[_dungeon("Skyreach", [(10, 62.0, 56.0, 3)])],
        score=2800,
    )
    deadweight = _app(
        dps_breakdown=[_dungeon("Skyreach", [(4, 99.0, 95.0, 3)])],
        score=1900,
    )

    supported = package_fit([overqualified, normal_friend], target)
    risky = package_fit([overqualified, deadweight], target)

    assert supported.score > package_fit([normal_friend], target).score
    assert risky.score < supported.score
    assert risky.low_score < 48


def test_package_fit_mplus_weak_link_is_harsher_than_raid():
    mplus_listing = _listing(key_level=16)
    raid_listing = _listing(category_id=3, difficulty_id=15)
    strong_mplus = _app(
        dps_breakdown=[_dungeon("Skyreach", [(20, 95.0, 90.0, 3)])],
        score=3600,
    )
    weak_mplus = _app(
        dps_breakdown=[_dungeon("Skyreach", [(10, 99.0, 95.0, 3)])],
        score=3300,
    )
    strong_raid = _app(raid_heroic=95.0, raid_heroic_median=90.0, score=2500)
    weak_raid = _app(raid_heroic=45.0, raid_heroic_median=40.0, score=2500)

    mplus_group = package_fit([strong_mplus, weak_mplus], mplus_listing)
    raid_group = package_fit([strong_raid, weak_raid], raid_listing)
    mplus_avg = (mplus_group.high_score + mplus_group.low_score) / 2.0
    raid_avg = (raid_group.high_score + raid_group.low_score) / 2.0

    assert mplus_avg - mplus_group.score > raid_avg - raid_group.score


def test_package_fit_status_confidence_and_penalty():
    target = _listing(key_level=16)
    ready = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
    )
    loading = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
        fetch_status="loading",
    )
    error = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
        fetch_status="error",
    )
    not_found = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
        fetch_status="not_found",
    )

    loading_group = package_fit([ready, loading], target)
    error_group = package_fit([ready, error, not_found], target)

    assert loading_group.status_penalty == 0.0
    assert loading_group.confidence < package_fit([ready, ready], target).confidence
    assert error_group.status_penalty == 10.0
    assert error_group.score < loading_group.score


def test_package_fit_terminal_member_does_not_score_from_stale_wcl_metrics():
    target = _listing(key_level=16)
    ready = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
    )
    stale_error = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 99.0, 95.0, 3)])],
        score=3300,
        fetch_status="error",
    )

    group = package_fit([ready, stale_error], target)

    assert group.member_scores[1] == 0.0
    assert group.label == "RISK"
    assert group.display.startswith("G2 RISK ")


def test_raid_heroic_listing_prioritises_heroic_parse():
    target = _listing(category_id=3, difficulty_id=15)
    heroic = _app(raid_heroic=90.0, raid_heroic_median=80.0, score=2500)
    mplus_only = _app(
        dps_breakdown=[_dungeon("Skyreach", [(20, 99.0, 99.0, 3)])],
        score=3600,
    )

    assert candidate_fit(heroic, target).score > candidate_fit(mplus_only, target).score


def test_raid_support_uses_effective_rio_score():
    target = _listing(category_id=3, difficulty_id=15)
    current_only = _app(score=1200, main_score=0)
    alt_with_main = _app(score=1200, main_score=3400)

    assert candidate_fit(alt_with_main, target).score > candidate_fit(
        current_only, target
    ).score


def test_raid_heroic_uses_mythic_as_higher_difficulty_fallback():
    target = _listing(category_id=3, difficulty_id=15)
    mythic = _app(raid_mythic=90.0, raid_mythic_median=80.0, score=2500)

    fit = candidate_fit(mythic, target)

    assert fit.context == CONTEXT_RAID
    assert fit.source == "raid_higher_fallback"
    assert fit.score > 70
