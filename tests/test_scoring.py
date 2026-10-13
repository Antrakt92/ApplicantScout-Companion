from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

import applicant_scout.scoring as scoring_mod
from applicant_scout.constants import percentile_colour
from applicant_scout.scoring import (
    CONTEXT_MPLUS,
    CandidateFit,
    _mplus_timed_experience_fit,
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


def _timed_fit(applicant: Applicant, listing: Listing) -> CandidateFit:
    """Exercise the independent E component; combined output has separate tests."""
    return _mplus_timed_experience_fit(applicant, listing) or CandidateFit(
        context=CONTEXT_MPLUS, target_key=listing.key_level,
    )


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
    rio_summary_activity_id: int = 0,
    rio_summary_dungeon_name: str = "",
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
        rio_summary_activity_id=rio_summary_activity_id,
        rio_summary_dungeon_name=rio_summary_dungeon_name,
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
        (0, "#666666"),
        (24.49, "#666666"),
        (24.5, "#666666"),
        (24.51, "#1eff00"),
        (49.49, "#1eff00"),
        (49.5, "#0070ff"),
        (74.5, "#0070ff"),
        (74.51, "#a335ee"),
        (94.5, "#a335ee"),
        (94.51, "#ff8000"),
        (98.5, "#ff8000"),
        (98.51, "#e268a8"),
        (99.5, "#e5cc80"),
        (100, "#e5cc80"),
    ],
)
def test_fit_colour_matches_the_displayed_rounded_score_rarity_band(score, expected):
    assert fit_colour(score) == expected


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_fit_colour_is_missing_data(score):
    assert fit_colour(score) == "#5d5d5d"


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
def test_fit_label_helper_thresholds_do_not_define_the_estimate_palette(score, expected):
    assert fit_label(score) == expected


def test_effective_rio_score_uses_higher_of_current_and_main():
    assert effective_rio_score(_app(score=2443, main_score=3468)) == 3468
    assert effective_rio_score(_app(score=3600, main_score=3300)) == 3600
    assert effective_rio_score(_app(score=2200, main_score=0)) == 2200


def test_mplus_current_and_main_scores_do_not_manufacture_timed_history():
    target = _listing(key_level=16)
    current_only = _app(score=1200, main_score=0)
    alt_with_main = _app(score=1200, main_score=3400)

    assert not candidate_fit(alt_with_main, target).is_available
    assert not candidate_fit(current_only, target).is_available


def test_mplus_rio_fallback_ignores_lower_main_score():
    target = _listing(key_level=16)

    assert candidate_fit(_app(score=3400, main_score=1200), target).score == candidate_fit(
        _app(score=3400, main_score=0), target
    ).score


def test_mplus_timed_component_low_damage_keeps_near_target_experience_and_damage_context():
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

    fit = _timed_fit(applicant, target)

    clean = _app(score=3328, dps_breakdown=[
        _dungeon(entry["name"], [(entry["key_level"], 80, 80, entry["run_count"])])
        for entry in applicant.mplus_dps_breakdown
    ])
    assert fit.score == _timed_fit(clean, target).score
    assert not fit.is_available
    assert fit.coverage == 0.0
    assert mplus_dungeon_fit_rows(applicant, target)
    assert fit.label == ""


def test_mplus_timed_component_character_key_history_does_not_change_with_main_rating():
    target = _listing(key_level=16, dungeon_name="Windrunner Spire")
    applicant = _app(
        role="HEALER",
        score=3381,
        main_score=3596,
        dps_breakdown=[
            _dungeon("Windrunner Spire", [(16, 47.0, 47.0, 1)]),
            _dungeon("Nexus-Point Xenas", [(16, 24.0, 24.0, 1)]),
            _dungeon("Maisara Caverns", [(16, 10.0, 10.0, 1)]),
            _dungeon("Seat of the Triumvirate", [(16, 8.0, 8.0, 1)]),
            _dungeon("Algeth'ar Academy", [(15, 28.0, 14.0, 2)]),
            _dungeon("Pit of Saron", [(16, 6.0, 6.0, 1)]),
            _dungeon("Skyreach", [(16, 1.0, 1.0, 1)]),
        ],
    )

    fit = _timed_fit(applicant, target)

    applicant.main_score = 0
    assert fit.score == _timed_fit(applicant, target).score
    assert not fit.is_available
    assert not _timed_fit(applicant, target).is_available
    assert fit.label == ""


def test_mplus_timed_component_high_logged_keys_and_target_parses_do_not_prove_timed_readiness():
    target = _listing(key_level=10)
    overqualified = _app(
        dps_breakdown=[_dungeon("Skyreach", [(20, 31.0, 31.0, 1)])],
        score=3400,
    )
    farm_parse = _app(
        dps_breakdown=[_dungeon("Skyreach", [(10, 99.0, 95.0, 3)])],
        score=2600,
    )

    overqualified_fit = _timed_fit(overqualified, target)
    farm_fit = _timed_fit(farm_parse, target)

    assert not overqualified_fit.is_available
    assert not farm_fit.is_available
    assert mplus_dungeon_fit_rows(overqualified, target)[0].key_level == 20


def test_mplus_timed_component_orange_and_relevant_logged_keys_both_require_timed_confirmation():
    target = _listing(key_level=16)
    farm_parse = _app(
        dps_breakdown=[_dungeon("Skyreach", [(10, 99.0, 95.0, 3)])],
        score=3300,
    )
    relevant = _app(
        dps_breakdown=[_dungeon("Skyreach", [(16, 88.0, 78.0, 2), (20, 31.0, 31.0, 1)])],
        score=3300,
    )

    farm_fit = _timed_fit(farm_parse, target)
    relevant_fit = _timed_fit(relevant, target)

    assert not farm_fit.is_available
    assert not relevant_fit.is_available
    assert mplus_dungeon_fit_rows(relevant, target)[0].key_level == 16


def test_mplus_timed_component_same_dungeon_match_uses_activity_id_when_listing_name_is_localized():
    target = _listing(
        activity_id=512,
        key_level=16,
        dungeon_name="Королевский Покой",
    )
    applicant = _app(
        rio_dungeons=[{"name": "Kings' Rest", "key_level": 16}],
        dps_breakdown=[_dungeon("Kings' Rest", [(16, 88.0, 78.0, 2)])],
        score=3300,
    )

    fit = _timed_fit(applicant, target)

    assert fit.has_same_dungeon_context
    assert fit.same_dungeon_rio_key == 16
    assert fit.same_dungeon_wcl_key == 0
    assert mplus_dungeon_fit_rows(applicant, target)[0].best_percent == 88.0


def test_mplus_timed_component_fit_exposes_best_nearby_and_same_dungeon_key_evidence():
    target = _listing(
        activity_id=512,
        key_level=16,
        dungeon_name="Королевский Покой",
    )
    applicant = _app(
        rio_profile=True,
        rio_summary_target_key=target.key_level,
        rio_dungeons=[
            {"name": "Kings' Rest", "key_level": 15},
            {"name": "Ruby Life Pools", "key_level": 20},
        ],
        dps_breakdown=[
            _dungeon("Kings' Rest", [(14, 82.0, 74.0, 2)]),
            _dungeon("Voidscar Arena", [(18, 78.0, 70.0, 2)]),
        ],
    )

    fit = _timed_fit(applicant, target)

    assert fit.target_key == 16
    assert fit.primary_key == 20
    assert fit.best_nearby_key == 15
    assert fit.same_dungeon_rio_key == 15
    assert fit.same_dungeon_wcl_key == 0
    assert fit.same_dungeon_wcl_best is None


def test_mplus_fit_does_not_describe_far_key_as_nearby_evidence():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    applicant = _app(
        rio_dungeons=[{"name": "Pit of Saron", "key_level": 20}],
        dps_breakdown=[
            _dungeon("Pit of Saron", [(20, 85.0, 78.0, 2)]),
        ]
    )

    fit = candidate_fit(applicant, target)

    assert fit.primary_key == 20
    assert fit.best_nearby_key == 0
    assert fit.same_dungeon_rio_key == 0
    assert fit.same_dungeon_wcl_key == 0


def test_mplus_unverified_logged_key_stays_raw_context_without_timed_fit():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    applicant = _app(
        dps_breakdown=[
            _dungeon("Skyreach", [(20, 31.0, 31.0, 1)]),
        ]
    )

    fit = candidate_fit(applicant, target)

    assert not fit.is_available
    assert fit.primary_key == 0
    assert fit.best_nearby_key == 0
    raw = mplus_dungeon_fit_rows(applicant, target)[0]
    assert raw.key_level == 20
    assert raw.best_percent == 31.0
    assert raw.run_count == 1


def test_mplus_timed_component_raw_dungeon_context_can_show_target_bracket_without_timed_fit():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    applicant = _app(
        dps_breakdown=[
            _dungeon(
                "Skyreach",
                [(16, 82.0, 74.0, 2), (18, 10.0, 10.0, 1)],
            ),
        ]
    )

    fit = _timed_fit(applicant, target)

    assert not fit.is_available
    raw = mplus_dungeon_fit_rows(applicant, target)[0]
    assert raw.key_level == 16
    assert raw.best_percent == 82.0
    assert raw.median_percent == 74.0
    assert raw.run_count == 2


def test_mplus_timed_component_low_and_high_logged_brackets_do_not_create_timed_fit():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    applicant = _app(
        dps_breakdown=[
            _dungeon(
                "Skyreach",
                [(16, 10.0, 10.0, 3), (20, 99.0, 95.0, 3)],
            ),
        ]
    )

    fit = _timed_fit(applicant, target)

    assert not fit.is_available
    raw = mplus_dungeon_fit_rows(applicant, target)[0]
    assert raw.key_level == 16
    assert raw.best_percent == 10.0
    assert raw.run_count == 3


def test_mplus_timed_component_far_low_key_remains_observed_same_dungeon_history():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    rio_dungeons = [
        {"name": f"Other Dungeon {index}", "key_level": 16}
        for index in range(8)
    ]
    baseline = _app(rio_profile=True, rio_dungeons=rio_dungeons, dps_breakdown=[])
    ignored_log = _app(
        rio_profile=True,
        rio_dungeons=rio_dungeons,
        dps_breakdown=[_dungeon("Skyreach", [(10, 10.0, 10.0, 1)])],
    )

    baseline_fit = _timed_fit(baseline, target)
    ignored_fit = _timed_fit(ignored_log, target)

    assert ignored_fit.score == baseline_fit.score
    assert ignored_fit.confidence >= baseline_fit.confidence
    assert ignored_fit.same_dungeon_wcl_key == 0
    assert ignored_fit.same_dungeon_wcl_best is None
    assert ignored_fit.same_dungeon_wcl_run_count == 0
    assert baseline_fit.limit_reason == ignored_fit.limit_reason == "no_same_dungeon"


def test_mplus_timed_component_additional_unverified_logged_dungeons_cannot_create_timed_breadth():
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

    assert not _timed_fit(strong_with_more_bad, target).is_available
    assert not _timed_fit(strong_with_some_bad, target).is_available


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


def test_mplus_timed_component_timed_target_profile_is_available_without_wcl_damage():
    target = _listing(key_level=15)
    no_logs = _app(score=3270, **_rio_profile([15] * 8, target_key=15))

    fit = _timed_fit(no_logs, target)

    assert fit.source == "mplus_scorecard"
    assert fit.is_available
    assert 50.0 <= fit.score <= 58.0
    assert fit.display == f"{int(round(fit.score))} +15"
    assert fit.label == ""
    assert all(word not in fit.display for word in ("TOP", "FIT", "OK", "RISK", "RIO"))


def test_mplus_timed_component_timed_target_experience_outranks_lower_logged_key_experience():
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

    target_fit = _timed_fit(target_no_logs, target)
    lower_fit = _timed_fit(lower_with_good_logs, target)

    assert target_fit.score > lower_fit.score
    assert lower_fit.primary_key == 14


def test_mplus_timed_component_same_key_damage_logs_add_evidence_without_changing_experience():
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

    no_log_fit = _timed_fit(no_logs, target)
    one_gray_fit = _timed_fit(one_gray, target)
    repeated_gray_fit = _timed_fit(repeated_gray, target)

    assert one_gray_fit.score == no_log_fit.score == repeated_gray_fit.score
    assert no_log_fit.confidence == one_gray_fit.confidence == repeated_gray_fit.confidence
    assert one_gray_fit.same_dungeon_wcl_best is None


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


def test_mplus_timed_component_scorecard_low_key_logs_do_not_cheese_target_listing():
    target = _listing(key_level=15)
    low_log_spam = _app(
        score=3000,
        **_rio_profile([13] * 8, target_key=15),
        dps_breakdown=[
            _dungeon(name, [(13, 99.0, 95.0, 3)])
            for name in MPLUS_DUNGEONS
        ],
    )

    fit = _timed_fit(low_log_spam, target)

    assert fit.score < 35.0
    assert fit.primary_key == 13


def test_mplus_broad_target_minus_one_profile_is_ok_not_risk():
    target = _listing(key_level=17, dungeon_name="Algeth'ar Academy")
    applicant = _app(
        **_rio_profile([16, 16, 16, 16, 16, 16, 16, 15], target_key=17),
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


def test_mplus_timed_component_single_target_minus_one_parse_does_not_overpromote():
    target = _listing(key_level=17, dungeon_name="Other Dungeon")
    applicant = _app(
        score=3409,
        dps_breakdown=[_dungeon("Skyreach", [(16, 96.0, 96.0, 1)])],
    )

    fit = _timed_fit(applicant, target)

    assert fit.score < 55.0


def test_mplus_timed_component_healer_dps_context_ignores_legacy_hps_without_creating_timed_fit():
    target = _listing(key_level=16)
    healer = _app(
        role="HEALER",
        dps_breakdown=[_dungeon("Skyreach", [(16, 60.0, 50.0, 2)])],
        hps_breakdown=[_dungeon("Skyreach", [(20, 99.0, 99.0, 3)])],
        score=3300,
    )

    fit = _timed_fit(healer, target)

    assert not fit.is_available
    assert mplus_dungeon_fit_rows(healer, target)[0].key_level == 16
    assert fit.score < 80


def test_mplus_timed_component_partial_timed_rio_rows_cannot_be_upgraded_by_unverified_wcl_keys():
    target = _listing(key_level=12, dungeon_name="Mythic+", activity_id=0)
    healer = _app(
        role="HEALER",
        score=3271,
        rio_dungeons=[
            {"name": "Magisters' Terrace", "key_level": 11},
            {"name": "Maisara Caverns", "key_level": 10},
        ],
        dps_breakdown=[
            _dungeon("Algeth'ar Academy", [(12, 37.0, 37.0, 1)]),
            _dungeon("Pit of Saron", [(12, 84.0, 84.0, 1)]),
            _dungeon("Magisters' Terrace", [(10, 84.0, 84.0, 1)]),
            _dungeon("Maisara Caverns", [(10, 37.0, 37.0, 1)]),
            _dungeon("Skyreach", [(10, 100.0, 100.0, 1)]),
            _dungeon("Windrunner Spire", [(10, 55.0, 55.0, 1)]),
        ],
    )

    fit = _timed_fit(healer, target)

    assert fit.primary_key == 11
    assert fit.is_available
    assert fit.coverage == 2 / 8
    assert fit.score == _timed_fit(
        _app(role="HEALER", score=0, rio_dungeons=healer.rio_dungeons), target
    ).score


def test_mplus_timed_component_scorecard_rio_summary_rescues_missing_wcl_without_top_rating():
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

    fit = _timed_fit(applicant, target)

    assert fit.source == "mplus_scorecard"
    assert 40.0 <= fit.score < 58.0
    assert fit.confidence >= 0.55
    assert "+17" in fit.display


def test_mplus_scorecard_keeps_captured_floors_without_rebasing_them_to_new_target():
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
    stale_fit = candidate_fit(stale_summary, current_target)

    # Eight timed keys at least +10 are not eight timed keys at least +16.
    # The independently observed global timed +17 still supplies one strong key.
    assert stale_fit.is_available
    assert 0 < stale_fit.score < 10
    assert stale_fit.primary_key == 17
    assert stale_fit.best_nearby_key == 17
    assert stale_fit.dungeon_count == 8
    assert stale_fit.relevant_dungeon_count == 1
    assert stale_fit.same_dungeon_rio_key == 0


def test_mplus_timed_component_scorecard_uses_rio_dungeon_rows_for_same_dungeon_key():
    target = _listing(activity_id=512, key_level=16, dungeon_name="Королевский Покой")
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
        rio_dungeons=[{"name": "Kings' Rest", "key_level": 15}],
    )

    fit = _timed_fit(applicant, target)

    assert fit.source == "mplus_scorecard"
    assert fit.primary_key == 17
    assert fit.best_nearby_key == 17
    assert fit.same_dungeon_rio_key == 15
    assert fit.same_dungeon_wcl_key == 0
    assert "+17" in fit.display
    assert fit.confidence >= 0.55


def test_mplus_timed_component_scorecard_keeps_higher_summary_same_dungeon_key():
    target = _listing(activity_id=512, key_level=16, dungeon_name="Королевский Покой")
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
        rio_summary_activity_id=target.activity_id,
        rio_summary_dungeon_name=target.dungeon_name,
        rio_dungeons=[{"name": "Kings' Rest", "key_level": 15}],
    )

    fit = _timed_fit(applicant, target)

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
        rio_summary_activity_id=target.activity_id,
        rio_summary_dungeon_name=target.dungeon_name,
        rio_dungeons=[{"name": "Kings' Rest", "key_level": 15}],
    )
    assert fit.primary_key == 17
    assert "+17" in fit.display
    weaker_fit = _timed_fit(weaker_same_dungeon, target)
    assert fit.same_dungeon_rio_key == 16
    assert weaker_fit.same_dungeon_rio_key == 15
    # Both profiles can meet the same no-WCL ceiling; the stronger target key
    # must remain visible without creating another dungeon to bypass that cap.
    assert fit.score >= weaker_fit.score


def test_mplus_timed_component_scorecard_rio_summary_rescues_not_found_wcl_status():
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

    fit = _timed_fit(applicant, target)

    assert fit.source == "mplus_scorecard"
    assert 40.0 <= fit.score < 58.0
    assert "+17" in fit.display


def test_mplus_timed_component_terminal_wcl_status_ignores_stale_wcl_but_uses_scorecard():
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

    fit = _timed_fit(applicant, target)

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


def test_mplus_timed_component_low_damage_does_not_veto_independent_key_experience():
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
        rio_summary_target_key=16,
        dps_breakdown=[_dungeon("Skyreach", [(16, 18.0, 16.0, 3)])],
    )

    fit = _timed_fit(applicant, target)

    applicant.mplus_dps_breakdown = [_dungeon("Skyreach", [(16, 80.0, 80.0, 3)])]
    assert fit.score == _timed_fit(applicant, target).score
    assert mplus_dungeon_fit_rows(applicant, target)[0].best_percent == 80.0
    assert fit.label == ""


def test_mplus_timed_component_damage_consistency_is_separate_from_same_key_experience():
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

    spike_fit = _timed_fit(spike, target)
    steady_fit = _timed_fit(steady, target)

    assert steady_fit.score == spike_fit.score
    assert mplus_dungeon_fit_rows(spike, target)[0].median_percent == 35.0
    assert mplus_dungeon_fit_rows(steady, target)[0].median_percent == 80.0


def test_mplus_timed_component_same_key_low_damage_log_preserves_independent_rio_experience():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    no_log = _app(score=3300, **_rio_profile([16] * 8, target_key=16))
    gray_log = _app(
        score=3300,
        **_rio_profile([16] * 8, target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(16, 35.0, 35.0, 3)])],
    )

    no_log_fit = _timed_fit(no_log, target)
    gray_fit = _timed_fit(gray_log, target)

    assert gray_fit.score == no_log_fit.score
    assert gray_fit.confidence == no_log_fit.confidence
    assert mplus_dungeon_fit_rows(gray_log, target)[0].best_percent == 35.0


def test_mplus_higher_key_breadth_provides_more_experience_despite_lower_damage():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    high_gray = _app(
        **_rio_profile([20] * 8, target_key=16),
        score=0,
        dps_breakdown=[
            _dungeon(name, [(20, 35.0, 35.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )
    clean_target = _app(
        **_rio_profile([16] * 8, target_key=16),
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 55.0, 55.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )

    high_gray_fit = candidate_fit(high_gray, target)
    clean_target_fit = candidate_fit(clean_target, target)

    assert high_gray_fit.score > clean_target_fit.score
    assert high_gray_fit.confidence == clean_target_fit.confidence


def test_mplus_low_damage_does_not_rewrite_actual_logged_key():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    high_gray = _app(
        score=0,
        dps_breakdown=[
            _dungeon(name, [(20, 49.0, 49.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )

    fit = candidate_fit(high_gray, target)

    assert not fit.is_available
    assert fit.primary_key == 0
    assert mplus_dungeon_fit_rows(high_gray, target)[0].key_level == 20


@pytest.mark.parametrize("target_key", [2, 3, 4, 12, 20])
@pytest.mark.parametrize("rio_support", ["none", "lower", "same", "higher"])
@pytest.mark.parametrize("improve_all", [False, True])
def test_mplus_improving_wcl_quality_across_thresholds_never_lowers_fit(
    target_key, rio_support, improve_all
):
    target = _listing(key_level=target_key)
    rio_key = {
        "none": 0,
        "lower": max(2, target_key - 2),
        "same": target_key,
        "higher": target_key + 2,
    }[rio_support]
    rio = _rio_profile([rio_key] * 8, target_key=target_key) if rio_key else {}
    qualities = [24.9, 24.99, 25.0, 25.01, 49.9, 49.99, 50.0, 50.01]
    scores = [
        candidate_fit(
            _app(
                **rio,
                score=0,
                dps_breakdown=[
                    _dungeon(name, [(target_key, q, q, 3)])
                    for index, name in enumerate(MPLUS_DUNGEONS)
                    for q in [quality if improve_all or index == 0 else 24.99]
                ],
            ),
            target,
        ).score
        for quality in qualities
    ]

    assert all(after + 1e-9 >= before for before, after in zip(scores, scores[1:])), (
        qualities,
        scores,
    )


def test_mplus_timed_component_logged_dungeon_breadth_is_independent_of_damage_colour():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    broad_gray_target = _app(
        **_rio_profile([16] * 8, target_key=16),
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 49.0, 49.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )
    sparse_clean_target = _app(
        rio_dungeons=[{"name": "Skyreach", "key_level": 16}],
        score=0,
        dps_breakdown=[_dungeon("Skyreach", [(16, 99.0, 95.0, 3)])],
    )
    clean_target = _app(
        **_rio_profile([16] * 8, target_key=16),
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 50.0, 50.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )

    broad_gray_fit = _timed_fit(broad_gray_target, target)
    sparse_clean_fit = _timed_fit(sparse_clean_target, target)
    clean_target_fit = _timed_fit(clean_target, target)

    assert sparse_clean_fit.score < broad_gray_fit.score == clean_target_fit.score
    assert broad_gray_fit.confidence == clean_target_fit.confidence


def test_mplus_timed_component_damage_thresholds_do_not_change_experience_or_evidence_strength():
    target = _listing(key_level=16, dungeon_name="Skyreach")

    def broad_fit(percentile: float):
        applicant = _app(
            **_rio_profile([16] * 8, target_key=16),
            score=0,
            dps_breakdown=[
                _dungeon(name, [(16, percentile, percentile, 3)])
                for name in MPLUS_DUNGEONS
            ],
        )
        return _timed_fit(applicant, target)

    bad = broad_fit(24.0)
    gray_floor = broad_fit(25.0)
    near_median = broad_fit(49.0)
    clean_floor = broad_fit(50.0)

    assert bad.score == gray_floor.score == near_median.score == clean_floor.score
    assert bad.confidence == gray_floor.confidence == near_median.confidence == clean_floor.confidence
    assert bad.coverage == gray_floor.coverage == near_median.coverage == 1.0
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


def test_mplus_higher_logged_key_breadth_outranks_target_and_lower_key_breadth():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    high_gray = _app(
        **_rio_profile([19] * 8, target_key=16),
        score=0,
        dps_breakdown=[
            _dungeon(name, [(19, 49.0, 49.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )
    clean_minus_one = _app(
        **_rio_profile([15] * 8, target_key=16),
        score=0,
        dps_breakdown=[
            _dungeon(name, [(15, 50.0, 50.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )
    clean_target = _app(
        **_rio_profile([16] * 8, target_key=16),
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 50.0, 50.0, 3)]) for name in MPLUS_DUNGEONS
        ],
    )

    high_gray_fit = candidate_fit(high_gray, target)
    clean_minus_one_fit = candidate_fit(clean_minus_one, target)
    clean_target_fit = candidate_fit(clean_target, target)

    assert clean_minus_one_fit.score < clean_target_fit.score < high_gray_fit.score
    assert high_gray_fit.confidence == clean_minus_one_fit.confidence


def test_mplus_higher_unverified_wcl_bracket_cannot_change_timed_breadth():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    clean_target = _app(
        rio_dungeons=[{"name": "Skyreach", "key_level": 16}],
        score=0,
        dps_breakdown=[_dungeon("Skyreach", [(16, 80.0, 75.0, 3)])],
    )
    clean_with_gray_higher = _app(
        rio_dungeons=[{"name": "Skyreach", "key_level": 16}],
        score=0,
        dps_breakdown=[
            _dungeon("Skyreach", [(16, 80.0, 75.0, 3), (20, 49.0, 49.0, 3)])
        ],
    )

    clean_fit = candidate_fit(clean_target, target)
    gray_higher_fit = candidate_fit(clean_with_gray_higher, target)

    assert gray_higher_fit.score == clean_fit.score
    assert gray_higher_fit.coverage == clean_fit.coverage


def test_mplus_timed_component_distinct_dungeon_breadth_beats_repeated_same_dungeon_brackets():
    target = _listing(key_level=16, dungeon_name="Skyreach")
    repeated_same_dungeon = _app(
        rio_dungeons=[{"name": "Skyreach", "key_level": 18}],
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
        rio_dungeons=[{"name": name, "key_level": 16} for name in MPLUS_DUNGEONS[:3]],
        score=0,
        dps_breakdown=[
            _dungeon(name, [(16, 80.0, 76.0, 3)])
            for name in MPLUS_DUNGEONS[:3]
        ],
    )

    repeated_fit = _timed_fit(repeated_same_dungeon, target)
    broad_fit = _timed_fit(broad_profile, target)

    assert broad_fit.score > repeated_fit.score


def test_mplus_only_in_time_summary_counts_can_raise_verified_readiness():
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

    assert no_completion_fit.score == completed_fit.score < timed_fit.score
    assert no_completion_fit.dungeon_count == completed_fit.dungeon_count == 1
    assert timed_fit.dungeon_count == 8


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


def test_mplus_scalar_rating_does_not_prove_target_timed_history():
    target = _listing(key_level=16)
    rating_only = _app(score=10000, main_score=10000)
    timed_below = _app(score=0, **_rio_profile([15] * 8, target_key=16))
    timed_target = _app(score=0, **_rio_profile([16] * 8, target_key=16))
    fit = candidate_fit(rating_only, target)

    assert not fit.is_available
    assert fit.display == ""
    assert fit.colour is None
    assert candidate_fit(timed_below, target).is_available
    assert candidate_fit(timed_target, target).score > candidate_fit(timed_below, target).score


@pytest.mark.parametrize("target_key", [10, 16, 20])
def test_mplus_timed_experience_order_is_consistent_across_key_levels(target_key: int):
    target = _listing(key_level=target_key, dungeon_name="Skyreach")
    ordered = [
        _app(score=0, **_rio_profile([target_key + delta] * 8, target_key=target_key))
        for delta in (4, 2, 0, -1, -2, -3)
    ]
    fits = [candidate_fit(applicant, target) for applicant in ordered]

    assert all(fit.is_available for fit in fits)
    assert [fit.score for fit in fits] == sorted((fit.score for fit in fits), reverse=True)
    assert len({fit.score for fit in fits}) == len(fits)


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


def test_mplus_timed_component_target_key_without_logs_outranks_below_target_parse_spike():
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

    target_fit = _timed_fit(completed_target, target)
    spike_fit = _timed_fit(below_target_parse_spike, target)

    assert target_fit.primary_key == 15
    assert spike_fit.primary_key == 15
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


def test_mplus_scorecard_normalizes_rio_dungeon_rows_once(
    monkeypatch: pytest.MonkeyPatch,
):
    target = _listing(key_level=15, dungeon_name="Skyreach")
    applicant = _app(
        score=3200,
        rio_profile=True,
        rio_best_key=15,
        rio_best_dungeon_key=15,
        rio_dungeon_count=8,
        rio_dungeons=[
            {"name": "Skyreach", "key_level": 15},
            {"name": "Pit of Saron", "key_level": 14},
        ],
    )
    original = scoring_mod._mplus_rio_dungeon_key_levels
    calls = 0

    def count_normalization(value: Applicant, listing: Listing) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return original(value, listing)

    monkeypatch.setattr(
        scoring_mod,
        "_mplus_rio_dungeon_key_levels",
        count_normalization,
    )

    fit = candidate_fit(applicant, target)

    assert fit.context == CONTEXT_MPLUS
    assert calls == 1


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


@pytest.mark.parametrize("rio_key", [11, 12, 16])
def test_mplus_timed_component_overlapping_wcl_cannot_change_verified_rio_readiness(rio_key):
    target = _listing(key_level=12, activity_id=0, dungeon_name="Mythic+")
    logged = _app(score=0, dps_breakdown=[_dungeon("Skyreach", [(12, 80, 80, 3)])])
    timed_only = _app(score=0, rio_dungeons=[{"name": "Skyreach", "key_level": rio_key}])
    corroborated = _app(
        score=0, rio_dungeons=timed_only.rio_dungeons,
        dps_breakdown=logged.mplus_dps_breakdown,
    )

    assert not _timed_fit(logged, target).is_available
    assert _timed_fit(timed_only, target).is_available
    assert _timed_fit(corroborated, target).score == _timed_fit(timed_only, target).score
    assert _timed_fit(corroborated, target).coverage == _timed_fit(timed_only, target).coverage


def test_mplus_timed_component_disjoint_unverified_wcl_cannot_inflate_timed_rio_breadth():
    target = _listing(key_level=12, activity_id=0, dungeon_name="Mythic+")
    rows = [{"name": "Skyreach", "key_level": 12}]
    overlap = _app(
        score=0,
        rio_dungeons=rows,
        dps_breakdown=[_dungeon("Skyreach", [(12, 80.0, 80.0, 3)])],
    )
    distinct = _app(
        score=0,
        rio_dungeons=rows,
        dps_breakdown=[_dungeon("Algeth'ar Academy", [(12, 80.0, 80.0, 3)])],
    )

    overlap_fit = _timed_fit(overlap, target)
    distinct_fit = _timed_fit(distinct, target)

    assert overlap_fit.coverage == 1 / 8
    assert distinct_fit.coverage == 1 / 8
    assert distinct_fit.confidence == overlap_fit.confidence
    assert distinct_fit.score == overlap_fit.score


def test_mplus_anonymous_summary_does_not_duplicate_best_same_dungeon():
    target = _listing(key_level=12)
    summary: dict[str, Any] = dict(
        rio_profile=True,
        rio_summary_target_key=12,
        rio_summary_activity_id=target.activity_id,
        rio_summary_dungeon_name=target.dungeon_name,
        rio_dungeon_count=8,
        rio_best_key=12,
        rio_best_dungeon_key=12,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=1,
        rio_timed_at_or_above_minus2=1,
    )
    anonymous = _app(score=0, **summary)
    named = _app(
        score=0,
        **summary,
        rio_dungeons=[{"name": "Skyreach", "key_level": 12}],
    )

    anonymous_fit = candidate_fit(anonymous, target)
    named_fit = candidate_fit(named, target)

    assert anonymous_fit.coverage == named_fit.coverage == 1 / 8
    assert anonymous_fit.score == named_fit.score


def test_mplus_partial_rio_names_do_not_duplicate_anonymous_highest_key():
    target = _listing(key_level=12)
    summary: dict[str, Any] = dict(
        rio_profile=True,
        rio_summary_target_key=12,
        rio_summary_activity_id=target.activity_id,
        rio_summary_dungeon_name=target.dungeon_name,
        rio_dungeon_count=8,
        rio_best_key=16,
        rio_best_dungeon_key=16,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=2,
        rio_timed_at_or_above_minus2=2,
    )
    anonymous = _app(score=0, **summary)
    named = _app(
        score=0,
        **summary,
        rio_dungeons=[{"name": "Skyreach", "key_level": 16}],
    )

    anonymous_fit = candidate_fit(anonymous, target)
    named_fit = candidate_fit(named, target)

    assert anonymous_fit.coverage == named_fit.coverage == 2 / 8
    assert anonymous_fit.score == named_fit.score


@pytest.mark.parametrize("name", [None, "", "?", 42])
def test_mplus_unnamed_wcl_evidence_cannot_lift_caps_or_primary_key(name):
    target = _listing(key_level=12)
    rio: dict[str, Any] = _rio_profile([12] * 8, target_key=12)
    canonical = _app(score=0, **rio)
    invalid = _app(
        score=0,
        **rio,
        dps_breakdown=[_dungeon(cast(Any, name), [(20, 99.0, 99.0, 3)])],
    )

    assert candidate_fit(invalid, target) == candidate_fit(canonical, target)


def test_mplus_named_evidence_fusion_is_order_independent():
    target = _listing(key_level=12, activity_id=0, dungeon_name="Mythic+")
    rows = [
        {"name": "Skyreach", "key_level": 16},
        {"name": "Pit of Saron", "key_level": 11},
    ]
    logs = [
        _dungeon("Skyreach", [(12, 80.0, 80.0, 3), (15, 80.0, 80.0, 3)]),
        _dungeon("Algeth'ar Academy", [(13, 80.0, 80.0, 3)]),
    ]
    original = _app(score=0, rio_dungeons=rows, dps_breakdown=logs)
    reversed_rows = _app(
        score=0,
        rio_dungeons=list(reversed(rows)),
        dps_breakdown=list(reversed(logs)),
    )

    fit = candidate_fit(original, target)
    assert fit == candidate_fit(reversed_rows, target)
    assert fit.coverage == 2 / 8
    assert fit.primary_key == 16


def test_mplus_anonymous_summary_does_not_assume_wcl_is_another_dungeon():
    target = _listing(key_level=12)
    summary: dict[str, Any] = dict(
        rio_profile=True,
        rio_summary_target_key=12,
        rio_summary_activity_id=target.activity_id,
        rio_summary_dungeon_name=target.dungeon_name,
        rio_dungeon_count=8,
        rio_best_key=12,
        rio_best_dungeon_key=12,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=1,
        rio_timed_at_or_above_minus2=1,
    )
    logs = [_dungeon("Skyreach", [(12, 80.0, 80.0, 3)])]
    anonymous = _app(score=0, **summary, dps_breakdown=logs)
    named = _app(
        score=0,
        **summary,
        rio_dungeons=[{"name": "Skyreach", "key_level": 12}],
        dps_breakdown=logs,
    )

    assert candidate_fit(anonymous, target) == candidate_fit(named, target)


@pytest.mark.parametrize(
    ("dungeon_name", "activity_id", "row_name"),
    [("Skyreach", 0, "Skyreach"), ("Королевский Покой", 512, "Kings' Rest")],
)
def test_mplus_specific_summary_retains_identity_without_redundant_rows(
    dungeon_name, activity_id, row_name
):
    target = _listing(key_level=12, activity_id=activity_id, dungeon_name=dungeon_name)
    summary: dict[str, Any] = dict(
        rio_profile=True,
        rio_summary_target_key=12,
        rio_summary_activity_id=target.activity_id,
        rio_summary_dungeon_name=target.dungeon_name,
        rio_dungeon_count=8,
        rio_best_key=12,
        rio_best_dungeon_key=12,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=1,
        rio_timed_at_or_above_minus2=1,
    )
    logs = [_dungeon("Pit of Saron", [(12, 80.0, 80.0, 3)])]
    summary_only = _app(score=0, **summary, dps_breakdown=logs)
    named = _app(
        score=0,
        **summary,
        rio_dungeons=[{"name": row_name, "key_level": 12}],
        dps_breakdown=logs,
    )
    localized = _app(
        score=0,
        **summary,
        rio_dungeons=[
            {"name": row_name, "key_level": 12},
            {"name": dungeon_name, "key_level": 12},
        ],
        dps_breakdown=logs,
    )

    summary_fit = candidate_fit(summary_only, target)
    assert summary_fit == candidate_fit(named, target)
    assert summary_fit == candidate_fit(localized, target)
    assert summary_fit.coverage == 1 / 8


@pytest.mark.parametrize("dungeon_name", ["Mythic+", "?", "??", "---"])
def test_mplus_generic_listing_keeps_summary_identity_unknown(dungeon_name):
    target = _listing(key_level=12, activity_id=0, dungeon_name=dungeon_name)
    applicant = _app(
        score=0,
        rio_profile=True,
        rio_summary_target_key=12,
        rio_dungeon_count=8,
        rio_best_key=12,
        rio_best_dungeon_key=12,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=1,
        rio_timed_at_or_above_minus2=1,
        dps_breakdown=[_dungeon("Pit of Saron", [(12, 80.0, 80.0, 3)])],
    )

    fit = candidate_fit(applicant, target)

    assert not fit.has_same_dungeon_context
    assert fit.coverage == 1 / 8


@pytest.mark.parametrize("rio_key", [2, 10, 11, 12, 16])
@pytest.mark.parametrize("same_dungeons", [True, False])
def test_mplus_timed_component_verified_rio_readiness_is_independent_of_unverified_logs(rio_key, same_dungeons):
    target = _listing(key_level=12, activity_id=0, dungeon_name="Mythic+")
    logs = [_dungeon(name, [(12, 40.0, 40.0, 3)]) for name in MPLUS_DUNGEONS[:4]]
    logged = _app(score=0, dps_breakdown=logs)
    rio_names = MPLUS_DUNGEONS[:4] if same_dungeons else MPLUS_DUNGEONS[4:]
    rows = [{"name": name, "key_level": rio_key} for name in rio_names]
    with_rio = _app(score=0, dps_breakdown=logs, rio_dungeons=rows)
    duplicated = _app(score=0, dps_breakdown=logs, rio_dungeons=rows + rows)

    logged_fit = _timed_fit(logged, target)
    with_rio_fit = _timed_fit(with_rio, target)

    assert not logged_fit.is_available
    assert with_rio_fit.is_available
    assert with_rio_fit.score == _timed_fit(_app(score=0, rio_dungeons=rows), target).score
    assert _timed_fit(duplicated, target) == with_rio_fit


def test_mplus_higher_unverified_wcl_key_cannot_change_verified_rio_experience():
    target = _listing(key_level=12, activity_id=0, dungeon_name="Mythic+")
    clean = _app(
        rio_dungeons=[{"name": "Skyreach", "key_level": 12}],
        score=0,
        dps_breakdown=[_dungeon("Skyreach", [(12, 80.0, 80.0, 3)])],
    )
    with_weaker = _app(
        rio_dungeons=[{"name": "Skyreach", "key_level": 12}],
        score=0,
        dps_breakdown=[
            _dungeon("Skyreach", [(12, 80.0, 80.0, 3), (14, 49.0, 49.0, 3)])
        ],
    )

    clean_fit = candidate_fit(clean, target)
    weaker_fit = candidate_fit(with_weaker, target)
    assert weaker_fit.score == clean_fit.score
    assert weaker_fit.primary_key == clean_fit.primary_key == 12
    assert weaker_fit.coverage == clean_fit.coverage
    assert weaker_fit.confidence == clean_fit.confidence


@pytest.mark.parametrize("role", ["DAMAGER", "HEALER", "TANK"])
def test_mplus_timed_component_single_damage_log_does_not_prove_timed_readiness_for_any_role(role):
    target = _listing(key_level=16)
    applicant = _app(
        role=role,
        score=0,
        dps_breakdown=[_dungeon("Skyreach", [(16, 20.0, 20.0, 3)])],
    )

    fit = _timed_fit(applicant, target)

    positive_damage = _app(
        role=role, score=0,
        dps_breakdown=[_dungeon("Skyreach", [(16, 80.0, 80.0, 3)])],
    )
    assert not fit.is_available
    assert not _timed_fit(positive_damage, target).is_available
    assert fit.score == _timed_fit(positive_damage, target).score
    assert fit.coverage == 0
    assert mplus_dungeon_fit_rows(applicant, target)[0].best_percent == 20.0


def test_mplus_timed_component_anonymous_timed_summary_is_independent_of_unverified_wcl():
    target = _listing(key_level=12, activity_id=0, dungeon_name="Mythic+")
    logs = [_dungeon(name, [(12, 40.0, 40.0, 3)]) for name in MPLUS_DUNGEONS[:4]]
    logged = _app(score=0, dps_breakdown=logs)
    anonymous = _app(
        score=0,
        rio_profile=True,
        rio_summary_target_key=12,
        rio_dungeon_count=8,
        rio_best_key=10,
        rio_timed_at_or_above_minus2=4,
        dps_breakdown=logs,
    )

    assert not _timed_fit(logged, target).is_available
    timed_only = _app(
        score=0, rio_profile=True, rio_summary_target_key=12,
        rio_dungeon_count=8, rio_best_key=10, rio_timed_at_or_above_minus2=4,
    )
    assert _timed_fit(anonymous, target).is_available
    assert _timed_fit(anonymous, target).score == _timed_fit(timed_only, target).score


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


def test_mplus_timed_component_duplicate_low_damage_wcl_rows_do_not_duplicate_experience():
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

    assert _timed_fit(duplicate, target).score == _timed_fit(canonical, target).score


def test_mplus_timed_component_mixed_duplicate_wcl_rows_preserve_experience_and_raw_damage():
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

    mixed_fit = _timed_fit(mixed, target)

    assert mixed_fit.score == _timed_fit(best_positive, target).score
    assert mixed_fit.score == _timed_fit(worst_bad, target).score
    assert {row.best_percent for row in mplus_dungeon_fit_rows(mixed, target)} == {91, 10}
    assert mplus_dungeon_fit_rows(worst_bad, target)[0].best_percent == 10.0


def test_mplus_damage_quality_does_not_change_independent_rio_key_experience():
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


def test_mplus_timed_component_broad_rio_experience_is_not_reduced_by_low_damage_logs():
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

    fit = _timed_fit(applicant, target)

    applicant.mplus_dps_breakdown = []
    assert fit.is_available
    assert fit.score == _timed_fit(applicant, target).score


def test_mplus_timed_component_healer_damage_stays_separate_from_key_experience():
    target = _listing(key_level=18, dungeon_name="Mythic+", activity_id=0)
    healer = _app(
        role="HEALER",
        score=3552,
        **_rio_profile([18, 17, 17, 17, 17, 17, 17, 17], target_key=18),
        dps_breakdown=[
            _dungeon("Windrunner Spire", [(18, 1.0, 1.0, 1)]),
            _dungeon("Pit of Saron", [(17, 3.0, 3.0, 1)]),
            _dungeon("Magisters' Terrace", [(17, 18.0, 18.0, 1)]),
            _dungeon("Algeth'ar Academy", [(17, 23.0, 18.0, 2)]),
        ],
    )

    fit = _timed_fit(healer, target)

    healer.mplus_dps_breakdown = [
        _dungeon(entry["name"], [(entry["key_level"], 80, 80, entry["run_count"])])
        for entry in healer.mplus_dps_breakdown
    ]
    assert fit.score == _timed_fit(healer, target).score


def test_package_fit_penalizes_weak_member_in_multi_member_group():
    target = _listing(key_level=16)
    strong = _app(
        **_rio_profile([20] * 8, target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(20, 92.0, 88.0, 3)])],
        score=3600,
    )
    weak = _app(
        **_rio_profile([10] * 8, target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(10, 99.0, 95.0, 3)])],
        score=3300,
    )

    group = package_fit([strong, weak], target)
    solo = package_fit([strong], target)

    assert group.score < solo.score
    assert group.low_score < group.high_score
    assert group.display.startswith("G2 ")


def test_package_fit_solo_uses_individual_score_without_group_display():
    target = _listing(key_level=16)
    applicant = _app(
        **_rio_profile([16] * 8, target_key=16),
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
    )

    solo = package_fit([applicant], target)
    fit = candidate_fit(applicant, target)

    assert solo.score == fit.score
    assert solo.display == ""
    assert solo.member_scores == (fit.score,)
    assert solo.member_fits == (fit,)


def test_package_fit_unknown_context_keeps_member_stats_consistent():
    group = package_fit(
        [
            _app(role="TANK", score=3600, main_score=1200),
            _app(role="HEALER", score=2400, main_score=3100),
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
    assert (group.tank_count, group.healer_count, group.dps_count) == (1, 1, 1)


def test_package_fit_solid_group_has_no_flat_size_penalty():
    target = _listing(key_level=16)
    solid = [
        _app(
            **_rio_profile([16] * 8, target_key=16),
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
        **_rio_profile([22] * 8, target_key=10),
        dps_breakdown=[_dungeon("Skyreach", [(22, 95.0, 90.0, 3)])],
        score=3900,
    )
    normal_friend = _app(
        **_rio_profile([10] * 8, target_key=10),
        dps_breakdown=[_dungeon("Skyreach", [(10, 62.0, 56.0, 3)])],
        score=2800,
    )
    deadweight = _app(
        **_rio_profile([4] * 8, target_key=10),
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


def test_package_fit_uses_available_independent_evidence_without_access_penalty():
    target = _listing(key_level=16)
    rio = _rio_profile([16] * 8, target_key=16)
    ready = _app(
        **rio,
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
    )
    loading = _app(
        **rio,
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
        fetch_status="loading",
    )
    error = _app(
        **rio,
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
        fetch_status="error",
    )
    not_found = _app(
        **rio,
        dps_breakdown=[_dungeon("Skyreach", [(16, 82.0, 74.0, 3)])],
        score=3300,
        fetch_status="not_found",
    )

    loading_group = package_fit([ready, loading], target)
    error_group = package_fit([ready, error, not_found], target)

    assert loading_group.status_penalty == 0.0
    assert loading_group.confidence == package_fit([ready, ready], target).confidence
    assert error_group.status_penalty == 0.0
    assert error_group.is_complete
    # Missing logs retain the independently observed E component. They must
    # match an otherwise identical no-log profile, not a profile with usable P.
    missing_error_logs = replace(error, fetch_status="ready", mplus_dps_breakdown=[])
    missing_not_found_logs = replace(not_found, fetch_status="ready", mplus_dps_breakdown=[])
    assert error_group.score == pytest.approx(
        package_fit([ready, missing_error_logs, missing_not_found_logs], target).score
    )
    assert [fit.source for fit in error_group.member_fits] == [
        "mplus_combined", "mplus_timed_only", "mplus_timed_only",
    ]
    assert len({fit.timed_experience_score for fit in error_group.member_fits}) == 1


def test_package_fit_exposes_role_composition_and_incomplete_member_counts():
    target = _listing(key_level=16)
    members = [
        _app(role="TANK", score=0, fetch_status="ready"),
        _app(role="HEALER", score=0, fetch_status="pending"),
        _app(role="DAMAGER", score=0, fetch_status="loading"),
        _app(role="DAMAGER", score=0, fetch_status="error"),
        _app(role="UNKNOWN", score=0, fetch_status="not_found"),
        _app(role="DAMAGER", score=0, fetch_status="restricted"),
    ]

    group = package_fit(members, target)

    assert group.size == 6
    assert (group.tank_count, group.healer_count, group.dps_count) == (1, 1, 3)
    assert group.unknown_role_count == 1
    assert group.loading_count == 2
    assert group.error_count == 1
    assert group.not_found_count == 1
    assert group.restricted_count == 1
    assert (
        group.tank_count
        + group.healer_count
        + group.dps_count
        + group.unknown_role_count
        == group.size
    )
    assert not group.is_complete
    assert group.available_count == 0
    assert group.unavailable_count == 6
    assert group.display == "G6 unknown"


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

    rating_only = candidate_fit(_app(score=3300, fetch_status="error"), target)
    assert group.member_scores[1] == rating_only.score
    assert group.member_fits[1].primary_key == 0
    assert not group.member_fits[1].is_available
    assert not group.is_complete
    assert group.display == "G2 partial"
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


def test_raid_does_not_invent_performance_from_current_or_main_mplus_rating():
    target = _listing(category_id=3, difficulty_id=15)
    current_only = _app(score=1200, main_score=0)
    alt_with_main = _app(score=1200, main_score=3400)

    assert not candidate_fit(alt_with_main, target).is_available
    assert not candidate_fit(current_only, target).is_available
    assert candidate_fit(alt_with_main, target).display == ""


def test_raid_heroic_uses_mythic_as_higher_difficulty_fallback():
    target = _listing(category_id=3, difficulty_id=15)
    mythic = _app(raid_mythic=90.0, raid_mythic_median=80.0, score=2500)

    fit = candidate_fit(mythic, target)

    assert fit.context == CONTEXT_RAID
    assert fit.source == "raid_higher_fallback"
    assert fit.score > 70


def test_mplus_display_score_guards_non_finite_input():
    assert scoring_mod._mplus_display_score(float("nan")) == 0.0
    assert scoring_mod._mplus_display_score(float("inf")) == 0.0
    assert scoring_mod._mplus_display_score(float("-inf")) == 0.0


def test_effective_rio_score_clamps_negative_scores():
    assert effective_rio_score(_app(score=-5, main_score=-10)) == 0
    assert effective_rio_score(_app(score=-5, main_score=1200)) == 1200
    assert effective_rio_score(_app(score=1200, main_score=-5)) == 1200
