"""Recruitment estimates must separate unknown evidence from measured results."""

from dataclasses import replace

import pytest

from applicant_scout.__main__ import StateMachine
from applicant_scout import overlay, overlay_presenters
from applicant_scout.constants import CURRENT_RAID_ENCOUNTERS
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.scoring import candidate_fit, package_fit
from applicant_scout.state import Applicant, Listing


TOTAL_BOSSES = len(CURRENT_RAID_ENCOUNTERS)


def _raid(difficulty=15):
    return Listing(0, "Raid", "Raid applicants", "", category_id=3, difficulty_id=difficulty)


def _app(**fields):
    defaults = dict(fetch_status="ready")
    defaults.update(fields)
    return Applicant("1:1", "Scout-Realm", "WARRIOR", 71, 300, 3000, "DAMAGER", **defaults)


def _evidence(bosses, kills=None):
    row = {"boss_count": bosses, "total_bosses": TOTAL_BOSSES,
           "sample_known": kills is not None}
    if kills is not None:
        row["kill_count"] = kills
    return row


@pytest.mark.parametrize("status", ["ready", "pending", "loading", "error", "not_found", "restricted"])
def test_no_raid_logs_have_no_numeric_performance_estimate(status):
    fit = candidate_fit(_app(fetch_status=status, main_score=4000,
                             mplus_dps=99, mplus_dps_median=99), _raid())
    assert not fit.is_available
    assert fit.display == ""
    assert fit.colour is None


def test_observed_zero_is_available_and_different_from_missing():
    measured = candidate_fit(_app(raid_heroic=0, raid_heroic_median=0), _raid())
    missing = candidate_fit(_app(), _raid())
    assert measured.is_available
    assert measured.score == 0
    assert measured.display
    assert not missing.is_available
    assert not missing.display
    assert package_fit([_app(raid_heroic=0, raid_heroic_median=0)], _raid()).is_complete


@pytest.mark.parametrize("state", ["restricted", "empty", "disabled", "unavailable", "error"])
def test_unavailable_target_scope_never_uses_a_stale_headline(state):
    fit = candidate_fit(_app(raid_heroic=99, raid_heroic_median=99,
                             wcl_availability={"raid_heroic": state}), _raid())
    assert not fit.is_available


def test_private_target_can_use_only_the_independent_available_higher_scope():
    app = _app(raid_heroic=99, raid_heroic_median=99,
               raid_mythic=80, raid_mythic_median=80,
               wcl_availability={"raid_heroic": "restricted", "raid_mythic": "available"},
               raid_evidence={"H": _evidence(TOTAL_BOSSES, 30), "M": _evidence(2, 6)})
    fit = candidate_fit(app, _raid())
    assert fit.is_available
    assert fit.source == "raid_higher_fallback"
    assert fit.evidence_source == "M"
    assert fit.score == pytest.approx(70.4)
    assert fit.raid_boss_count == 2
    assert fit.raid_kill_count == 6


def test_explicit_disabled_fetched_scope_cannot_score_stale_fields():
    app = _app(raid_heroic=99, raid_heroic_median=99,
               wcl_metric_preferences=MetricPreferences(
                   mplus=True, raid_normal=True, raid_heroic=False, raid_mythic=False))
    assert not candidate_fit(app, _raid()).is_available


@pytest.mark.parametrize("spec_id", [0, -1, 999999])
def test_unidentified_applying_spec_cannot_reuse_stale_raid_performance(spec_id):
    app = replace(_app(raid_heroic=99, raid_heroic_median=99,
                       raid_evidence={"H": _evidence(TOTAL_BOSSES, 30)}), spec_id=spec_id)
    assert not candidate_fit(app, _raid()).is_available


@pytest.mark.parametrize("exact", [0, 20, 50, 80])
def test_adding_low_exact_result_cannot_erase_stronger_higher_difficulty_evidence(exact):
    app = _app(raid_mythic=95, raid_mythic_median=90,
               raid_evidence={"M": _evidence(4, 12)})
    before = candidate_fit(app, _raid())
    after = candidate_fit(replace(app, raid_heroic=exact, raid_heroic_median=exact,
                                  raid_evidence={"M": _evidence(4, 12), "H": _evidence(1, 1)}), _raid())
    assert after.score >= before.score
    assert after.evidence_source == "M"
    assert after.raid_boss_count == 4
    assert after.raid_kill_count == 12


@pytest.mark.parametrize("difficulty", [14, 15, 16])
def test_unrelated_mplus_and_main_score_do_not_change_raid_output(difficulty):
    base = replace(_app(raid_normal=70, raid_normal_median=65,
                        raid_heroic=80, raid_heroic_median=75,
                        raid_mythic=90, raid_mythic_median=85), score=0)
    unrelated = replace(base, score=4000, main_score=4500, mplus_dps=100,
                        mplus_dps_median=100,
                        mplus_dps_breakdown=[{"name": "Skyreach", "key_level": 25,
                                               "parse_percent": 100, "run_count": 10}])
    assert candidate_fit(base, _raid(difficulty)).score == candidate_fit(unrelated, _raid(difficulty)).score


@pytest.mark.parametrize("difficulty,expected_source,source_key,expected", [
    (14, "raid_higher_fallback", "M", 88),
    (15, "raid_higher_fallback", "M", 88),
    (16, "raid_exact", "M", 100),
])
def test_metadata_belongs_to_the_selected_source_not_the_requested_difficulty(
    difficulty, expected_source, source_key, expected,
):
    app = _app(raid_normal=20, raid_normal_median=20,
               raid_heroic=40, raid_heroic_median=40,
               raid_mythic=100, raid_mythic_median=100,
               raid_evidence={"N": _evidence(1, 1), "H": _evidence(2, 4),
                              "M": _evidence(TOTAL_BOSSES, TOTAL_BOSSES*3)})
    fit = candidate_fit(app, _raid(difficulty))
    assert fit.score == expected
    assert fit.source == expected_source
    assert fit.evidence_source == source_key
    assert fit.raid_boss_count == TOTAL_BOSSES
    assert fit.raid_kill_count == TOTAL_BOSSES*3


def test_lower_fallback_retains_the_actual_selected_lower_source_coverage():
    app = _app(raid_normal=90, raid_normal_median=90,
               raid_heroic=60, raid_heroic_median=60,
               raid_evidence={"N": _evidence(4, 8), "H": _evidence(1, 1)})
    fit = candidate_fit(app, _raid(16))
    assert fit.score == pytest.approx(49.5)
    assert fit.source == "raid_lower_fallback"
    assert fit.evidence_source == "N"
    assert fit.raid_boss_count == 4
    assert fit.raid_kill_count == 8


def test_same_output_with_more_bosses_and_repeats_has_more_evidence_not_more_performance():
    one = _app(raid_heroic=90, raid_heroic_median=90,
                raid_evidence={"H": _evidence(1, 1)})
    full = replace(one, raid_evidence={"H": _evidence(TOTAL_BOSSES, TOTAL_BOSSES*3)})
    a, b = candidate_fit(one, _raid()), candidate_fit(full, _raid())
    assert a.score == b.score
    assert a.coverage < b.coverage
    assert a.confidence < b.confidence


def test_average_alone_does_not_claim_a_repeat_sample_or_full_boss_coverage():
    fit = candidate_fit(_app(raid_heroic=90, raid_heroic_median=90), _raid())
    assert fit.is_available
    assert fit.raid_boss_count == fit.raid_total_bosses == fit.raid_kill_count == 0
    assert fit.confidence == 0
    assert "unknown" in fit.evidence_note.lower()


@pytest.mark.parametrize("metadata", [
    {"H": None}, {"H": []}, {"H": "bad"},
    {"H": {"boss_count": TOTAL_BOSSES+1, "total_bosses": TOTAL_BOSSES,
           "sample_known": True, "kill_count": 99}},
    {"H": {"boss_count": True, "total_bosses": TOTAL_BOSSES,
           "sample_known": True, "kill_count": 99}},
])
def test_invalid_metadata_stays_unknown_without_losing_valid_performance(metadata):
    fit = candidate_fit(_app(raid_heroic=90, raid_heroic_median=90,
                             raid_evidence=metadata), _raid())
    assert fit.is_available
    assert fit.score == 90
    assert fit.raid_boss_count == fit.raid_total_bosses == fit.raid_kill_count == 0
    assert fit.confidence == 0


@pytest.mark.parametrize("status", ["ready", "pending", "loading", "error", "not_found", "restricted"])
def test_partial_package_is_unknown_without_api_or_privacy_skill_penalties(status):
    known = _app(raid_heroic=90, raid_heroic_median=90,
                 raid_evidence={"H": _evidence(3, 9)})
    unknown = replace(_app(fetch_status=status), applicant_id="1:2")
    package = package_fit([known, unknown], _raid())
    assert not package.is_complete
    assert package.available_count == package.unavailable_count == 1
    assert package.colour is None
    assert package.display == "G2 partial"
    assert "~" not in package.display
    assert package.status_penalty == 0
    assert package.average_score == 90
    assert package.low_score == 90


def test_measured_zero_member_is_a_complete_package_with_a_real_lower_result():
    known = _app(raid_heroic=90, raid_heroic_median=90)
    zero = replace(_app(raid_heroic=0, raid_heroic_median=0), applicant_id="1:2")
    package = package_fit([known, zero], _raid())
    assert package.is_complete
    assert package.available_count == 2
    assert package.unavailable_count == 0
    assert package.low_score == 0
    assert package.display


def test_state_snapshot_copy_preserves_and_detaches_summary_evidence():
    source = _app(raid_evidence={"H": _evidence(3, 9)})
    target = replace(_app(), raid_evidence={"M": _evidence(1, 1)})
    StateMachine._copy_wcl_data(source, target)
    assert target.raid_evidence == {"H": _evidence(3, 9)}
    source.raid_evidence["H"]["kill_count"] = 99
    assert target.raid_evidence["H"]["kill_count"] == 9


@pytest.mark.parametrize("difficulty,fields,selected,expected", [
    (14, {"raid_heroic": 80, "raid_heroic_median": 80,
          "raid_mythic": 99, "raid_mythic_median": 99,
          "wcl_availability": {"raid_heroic": "available", "raid_mythic": "restricted"}},
     "H", "H 80/80"),
    (16, {"raid_normal": 80, "raid_normal_median": 80,
          "raid_heroic": 80, "raid_heroic_median": 80}, "N", "N 80/80"),
])
def test_raid_evidence_presenter_uses_the_actual_selected_difficulty(
    difficulty, fields, selected, expected,
):
    app = _app(**fields)
    fit = candidate_fit(app, _raid(difficulty))
    assert fit.evidence_source == selected
    assert overlay_presenters.raid_fit_evidence_text(
        app, fit.target_raid, fit.source, selected_difficulty=fit.evidence_source,
    ) == expected
    # The backwards-compatible helper must not choose a private higher scope
    # or reverse equal-score lower difficulties relative to the scorer.
    assert overlay_presenters.raid_fit_evidence_text(app, fit.target_raid, fit.source) == expected


def test_raid_fit_tooltip_does_not_claim_private_stale_source_evidence(qtbot):
    app = _app(raid_heroic=80, raid_heroic_median=80,
               raid_mythic=99, raid_mythic_median=99,
               wcl_availability={"raid_heroic": "available", "raid_mythic": "restricted"})
    fit = candidate_fit(app, _raid(14))
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(app, _raid(14), fit=fit)
    tooltip = panel._metric_labels["Fit"].toolTip()
    assert "Source difficulty Heroic" in tooltip
    assert "Source evidence: H 80/80" in tooltip
    assert "Source evidence: M" not in tooltip


def test_explicit_private_selected_source_is_not_rendered_by_presenter():
    app = _app(raid_mythic=99, raid_mythic_median=99,
               wcl_availability={"raid_mythic": "restricted"})
    assert overlay_presenters.raid_fit_evidence_text(
        app, "N", "raid_higher_fallback", selected_difficulty="M",
    ) == ""
