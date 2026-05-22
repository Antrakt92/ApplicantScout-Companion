from __future__ import annotations

from typing import Any, cast

import pytest

from applicant_scout.constants import percentile_colour
from applicant_scout.scoring import (
    CONTEXT_MPLUS,
    CONTEXT_RAID,
    CONTEXT_UNKNOWN,
    _package_score_for_member_scores,
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
    activity_id: int = 401,
    key_level: int = 0,
    dungeon_name: str = "Skyreach",
    category_id: int = 0,
    difficulty_id: int = 0,
) -> Listing:
    return Listing(
        activity_id=activity_id,
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
    rio_summary_target_key: int = 0,
    rio_dungeons: list[dict] | None = None,
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
        rio_summary_target_key=rio_summary_target_key,
        rio_dungeons=rio_dungeons or [],
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


MPLUS_DUNGEONS = [
    "Skyreach",
    "Algeth'ar Academy",
    "Magisters' Terrace",
    "Maisara Caverns",
    "Nexus-Point Xenas",
    "Pit of Saron",
    "Seat of the Triumvirate",
    "Windrunner Spire",
]


def _rio_dungeons(levels: list[int]) -> list[dict]:
    return [
        {"name": name, "key_level": level}
        for name, level in zip(MPLUS_DUNGEONS, levels, strict=True)
    ]


def _rio_profile(levels: list[int], *, target_key: int) -> dict[str, object]:
    return {
        "rio_profile": True,
        "rio_best_key": max(levels),
        "rio_best_dungeon_key": levels[0],
        "rio_timed_at_or_above": sum(1 for level in levels if level >= target_key),
        "rio_timed_at_or_above_minus1": sum(
            1 for level in levels if level >= target_key - 1
        ),
        "rio_timed_at_or_above_minus2": sum(
            1 for level in levels if level >= target_key - 2
        ),
        "rio_completed_at_or_above_minus1": sum(
            1 for level in levels if level >= target_key - 1
        ),
        "rio_dungeon_count": len(levels),
        "rio_summary_target_key": target_key,
        "rio_dungeons": _rio_dungeons(levels),
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

    assert fit.score < 25.0
    assert fit.colour == percentile_colour(24.0)
    assert fit.label == ""


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

    assert fit.score < 50.0
    assert fit.label == ""


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

    assert overqualified_fit.score > 0.0
    assert farm_fit.score > overqualified_fit.score


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


def test_mplus_same_dungeon_match_uses_activity_id_when_listing_name_is_localized():
    target = _listing(
        activity_id=404,
        key_level=16,
        dungeon_name="Небесный Путь",
    )
    applicant = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 88.0, 78.0, 2)])],
        score=3300,
    )

    fit = candidate_fit(applicant, target)

    assert fit.same_dungeon_score > 0.0


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

    assert rows[0].text == "64 N=1"
    assert rows[0].score < 55.0
    assert rows[0].colour == percentile_colour(64.0)


def test_mplus_scorecard_keeps_no_log_target_profile_unknown_not_good():
    target = _listing(key_level=15)
    no_logs = _app(score=3270, **_rio_profile([15] * 8, target_key=15))

    fit = candidate_fit(no_logs, target)

    assert fit.source == "mplus_scorecard"
    assert 50.0 <= fit.score <= 58.0
    assert fit.display == f"{int(round(fit.score))} +15"
    assert fit.label == ""
    assert all(word not in fit.display for word in ("TOP", "FIT", "OK", "RISK", "RIO"))


def test_mplus_scorecard_good_logs_on_lower_keys_beat_unknown_target_keys():
    target = _listing(key_level=15)
    target_no_logs = _app(score=3270, **_rio_profile([15] * 8, target_key=15))
    lower_with_good_logs = _app(
        score=3150,
        **_rio_profile([14] * 8, target_key=15),
        dps_breakdown=[
            _dungeon(name, [(14, pct, pct, 2)])
            for name, pct in zip(
                MPLUS_DUNGEONS,
                [96.0, 91.0, 83.0, 81.0, 64.0, 62.0, 58.0, 51.0],
                strict=True,
            )
        ],
    )

    target_fit = candidate_fit(target_no_logs, target)
    lower_fit = candidate_fit(lower_with_good_logs, target)

    assert lower_fit.score > target_fit.score
    assert 56.0 <= lower_fit.score <= 64.0
    assert lower_fit.primary_key == 14


def test_mplus_scorecard_gray_relevant_logs_are_worse_than_no_logs():
    target = _listing(key_level=15)
    no_logs = _app(score=3270, **_rio_profile([15] * 8, target_key=15))
    one_gray = _app(
        score=3270,
        **_rio_profile([15] * 8, target_key=15),
        dps_breakdown=[_dungeon("Skyreach", [(15, 12.0, 12.0, 1)])],
    )
    repeated_gray = _app(
        score=3270,
        **_rio_profile([15] * 8, target_key=15),
        dps_breakdown=[
            _dungeon(name, [(15, pct, pct, 1)])
            for name, pct in zip(
                MPLUS_DUNGEONS,
                [12.0, 18.0, 15.0, 8.0, 10.0, 14.0, 20.0, 6.0],
                strict=True,
            )
        ],
    )

    no_log_fit = candidate_fit(no_logs, target)
    one_gray_fit = candidate_fit(one_gray, target)
    repeated_gray_fit = candidate_fit(repeated_gray, target)

    assert one_gray_fit.score < no_log_fit.score
    assert repeated_gray_fit.score < one_gray_fit.score
    assert repeated_gray_fit.score < 40.0


def test_mplus_scorecard_overqualified_no_logs_ramp_without_clamping_sort_score():
    target = _listing(key_level=15)
    target_profile = candidate_fit(
        _app(score=3270, **_rio_profile([15] * 8, target_key=15)), target
    )
    plus18_profile = candidate_fit(
        _app(score=3600, **_rio_profile([18] * 8, target_key=15)), target
    )
    plus20_profile = candidate_fit(
        _app(score=3900, **_rio_profile([20] * 8, target_key=15)), target
    )

    assert target_profile.score < plus18_profile.score < plus20_profile.score < 100.0
    assert 76.0 <= plus18_profile.score <= 82.0
    assert 87.0 <= plus20_profile.score <= 91.0
    assert int(round(plus18_profile.score)) != int(round(plus20_profile.score))


def test_mplus_scorecard_one_big_key_without_breadth_stays_low():
    target = _listing(key_level=15)
    fit = candidate_fit(
        _app(score=3300, **_rio_profile([20, 12, 12, 12, 12, 12, 12, 12], target_key=15)),
        target,
    )

    assert fit.score < 25.0
    assert fit.primary_key == 20


def test_mplus_scorecard_low_key_logs_do_not_cheese_target_listing():
    target = _listing(key_level=15)
    low_log_spam = _app(
        score=3000,
        **_rio_profile([13] * 8, target_key=15),
        dps_breakdown=[
            _dungeon(name, [(13, 99.0, 95.0, 3)])
            for name in MPLUS_DUNGEONS
        ],
    )

    fit = candidate_fit(low_log_spam, target)

    assert fit.score < 35.0
    assert fit.primary_key == 13


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

    assert fit.label == ""
    assert fit.score >= 35.0


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


def test_mplus_partial_rio_rows_do_not_discard_wcl_key_readiness():
    target = _listing(key_level=12, dungeon_name="Mythic+", activity_id=0)
    healer = _app(
        role="HEALER",
        score=3271,
        rio_dungeons=[
            {"name": "Magisters' Terrace", "key_level": 11},
            {"name": "Maisara Caverns", "key_level": 10},
        ],
        hps_breakdown=[
            _dungeon("Algeth'ar Academy", [(12, 37.0, 37.0, 1)]),
            _dungeon("Pit of Saron", [(12, 84.0, 84.0, 1)]),
            _dungeon("Magisters' Terrace", [(10, 84.0, 84.0, 1)]),
            _dungeon("Maisara Caverns", [(10, 37.0, 37.0, 1)]),
            _dungeon("Skyreach", [(10, 100.0, 100.0, 1)]),
            _dungeon("Windrunner Spire", [(10, 55.0, 55.0, 1)]),
        ],
    )

    fit = candidate_fit(healer, target)

    assert fit.primary_key == 12
    assert fit.score >= 25.0


def test_mplus_scorecard_rio_summary_rescues_missing_wcl_without_top_rating():
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
        rio_summary_target_key=target.key_level,
    )

    fit = candidate_fit(applicant, target)

    assert fit.source == "mplus_scorecard"
    assert 40.0 <= fit.score < 58.0
    assert fit.confidence >= 0.55
    assert "+17" in fit.display


def test_mplus_scorecard_ignores_compact_rio_summary_for_different_target_key():
    original_target = _listing(key_level=10, dungeon_name="Skyreach")
    current_target = _listing(key_level=16, dungeon_name="Skyreach")
    stale_summary = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=17,
        rio_best_dungeon_key=15,
        rio_timed_at_or_above=8,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=original_target.key_level,
    )
    no_summary = _app(score=3200)

    stale_fit = candidate_fit(stale_summary, current_target)
    baseline_fit = candidate_fit(no_summary, current_target)

    assert stale_fit.score == baseline_fit.score
    assert stale_fit.primary_key == baseline_fit.primary_key


def test_mplus_scorecard_uses_rio_dungeon_rows_for_same_dungeon_key():
    target = _listing(activity_id=404, key_level=16, dungeon_name="Небесный Путь")
    applicant = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=17,
        rio_best_dungeon_key=0,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
    )

    fit = candidate_fit(applicant, target)

    assert fit.source == "mplus_scorecard"
    assert fit.primary_key == 17
    assert "+17" in fit.display
    assert fit.confidence >= 0.55


def test_mplus_scorecard_keeps_higher_summary_same_dungeon_key():
    target = _listing(activity_id=404, key_level=16, dungeon_name="Небесный Путь")
    applicant = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=17,
        rio_best_dungeon_key=16,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
    )

    fit = candidate_fit(applicant, target)

    assert fit.source == "mplus_scorecard"
    weaker_same_dungeon = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=17,
        rio_best_dungeon_key=0,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
    )
    assert fit.primary_key == 17
    assert "+17" in fit.display
    assert fit.score > candidate_fit(weaker_same_dungeon, target).score


def test_mplus_scorecard_rio_summary_rescues_not_found_wcl_status():
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
        rio_summary_target_key=target.key_level,
        fetch_status="not_found",
    )

    fit = candidate_fit(applicant, target)

    assert fit.source == "mplus_scorecard"
    assert 40.0 <= fit.score < 58.0
    assert "+17" in fit.display


def test_mplus_terminal_wcl_status_ignores_stale_wcl_but_uses_scorecard():
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
        rio_summary_target_key=target.key_level,
        dps_breakdown=[_dungeon("Skyreach", [(16, 99.0, 95.0, 3)])],
        fetch_status="error",
    )

    fit = candidate_fit(applicant, target)

    assert fit.source == "mplus_scorecard"
    assert fit.score < 92.0
    assert "+18" in fit.display


def test_mplus_scorecard_beats_low_key_parse_spike_for_target_key():
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
        rio_summary_target_key=target.key_level,
        dps_breakdown=[_dungeon("Skyreach", [(12, 42.0, 38.0, 2)])],
    )

    experienced_fit = candidate_fit(experienced_low_log, target)
    assert experienced_fit.score > candidate_fit(low_key_parse_spike, target).score
    assert "+17" in experienced_fit.display


def test_mplus_scorecard_display_keeps_higher_rio_key_when_wcl_is_lower():
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
        rio_dungeons=[
            {"name": "Skyreach", "key_level": 16},
            {"name": "Pit of Saron", "key_level": 18},
            {"name": "Maisara Caverns", "key_level": 17},
            {"name": "Algeth'ar Academy", "key_level": 17},
            {"name": "Magisters' Terrace", "key_level": 16},
            {"name": "Nexus-Point Xenas", "key_level": 16},
            {"name": "Seat of the Triumvirate", "key_level": 16},
            {"name": "Windrunner Spire", "key_level": 16},
        ],
        dps_breakdown=[_dungeon("Skyreach", [(14, 90.0, 86.0, 3)])],
    )

    fit = candidate_fit(applicant, target)

    assert fit.primary_key == 18
    assert "+18" in fit.display


def test_mplus_bad_relevant_wcl_caps_strong_scorecard_evidence():
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
    assert fit.label == ""


def test_mplus_wcl_quality_prefers_stable_median_over_best_parse_spike():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    spike = _app(
        score=3300,
        **_rio_profile([16] * 8, target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(16, 99.0, 35.0, 5)])],
    )
    steady = _app(
        score=3300,
        **_rio_profile([16] * 8, target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(16, 85.0, 80.0, 3)])],
    )

    spike_fit = candidate_fit(spike, target)
    steady_fit = candidate_fit(steady, target)

    assert steady_fit.score > spike_fit.score


def test_mplus_gray_relevant_wcl_is_weaker_than_no_log_equivalent():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    no_log = _app(score=3300, **_rio_profile([16] * 8, target_key=16))
    gray_log = _app(
        score=3300,
        **_rio_profile([16] * 8, target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(16, 35.0, 35.0, 3)])],
    )

    no_log_fit = candidate_fit(no_log, target)
    gray_fit = candidate_fit(gray_log, target)

    assert gray_fit.score < no_log_fit.score


def test_mplus_high_gray_overqualified_breadth_loses_to_clean_target_breadth():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    high_gray = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(20, 35.0, 35.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )
    clean_target = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 55.0, 55.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )

    high_gray_fit = candidate_fit(high_gray, target)
    clean_target_fit = candidate_fit(clean_target, target)

    assert high_gray_fit.score < clean_target_fit.score


def test_mplus_high_gray_wcl_does_not_display_raw_overqualified_key():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    high_gray = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(20, 49.0, 49.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )

    fit = candidate_fit(high_gray, target)

    assert fit.primary_key == 16
    assert fit.display.endswith(" +16")


def test_mplus_broad_near_median_gray_wcl_counts_as_weak_completion_evidence():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    broad_gray_target = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 49.0, 49.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )
    sparse_clean_target = _app(
        score=0,
        dps_breakdown=[_dungeon("Skyreach", [(16, 99.0, 95.0, 3)])],
    )
    clean_target = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 50.0, 50.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )

    broad_gray_fit = candidate_fit(broad_gray_target, target)
    sparse_clean_fit = candidate_fit(sparse_clean_target, target)
    clean_target_fit = candidate_fit(clean_target, target)

    assert sparse_clean_fit.score < broad_gray_fit.score < clean_target_fit.score
    assert broad_gray_fit.confidence < clean_target_fit.confidence


def test_mplus_gray_wcl_thresholds_are_weak_low_confidence_evidence():
    target = _listing(key_level=16, dungeon_name="Skyreach")

    def broad_fit(percentile: float):
        applicant = _app(
            score=0,
            dps_breakdown=[
                _dungeon(name, [(16, percentile, percentile, 3)])
                for name in MPLUS_DUNGEONS
            ],
        )
        return candidate_fit(applicant, target)

    bad = broad_fit(24.0)
    gray_floor = broad_fit(25.0)
    near_median = broad_fit(49.0)
    clean_floor = broad_fit(50.0)

    assert bad.score == 0.0
    assert 0.0 < gray_floor.score < near_median.score < clean_floor.score
    assert near_median.confidence < clean_floor.confidence
    assert near_median.coverage == 0.0
    assert clean_floor.coverage == 1.0


def test_mplus_wcl_quality_is_monotonic_when_rio_already_covers_target():
    target = _listing(key_level=16, dungeon_name="Skyreach")

    def broad_fit(percentile: float):
        applicant = _app(
            score=3300,
            **_rio_profile([16] * 8, target_key=16),
            dps_breakdown=[
                _dungeon(name, [(16, percentile, percentile, 3)])
                for name in MPLUS_DUNGEONS
            ],
        )
        return candidate_fit(applicant, target)

    scores = [broad_fit(percentile).score for percentile in (24.0, 25.0, 49.0, 50.0)]

    assert scores == sorted(scores)


def test_mplus_high_gray_breadth_is_experience_but_loses_to_clean_target_breadth():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    high_gray = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(19, 49.0, 49.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )
    clean_minus_one = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(15, 50.0, 50.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )
    clean_target = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 50.0, 50.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )

    high_gray_fit = candidate_fit(high_gray, target)
    clean_minus_one_fit = candidate_fit(clean_minus_one, target)
    clean_target_fit = candidate_fit(clean_target, target)

    assert clean_minus_one_fit.score < high_gray_fit.score < clean_target_fit.score
    assert high_gray_fit.confidence < clean_minus_one_fit.confidence


def test_mplus_same_dungeon_gray_higher_bracket_does_not_raise_clean_target_fit():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    clean_target = _app(
        score=0,
        dps_breakdown=[_dungeon("Skyreach", [(16, 80.0, 75.0, 3)])],
    )
    clean_with_gray_higher = _app(
        score=0,
        dps_breakdown=[
            _dungeon("Skyreach", [(16, 80.0, 75.0, 3), (20, 49.0, 49.0, 3)])
        ],
    )

    clean_fit = candidate_fit(clean_target, target)
    gray_higher_fit = candidate_fit(clean_with_gray_higher, target)

    assert gray_higher_fit.score <= clean_fit.score


def test_mplus_distinct_dungeon_breadth_beats_repeated_same_dungeon_brackets():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    repeated_same_dungeon = _app(
        score=0,
        dps_breakdown=[
            _dungeon(
                "Skyreach",
                [
                    (16, 90.0, 85.0, 3),
                    (17, 88.0, 82.0, 3),
                    (18, 86.0, 80.0, 3),
                ],
            )
        ],
    )
    broad_profile = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 80.0, 76.0, 3)])
            for name in MPLUS_DUNGEONS[:3]
        ],
    )

    repeated_fit = candidate_fit(repeated_same_dungeon, target)
    broad_fit = candidate_fit(broad_profile, target)

    assert broad_fit.score > repeated_fit.score


def test_mplus_completed_minus_one_supports_less_than_timed_minus_one():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    no_completion = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=0,
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
        rio_completed_at_or_above_minus1=0,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
    )
    completed_minus_one = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=0,
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
    )
    timed_minus_one = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=0,
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
    )

    no_completion_fit = candidate_fit(no_completion, target)
    completed_fit = candidate_fit(completed_minus_one, target)
    timed_fit = candidate_fit(timed_minus_one, target)

    assert no_completion_fit.score < completed_fit.score < timed_fit.score


def test_mplus_partial_rio_rows_do_not_double_count_summary_best_key():
    target = _listing(key_level=16, dungeon_name="Skyreach")

    def applicant_with_rows(levels: list[int]):
        return _app(
            score=3300,
            rio_profile=True,
            rio_best_key=max(levels),
            rio_best_dungeon_key=0,
            rio_timed_at_or_above=1,
            rio_timed_at_or_above_minus1=8,
            rio_timed_at_or_above_minus2=8,
            rio_completed_at_or_above_minus1=8,
            rio_dungeon_count=8,
            rio_summary_target_key=target.key_level,
            rio_dungeons=[
                {"name": name, "key_level": level}
                for name, level in zip(MPLUS_DUNGEONS, levels, strict=False)
            ],
        )

    partial = candidate_fit(applicant_with_rows([18]), target)
    explicit = candidate_fit(applicant_with_rows([18, 15, 15, 15, 15, 15, 15, 15]), target)

    assert partial.score == explicit.score


def test_mplus_score_only_fallback_is_capped_low_confidence_prior():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    max_score_only = _app(score=5000)
    clean_minus_one = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(15, 50.0, 50.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )
    clean_target = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 50.0, 50.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )

    score_only_fit = candidate_fit(max_score_only, target)

    assert score_only_fit.score == 42.0
    assert score_only_fit.confidence == 0.3
    assert candidate_fit(clean_minus_one, target).score < score_only_fit.score
    assert score_only_fit.score < candidate_fit(clean_target, target).score


@pytest.mark.parametrize("target_key", [10, 16, 20])
def test_mplus_scorecard_calibrates_core_evidence_order_across_key_levels(
    target_key: int,
):
    target = _listing(key_level=target_key, dungeon_name="Skyreach")

    def rio_app(key_level: int) -> Applicant:
        return _app(score=3300, **_rio_profile([key_level] * 8, target_key=target_key))

    def wcl_app(
        key_level: int,
        parse: float,
        median: float,
        dungeon_count: int = 8,
    ) -> Applicant:
        return _app(
            score=0,
            dps_breakdown=[
                _dungeon(name, [(key_level, parse, median, 3)])
                for name in MPLUS_DUNGEONS[:dungeon_count]
            ],
        )

    ordered_applicants = [
        wcl_app(target_key, 80.0, 75.0),
        rio_app(target_key + 2),
        wcl_app(target_key, 55.0, 55.0),
        rio_app(target_key),
        wcl_app(target_key + 4, 35.0, 35.0),
        rio_app(target_key - 1),
        wcl_app(target_key, 99.0, 95.0, dungeon_count=1),
        wcl_app(target_key, 10.0, 10.0),
    ]

    scores = [candidate_fit(applicant, target).score for applicant in ordered_applicants]

    assert scores == sorted(scores, reverse=True)


def test_mplus_all_minus_one_rio_with_mixed_lower_wcl_does_not_become_top():
    target = _listing(key_level=15, dungeon_name="Pit of Saron")
    applicant = _app(
        score=3209,
        rio_profile=True,
        rio_best_key=14,
        rio_best_dungeon_key=14,
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_dungeons=[
            {"name": "Skyreach", "key_level": 14},
            {"name": "Algeth'ar Academy", "key_level": 14},
            {"name": "Magisters' Terrace", "key_level": 14},
            {"name": "Maisara Caverns", "key_level": 14},
            {"name": "Nexus-Point Xenas", "key_level": 14},
            {"name": "Pit of Saron", "key_level": 14},
            {"name": "Seat of the Triumvirate", "key_level": 14},
            {"name": "Windrunner Spire", "key_level": 14},
        ],
        dps_breakdown=[
            _dungeon("Skyreach", [(14, 51.0, None, 1)]),
            _dungeon("Algeth'ar Academy", [(10, 12.0, None, 1)]),
            _dungeon("Magisters' Terrace", [(10, 15.0, None, 1)]),
            _dungeon("Maisara Caverns", [(13, 51.0, None, 1)]),
            _dungeon("Nexus-Point Xenas", [(14, 18.0, None, 1)]),
            _dungeon("Pit of Saron", [(11, 22.0, None, 1)]),
            _dungeon("Seat of the Triumvirate", [(12, 18.0, None, 1)]),
            _dungeon("Windrunner Spire", [(11, 62.0, None, 1)]),
        ],
    )

    fit = candidate_fit(applicant, target)

    assert fit.score < 80.0
    assert fit.label != "TOP"
    assert fit.primary_key == 14


def test_mplus_rio_floor_does_not_mark_below_target_same_dungeon_as_top():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    applicant = _app(
        score=3277,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=14,
        rio_timed_at_or_above=4,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_dungeons=[
            {"name": "Skyreach", "key_level": 14},
            {"name": "Algeth'ar Academy", "key_level": 15},
            {"name": "Maisara Caverns", "key_level": 15},
            {"name": "Pit of Saron", "key_level": 15},
            {"name": "Windrunner Spire", "key_level": 15},
            {"name": "Magisters' Terrace", "key_level": 14},
            {"name": "Nexus-Point Xenas", "key_level": 14},
            {"name": "Seat of the Triumvirate", "key_level": 14},
        ],
        dps_breakdown=[
            _dungeon("Algeth'ar Academy", [(12, 61.0, None, 1)]),
            _dungeon("Maisara Caverns", [(15, 65.0, None, 1)]),
            _dungeon("Magisters' Terrace", [(10, 12.0, None, 1)]),
            _dungeon("Nexus-Point Xenas", [(14, 62.0, None, 1)]),
            _dungeon("Seat of the Triumvirate", [(13, 61.0, None, 1)]),
        ],
    )

    fit = candidate_fit(applicant, target)

    assert fit.primary_key == 15
    assert fit.score < 85.0
    assert fit.label == ""


def test_mplus_same_dungeon_target_key_outranks_broader_below_target_profile():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    exact_target = _app(
        score=3270,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=15,
        rio_timed_at_or_above=3,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
        dps_breakdown=[
            _dungeon("Skyreach", [(15, 70.0, 60.0, 2)]),
            _dungeon("Pit of Saron", [(15, 65.0, 55.0, 2)]),
        ],
    )
    broad_but_below_target = _app(
        score=3277,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=14,
        rio_timed_at_or_above=4,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
        rio_dungeons=[
            {"name": "Skyreach", "key_level": 14},
            {"name": "Algeth'ar Academy", "key_level": 15},
            {"name": "Maisara Caverns", "key_level": 15},
            {"name": "Pit of Saron", "key_level": 15},
            {"name": "Windrunner Spire", "key_level": 15},
        ],
        dps_breakdown=[
            _dungeon("Algeth'ar Academy", [(12, 61.0, None, 1)]),
            _dungeon("Maisara Caverns", [(15, 65.0, None, 1)]),
            _dungeon("Nexus-Point Xenas", [(14, 62.0, None, 1)]),
            _dungeon("Seat of the Triumvirate", [(13, 61.0, None, 1)]),
        ],
    )

    exact_fit = candidate_fit(exact_target, target)
    broad_fit = candidate_fit(broad_but_below_target, target)

    assert exact_fit.primary_key == 15
    assert broad_fit.primary_key == 15
    assert exact_fit.score > broad_fit.score


def test_mplus_target_key_without_logs_outranks_below_target_parse_spike():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    completed_target = _app(
        score=3270,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=15,
        rio_timed_at_or_above=8,
        rio_timed_at_or_above_minus1=8,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=8,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
    )
    below_target_parse_spike = _app(
        score=3230,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=14,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=7,
        rio_timed_at_or_above_minus2=8,
        rio_completed_at_or_above_minus1=7,
        rio_dungeon_count=8,
        rio_summary_target_key=target.key_level,
        rio_dungeons=[{"name": "Skyreach", "key_level": 14}],
        dps_breakdown=[
            _dungeon("Skyreach", [(14, 92.0, None, 1)]),
            _dungeon("Pit of Saron", [(14, 71.0, None, 1)]),
            _dungeon("Maisara Caverns", [(10, 81.0, 79.0, 1)]),
        ],
    )

    target_fit = candidate_fit(completed_target, target)
    spike_fit = candidate_fit(below_target_parse_spike, target)

    assert target_fit.primary_key == 15
    assert spike_fit.primary_key == 14
    assert target_fit.score > spike_fit.score


def test_mplus_malformed_rio_completion_counts_do_not_crash():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    applicant = _app(
        score=3270,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=15,
        rio_timed_at_or_above=cast(Any, None),
        rio_timed_at_or_above_minus1=cast(Any, None),
        rio_timed_at_or_above_minus2=cast(Any, "bad"),
        rio_completed_at_or_above_minus1=cast(Any, object()),
        rio_dungeon_count=8,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
    )

    fit = candidate_fit(applicant, target)

    assert fit.context == CONTEXT_MPLUS
    assert fit.score >= 0.0


def test_mplus_duplicate_rio_dungeon_rows_score_like_single_canonical_row():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    canonical = _app(
        score=0,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=15,
        rio_dungeon_count=8,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
    )
    duplicate = _app(
        score=0,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=15,
        rio_dungeon_count=8,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15} for _ in range(8)],
    )

    assert candidate_fit(duplicate, target).score == candidate_fit(canonical, target).score
    assert candidate_fit(duplicate, target).confidence == candidate_fit(
        canonical, target
    ).confidence


def test_mplus_duplicate_rio_dungeon_rows_keep_highest_normalized_key_level():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    canonical = _app(
        score=0,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=15,
        rio_dungeon_count=8,
        rio_dungeons=[{"name": "Skyreach", "key_level": 15}],
    )
    duplicate = _app(
        score=0,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=15,
        rio_dungeon_count=8,
        rio_dungeons=[
            {"name": "Skyreach", "key_level": 14},
            {"name": " skyreach ", "key_level": 15},
        ],
    )

    assert candidate_fit(duplicate, target).score == candidate_fit(canonical, target).score
    assert candidate_fit(duplicate, target).confidence == candidate_fit(
        canonical, target
    ).confidence


def test_mplus_duplicate_good_wcl_dungeon_rows_do_not_inflate_score_or_confidence():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    canonical = _app(
        score=0,
        dps_breakdown=[_dungeon("Skyreach", [(15, 88.0, 82.0, 2)])],
    )
    duplicate = _app(
        score=0,
        dps_breakdown=[
            _dungeon("Skyreach", [(15, 88.0, 82.0, 2)]),
            _dungeon(" skyreach ", [(15, 88.0, 82.0, 2)]),
        ],
    )

    canonical_fit = candidate_fit(canonical, target)
    duplicate_fit = candidate_fit(duplicate, target)

    assert duplicate_fit.score == canonical_fit.score
    assert duplicate_fit.confidence == canonical_fit.confidence


def test_mplus_same_dungeon_distinct_wcl_key_brackets_do_not_inflate_breadth():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    single_bracket = _app(
        score=0,
        dps_breakdown=[_dungeon("Skyreach", [(16, 88.0, 82.0, 1)])],
    )
    distinct_brackets = _app(
        score=0,
        dps_breakdown=[
            _dungeon("Skyreach", [(15, 88.0, 82.0, 1), (16, 88.0, 82.0, 1)])
        ],
    )

    single_fit = candidate_fit(single_bracket, target)
    distinct_fit = candidate_fit(distinct_brackets, target)

    assert distinct_fit.score == single_fit.score
    assert distinct_fit.confidence == single_fit.confidence


def test_mplus_duplicate_bad_wcl_dungeon_rows_use_representative_penalty():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    canonical = _app(
        score=3270,
        **_rio_profile([15] * 8, target_key=15),
        dps_breakdown=[_dungeon("Skyreach", [(15, 20.0, 20.0, 1)])],
    )
    duplicate = _app(
        score=3270,
        **_rio_profile([15] * 8, target_key=15),
        dps_breakdown=[
            _dungeon("Skyreach", [(15, 12.0, 12.0, 1)]),
            _dungeon("Skyreach", [(15, 20.0, 20.0, 1)]),
        ],
    )

    assert candidate_fit(duplicate, target).score == candidate_fit(canonical, target).score


def test_mplus_mixed_duplicate_wcl_rows_use_representative_positive():
    target = _listing(key_level=15, dungeon_name="Skyreach")
    mixed = _app(
        score=3270,
        **_rio_profile([15] * 8, target_key=15),
        dps_breakdown=[
            _dungeon("Skyreach", [(15, 91.0, 88.0, 3)]),
            _dungeon("Skyreach", [(15, 10.0, 10.0, 1)]),
        ],
    )
    best_positive = _app(
        score=3270,
        **_rio_profile([15] * 8, target_key=15),
        dps_breakdown=[_dungeon("Skyreach", [(15, 91.0, 88.0, 3)])],
    )
    worst_bad = _app(
        score=3270,
        **_rio_profile([15] * 8, target_key=15),
        dps_breakdown=[_dungeon("Skyreach", [(15, 10.0, 10.0, 1)])],
    )

    mixed_fit = candidate_fit(mixed, target)

    assert mixed_fit.score == candidate_fit(best_positive, target).score
    assert mixed_fit.score > candidate_fit(worst_bad, target).score


def test_mplus_bad_wcl_ignores_hidden_worse_bracket_when_representative_row_is_good():
    target = _listing(key_level=18, dungeon_name="Mythic+", activity_id=0)
    rio_profile = _rio_profile([18, 17, 17, 17, 17, 17, 17, 17], target_key=18)
    representative_good = _app(
        score=3552,
        **rio_profile,
        dps_breakdown=[_dungeon("Skyreach", [(17, 78.0, 70.0, 1)])],
    )
    hidden_bad = _app(
        score=3552,
        **rio_profile,
        dps_breakdown=[_dungeon("Skyreach", [(18, 8.0, 8.0, 1), (17, 78.0, 70.0, 1)])],
    )

    assert candidate_fit(hidden_bad, target).score == pytest.approx(
        candidate_fit(representative_good, target).score
    )


def test_mplus_broad_rio_with_bad_representative_wcl_stays_risky_not_zero():
    target = _listing(key_level=18, dungeon_name="Mythic+", activity_id=0)
    applicant = _app(
        score=3552,
        **_rio_profile([18, 17, 17, 17, 17, 17, 17, 17], target_key=18),
        dps_breakdown=[
            _dungeon("Skyreach", [(18, 8.0, 8.0, 1)]),
            _dungeon("Magisters' Terrace", [(17, 2.0, 2.0, 1)]),
            _dungeon("Seat of the Triumvirate", [(17, 1.0, 1.0, 1)]),
            _dungeon("Pit of Saron", [(16, 9.0, 9.0, 1)]),
        ],
    )

    fit = candidate_fit(applicant, target)

    assert fit.score >= 20.0
    assert fit.score < 40.0


def test_mplus_healer_broad_rio_low_hps_is_warning_not_hard_veto():
    target = _listing(key_level=18, dungeon_name="Mythic+", activity_id=0)
    healer = _app(
        role="HEALER",
        score=3552,
        **_rio_profile([18, 17, 17, 17, 17, 17, 17, 17], target_key=18),
        hps_breakdown=[
            _dungeon("Windrunner Spire", [(18, 1.0, 1.0, 1)]),
            _dungeon("Pit of Saron", [(17, 3.0, 3.0, 1)]),
            _dungeon("Magisters' Terrace", [(17, 18.0, 18.0, 1)]),
            _dungeon("Algeth'ar Academy", [(17, 23.0, 18.0, 2)]),
        ],
    )

    fit = candidate_fit(healer, target)

    assert fit.score >= 30.0
    assert fit.score < 45.0


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
        **_rio_profile([22, 21, 20, 20, 19, 19, 18, 18], target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(22, 99.0, 95.0, 3)])],
        score=3900,
    )
    weak_friend = _app(
        **_rio_profile([12] * 8, target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(10, 99.0, 95.0, 3)])],
        score=3300,
    )
    solid_solo = _app(
        **_rio_profile([16] * 8, target_key=16),
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
        **_rio_profile([20, 20, 19, 19, 18, 18, 17, 17], target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(20, 95.0, 90.0, 3)])],
        score=3600,
    )
    weak_mplus = _app(
        **_rio_profile([12] * 8, target_key=16),
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


def test_package_fit_mplus_member_score_improvement_is_monotonic_above_carry_threshold():
    baseline = _package_score_for_member_scores((60.0, 95.0, 95.0), CONTEXT_MPLUS, 0.0)
    improved = _package_score_for_member_scores((60.0, 95.0, 100.0), CONTEXT_MPLUS, 0.0)

    assert improved >= baseline


def test_package_fit_mplus_member_score_improvement_is_monotonic_below_carry_threshold():
    baseline = _package_score_for_member_scores((12.9, 76.8), CONTEXT_MPLUS, 0.0)
    improved = _package_score_for_member_scores((12.9, 84.8), CONTEXT_MPLUS, 0.0)

    assert improved >= baseline


def test_package_fit_low_carry_floor_uses_ramp_not_hard_cliff():
    just_below = _package_score_for_member_scores(
        (47.9, 100.0, 100.0, 100.0, 100.0), CONTEXT_MPLUS, 0.0
    )
    at_floor = _package_score_for_member_scores(
        (48.0, 100.0, 100.0, 100.0, 100.0), CONTEXT_MPLUS, 0.0
    )

    assert at_floor - just_below < 0.5


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
        **_rio_profile([16] * 8, target_key=16),
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
    assert group.label == ""
    assert group.display.startswith("G2 ")


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
