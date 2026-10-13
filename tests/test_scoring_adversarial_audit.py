"""Source contradictions and aggregate boundaries in target experience estimates."""

from dataclasses import replace
import math
from typing import Any, cast

import pytest

from applicant_scout.constants import MPLUS_ENCOUNTERS
from applicant_scout.scoring import candidate_fit, package_fit
from applicant_scout.state import Applicant, Listing


DUNGEONS = tuple(name for _alias, _encounter, name in MPLUS_ENCOUNTERS)


def _listing(key: int = 12) -> Listing:
    return Listing(1930, DUNGEONS[0], "Listing", "", key_level=key, category_id=2)


def _member(levels: list[int], *, role: str = "DAMAGER", index: int = 1) -> Applicant:
    cls, spec = {
        "DAMAGER": ("MAGE", 63),
        "TANK": ("WARRIOR", 73),
        "HEALER": ("PRIEST", 257),
    }[role]
    return Applicant(
        f"1:{index}", f"Player{index}-Realm", cls, spec, 280, 0, role,
        rio_dungeons=[
            {"name": name, "key_level": level}
            for name, level in zip(DUNGEONS, levels)
        ],
    )


@pytest.mark.parametrize("role", ["DAMAGER", "TANK", "HEALER"])
@pytest.mark.parametrize("size", [2, 3, 4, 5])
def test_identical_actual_timed_profiles_cannot_gain_a_colour_band_as_a_package(
    role: str, size: int,
) -> None:
    # These real key coefficients yield exactly 24.5 before aggregation.
    # The displayed half-to-even 24 and grey band must survive grouping.
    applicant = _member([12, 2, 2, 2, 8, 11, 11, 11], role=role)
    listing = _listing()
    individual = candidate_fit(applicant, listing)
    assert individual.is_available
    assert individual.score == 24.5
    assert individual.display == "24 +12"
    assert individual.colour == "#666666"
    group = package_fit([
        replace(applicant, applicant_id=f"1:{index}", name=f"Player{index}-Realm")
        for index in range(1, size + 1)
    ], listing)

    assert group.is_complete
    assert group.score == individual.score
    assert group.display == f"G{size} ~24"
    assert group.colour == individual.colour


@pytest.mark.parametrize("key", [
    1, 51, 63, 255, 10**18, True, None, "12", 12.0,
    float("inf"), float("-inf"), float("nan"),
])
def test_invalid_current_target_cannot_become_an_observed_player_or_group_score(key: object) -> None:
    members = [_member([12] * len(DUNGEONS), index=index) for index in (1, 2)]
    listing = _listing(cast(Any, key))
    individual = candidate_fit(members[0], listing)
    group = package_fit(members, listing)

    assert not individual.is_available
    assert individual.display == ""
    assert individual.colour is None
    assert not group.is_complete
    assert group.display == ""
    assert group.colour is None


@pytest.mark.parametrize("key", [2, 50])
def test_valid_current_target_boundaries_retain_known_experience_including_zero(key: int) -> None:
    applicant = _member([12] * len(DUNGEONS))
    individual = candidate_fit(applicant, _listing(key))
    group = package_fit([applicant, replace(applicant, applicant_id="1:2")], _listing(key))

    assert individual.is_available
    assert group.is_complete
    assert math.isfinite(individual.score) and math.isfinite(group.score)
    if key == 50:
        assert individual.score == group.score == 0
        assert individual.colour == group.colour == "#666666"


def test_mixed_role_partial_package_resolves_unknown_to_observed_timed_zero() -> None:
    zero = _member([2], role="TANK")
    strong = _member([12] * len(DUNGEONS), role="DAMAGER", index=2)
    unknown = replace(
        _member([], role="HEALER", index=3),
        score=10000, main_score=10000, fetch_status="restricted",
        mplus_dps=100, mplus_hps=100,
    )
    listing = _listing()
    partial = package_fit([zero, strong, unknown], listing)

    assert candidate_fit(zero, listing).is_available
    assert candidate_fit(zero, listing).score == 0
    assert not candidate_fit(unknown, listing).is_available
    assert not partial.is_complete
    assert partial.available_count == 2 and partial.unavailable_count == 1
    assert partial.display == "G3 partial" and partial.colour is None
    assert partial.low_score == 0  # Observed zero belongs to known members.

    observed_zero = replace(unknown, rio_dungeons=[{"name": DUNGEONS[0], "key_level": 2}])
    complete = package_fit([zero, strong, observed_zero], listing)
    reversed_group = package_fit([observed_zero, strong, zero], listing)
    assert complete.is_complete
    assert complete.available_count == 3 and complete.unavailable_count == 0
    assert complete.status_penalty == 0
    assert complete.tank_count == complete.healer_count == complete.dps_count == 1
    assert complete.low_score == 0
    assert 0 < complete.score < complete.high_score
    assert complete.score == pytest.approx(reversed_group.score)
    assert complete.display == reversed_group.display
    assert complete.colour == reversed_group.colour


@pytest.mark.parametrize("named_history", [False, True])
@pytest.mark.parametrize(
    "best,at,minus1,minus2",
    [(11, 1, 1, 1), (10, 0, 1, 1), (9, 0, 0, 1)],
)
def test_positive_global_best_contradicting_count_floors_cannot_supply_fit(
    best: int, at: int, minus1: int, minus2: int, named_history: bool,
) -> None:
    applicant = replace(
        _member([8] if named_history else []),
        rio_profile=True,
        rio_transport_profile=True,
        rio_summary_target_key=12,
        rio_best_key=best,
        rio_timed_at_or_above=at,
        rio_timed_at_or_above_minus1=minus1,
        rio_timed_at_or_above_minus2=minus2,
        rio_dungeon_count=1,
    )
    reference = replace(applicant, rio_profile=False, rio_transport_profile=False)
    fit = candidate_fit(applicant, _listing())
    expected = candidate_fit(reference, _listing())

    # A summary's positive exact overall maximum cannot be below a key floor
    # it simultaneously claims to have timed. Its independent local rows survive.
    assert fit.is_available == expected.is_available
    assert fit.score == expected.score
    assert fit.primary_key == expected.primary_key
    assert fit.dungeon_count == expected.dungeon_count
    assert fit.colour == expected.colour
    if not named_history:
        assert fit.display == ""
        partner = _member([12] * len(DUNGEONS), index=2)
        group = package_fit([applicant, partner], _listing())
        assert not group.is_complete
        assert group.available_count == 1
        assert group.unavailable_count == 1
        assert group.display == "G2 partial" and group.colour is None


@pytest.mark.parametrize("floor_field", [
    "rio_timed_at_or_above", "rio_timed_at_or_above_minus1", "rio_timed_at_or_above_minus2",
])
def test_missing_exact_global_best_does_not_invalidate_legitimate_anonymous_timed_floors(
    floor_field: str,
) -> None:
    applicant = replace(
        _member([]),
        rio_profile=True,
        rio_transport_profile=True,
        rio_summary_target_key=12,
        rio_best_key=0,
        rio_dungeon_count=1,
        **{floor_field: 1},
    )
    fit = candidate_fit(applicant, _listing())

    assert fit.is_available
    assert fit.dungeon_count == 1
    assert fit.primary_key == 0
    assert fit.score > 0


@pytest.mark.parametrize("named_history", [False, True])
def test_positive_global_best_below_bound_captured_dungeon_key_invalidates_only_summary(
    named_history: bool,
) -> None:
    applicant = replace(
        _member([8] if named_history else []),
        rio_profile=True,
        rio_transport_profile=True,
        rio_summary_target_key=12,
        rio_summary_activity_id=1930,
        rio_summary_dungeon_name=DUNGEONS[0],
        rio_best_key=12,
        rio_best_dungeon_key=16,
        rio_dungeon_count=1,
    )
    reference = replace(applicant, rio_profile=False, rio_transport_profile=False)
    fit = candidate_fit(applicant, _listing())
    expected = candidate_fit(reference, _listing())

    assert fit.is_available == expected.is_available
    assert fit.score == expected.score
    assert fit.primary_key == expected.primary_key
    assert fit.dungeon_count == expected.dungeon_count
    assert fit.same_dungeon_rio_key == expected.same_dungeon_rio_key
    assert fit.colour == expected.colour


def test_unknown_overall_maximum_preserves_bound_exact_captured_dungeon_key() -> None:
    applicant = replace(
        _member([]),
        rio_profile=True,
        rio_transport_profile=True,
        rio_summary_target_key=0,
        rio_summary_activity_id=1930,
        rio_summary_dungeon_name=DUNGEONS[0],
        rio_best_key=0,
        rio_best_dungeon_key=16,
    )
    reference = _member([16])
    fit = candidate_fit(applicant, _listing())
    expected = candidate_fit(reference, _listing())

    assert fit.is_available
    assert fit.score == expected.score
    assert fit.primary_key == fit.same_dungeon_rio_key == 16
    assert fit.dungeon_count == 1


@pytest.mark.parametrize("capture_name", ["", "Mythic+", "Unknown"])
def test_unbound_captured_dungeon_value_cannot_revoke_valid_overall_timed_best(
    capture_name: str,
) -> None:
    applicant = replace(
        _member([]),
        rio_profile=True,
        rio_transport_profile=True,
        rio_summary_target_key=0,
        rio_summary_dungeon_name=capture_name,
        rio_best_key=12,
        rio_best_dungeon_key=16,
    )
    reference = replace(applicant, rio_best_dungeon_key=0)

    assert candidate_fit(applicant, _listing()) == candidate_fit(reference, _listing())
    assert candidate_fit(applicant, _listing()).is_available
