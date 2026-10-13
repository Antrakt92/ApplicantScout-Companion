"""Keep M+ summary and its visible dungeon evidence on the same population."""

from copy import deepcopy
from dataclasses import asdict, replace
from typing import cast

import pytest

from applicant_scout import overlay_presenters, overlay_rows, scoring
from applicant_scout.constants import MPLUS_ENCOUNTERS
from applicant_scout.state import Applicant, Listing
from applicant_scout.wcl import _compute_mplus_headline, _process_encounter_ranks


NAMES = [name for _alias, _encounter, name in MPLUS_ENCOUNTERS]


def _listing(key: int = 10) -> Listing:
    return Listing(
        activity_id=0, dungeon_name="Mythic+", listing_name="", comment="",
        key_level=key, category_id=2, difficulty_id=8,
    )


def _row(
    name: str, key: int, best: object, median: object = None, count: int = 3,
) -> dict:
    return {
        "name": name, "key_level": key, "parse_percent": best,
        "median_percent": median, "run_count": count,
    }


def _app(rows: list[dict]) -> Applicant:
    return Applicant(
        applicant_id="synthetic", name="Synthetic-Realm", cls="MAGE",
        spec_id=62, ilvl=0, score=0, role="DAMAGER", fetch_status="ready",
        mplus_dps=36.0, mplus_dps_median=None, mplus_dps_breakdown=rows,
        rio_dungeons=[{"name": name, "key_level": 10} for name in NAMES],
    )


def _screenshot_app() -> Applicant:
    # Different populations: highest logged keys are +15/+16, while the
    # selected +10 target exposes the stronger +8/+10/+12 brackets.
    lower = [
        (12, 79, 69), (10, 80, None), (12, 93, 72), (10, 83, 66),
        (8, 64, 36), (10, 88, 73), (12, 94, 56), (8, 82, None),
    ]
    higher = [35, 10, 43, 74, 9, 32, 67, 22]
    perfs = []
    for index, (name, (key, best, median), high_best) in enumerate(
        zip(NAMES, lower, higher, strict=True)
    ):
        percentiles = [best] if median is None else [best, 2 * median - best]
        ranks = [
            {"spec": "Arcane", "bracketData": key, "rankPercent": percentile}
            for percentile in percentiles
        ]
        ranks.append({
            "spec": "Arcane", "bracketData": 15 + index % 2,
            "rankPercent": high_best,
        })
        if index == 3:
            ranks.append({"spec": "Arcane", "bracketData": 16, "rankPercent": 70.0})
        perf = _process_encounter_ranks({"ranks": ranks}, "Arcane", name)
        assert perf is not None
        perfs.append(perf)
    best, median = _compute_mplus_headline(perfs)
    return replace(
        _app([asdict(perf) for perf in perfs]),
        mplus_dps=best, mplus_dps_median=median,
    )


def test_screenshot_summary_uses_visible_target_brackets_instead_of_highest_keys():
    applicant = _screenshot_app()
    assert applicant.mplus_dps == pytest.approx(36.5)
    assert applicant.mplus_dps_median is None

    rows = overlay_presenters.wcl_dungeon_rows_by_name(applicant, _listing())
    by_name = {row["name"]: row for row in rows.values()}
    expected = [
        (12, 79, 69, 2), (10, 80, 80, 1), (12, 93, 72, 2), (10, 83, 66, 2),
        (8, 64, 36, 2), (10, 88, 73, 2), (12, 94, 56, 2), (8, 82, 82, 1),
    ]
    for name, (key, best, median, count) in zip(NAMES, expected, strict=True):
        row = by_name[name]
        assert (row["key_level"], row["best_percent"], row["median_percent"], row["run_count"]) == (
            key, best, median, count,
        )

    summary = overlay_presenters.mplus_display_summary(applicant, _listing())
    assert summary.best == pytest.approx(82.875)
    assert summary.median is None  # Two selected dungeons have only one run.
    assert (summary.key_min, summary.key_max) == (8, 12)
    assert (summary.target_key, summary.is_contextual, summary.all_single_run) == (10, True, False)


@pytest.mark.parametrize("listing", [
    None, _listing(0), _listing(1), _listing(51),
    Listing(0, "Raid", "", "", category_id=3, difficulty_id=4),
])
def test_without_valid_mplus_target_summary_and_details_share_highest_key_fallback(listing):
    applicant = _screenshot_app()
    rows = overlay_presenters.wcl_dungeon_rows_by_name(applicant, listing)
    assert sorted(cast(int, row["key_level"]) for row in rows.values()) == [15] * 4 + [16] * 4
    summary = overlay_presenters.mplus_display_summary(applicant, listing)
    assert summary.best == pytest.approx(36.5)
    assert summary.median is None
    # Outside a valid target the original summary remains a global average;
    # its suffix retains the existing single highest-key context.
    assert (summary.key_min, summary.key_max) == (16, 16)
    assert (summary.target_key, summary.is_contextual, summary.all_single_run) == (0, False, False)


def test_same_selected_population_provides_full_precision_best_and_median():
    applicant = _app([
        _row(NAMES[0], 10, 89.75, 61.25, 3),
        _row(NAMES[1], 12, 20.5, 12.5, 4),
    ])
    summary = overlay_presenters.mplus_display_summary(applicant, _listing())
    assert summary.best == pytest.approx(55.125)
    assert summary.median == pytest.approx(36.875)
    assert (summary.key_min, summary.key_max) == (10, 12)
    assert not summary.all_single_run


def test_canonical_duplicate_dungeon_is_not_an_extra_summary_contributor():
    applicant = _app([
        _row(NAMES[0], 10, 98, 94, 3),
        _row(NAMES[0].replace(" ", "-"), 10, 98, 94, 3),
        _row(NAMES[1], 10, 0, 0, 3),
    ])
    rows = overlay_presenters.wcl_dungeon_rows_by_name(applicant, _listing())
    assert len(rows) == 2
    summary = overlay_presenters.mplus_display_summary(applicant, _listing())
    assert (summary.best, summary.median) == (49, 47)


@pytest.mark.parametrize("missing_row", [
    _row(NAMES[1], 10, 20, None, 3),
    _row(NAMES[1], 10, 20, 20, 1),
    _row(NAMES[1], 10, 20, 20, 0),
])
def test_partial_repeat_evidence_does_not_become_whole_population_median(missing_row):
    applicant = _app([_row(NAMES[0], 10, 90, 70, 3), missing_row])
    summary = overlay_presenters.mplus_display_summary(applicant, _listing())
    assert summary.best == pytest.approx(55)
    assert summary.median is None
    assert not summary.all_single_run


def test_all_single_run_metadata_describes_selected_brackets_not_old_headline():
    rows = []
    for name, best in zip(NAMES[:2], [75, 85], strict=True):
        row = _row(name, 16, 10, 5, 8)
        row["brackets"] = [
            _row(name, 10, best, best, 1), _row(name, 16, 10, 5, 8),
        ]
        rows.append(row)
    summary = overlay_presenters.mplus_display_summary(_app(rows), _listing())
    assert summary.best == pytest.approx(80)
    assert summary.median is None
    assert summary.all_single_run
    assert (summary.key_min, summary.key_max) == (10, 10)


@pytest.mark.parametrize("invalid", [None, True, float("nan"), float("inf"), -1, 101, "invalid"])
def test_invalid_dungeon_percentile_neither_becomes_zero_nor_dilutes_observed_zero(invalid):
    applicant = _app([
        _row(NAMES[0], 10, 0, 0, 3), _row(NAMES[1], 12, invalid, 90, 3),
    ])
    rows = overlay_presenters.wcl_dungeon_rows_by_name(applicant, _listing())
    assert len(rows) == 1
    summary = overlay_presenters.mplus_display_summary(applicant, _listing())
    assert (summary.best, summary.median) == (0, 0)
    assert (summary.key_min, summary.key_max) == (10, 10)
    assert not summary.all_single_run


def test_missing_logged_key_does_not_fabricate_zero_key_in_fallback_range():
    applicant = replace(
        _app([_row(NAMES[0], 0, 90, 70, 3), _row(NAMES[1], 12, 70, 50, 3)]),
        mplus_dps=80, mplus_dps_median=60,
    )
    summary = overlay_presenters.mplus_display_summary(applicant, None)
    assert (summary.best, summary.median) == (80, 60)
    assert (summary.key_min, summary.key_max) == (12, 12)
    assert not summary.is_contextual


def test_summary_only_legacy_data_has_no_invented_target_or_key_range():
    applicant = replace(_app([]), mplus_dps=45.75, mplus_dps_median=21.25)
    summary = overlay_presenters.mplus_display_summary(applicant, _listing())
    assert (summary.best, summary.median) == (45.75, 21.25)
    assert (summary.key_min, summary.key_max, summary.target_key) == (0, 0, 0)
    assert not summary.is_contextual
    assert not summary.all_single_run


def test_no_logs_preserve_unknown_percentiles_instead_of_player_zero():
    applicant = replace(_app([]), mplus_dps=None, mplus_dps_median=None)
    summary = overlay_presenters.mplus_display_summary(applicant, _listing())
    assert (summary.best, summary.median) == (None, None)
    assert (summary.key_min, summary.key_max) == (0, 0)


def test_noncontext_display_preserves_stored_summary_instead_of_reinterpreting_cache():
    applicant = replace(
        _app([_row(NAMES[0], 12, 90, 70, 3)]),
        mplus_dps=45.75, mplus_dps_median=21.25,
    )
    summary = overlay_presenters.mplus_display_summary(applicant, None)
    assert (summary.best, summary.median) == (45.75, 21.25)
    assert (summary.key_min, summary.key_max) == (12, 12)
    assert (summary.target_key, summary.is_contextual) == (0, False)


@pytest.mark.parametrize("status", ["pending", "loading", "error", "not_found", "restricted"])
def test_unavailable_fetch_suppresses_cached_numbers_and_ranges(status):
    applicant = replace(_screenshot_app(), fetch_status=status)
    assert overlay_presenters.wcl_dungeon_rows_by_name(applicant, _listing()) == {}
    summary = overlay_presenters.mplus_display_summary(applicant, _listing())
    assert (summary.best, summary.median) == (None, None)
    assert (summary.key_min, summary.key_max) == (0, 0)
    assert not summary.all_single_run


@pytest.mark.parametrize("scope_status", ["restricted", "error", "empty", "unavailable"])
def test_unavailable_mplus_scope_suppresses_otherwise_ready_cached_evidence(scope_status):
    applicant = replace(_screenshot_app(), wcl_availability={"mplus": scope_status})
    assert overlay_presenters.wcl_dungeon_rows_by_name(applicant, _listing()) == {}
    summary = overlay_presenters.mplus_display_summary(applicant, _listing())
    assert (summary.best, summary.median) == (None, None)
    assert (summary.key_min, summary.key_max) == (0, 0)


@pytest.mark.parametrize("role", ["DAMAGER", "TANK", "HEALER"])
def test_display_projection_is_pure_while_damage_changes_combined_fit_for_any_role(role):
    applicant = replace(_screenshot_app(), role=role)
    original = deepcopy(applicant)
    expected = scoring.candidate_fit(applicant, _listing())
    assert expected.is_available
    for key in (10, 16, 10):
        overlay_presenters.mplus_display_summary(applicant, _listing(key))
    assert applicant == original
    assert scoring.candidate_fit(applicant, _listing()) == expected

    changed_damage = replace(
        applicant, mplus_dps=0, mplus_dps_median=0,
        mplus_dps_breakdown=[_row(name, 10, 0, 0, 3) for name in NAMES],
    )
    assert overlay_presenters.mplus_display_summary(changed_damage, _listing()).best == 0
    changed_fit = scoring.candidate_fit(changed_damage, _listing())
    assert changed_fit.timed_experience_score == expected.timed_experience_score
    assert changed_fit.wcl_score == 0
    assert changed_fit.score < expected.score
    assert changed_fit.source == expected.source == "mplus_combined"


def test_package_fit_uses_same_target_damage_as_visible_parse_metadata():
    stronger_row = _row(NAMES[0], 16, 0, 0, 3)
    stronger_row["brackets"] = [
        _row(NAMES[0], 10, 90, 90, 3), _row(NAMES[0], 16, 0, 0, 3),
    ]
    weaker_row = _row(NAMES[0], 16, 99, 99, 3)
    weaker_row["brackets"] = [
        _row(NAMES[0], 10, 60, 60, 3), _row(NAMES[0], 16, 99, 99, 3),
    ]
    stronger = replace(_app([stronger_row]), applicant_id="2:1", mplus_dps=0)
    weaker = replace(_app([weaker_row]), applicant_id="1:1", mplus_dps=99)
    listing = _listing()
    stronger_fit = scoring.candidate_fit(stronger, listing)
    weaker_fit = scoring.candidate_fit(weaker, listing)
    assert stronger_fit.timed_experience_score == weaker_fit.timed_experience_score
    assert (stronger_fit.wcl_score, weaker_fit.wcl_score) == (90, 60)
    assert stronger_fit.score > weaker_fit.score
    assert stronger_fit.source == weaker_fit.source == "mplus_combined"
    assert scoring.listing_parse_percentile(stronger, listing) == 90
    assert scoring.listing_parse_percentile(weaker, listing) == 60
    rows, packages, _fits = overlay_rows.sort_applicants_grouped_with_package_fits(
        [weaker, stronger], listing,
    )
    assert packages["2"].parse_percentile == 90
    assert packages["1"].parse_percentile == 60
    assert packages["2"].score > packages["1"].score
    # Nearby output drives Fit even when stored highest-key headlines favor
    # the other applicant. Timed history remains identical and separate.
    assert [row.applicant_id for row in rows] == ["2:1", "1:1"]
