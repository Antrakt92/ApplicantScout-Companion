"""Player evidence must improve consistently across key and sample changes."""

from copy import deepcopy
from dataclasses import asdict, replace
import random
from statistics import median

import pytest

from applicant_scout.constants import MPLUS_ENCOUNTERS
from applicant_scout.overlay_rows import sort_applicants_grouped_with_package_fits
from applicant_scout.scoring import (
    CONTEXT_MPLUS,
    _package_score_for_member_scores,
    candidate_fit,
    package_fit,
)
from applicant_scout.state import Applicant, Listing
from applicant_scout.wcl import _process_encounter_ranks


DUNGEONS = tuple(display_name for _alias, _encounter, display_name in MPLUS_ENCOUNTERS)


def _listing(target: int) -> Listing:
    return Listing(
        activity_id=0,
        dungeon_name=DUNGEONS[0],
        listing_name=f"+{target}",
        comment="",
        key_level=target,
        category_id=2,
    )


def _applicant(
    target: int,
    brackets: list[tuple[int, float, float, int]],
    *,
    rio: bool = True,
) -> Applicant:
    return Applicant(
        applicant_id="1:1",
        name="Player-Realm",
        cls="MAGE",
        spec_id=63,
        ilvl=280,
        score=0,
        role="DAMAGER",
        fetch_status="ready",
        rio_dungeons=[
            {"name": name, "key_level": target} for name in DUNGEONS
        ] if rio else [],
        mplus_dps_breakdown=[{
            "name": DUNGEONS[0],
            "brackets": [
                {
                    "key_level": key,
                    "parse_percent": best,
                    "median_percent": median,
                    "run_count": count,
                }
                for key, best, median, count in brackets
            ],
        }],
    )


@pytest.mark.parametrize("target", [5, 12, 18])
def test_better_additional_wcl_run_cannot_reduce_fit(target: int) -> None:
    ranks = [
        {"spec": "Fire", "bracketData": target - 2, "rankPercent": 25}
        for _ in range(3)
    ] + [
        {"spec": "Fire", "bracketData": target, "rankPercent": 5}
        for _ in range(3)
    ]
    before = _process_encounter_ranks({"ranks": ranks}, "Fire", DUNGEONS[0])
    after = _process_encounter_ranks({
        "ranks": ranks + [{
            "spec": "Fire", "bracketData": target, "rankPercent": 20,
        }],
    }, "Fire", DUNGEONS[0])
    assert before is not None and after is not None
    assert (before.parse_percent, before.median_percent, before.run_count) == (5, 5, 3)
    assert (after.parse_percent, after.median_percent, after.run_count) == (20, 5, 4)
    applicant = _applicant(target, [])
    applicant.mplus_dps_breakdown = [asdict(before)]
    improved = replace(applicant, mplus_dps_breakdown=[asdict(after)])

    assert candidate_fit(improved, _listing(target)).score == candidate_fit(
        applicant, _listing(target)
    ).score


@pytest.mark.parametrize("target", [5, 12, 18])
@pytest.mark.parametrize("easier_by", [1, 2])
def test_same_history_does_not_fit_an_easier_key_less(
    target: int, easier_by: int
) -> None:
    applicant = _applicant(
        target,
        [(target, 10, 10, 3), (target + 2, 80, 80, 3)],
    )

    assert candidate_fit(applicant, _listing(target - easier_by)).score >= candidate_fit(
        applicant, _listing(target)
    ).score


@pytest.mark.parametrize("target", [5, 12, 18])
def test_unknown_timing_logged_key_cannot_change_verified_timed_readiness(
    target: int,
) -> None:
    applicant = _applicant(
        target,
        [(target, 10, 10, 3), (target + 2, 80, 80, 3)],
    )
    improved = _applicant(
        target,
        [(target, 10, 10, 3), (target + 3, 80, 80, 3)],
    )

    assert candidate_fit(improved, _listing(target)).score == candidate_fit(
        applicant, _listing(target)
    ).score


@pytest.mark.parametrize("percentile", [10, 25, 49.999, 50, 80])
def test_unverified_wcl_coverage_does_not_establish_timed_readiness(
    percentile: float,
) -> None:
    applicant = _applicant(12, [], rio=False)
    applicant.mplus_dps_breakdown = [
        {
            "name": name,
            "key_level": 12,
            "parse_percent": percentile,
            "median_percent": percentile,
            "run_count": 3,
        }
        for name in DUNGEONS
    ]
    positive = replace(applicant, mplus_dps_breakdown=[
        {**entry, "parse_percent": 80, "median_percent": 80}
        for entry in applicant.mplus_dps_breakdown
    ])
    fit = candidate_fit(applicant, _listing(12))
    positive_fit = candidate_fit(positive, _listing(12))

    assert not fit.is_available
    assert not positive_fit.is_available
    assert fit.coverage == 0
    assert fit.coverage == positive_fit.coverage
    assert fit.confidence == positive_fit.confidence


def test_repeated_wcl_logs_do_not_raise_timed_readiness_evidence() -> None:
    applicant = _applicant(12, [])
    applicant.mplus_dps_breakdown = [
        {
            "name": name,
            "key_level": 12,
            "parse_percent": 10,
            "median_percent": 10,
            "run_count": 1,
        }
        for name in DUNGEONS
    ]
    repeated = replace(applicant, mplus_dps_breakdown=[
        {**entry, "run_count": 3} for entry in applicant.mplus_dps_breakdown
    ])

    assert candidate_fit(repeated, _listing(12)).confidence == candidate_fit(
        applicant, _listing(12)
    ).confidence


def test_measured_zero_raid_result_remains_available() -> None:
    applicant = _applicant(12, [], rio=False)
    applicant.raid_heroic = 0
    applicant.raid_heroic_median = 0
    listing = replace(_listing(12), key_level=0, category_id=3, difficulty_id=15)

    fit = candidate_fit(applicant, listing)
    group = package_fit([applicant], listing)

    assert fit.is_available
    assert fit.score == 0
    assert group.is_complete
    assert group.available_count == 1
    assert group.unavailable_count == 0


@pytest.mark.parametrize("status", ["ready", "loading", "error", "not_found", "restricted"])
def test_unknown_member_cannot_produce_a_complete_numeric_group_fit(status: str) -> None:
    known = _applicant(12, [], rio=False)
    known.raid_heroic = 80
    known.raid_heroic_median = 80
    unknown = replace(
        _applicant(12, [], rio=False),
        applicant_id="1:2",
        name="Other-Realm",
        fetch_status=status,
    )
    listing = replace(_listing(12), key_level=0, category_id=3, difficulty_id=15)

    assert not candidate_fit(unknown, listing).is_available
    group = package_fit([known, unknown], listing)
    reversed_group = package_fit([unknown, known], listing)

    assert not group.is_complete
    assert group.available_count == 1
    assert group.unavailable_count == 1
    assert group.display == "G2 partial"
    assert group.colour is None
    assert group.status_penalty == 0
    assert reversed_group.available_count == group.available_count
    assert reversed_group.unavailable_count == group.unavailable_count
    assert reversed_group.display == group.display


@pytest.mark.parametrize("status", ["ready", "loading", "error", "not_found", "restricted"])
def test_independent_rio_evidence_is_not_penalized_by_wcl_access_status(
    status: str,
) -> None:
    known = _applicant(12, [])
    baseline_partner = replace(known, applicant_id="1:2", name="Other-Realm")
    affected_partner = replace(baseline_partner, fetch_status=status)
    listing = _listing(12)
    baseline = package_fit([known, baseline_partner], listing)
    affected = package_fit([known, affected_partner], listing)

    assert candidate_fit(affected_partner, listing).is_available
    assert affected.is_complete
    assert affected.available_count == 2
    assert affected.unavailable_count == 0
    assert affected.status_penalty == 0
    assert affected.score == pytest.approx(baseline.score)


@pytest.mark.parametrize("percentile", [0, 10, 25, 49.999, 50, 80, 100])
@pytest.mark.parametrize(
    ("role", "cls", "spec_id"),
    [("DAMAGER", "MAGE", 63), ("TANK", "WARRIOR", 73), ("HEALER", "PRIEST", 257)],
)
def test_mplus_experience_fit_is_independent_of_damage_and_role(
    percentile: float, role: str, cls: str, spec_id: int
) -> None:
    baseline = _applicant(12, [(12, 80, 80, 3)])
    applicant = replace(
        _applicant(12, [(12, percentile, percentile, 3)]),
        role=role,
        cls=cls,
        spec_id=spec_id,
    )
    fit = candidate_fit(applicant, _listing(12))
    baseline_fit = candidate_fit(baseline, _listing(12))

    assert fit.is_available
    assert fit.score == baseline_fit.score
    assert fit.primary_key == baseline_fit.primary_key
    assert fit.coverage == baseline_fit.coverage
    assert fit.confidence == baseline_fit.confidence


def test_known_below_target_experience_zero_is_available() -> None:
    applicant = _applicant(12, [(2, 10, 10, 3)])
    applicant.rio_dungeons = [{"name": name, "key_level": 2} for name in DUNGEONS]
    fit = candidate_fit(applicant, _listing(12))

    assert fit.is_available
    assert fit.score == 0
    assert fit.display


def test_unleveled_wcl_headline_is_not_target_experience_evidence() -> None:
    applicant = _applicant(12, [], rio=False)
    applicant.mplus_dps_breakdown = []
    applicant.mplus_dps = 90
    applicant.mplus_dps_median = 80
    fit = candidate_fit(applicant, _listing(12))

    assert not fit.is_available
    assert fit.display == ""


def test_untimed_summary_and_unknown_timing_wcl_cannot_create_timed_readiness() -> None:
    applicant = _applicant(12, [], rio=False)
    applicant.rio_profile = True
    applicant.rio_dungeon_count = len(DUNGEONS)
    applicant.rio_summary_target_key = 12
    applicant.rio_completed_at_or_above_minus1 = len(DUNGEONS)
    applicant.mplus_dps_breakdown = [
        {
            "name": name,
            "key_level": 16,
            "parse_percent": 50,
            "median_percent": 50,
            "run_count": 3,
        }
        for name in DUNGEONS
    ]
    without_summary = replace(
        applicant,
        rio_profile=False,
        rio_summary_target_key=0,
        rio_completed_at_or_above_minus1=0,
    )

    for target in (2, 11, 12):
        assert not candidate_fit(applicant, _listing(target)).is_available
        assert not candidate_fit(without_summary, _listing(target)).is_available


@pytest.mark.parametrize(
    "levels",
    [
        [2 + index for index in range(len(DUNGEONS))],
        [12] * len(DUNGEONS),
        [18 - index for index in range(len(DUNGEONS))],
    ],
)
def test_named_experience_fit_decreases_consistently_across_target_grid(
    levels: list[int],
) -> None:
    applicant = _applicant(12, [], rio=False)
    applicant.rio_dungeons = [
        {"name": name, "key_level": key}
        for name, key in zip(DUNGEONS, levels, strict=True)
    ]
    applicant.mplus_dps_breakdown = [
        {
            "name": name,
            "brackets": [
                {
                    "key_level": key,
                    "parse_percent": 10,
                    "median_percent": 10,
                    "run_count": 3,
                },
                {
                    "key_level": key + 2,
                    "parse_percent": 80,
                    "median_percent": 80,
                    "run_count": 3,
                },
            ],
        }
        for name, key in zip(DUNGEONS, levels, strict=True)
    ]
    scores = [candidate_fit(applicant, _listing(target)).score for target in range(2, 25)]

    assert all(
        harder <= easier
        for easier, harder in zip(scores, scores[1:], strict=False)
    ), scores


@pytest.mark.parametrize("status", ["ready", "loading", "error", "not_found", "restricted"])
def test_current_rio_rating_never_manufactures_timed_fit_for_any_wcl_status(status: str) -> None:
    baseline = _applicant(12, [], rio=False)
    baseline.mplus_dps_breakdown = []
    baseline.score = 3400
    affected = replace(baseline, fetch_status=status)
    baseline_fit = candidate_fit(baseline, _listing(12))
    affected_fit = candidate_fit(affected, _listing(12))

    assert not baseline_fit.is_available
    assert not affected_fit.is_available
    assert affected_fit.display == baseline_fit.display == ""


@pytest.mark.parametrize("context", ["mplus", "raid"])
@pytest.mark.parametrize("size", [2, 3, 5])
def test_equally_supported_members_do_not_gain_fit_from_package_size(
    context: str, size: int
) -> None:
    applicant = _applicant(12, [])
    applicant.mplus_dps_breakdown = []
    listing = _listing(12)
    if context == "mplus":
        applicant.rio_dungeons = [
            {"name": name, "key_level": 16} for name in DUNGEONS
        ]
    else:
        applicant.raid_heroic = 95
        applicant.raid_heroic_median = 95
        listing = replace(listing, key_level=0, category_id=3, difficulty_id=15)
    solo = candidate_fit(applicant, listing)
    members = [
        replace(applicant, applicant_id=f"1:{index}", name=f"Player{index}-Realm")
        for index in range(size)
    ]
    group = package_fit(members, listing)

    assert group.is_complete
    assert group.available_count == size
    assert group.score == pytest.approx(solo.score)
    assert group.low_score == group.high_score == solo.score


def test_seeded_mixed_named_evidence_preserves_experience_invariants() -> None:
    rng = random.Random(20261013)
    for index in range(300):
        applicant = _applicant(12, [], rio=False)
        applicant.rio_dungeons = [
            {"name": name, "key_level": rng.randrange(2, 23)}
            for name in DUNGEONS
            if rng.random() < 0.7
        ]
        logs = []
        for name in DUNGEONS:
            if rng.random() >= 0.7:
                continue
            brackets = []
            for _ in range(rng.randrange(1, 4)):
                percentiles = [rng.randrange(101) for _ in range(rng.randrange(1, 7))]
                brackets.append({
                    "key_level": rng.randrange(2, 23),
                    "parse_percent": max(percentiles),
                    "median_percent": median(percentiles),
                    "run_count": len(percentiles),
                })
            logs.append({"name": name, "brackets": brackets})
        applicant.mplus_dps_breakdown = logs
        scores = [
            candidate_fit(applicant, _listing(target)).score for target in range(2, 25)
        ]
        assert all(
            harder <= easier + 1e-9 for easier, harder in zip(scores, scores[1:])
        ), (index, "target", scores)

        target = rng.randrange(2, 25)
        low_damage = deepcopy(applicant)
        high_damage = deepcopy(applicant)
        for entry in low_damage.mplus_dps_breakdown:
            for bracket in entry["brackets"]:
                bracket.update(parse_percent=0, median_percent=0)
        for entry in high_damage.mplus_dps_breakdown:
            for bracket in entry["brackets"]:
                bracket.update(parse_percent=100, median_percent=100)
        assert candidate_fit(low_damage, _listing(target)).score == candidate_fit(
            high_damage, _listing(target)
        ).score, (index, "damage")

        if logs:
            stronger = deepcopy(applicant)
            stronger.mplus_dps_breakdown[0]["brackets"][0]["key_level"] += 1
            assert candidate_fit(stronger, _listing(target)).score == candidate_fit(
                applicant, _listing(target)
            ).score, (index, "key")
        if applicant.rio_dungeons:
            stronger_timed = deepcopy(applicant)
            stronger_timed.rio_dungeons[0]["key_level"] += 1
            assert candidate_fit(stronger_timed, _listing(target)).score + 1e-9 >= candidate_fit(
                applicant, _listing(target)
            ).score, (index, "timed key")

        member_scores = tuple(rng.random() * 100 for _ in range(rng.randrange(2, 6)))
        improved_scores = list(member_scores)
        chosen = rng.randrange(len(improved_scores))
        improved_scores[chosen] = min(100, improved_scores[chosen] + rng.random() * 20)
        assert _package_score_for_member_scores(
            tuple(improved_scores), CONTEXT_MPLUS, 0
        ) + 1e-9 >= _package_score_for_member_scores(
            member_scores, CONTEXT_MPLUS, 0
        ), (index, "package")


@pytest.mark.parametrize("status", ["pending", "loading", "error", "not_found", "restricted"])
def test_wcl_status_cannot_break_default_sort_ties_with_identical_rio_evidence(
    status: str,
) -> None:
    first = _applicant(12, [])
    first.mplus_dps_breakdown = []
    second = replace(first, applicant_id="2:1", name="Other-Realm")
    baseline, _baseline_packages, baseline_fits = sort_applicants_grouped_with_package_fits(
        [first, second], _listing(12),
    )
    affected = replace(first, fetch_status=status)
    actual, _actual_packages, actual_fits = sort_applicants_grouped_with_package_fits(
        [affected, second], _listing(12),
    )

    assert actual_fits == baseline_fits
    assert [row.applicant_id for row in actual] == [
        row.applicant_id for row in baseline
    ]


@pytest.mark.parametrize("rating", [0, 2000])
@pytest.mark.parametrize("target", [2, 12])
def test_completed_only_summary_does_not_establish_verified_timed_fit(
    rating: int, target: int,
) -> None:
    applicant = _applicant(target, [], rio=False)
    applicant.mplus_dps_breakdown = []
    applicant.score = rating
    applicant.rio_profile = True
    applicant.rio_dungeon_count = len(DUNGEONS)
    applicant.rio_summary_target_key = target
    applicant.rio_completed_at_or_above_minus1 = len(DUNGEONS)
    timed = replace(
        applicant, rio_best_key=max(2, target - 1),
        rio_timed_at_or_above=len(DUNGEONS) if target == 2 else 0,
        rio_timed_at_or_above_minus1=len(DUNGEONS),
        rio_timed_at_or_above_minus2=len(DUNGEONS),
    )
    fit = candidate_fit(applicant, _listing(target))

    assert not fit.is_available
    assert candidate_fit(timed, _listing(target)).is_available
    assert candidate_fit(timed, _listing(target)).score > 0
    assert fit.dungeon_count == 0
    assert fit.relevant_dungeon_count == 0
    assert fit.primary_key == 0


def test_verified_timed_summary_retains_its_original_floors_after_target_changes() -> None:
    applicant = _applicant(12, [], rio=False)
    applicant.mplus_dps_breakdown = []
    applicant.rio_profile = True
    applicant.rio_dungeon_count = len(DUNGEONS)
    applicant.rio_summary_target_key = 12
    applicant.rio_timed_at_or_above = len(DUNGEONS)
    applicant.rio_timed_at_or_above_minus1 = len(DUNGEONS)
    applicant.rio_timed_at_or_above_minus2 = len(DUNGEONS)
    applicant.rio_best_key = 12

    current = candidate_fit(applicant, _listing(12))
    harder = candidate_fit(applicant, _listing(13))
    easier = candidate_fit(applicant, _listing(11))

    assert current.is_available and harder.is_available and easier.is_available
    assert harder.score < current.score <= easier.score
    assert harder.primary_key == current.primary_key == easier.primary_key == 12
    assert harder.dungeon_count == current.dungeon_count == easier.dungeon_count == len(DUNGEONS)


@pytest.mark.parametrize("rating", [0, 1, 1200, 3400, 10000])
@pytest.mark.parametrize("main_rating", [0, 5000])
def test_scalar_rating_never_manufactures_verified_timed_fit(
    rating: int, main_rating: int,
) -> None:
    applicant = _applicant(12, [], rio=False)
    applicant.mplus_dps_breakdown = []
    applicant.score = rating
    applicant.main_score = main_rating
    fit = candidate_fit(applicant, _listing(12))

    assert not fit.is_available
    assert fit.display == ""
    assert fit.colour is None


@pytest.mark.parametrize("completed_count", [0, 1, 8, 10**9])
@pytest.mark.parametrize("logged_key", [2, 12, 40])
def test_arbitrary_untimed_counts_and_wcl_keys_cannot_change_timed_fit(
    completed_count: int, logged_key: int,
) -> None:
    baseline = _applicant(12, [])
    baseline.mplus_dps_breakdown = []
    baseline.rio_profile = True
    baseline.rio_summary_target_key = 12
    baseline.rio_dungeon_count = len(DUNGEONS)
    baseline.rio_timed_at_or_above = len(DUNGEONS)
    baseline.rio_timed_at_or_above_minus1 = len(DUNGEONS)
    baseline.rio_timed_at_or_above_minus2 = len(DUNGEONS)
    affected = replace(
        baseline,
        rio_completed_at_or_above_minus1=completed_count,
        mplus_dps_breakdown=[
            {
                "name": name, "key_level": logged_key,
                "parse_percent": 100 if index % 2 else 0,
                "median_percent": 100 if index % 2 else 0,
                "run_count": 1000,
            }
            for index, name in enumerate(DUNGEONS)
        ],
    )
    before = candidate_fit(baseline, _listing(12))
    after = candidate_fit(affected, _listing(12))

    assert after.is_available
    assert after.score == before.score
    assert after.primary_key == before.primary_key
    assert after.best_nearby_key == before.best_nearby_key
    assert after.coverage == before.coverage
    assert after.confidence == before.confidence
    assert after.dungeon_count == before.dungeon_count
    assert after.relevant_dungeon_count == before.relevant_dungeon_count


@pytest.mark.parametrize(
    "flags",
    [{"timed": False}, {"timed": None}, {"timed": 1}, {"chests": 0}, {"upgrades": 0}],
)
def test_explicitly_untimed_rio_rows_cannot_create_or_inflate_fit(
    flags: dict[str, object],
) -> None:
    baseline = _applicant(12, [])
    baseline.mplus_dps_breakdown = []
    contradicted = {"name": DUNGEONS[0], "key_level": 40, **flags}
    augmented = replace(baseline, rio_dungeons=baseline.rio_dungeons + [contradicted])
    only_untimed = replace(baseline, rio_dungeons=[contradicted], score=10000)

    assert candidate_fit(augmented, _listing(12)).score == candidate_fit(
        baseline, _listing(12)
    ).score
    assert candidate_fit(augmented, _listing(12)).primary_key == 12
    assert not candidate_fit(only_untimed, _listing(12)).is_available


def test_orphaned_timed_summary_fields_cannot_modify_named_timed_readiness() -> None:
    baseline = _applicant(12, [])
    baseline.mplus_dps_breakdown = []
    orphaned = replace(
        baseline, rio_profile=False, rio_summary_target_key=12,
        rio_summary_dungeon_name=DUNGEONS[0], rio_best_key=40,
        rio_best_dungeon_key=40, rio_timed_at_or_above=8,
        rio_timed_at_or_above_minus1=8, rio_timed_at_or_above_minus2=8,
    )
    before = candidate_fit(baseline, _listing(12))
    after = candidate_fit(orphaned, _listing(12))

    assert after.score == before.score
    assert after.primary_key == before.primary_key == 12
    assert after.same_dungeon_rio_key == before.same_dungeon_rio_key
    assert after.coverage == before.coverage
    assert not candidate_fit(replace(orphaned, rio_dungeons=[]), _listing(12)).is_available
