"""Recruitment sorting and factual evidence display across incomplete profiles."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication

from applicant_scout import overlay, overlay_presenters, overlay_rows, scoring
from applicant_scout.constants import MPLUS_ENCOUNTERS, percentile_colour
from applicant_scout.state import Applicant, Listing


def _listing():
    return Listing(
        activity_id=0, dungeon_name=MPLUS_ENCOUNTERS[0][2],
        listing_name="+16", comment="", key_level=16,
        category_id=2, difficulty_id=8,
    )


def _app(aid, key, percentile, *, score=3200, status="ready"):
    names = [name for _alias, _encounter, name in MPLUS_ENCOUNTERS]
    return Applicant(
        applicant_id=aid, name=f"Player{aid}-Realm", cls="WARRIOR",
        spec_id=71, ilvl=300, score=score, role="DAMAGER", fetch_status=status,
        mplus_dps=percentile, mplus_dps_median=percentile,
        mplus_dps_breakdown=[
            dict(name=name, key_level=key, parse_percent=percentile,
                 median_percent=percentile, run_count=3)
            for name in names
        ] if percentile is not None else [],
        rio_dungeons=[dict(name=name, key_level=key) for name in names],
    )


def test_default_sort_prefers_target_evidence_over_easy_content_and_log_visibility():
    farm = _app("1:1", 5, 99, score=1200)
    target = _app("2:1", 16, 65)
    no_logs = _app("3:1", 18, None, score=3500, status="not_found")
    poor_damage = _app("4:1", 16, 0)
    rows, _packages, _fits = overlay_rows.sort_applicants_grouped_with_package_fits(
        [farm, target, no_logs, poor_damage], _listing(),
    )
    ids = [row.applicant_id for row in rows]
    assert ids.index(target.applicant_id) < ids.index(farm.applicant_id)
    assert ids.index(no_logs.applicant_id) < ids.index(poor_damage.applicant_id)
    # Target sorting does not conceal a genuinely unfavorable damage result.
    assert overlay._mplus_cell_visuals(poor_damage)[0].startswith("0/0")
    assert overlay._mplus_cell_visuals(poor_damage)[2] == percentile_colour(0)


def test_identical_timed_histories_sort_stably_regardless_of_general_rating():
    first = _app("1:1", 16, 50, score=0)
    second = _app("2:1", 16, 50, score=4000)
    first_fit = scoring.candidate_fit(first, _listing())
    second_fit = scoring.candidate_fit(second, _listing())
    assert first_fit.is_available and first_fit == second_fit
    for profiles in ([second, first], [replace(second, score=0), replace(first, score=4000)]):
        rows, _packages, _fits = overlay_rows.sort_applicants_grouped_with_package_fits(
            profiles, _listing(),
        )
        assert [row.applicant_id for row in rows] == ["1:1", "2:1"]


def test_fit_palette_is_distinct_from_raw_damage_palette():
    applicant = _app("1:1", 16, 99)
    fit = scoring.candidate_fit(applicant, _listing())
    _text, _foreground, background = overlay._fit_cell_visuals(
        applicant, _listing(), fit=fit,
    )
    assert background == overlay.FIT_ESTIMATE_BACKGROUND
    assert background != overlay._mplus_cell_visuals(applicant)[2]


@pytest.mark.parametrize("status", ["ready", "pending", "loading", "error", "not_found", "restricted"])
def test_missing_target_evidence_remains_unavailable_and_neutral(status):
    applicant = _app("1:1", 0, None, score=0, status=status)
    fit = scoring.candidate_fit(applicant, _listing())
    assert not fit.is_available
    item = overlay._fit_cell(applicant, _listing(), fit=fit)
    assert item.text() == ("…" if status in {"pending", "loading"} else "—")
    assert item.background().style() == Qt.BrushStyle.NoBrush


def test_observed_below_target_zero_is_available_in_neutral_fit_cell():
    applicant = _app("1:1", 5, 99, score=1200)
    fit = scoring.candidate_fit(applicant, _listing())
    assert fit.is_available and fit.score == 0
    assert overlay._fit_cell_visuals(applicant, _listing(), fit=fit) == (
        "~0", overlay.FIT_ESTIMATE_FOREGROUND, overlay.FIT_ESTIMATE_BACKGROUND,
    )


@pytest.mark.parametrize("status", ["pending", "loading", "error", "not_found", "restricted"])
def test_known_rio_experience_survives_wcl_availability_changes(qtbot, status):
    applicant = _app("1:1", 16, None, status=status)
    fit = scoring.candidate_fit(applicant, _listing())
    assert fit.is_available
    item = overlay._fit_cell(applicant, _listing(), fit=fit)
    assert item.text() == f"~{round(fit.score)}"
    assert overlay._mplus_cell_visuals(applicant)[2] is None
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(applicant, _listing(), fit=fit)
    assert not panel._metric_labels["Fit"].isHidden()
    assert "RaiderIO in-time keys" in panel._metric_labels["Fit"].toolTip()


def test_partial_group_has_no_aggregate_verdict_and_preserves_known_individual(qtbot):
    known = _app("1:1", 16, 65)
    unknown = _app("1:2", 0, None, score=0)
    package = scoring.package_fit([known, unknown], _listing())
    assert not package.is_complete
    assert (package.available_count, package.unavailable_count) == (1, 1)
    assert overlay_presenters.package_fit_text(package) == "G2 partial"
    known_item = overlay._mplus_group_cell(package, known, _listing())
    unknown_item = overlay._mplus_group_cell(package, unknown, _listing())
    assert known_item.text().startswith("G2 partial | ~")
    assert unknown_item.text() == "G2 partial | —"
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(known, _listing(), package=package)
    assert "Partial target evidence" in panel._package_label.text()
    assert "1/2 members have target evidence" in panel._package_label.toolTip()
    assert "No combined estimate" in panel._package_label.toolTip()
    assert "evidence strength" not in panel._package_label.toolTip().lower()
    assert "%" not in panel._package_label.toolTip()


@pytest.mark.parametrize("descending", [True, False])
def test_manual_fit_sort_keeps_partial_group_missing_in_both_directions(descending):
    known = _app("1:1", 16, 65)
    unknown = _app("1:2", 0, None, score=0)
    solo = _app("2:1", 16, 65)
    package = scoring.package_fit([known, unknown], _listing())
    window = SimpleNamespace(
        _active_manual_sort=lambda: (overlay.COL_FIT, descending),
        _active_tab="applicants",
    )
    rows = overlay.OverlayWindow._sort_rows_by_selected_column(
        window, [known, unknown, solo], _listing(), {"1": package}, {},
    )
    assert [row.applicant_id for row in rows] == ["2:1", "1:1", "1:2"]


def test_factual_evidence_copy_has_counts_and_unknown_raid_coverage():
    mplus = scoring.CandidateFit(
        context=scoring.CONTEXT_MPLUS, dungeon_count=5,
        relevant_dungeon_count=2, evidence_count=3, target_key=16,
    )
    text = overlay_presenters.fit_evidence_text(mplus)
    assert text == "5/8 dungeons timed · 2 timed at +15 or higher"
    assert "WCL" not in text
    assert "%" not in text
    raid = scoring.CandidateFit(context=scoring.CONTEXT_RAID)
    assert overlay_presenters.fit_evidence_text(raid) == "WCL zone boss coverage and kill count unknown"
    observed_zero = replace(raid, raid_total_bosses=8)
    assert overlay_presenters.fit_evidence_text(observed_zero).startswith("0/8 WCL zone bosses logged")


def test_group_copy_does_not_inherit_internal_player_quality_labels(qtbot):
    applicant = _app("1:1", 16, 65)
    partner = replace(applicant, applicant_id="1:2")
    package = replace(scoring.package_fit([applicant, partner], _listing()), label="TOP")
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(applicant, _listing(), package=package)
    assert "TOP" not in panel._package_label.toolTip()
    assert "Group estimate" in panel._package_label.toolTip()


def test_fit_badge_keeps_observed_coverage_visible_without_opening_tooltip(qtbot):
    applicant = _app("1:1", 16, 65)
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(applicant, _listing())
    assert " · 8/8 timed dungeons" in panel._metric_labels["Fit"].text()
    assert "8/8 dungeons timed" in panel._metric_labels["Fit"].toolTip()
    assert "WCL samples" not in panel._metric_labels["Fit"].toolTip()


def test_rating_without_timed_history_has_no_fit_badge(qtbot):
    applicant = _app("1:1", 0, None, score=4000)
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(applicant, _listing())
    assert panel._metric_labels["Fit"].isHidden()
    assert "RIO 4000" in panel._rio_label.text()


def test_wcl_only_profile_has_unknown_fit_and_preserves_raw_damage(qtbot):
    applicant = _app("1:1", 20, 24, score=0)
    applicant.rio_dungeons = []
    fit = scoring.candidate_fit(applicant, _listing())
    assert not fit.is_available
    assert (fit.dungeon_count, fit.relevant_dungeon_count, fit.evidence_count) == (0, 0, 0)
    assert overlay._fit_cell_visuals(applicant, _listing(), fit=fit)[0] == "—"
    assert overlay._mplus_cell_visuals(applicant)[0].startswith("24/24")
    assert overlay._mplus_cell_visuals(applicant)[2] == percentile_colour(24)
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(applicant, _listing(), fit=fit)
    assert panel._metric_labels["Fit"].isHidden()
    assert not panel._metric_labels["M+"].isHidden()


def test_untimed_completions_cannot_expand_visible_fit_coverage(qtbot):
    applicant = _app("1:1", 16, 65)
    applicant.rio_dungeons = applicant.rio_dungeons[:1]
    applicant.rio_summary_target_key = 16
    original_fit = scoring.candidate_fit(applicant, _listing())
    untimed = replace(
        applicant, rio_completed_at_or_above_minus1=8,
        rio_dungeons=applicant.rio_dungeons + [
            dict(name=name, key_level=25, timed=False)
            for _alias, _encounter, name in MPLUS_ENCOUNTERS[1:]
        ],
    )
    fit = scoring.candidate_fit(untimed, _listing())
    assert fit == original_fit
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(untimed, _listing(), fit=fit)
    assert panel._metric_labels["Fit"].text().endswith(" · 1/8 timed dungeons")
    assert "1/8 dungeons timed" in panel._metric_labels["Fit"].toolTip()


def test_untimed_summary_without_timed_runs_has_no_fit(qtbot):
    applicant = _app("1:1", 0, None, score=4000)
    applicant.rio_summary_target_key = 16
    applicant.rio_completed_at_or_above_minus1 = 8
    fit = scoring.candidate_fit(applicant, _listing())
    assert not fit.is_available and fit.dungeon_count == 0
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(applicant, _listing(), fit=fit)
    assert panel._metric_labels["Fit"].isHidden()


@pytest.mark.parametrize("metadata,expected", [
    ({}, "sample unknown"),
    ({"H": {"boss_count": 3, "total_bosses": 9,
            "sample_known": True, "kill_count": 6}}, "H 3/9 zone bosses"),
])
def test_raid_fit_badge_keeps_sample_scope_visible(qtbot, metadata, expected):
    applicant = _app("1:1", 0, None, score=0)
    applicant.raid_heroic = applicant.raid_heroic_median = 82
    applicant.raid_evidence = metadata
    listing = Listing(0, "Raid", "Heroic", "", category_id=3, difficulty_id=15)
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(applicant, listing)
    assert panel._metric_labels["Fit"].text() == f"Fit · Heroic: ~82 · {expected}"


@pytest.mark.real_display
@pytest.mark.parametrize("coverage_known", [True, False])
def test_visible_raid_source_badge_fits_at_minimum_supported_width(qtbot, coverage_known):
    applicant = _app("1:1", 0, None, score=0)
    applicant.raid_heroic = applicant.raid_heroic_median = 82
    if coverage_known:
        applicant.raid_evidence = {
            "H": {"boss_count": 3, "total_bosses": 9,
                  "sample_known": True, "kill_count": 6},
        }
    listing = Listing(0, "Raid", "Mythic", "", category_id=3, difficulty_id=16)
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setStyleSheet(overlay._STYLESHEET)
    panel.setApplicantData(applicant, listing)
    panel.resize(overlay.USER_MIN_WINDOW_WIDTH, panel.target_height())
    panel.show()
    QApplication.processEvents()
    panel.setFixedHeight(panel.target_height())
    QApplication.processEvents()
    badge = panel._metric_labels["Fit"]
    assert not badge.isHidden()
    assert badge.fontMetrics().horizontalAdvance(badge.text()) + 10 <= badge.width()
    badge_rect = QRect(badge.mapTo(panel, QPoint()), badge.size())
    assert panel.contentsRect().contains(badge_rect)
