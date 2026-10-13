"""M+ Fit combines comparable logged output with separate timed evidence."""

from dataclasses import replace

import pytest

from applicant_scout import scoring
from applicant_scout.constants import MPLUS_ENCOUNTERS, mplus_dungeon_name_for_activity_id
from applicant_scout.state import Applicant, Listing


NAMES = [name for _alias, _encounter, name in MPLUS_ENCOUNTERS]


def _listing(key: int = 11) -> Listing:
    return Listing(0, NAMES[0], "", "", key_level=key, category_id=2)


def _profile(output: float | None, *, timed: bool = True, logged_key: int = 11) -> Applicant:
    keys = [13, 12, 12, 12, 14, 12, 12, 13]
    return Applicant(
        "synthetic", "Synthetic-Realm", "MAGE", 64, 319, 3044, "DAMAGER",
        fetch_status="ready",
        rio_dungeons=[dict(name=name, key_level=key, timed=True)
                      for name, key in zip(NAMES, keys, strict=True)] if timed else [],
        mplus_dps=output,
        mplus_dps_breakdown=[dict(
            name=name, key_level=logged_key, parse_percent=output,
            median_percent=output, run_count=3,
        ) for name in NAMES] if output is not None else [],
    )


def test_identical_timed_histories_are_no_longer_insensitive_to_logged_output():
    poor = scoring.candidate_fit(_profile(10), _listing())
    strong = scoring.candidate_fit(_profile(90), _listing())
    assert poor.score < strong.score
    assert poor.timed_experience_score == pytest.approx(74.83333333333334)
    assert strong.timed_experience_score == poor.timed_experience_score
    assert poor.wcl_score == 10
    assert strong.wcl_score == 90
    assert poor.score == pytest.approx(10 + .20 * (74.83333333333334 / 89) * 90)
    assert strong.score == pytest.approx(90 + .20 * (74.83333333333334 / 89) * 10)


def test_observed_zero_output_stays_known_and_cannot_be_erased_by_solid_timed_keys():
    fit = scoring.candidate_fit(_profile(0), _listing())
    assert fit.is_available
    assert fit.wcl_score == 0
    assert fit.score == pytest.approx(.20 * (74.83333333333334 / 89) * 100)
    assert fit.score < 20


def test_missing_nearby_logs_preserve_explicit_timed_only_fallback():
    fit = scoring.candidate_fit(_profile(None), _listing())
    assert fit.is_available
    assert fit.source == "mplus_timed_only"
    assert fit.score == fit.timed_experience_score == pytest.approx(74.83333333333334)
    assert fit.wcl_score is None
    assert fit.wcl_dungeon_count == 0
    assert fit.limit_reason == scoring.MPLUS_LIMIT_NO_RELEVANT_WCL
    assert fit.dungeon_count == fit.relevant_dungeon_count == 8


def test_logged_output_without_timer_history_has_explicit_output_only_source():
    fit = scoring.candidate_fit(_profile(70, timed=False), _listing())
    assert fit.is_available
    assert fit.source == "mplus_wcl_only"
    assert fit.score == fit.wcl_score == 70
    assert fit.timed_experience_score is None
    assert fit.dungeon_count == fit.relevant_dungeon_count == fit.primary_key == 0
    assert fit.wcl_dungeon_count == 8
    assert (fit.wcl_key_min, fit.wcl_key_max) == (11, 11)
    assert fit.wcl_run_count == 24
    assert "unknown" in fit.evidence_note.lower()


def test_neither_source_is_unknown_and_general_rating_is_not_a_substitute():
    fit = scoring.candidate_fit(_profile(None, timed=False), _listing())
    assert not fit.is_available
    assert fit.wcl_score is None and fit.timed_experience_score is None


def test_easy_logged_output_is_visible_but_not_comparable_to_requested_key():
    profile = _profile(99, timed=False, logged_key=2)
    assert not scoring.candidate_fit(profile, _listing(11)).is_available
    low_target = scoring.candidate_fit(profile, _listing(2))
    assert low_target.is_available
    assert low_target.wcl_score == 99
    assert low_target.wcl_key_min == low_target.wcl_key_max == 2


@pytest.mark.parametrize("status", ["pending", "loading", "error", "restricted", "not_found"])
def test_missing_wcl_access_is_not_a_timed_readiness_penalty(status):
    fit = scoring.candidate_fit(replace(_profile(0), fetch_status=status), _listing())
    assert fit.is_available
    assert fit.source == "mplus_timed_only"
    assert fit.score == pytest.approx(74.83333333333334)
    assert fit.wcl_score is None


def test_unavailable_wcl_scope_cannot_contribute_stale_output():
    fit = scoring.candidate_fit(
        replace(_profile(99), wcl_availability={"mplus": "restricted"}), _listing(),
    )
    assert fit.source == "mplus_timed_only"
    assert fit.wcl_score is None


def test_unknown_applying_spec_cannot_supply_a_logged_output_component():
    fit = scoring.candidate_fit(replace(_profile(99), spec_id=0), _listing())
    assert fit.source == "mplus_timed_only"
    assert fit.wcl_score is None


def test_untimed_summary_changes_neither_output_only_score_nor_timer_availability():
    base = _profile(70, timed=False)
    augmented = replace(
        base, rio_profile=True, rio_completed_at_or_above_minus1=8,
        rio_summary_target_key=11, rio_dungeon_count=8,
    )
    before = scoring.candidate_fit(base, _listing())
    after = scoring.candidate_fit(augmented, _listing())
    assert before.score == after.score == 70
    assert before.timed_experience_score is None and after.timed_experience_score is None
    assert after.dungeon_count == 0


def test_unknown_repeat_counts_do_not_become_an_invented_total_sample():
    profile = _profile(70, timed=False)
    profile.mplus_dps_breakdown[0]["run_count"] = 0
    fit = scoring.candidate_fit(profile, _listing())
    assert fit.is_available and fit.wcl_dungeon_count == 8
    assert fit.wcl_run_count is None


def test_selected_same_dungeon_output_has_separate_logged_and_timed_keys():
    profile = _profile(70, logged_key=10)
    fit = scoring.candidate_fit(profile, _listing())
    assert fit.source == "mplus_combined"
    assert fit.same_dungeon_rio_key == 13
    assert fit.same_dungeon_wcl_key == 10
    assert fit.same_dungeon_wcl_best == fit.same_dungeon_wcl_median == 70
    assert fit.same_dungeon_wcl_run_count == 3
    assert fit.primary_key == 14


def test_localized_target_uses_known_activity_for_both_source_identities():
    dungeon = mplus_dungeon_name_for_activity_id(512)
    assert dungeon is not None
    profile = _profile(70, timed=False)
    profile.rio_dungeons = [dict(name=dungeon, key_level=13, timed=True)]
    profile.mplus_dps_breakdown = [dict(
        name=dungeon, key_level=10, parse_percent=70, median_percent=60, run_count=3,
    )]
    target = replace(_listing(), activity_id=512, dungeon_name="Королевский Покой")
    fit = scoring.candidate_fit(profile, target)
    assert fit.same_dungeon_rio_key == 13
    assert fit.same_dungeon_wcl_key == 10
    assert fit.same_dungeon_wcl_best == 70


def test_combined_target_order_changes_with_relevant_output_even_if_timing_matches():
    poor = _profile(10)
    strong = replace(_profile(90), applicant_id="second")
    assert scoring.package_fit([strong], _listing()).score > scoring.package_fit([poor], _listing()).score
    assert scoring.candidate_fit(strong, _listing()).timed_experience_score == (
        scoring.candidate_fit(poor, _listing()).timed_experience_score
    )


@pytest.mark.parametrize("key", [0, 1, True, False, None, "invalid", 1.0, float("nan"), 64])
def test_unknown_or_invalid_logged_level_cannot_establish_target_two_output(key):
    profile = _profile(80, timed=False)
    profile.mplus_dps_breakdown = [dict(
        name=NAMES[0], key_level=key, parse_percent=80, median_percent=70, run_count=3,
    )]
    target = _listing(2)
    fit = scoring.candidate_fit(profile, target)
    assert not fit.is_available
    assert fit.wcl_score is None
    assert fit.wcl_dungeon_count == 0
    # Unknown levels can still appear in the raw evidence view.
    assert scoring.mplus_performance_rows(profile, target)


def test_missing_logged_level_stays_unknown_without_revoking_timed_only_fit():
    profile = _profile(80, timed=True)
    profile.mplus_dps_breakdown = [dict(
        name=NAMES[0], parse_percent=80, median_percent=70, run_count=3,
    )]
    target = _listing(2)
    fit = scoring.candidate_fit(profile, target)
    assert fit.is_available
    assert fit.source == "mplus_timed_only"
    assert fit.wcl_score is None and fit.wcl_dungeon_count == 0
    assert fit.score == fit.timed_experience_score
    assert fit.dungeon_count == 8


def test_valid_logged_minimum_two_remains_eligible_without_timer_history():
    fit = scoring.candidate_fit(_profile(80, timed=False, logged_key=2), _listing(2))
    assert fit.is_available
    assert fit.source == "mplus_wcl_only"
    assert fit.wcl_dungeon_count == 8
    assert (fit.wcl_key_min, fit.wcl_key_max) == (2, 2)
