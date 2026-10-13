"""Independent behavioral boundaries for output plus verified timed experience."""

from copy import deepcopy
from dataclasses import replace

import pytest

from applicant_scout.constants import MPLUS_ENCOUNTERS
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.scoring import candidate_fit, package_fit
from applicant_scout.state import Applicant, Listing


_NAMES = tuple(name for _alias, _encounter, name in MPLUS_ENCOUNTERS)
_PREFS = MetricPreferences(
    mplus=True, raid_normal=False, raid_heroic=False, raid_mythic=False,
)
_LEVELS = (12, 12, 12, 12, 12, 13, 13, 14)
_ROLE_SPECS = {"DAMAGER": ("MAGE", 63), "TANK": ("WARRIOR", 73), "HEALER": ("PRIEST", 257)}


def _listing(target: int = 11) -> Listing:
    return Listing(0, _NAMES[0], "Target", "", key_level=target, category_id=2)


def _row(name: str, value: float, *, key: int = 11, runs: int = 3) -> dict:
    return dict(
        name=name, key_level=key, parse_percent=value,
        median_percent=value if runs else None, run_count=runs,
    )


def _app(
    *, output: float | None = None, levels: tuple[int, ...] = _LEVELS,
    key: int = 11, role: str = "DAMAGER", aid: str = "1:1",
) -> Applicant:
    rows = [_row(name, output, key=key) for name in _NAMES] if output is not None else []
    cls, spec_id = _ROLE_SPECS[role]
    return Applicant(
        aid, f"Synthetic{aid.replace(':', '')}-Realm", cls, spec_id, 700, 0, role,
        rio_dungeons=[dict(name=name, key_level=level, timed=True)
                      for name, level in zip(_NAMES, levels, strict=False)],
        mplus_dps=output, mplus_dps_median=output, mplus_dps_breakdown=rows,
        fetch_status="ready", wcl_metric_preferences=_PREFS,
        wcl_availability={"mplus": "available"},
    )


@pytest.mark.parametrize("role", ["DAMAGER", "TANK", "HEALER"])
def test_same_timed_history_orders_repeated_strong_output_above_weak_output(role):
    low = candidate_fit(_app(output=10, role=role), _listing())
    high = candidate_fit(_app(output=90, role=role), _listing())

    assert low.score < high.score
    assert low.source == high.source == "mplus_combined"
    assert low.timed_experience_score == high.timed_experience_score == pytest.approx(74 + 5 / 6)
    assert (low.wcl_score, high.wcl_score) == (10, 90)
    assert (low.dungeon_count, high.dungeon_count) == (8, 8)
    assert (low.primary_key, high.primary_key) == (14, 14)


@pytest.mark.parametrize("output,expected", [(0, 20), (10, 28), (20, 36), (53, 62.4), (90, 92), (100, 100)])
def test_maximum_timed_experience_is_bounded_support_for_observed_output(output, expected):
    fit = candidate_fit(_app(output=output, levels=(15,) * 8), _listing())

    assert fit.wcl_score == output
    assert fit.timed_experience_score == 89
    assert fit.score == pytest.approx(expected)
    assert output <= fit.score <= output + 0.20 * (100 - output)
    if output <= 20:
        assert fit.score < 50  # Strong timer history cannot conceal repeated weak output.


@pytest.mark.parametrize("levels", [(), (2,) * 8, _LEVELS, (15,) * 8])
def test_relevant_output_improvement_has_at_least_point_eight_slope(levels):
    values = (0, 1, 24, 25, 49, 50, 74, 75, 99, 100)
    fits = [candidate_fit(_app(output=value, levels=levels), _listing()) for value in values]

    assert all(fit.is_available for fit in fits)
    for left, right, low, high in zip(fits, fits[1:], values, values[1:], strict=False):
        assert right.score - left.score >= 0.8 * (high - low) - 1e-10
        assert right.score >= left.score


@pytest.mark.parametrize("output", [0, 10, 50, 90, 100])
def test_improving_verified_timed_experience_never_reduces_fixed_output_fit(output):
    profiles = ((2,) * 8, (10,) * 8, (11,) * 8, _LEVELS, (15,) * 8)
    fits = [candidate_fit(_app(output=output, levels=levels), _listing()) for levels in profiles]

    assert all(fit.wcl_score == output for fit in fits)
    for left, right in zip(fits, fits[1:], strict=False):
        assert right.score >= left.score
        assert right.timed_experience_score is not None
        assert left.timed_experience_score is not None
        assert right.timed_experience_score >= left.timed_experience_score


def test_logged_output_with_unknown_timing_never_manufactures_timed_key_facts():
    fit = candidate_fit(_app(output=80, levels=()), _listing())

    assert fit.is_available and fit.source == "mplus_wcl_only"
    assert fit.score == fit.wcl_score == 80
    assert fit.timed_experience_score is None
    assert fit.dungeon_count == fit.relevant_dungeon_count == fit.primary_key == 0
    assert fit.same_dungeon_rio_key == 0
    assert (fit.wcl_dungeon_count, fit.wcl_key_min, fit.wcl_key_max, fit.wcl_run_count) == (8, 11, 11, 24)


@pytest.mark.parametrize("untimed_marker", [
    {"timed": False}, {"chests": 0}, {"upgrades": 0},
])
def test_explicit_untimed_rio_rows_cannot_add_experience_to_wcl_output(untimed_marker):
    app = _app(output=80, levels=())
    app.rio_dungeons = [dict(name=name, key_level=40, **untimed_marker) for name in _NAMES]
    app.rio_profile = True
    app.rio_summary_target_key = 11
    app.rio_dungeon_count = 8
    app.rio_completed_at_or_above_minus1 = 8
    fit = candidate_fit(app, _listing())

    assert fit.score == fit.wcl_score == 80
    assert fit.timed_experience_score is None
    assert fit.dungeon_count == fit.relevant_dungeon_count == fit.primary_key == 0
    assert fit.source == "mplus_wcl_only"


def test_completed_only_summary_and_scalar_rating_cannot_create_either_component():
    app = replace(
        _app(levels=()), score=9999, main_score=9999, rio_profile=True,
        rio_summary_target_key=11, rio_dungeon_count=8,
        rio_completed_at_or_above_minus1=8,
    )
    fit = candidate_fit(app, _listing())

    assert not fit.is_available
    assert fit.wcl_score is fit.timed_experience_score is None
    assert fit.dungeon_count == fit.primary_key == 0


@pytest.mark.parametrize("status", ["pending", "loading", "error", "restricted", "not_found"])
def test_unavailable_fetch_preserves_timed_only_estimate_without_stale_output(status):
    baseline = candidate_fit(_app(), _listing())
    app = replace(_app(output=100), fetch_status=status)
    fit = candidate_fit(app, _listing())

    assert fit.is_available and fit.source == "mplus_timed_only"
    assert fit.score == fit.timed_experience_score == baseline.score
    assert fit.wcl_score is None
    assert fit.wcl_dungeon_count == fit.wcl_key_min == fit.wcl_key_max == 0


@pytest.mark.parametrize("state", ["restricted", "disabled", "error", "empty", "unavailable"])
def test_unusable_wcl_scope_does_not_apply_cached_output_or_penalize_missing_data(state):
    baseline = candidate_fit(_app(), _listing())
    fit = candidate_fit(replace(_app(output=100), wcl_availability={"mplus": state}), _listing())

    assert fit.is_available and fit.source == "mplus_timed_only"
    assert fit.score == fit.timed_experience_score == baseline.score
    assert fit.wcl_score is None


def test_disabled_output_preference_preserves_verified_timed_component():
    baseline = candidate_fit(_app(), _listing())
    fit = candidate_fit(replace(_app(output=0), wcl_metric_preferences=replace(_PREFS, mplus=False)), _listing())

    assert fit.score == baseline.score
    assert fit.wcl_score is None and fit.source == "mplus_timed_only"


@pytest.mark.parametrize("spec_id", [0, -1, 999999, True])
def test_unknown_applying_spec_excludes_old_output_without_losing_timed_history(spec_id):
    baseline = candidate_fit(_app(), _listing())
    fit = candidate_fit(replace(_app(output=100), spec_id=spec_id), _listing())

    assert fit.score == baseline.score
    assert fit.wcl_score is None and fit.source == "mplus_timed_only"


@pytest.mark.parametrize("key", [7, 8, 14, 18])
@pytest.mark.parametrize("output", [0, 100])
def test_far_logged_brackets_are_neutral_for_timed_fallback_and_supply_no_output(key, output):
    baseline = candidate_fit(_app(), _listing())
    fit = candidate_fit(_app(output=output, key=key), _listing())

    assert fit.score == fit.timed_experience_score == baseline.score
    assert fit.wcl_score is None and fit.source == "mplus_timed_only"
    assert fit.wcl_dungeon_count == 0


def test_same_nearby_output_is_not_displaced_by_new_far_high_or_low_brackets():
    app = _app(output=70)
    expected = candidate_fit(app, _listing())
    for entry in app.mplus_dps_breakdown:
        near = {key: value for key, value in entry.items() if key != "name"}
        entry["brackets"] = [near, dict(key_level=18, parse_percent=0, median_percent=0, run_count=12),
                             dict(key_level=7, parse_percent=100, median_percent=100, run_count=12)]
    assert candidate_fit(app, _listing()) == expected


def test_duplicate_normalized_dungeon_does_not_add_weight_or_repeat_samples():
    app = _app(levels=())
    app.mplus_dps_breakdown = [_row(_NAMES[0], 95), _row(_NAMES[1], 5)]
    expected = candidate_fit(app, _listing())
    duplicate = replace(app, mplus_dps_breakdown=[
        _row(_NAMES[0], 95), _row(_NAMES[0].replace(" ", "-"), 95), _row(_NAMES[1], 5),
    ])
    actual = candidate_fit(duplicate, _listing())

    assert actual.score == actual.wcl_score == expected.score == 50
    assert actual.wcl_dungeon_count == expected.wcl_dungeon_count == 2
    assert actual.wcl_run_count == expected.wcl_run_count == 6


def test_conflicting_duplicate_resolution_is_stable_under_input_order():
    app = _app(output=None, levels=())
    rows = [_row(_NAMES[0], 95), _row(_NAMES[0], 5), _row(_NAMES[1], 40)]
    left = candidate_fit(replace(app, mplus_dps_breakdown=rows), _listing())
    right = candidate_fit(replace(app, mplus_dps_breakdown=list(reversed(rows))), _listing())

    assert left.score == right.score == 22.5
    assert left.wcl_dungeon_count == right.wcl_dungeon_count == 2
    assert left.wcl_run_count == right.wcl_run_count == 6


def test_single_run_output_is_visible_but_has_truthful_repeat_metadata():
    app = _app(levels=())
    app.mplus_dps_breakdown = [dict(name=_NAMES[0], key_level=11,
                                   parse_percent=80, median_percent=None, run_count=1)]
    fit = candidate_fit(app, _listing())

    assert fit.is_available and fit.source == "mplus_wcl_only"
    assert fit.wcl_score == pytest.approx(76.4)
    assert fit.timed_experience_score is None
    assert (fit.wcl_dungeon_count, fit.wcl_run_count, fit.wcl_all_single_run) == (1, 1, True)


def test_unknown_sample_count_is_not_presented_as_a_known_repeat_sample():
    app = _app(output=None, levels=())
    app.mplus_dps_breakdown = [_row(_NAMES[0], 80, runs=0), _row(_NAMES[1], 80)]
    fit = candidate_fit(app, _listing())

    assert fit.is_available
    assert fit.wcl_run_count is None
    assert not fit.wcl_all_single_run
    assert fit.timed_experience_score is None


def test_partial_available_output_does_not_fill_unlogged_dungeons_with_player_zero():
    app = replace(_app(levels=()), wcl_availability={"mplus": "partial"})
    app.mplus_dps_breakdown = [_row(_NAMES[0], 80)]
    fit = candidate_fit(app, _listing())

    assert fit.is_available and fit.source == "mplus_wcl_only"
    assert fit.score == fit.wcl_score == 80
    assert fit.wcl_dungeon_count == 1 and fit.wcl_run_count == 3
    assert fit.timed_experience_score is None and fit.dungeon_count == 0


def test_group_estimate_preserves_weak_output_member_with_matching_timer_history():
    strong = _app(output=90, aid="10:1")
    weak = _app(output=10, aid="10:2")
    strong_fit, weak_fit = (candidate_fit(app, _listing()) for app in (strong, weak))
    mixed = package_fit([strong, weak], _listing())
    all_strong = package_fit([strong, replace(strong, applicant_id="10:2")], _listing())

    assert mixed.is_complete and mixed.available_count == 2
    assert mixed.member_scores == (strong_fit.score, weak_fit.score)
    assert mixed.low_score == weak_fit.score < strong_fit.score
    assert mixed.score < all_strong.score
    assert mixed.status_penalty == 0


def test_group_without_either_component_for_one_member_remains_unknown_not_player_zero():
    known = _app(output=90, aid="10:1")
    unknown = replace(_app(levels=(), aid="10:2"), fetch_status="restricted")
    fit = package_fit([known, unknown], _listing())

    assert not fit.is_complete
    assert (fit.available_count, fit.unavailable_count) == (1, 1)
    assert fit.status_penalty == 0
    assert fit.colour is None


def test_projection_does_not_mutate_source_evidence_or_depend_on_call_order():
    app = _app(output=70)
    original = deepcopy(app)
    first = candidate_fit(app, _listing())
    candidate_fit(app, _listing(12))
    candidate_fit(app, _listing(14))
    package_fit([app, replace(app, applicant_id="1:2")], _listing())

    assert candidate_fit(app, _listing()) == first
    assert app == original
