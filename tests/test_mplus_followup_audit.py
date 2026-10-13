"""Independent regressions for merging differently aged verified timed sources."""

from dataclasses import replace
import random

import pytest

from applicant_scout.constants import MPLUS_ENCOUNTERS
from applicant_scout.scoring import candidate_fit
from applicant_scout.state import Applicant, Listing


DUNGEONS = tuple(name for _alias, _encounter, name in MPLUS_ENCOUNTERS)


def _listing(target: int) -> Listing:
    return Listing(
        activity_id=0,
        dungeon_name="Mythic+",
        listing_name=f"+{target}",
        comment="",
        key_level=target,
        category_id=2,
    )


def _fresh_summary(target: int) -> Applicant:
    return Applicant(
        applicant_id="1:1",
        name="Player-Realm",
        cls="MAGE",
        spec_id=63,
        ilvl=280,
        score=0,
        role="DAMAGER",
        rio_profile=True,
        rio_best_key=target + 4,
        rio_dungeon_count=len(DUNGEONS),
        rio_timed_at_or_above=len(DUNGEONS),
        rio_timed_at_or_above_minus1=len(DUNGEONS),
        rio_timed_at_or_above_minus2=len(DUNGEONS),
        rio_summary_target_key=target,
    )


@pytest.mark.parametrize("target", [8, 12, 18])
@pytest.mark.parametrize("local_gap", [0, 1, 4])
def test_final_older_local_row_cannot_discard_fresher_timed_summary(
    target: int, local_gap: int,
) -> None:
    # The addon may hold newer in-memory RIO data than the on-disk local reader.
    # Both sources establish lower bounds; completeness is not a freshness proof.
    applicant = _fresh_summary(target)
    older_rows = [
        {"name": name, "key_level": target - local_gap}
        for name in DUNGEONS
    ]
    before = candidate_fit(replace(applicant, rio_dungeons=older_rows[:-1]), _listing(target))
    after = candidate_fit(replace(applicant, rio_dungeons=older_rows), _listing(target))

    assert before.is_available and after.is_available
    assert after.score >= before.score
    assert after.primary_key == before.primary_key == target + 4
    assert after.relevant_dungeon_count >= before.relevant_dungeon_count


@pytest.mark.parametrize("target", [8, 12, 18])
def test_any_subset_of_older_named_rows_preserves_fresh_summary_bounds(target: int) -> None:
    applicant = _fresh_summary(target)
    summary_fit = candidate_fit(applicant, _listing(target))
    previous = summary_fit
    for count in range(1, len(DUNGEONS) + 1):
        enriched = replace(applicant, rio_dungeons=[
            {"name": name, "key_level": target - 4}
            for name in DUNGEONS[:count]
        ])
        fit = candidate_fit(enriched, _listing(target))

        assert fit.score >= previous.score
        assert fit.score >= summary_fit.score
        assert fit.dungeon_count == summary_fit.dungeon_count
        assert fit.relevant_dungeon_count == summary_fit.relevant_dungeon_count
        previous = fit


def test_older_summary_cannot_discard_stronger_complete_named_rows() -> None:
    # Conservative fusion must work in both age directions and must not sum
    # possibly overlapping eight-dungeon sources into sixteen observations.
    applicant = replace(_fresh_summary(12), rio_best_key=12)
    rows = [{"name": name, "key_level": 16} for name in DUNGEONS]
    named_only = replace(applicant, rio_profile=False, rio_dungeons=rows)
    merged = replace(applicant, rio_dungeons=rows)
    named_fit = candidate_fit(named_only, _listing(12))
    merged_fit = candidate_fit(merged, _listing(12))

    assert merged_fit.score == named_fit.score
    assert merged_fit.dungeon_count == len(DUNGEONS)
    assert merged_fit.relevant_dungeon_count == len(DUNGEONS)
    assert merged_fit.primary_key == 16


@pytest.mark.parametrize("captured_target", [8, 12, 18])
def test_timed_count_floors_keep_their_captured_level_across_target_changes(
    captured_target: int,
) -> None:
    applicant = _fresh_summary(captured_target)
    # Counts prove eight different dungeons at least at the captured level;
    # only the overall best has an exact known level four higher.
    conservative_rows = [
        {"name": name, "key_level": captured_target + (4 if index == 0 else 0)}
        for index, name in enumerate(DUNGEONS)
    ]
    reference = replace(applicant, rio_profile=False, rio_dungeons=conservative_rows)
    previous = None
    for requested_target in range(2, 25):
        fit = candidate_fit(applicant, _listing(requested_target))
        reference_fit = candidate_fit(reference, _listing(requested_target))

        assert fit.is_available
        assert fit.score == pytest.approx(reference_fit.score)
        assert fit.dungeon_count == len(DUNGEONS)
        assert fit.relevant_dungeon_count == reference_fit.relevant_dungeon_count
        assert fit.primary_key == captured_target + 4
        if previous is not None:
            assert fit.score <= previous.score
        previous = fit


def test_timed_global_best_survives_capture_without_a_requested_key() -> None:
    applicant = replace(
        _fresh_summary(12),
        rio_summary_target_key=0,
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
    )
    reference = replace(
        applicant, rio_profile=False,
        rio_dungeons=[{"name": DUNGEONS[0], "key_level": 16}],
    )
    fit = candidate_fit(applicant, _listing(12))
    reference_fit = candidate_fit(reference, _listing(12))

    assert fit.is_available
    assert fit.score == reference_fit.score
    assert fit.primary_key == 16
    assert fit.dungeon_count == 1


@pytest.mark.parametrize("captured_target", [
    0, 1, 51, 63, 64, 255, 10**18, -1, None, False, True, "bad",
    float("nan"), float("inf"), 12.0,
])
def test_counts_without_a_valid_capture_level_cannot_invent_timed_keys(
    captured_target: object,
) -> None:
    applicant = replace(
        _fresh_summary(12), rio_best_key=0,
        rio_summary_target_key=captured_target,
    )
    assert not candidate_fit(applicant, _listing(12)).is_available


@pytest.mark.parametrize("field_name", ["rio_best_key", "rio_best_dungeon_key"])
@pytest.mark.parametrize("key", [2, 50])
def test_summary_exact_key_accepts_both_addon_producer_boundaries(
    field_name: str, key: int,
) -> None:
    applicant = replace(
        _fresh_summary(12),
        rio_best_key=0,
        rio_best_dungeon_key=0,
        rio_summary_target_key=0,
        rio_summary_activity_id=1930,
        rio_summary_dungeon_name=DUNGEONS[0],
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
    )
    applicant = replace(applicant, **{field_name: key})
    reference = replace(
        applicant,
        rio_profile=False,
        rio_dungeons=[{"name": DUNGEONS[0], "key_level": key}],
    )
    fit = candidate_fit(applicant, _listing(12))
    reference_fit = candidate_fit(reference, _listing(12))

    assert fit.is_available
    assert fit.score == reference_fit.score
    assert fit.primary_key == key
    assert fit.dungeon_count == 1


@pytest.mark.parametrize("field_name", ["rio_best_key", "rio_best_dungeon_key"])
@pytest.mark.parametrize("key", [
    0, 1, 51, 63, 64, 255, 10**18, -1, None, True, "bad",
    float("nan"), float("inf"), 12.0,
])
def test_invalid_summary_exact_key_cannot_manufacture_timed_history(
    field_name: str, key: object,
) -> None:
    applicant = replace(
        _fresh_summary(12),
        rio_best_key=0,
        rio_best_dungeon_key=0,
        rio_summary_target_key=0,
        rio_summary_activity_id=1930,
        rio_summary_dungeon_name=DUNGEONS[0],
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
    )
    applicant = replace(applicant, **{field_name: key})

    assert not candidate_fit(applicant, _listing(12)).is_available


@pytest.mark.parametrize("captured_key", [2, 50])
def test_count_floor_accepts_both_addon_capture_boundaries(captured_key: int) -> None:
    applicant = replace(
        _fresh_summary(12),
        rio_best_key=0,
        rio_summary_target_key=captured_key,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=1,
        rio_timed_at_or_above_minus2=1,
    )
    reference = replace(
        applicant, rio_profile=False,
        rio_dungeons=[{"name": DUNGEONS[0], "key_level": captured_key}],
    )
    fit = candidate_fit(applicant, _listing(12))

    assert fit.is_available
    assert fit.score == candidate_fit(reference, _listing(12)).score
    assert fit.dungeon_count == 1
    assert fit.primary_key == 0  # A count floor does not reveal the exact best.


@pytest.mark.parametrize("key", [2, 50, 51, 63])
def test_local_timed_row_uses_its_six_bit_domain_instead_of_summary_cap(key: int) -> None:
    applicant = replace(
        _fresh_summary(12),
        rio_profile=False,
        rio_dungeons=[{"name": DUNGEONS[0], "key_level": key, "upgrades": 1}],
    )
    fit = candidate_fit(applicant, _listing(12))

    assert fit.is_available
    assert fit.primary_key == key
    assert fit.dungeon_count == 1


@pytest.mark.parametrize("key", [
    0, 1, 64, 255, 10**18, -1, None, True, "bad",
    float("nan"), float("inf"), 12.0,
])
def test_invalid_local_row_key_cannot_establish_or_raise_timed_readiness(key: object) -> None:
    valid = {"name": DUNGEONS[0], "key_level": 12, "upgrades": 1}
    malformed = {"name": DUNGEONS[1], "key_level": key, "upgrades": 1}
    applicant = replace(_fresh_summary(12), rio_profile=False, rio_dungeons=[malformed])
    baseline = replace(applicant, rio_dungeons=[valid])
    enriched = replace(applicant, rio_dungeons=[valid, malformed])

    assert not candidate_fit(applicant, _listing(12)).is_available
    assert candidate_fit(enriched, _listing(12)) == candidate_fit(baseline, _listing(12))


def test_same_dungeon_timed_best_retains_identity_when_requested_key_changes() -> None:
    applicant = replace(
        _fresh_summary(8),
        rio_best_key=16,
        rio_best_dungeon_key=16,
        rio_summary_activity_id=1930,
        rio_summary_dungeon_name=DUNGEONS[0],
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=1,
        rio_timed_at_or_above_minus2=1,
    )
    target = replace(_listing(12), activity_id=1930, dungeon_name="Алтарь Клыков")
    reference = replace(
        applicant, rio_profile=False,
        rio_dungeons=[{"name": DUNGEONS[0], "key_level": 16}],
    )
    fit = candidate_fit(applicant, target)
    reference_fit = candidate_fit(reference, target)

    assert fit.is_available
    assert fit.score == reference_fit.score
    assert fit.same_dungeon_rio_key == 16
    assert fit.dungeon_count == 1


@pytest.mark.parametrize("total_completed_dungeons", [1, 2, 8])
def test_adding_only_untimed_dungeons_to_summary_cannot_change_timed_fit(
    total_completed_dungeons: int,
) -> None:
    applicant = replace(
        _fresh_summary(12),
        rio_dungeon_count=1,
        rio_timed_at_or_above=1,
        rio_timed_at_or_above_minus1=1,
        rio_timed_at_or_above_minus2=1,
        rio_dungeons=[{"name": DUNGEONS[0], "key_level": 8}],
    )
    with_untimed = replace(applicant, rio_dungeon_count=total_completed_dungeons)

    assert candidate_fit(with_untimed, _listing(12)) == candidate_fit(applicant, _listing(12))


@pytest.mark.parametrize("named_local_rows", [False, True])
def test_enriched_profile_cannot_resurrect_an_absent_raw_summary(
    named_local_rows: bool,
) -> None:
    applicant = replace(
        _fresh_summary(12),
        rio_transport_profile=False,
        rio_dungeons=[{"name": DUNGEONS[0], "key_level": 8}] if named_local_rows else [],
    )
    reference = replace(
        applicant,
        rio_profile=False,
        rio_best_key=0,
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
    )

    assert candidate_fit(applicant, _listing(12)) == candidate_fit(reference, _listing(12))


def test_explicit_raw_profile_preserves_verified_summary_despite_enrichment_flag() -> None:
    source = _fresh_summary(12)
    enriched = replace(source, rio_profile=False, rio_transport_profile=True)

    assert candidate_fit(enriched, _listing(12)) == candidate_fit(source, _listing(12))
    assert candidate_fit(enriched, _listing(12)).is_available


def test_captured_dungeon_best_remains_general_evidence_after_dungeon_changes() -> None:
    applicant = replace(
        _fresh_summary(20),
        rio_best_key=16,
        rio_best_dungeon_key=12,
        rio_summary_activity_id=1930,
        rio_summary_dungeon_name="Алтарь Клыков",
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
    )
    target = replace(_listing(12), activity_id=1173, dungeon_name="Рубиновые Омуты Жизни")
    # A higher global best cannot be the captured dungeon's lower best, so
    # two different timed dungeon histories are proved even below count floors.
    reference = replace(
        applicant, rio_profile=False,
        rio_dungeons=[
            {"name": DUNGEONS[0], "key_level": 12},
            {"name": DUNGEONS[1], "key_level": 16},
        ],
    )
    fit = candidate_fit(applicant, target)
    reference_fit = candidate_fit(reference, target)

    assert fit.is_available
    assert fit.score == reference_fit.score
    assert fit.dungeon_count == 2
    assert fit.same_dungeon_rio_key == 0


@pytest.mark.parametrize("activity", [1930, 1173])
def test_new_higher_same_dungeon_row_keeps_proved_distinct_old_global_best(activity: int) -> None:
    applicant = replace(
        _fresh_summary(20),
        rio_best_key=16,
        rio_best_dungeon_key=12,
        rio_summary_activity_id=1930,
        rio_summary_dungeon_name=DUNGEONS[0],
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
    )
    target_name = DUNGEONS[0] if activity == 1930 else DUNGEONS[4]
    target = replace(_listing(12), activity_id=activity, dungeon_name=target_name)
    enriched = replace(applicant, rio_dungeons=[{"name": DUNGEONS[0], "key_level": 18}])
    before = candidate_fit(applicant, target)
    after = candidate_fit(enriched, target)

    assert before.is_available and after.is_available
    assert after.score >= before.score
    # The captured global +16 was strictly higher than captured Altar +12.
    # It therefore belonged to a different dungeon; Altar's new +18 cannot
    # erase that second dungeon even though it now exceeds the old global best.
    assert before.dungeon_count == after.dungeon_count == 2
    assert after.relevant_dungeon_count == 2


@pytest.mark.parametrize("activity,target", [(1930, 12), (1173, 16)])
def test_captured_localized_aliases_share_one_canonical_timed_history(
    activity: int, target: int,
) -> None:
    applicant = replace(
        _fresh_summary(20),
        rio_best_key=16,
        rio_best_dungeon_key=4,
        rio_summary_activity_id=1930,
        rio_summary_dungeon_name="Алтарь Клыков",
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
    )
    target_name = "Алтарь Клыков" if activity == 1930 else DUNGEONS[4]
    listing = replace(_listing(target), activity_id=activity, dungeon_name=target_name)
    english = {"name": DUNGEONS[0], "key_level": 18}
    localized = {"name": "Алтарь Клыков", "key_level": 18}
    reference = candidate_fit(replace(applicant, rio_dungeons=[english]), listing)
    translated = candidate_fit(replace(applicant, rio_dungeons=[localized]), listing)
    duplicated = candidate_fit(replace(applicant, rio_dungeons=[english, localized]), listing)

    assert translated == reference
    assert duplicated == reference


def test_conflicting_captured_known_name_cannot_rebind_another_dungeon_row() -> None:
    applicant = replace(
        _fresh_summary(20),
        rio_best_key=16,
        rio_best_dungeon_key=4,
        rio_summary_activity_id=1930,
        rio_summary_dungeon_name=DUNGEONS[4],  # Activity proves Altar; text is stale.
        rio_timed_at_or_above=0,
        rio_timed_at_or_above_minus1=0,
        rio_timed_at_or_above_minus2=0,
        rio_dungeons=[{"name": DUNGEONS[4], "key_level": 18}],
    )
    listing = replace(_listing(12), activity_id=1173, dungeon_name=DUNGEONS[4])
    canonical = replace(applicant, rio_summary_dungeon_name=DUNGEONS[0])

    assert candidate_fit(applicant, listing) == candidate_fit(canonical, listing)
    assert candidate_fit(applicant, listing).same_dungeon_rio_key == 18


def test_mixed_source_age_grid_is_monotone_and_never_exceeds_known_history() -> None:
    rng = random.Random(20261013)
    for _ in range(120):
        captured = rng.randint(2, 22)
        current_levels = [rng.randint(2, 26) for _ in DUNGEONS]
        older_rows = [
            {"name": name, "key_level": rng.randint(2, level)}
            for name, level in zip(DUNGEONS, current_levels, strict=True)
        ]
        rng.shuffle(older_rows)
        summary = replace(
            _fresh_summary(captured),
            rio_best_key=max(current_levels),
            rio_best_dungeon_key=current_levels[0],
            rio_summary_activity_id=1930,
            rio_summary_dungeon_name=DUNGEONS[0],
            rio_timed_at_or_above=sum(level >= captured for level in current_levels),
            rio_timed_at_or_above_minus1=sum(
                level >= max(2, captured - 1) for level in current_levels
            ),
            rio_timed_at_or_above_minus2=sum(
                level >= max(2, captured - 2) for level in current_levels
            ),
        )
        complete = replace(summary, rio_profile=False, rio_dungeons=[
            {"name": name, "key_level": level}
            for name, level in zip(DUNGEONS, current_levels, strict=True)
        ])
        for goal in {max(2, captured - 3), captured, captured + 3}:
            for activity, display_name in [(1930, "Алтарь Клыков"), (1173, DUNGEONS[4])]:
                target = replace(_listing(goal), activity_id=activity, dungeon_name=display_name)
                upper_bound = candidate_fit(complete, target)
                previous = candidate_fit(summary, target)
                assert previous.is_available
                for count in range(1, len(DUNGEONS) + 1):
                    enriched = replace(summary, rio_dungeons=older_rows[:count])
                    fit = candidate_fit(enriched, target)

                    assert fit.is_available
                    assert fit.score + 1e-9 >= previous.score
                    assert fit.score <= upper_bound.score + 1e-9
                    assert previous.dungeon_count <= fit.dungeon_count <= len(DUNGEONS)
                    assert fit.relevant_dungeon_count <= len(DUNGEONS)
                    previous = fit
