"""Guard seasonal provider colours independently of the Fit score palette."""

from applicant_scout.constants import SEASON_NAME, rio_score_colour
from applicant_scout.rio_score_tiers import (
    RIO_SCORE_BUCKETS,
    RIO_SCORE_SEASON,
    RIO_SCORE_SEASON_NAME,
    RIO_SCORE_SOURCE,
)


def test_palette_is_bound_to_shipped_season():
    assert RIO_SCORE_SEASON_NAME == SEASON_NAME
    assert RIO_SCORE_SOURCE.endswith(f"?season={RIO_SCORE_SEASON}")
    thresholds = [score for score, _colour in RIO_SCORE_BUCKETS]
    assert thresholds == sorted(set(thresholds), reverse=True)
    assert len(thresholds) > 100


def test_current_provider_bands_do_not_use_old_fixed_rarity_thresholds():
    # Observations from the public seasonal provider response, rather than
    # invented percentiles or the unrelated WCL quality thresholds.
    assert rio_score_colour(2200) == "#43ee42"
    assert rio_score_colour(2700) == "#579ab0"
    assert rio_score_colour(3200) == "#8351e8"
    assert rio_score_colour(0) == "#5d5d5d"


def test_score_uses_lower_boundary_of_provider_gradient():
    assert rio_score_colour(2194) != rio_score_colour(2195)
    assert rio_score_colour(2195) == rio_score_colour(2196)
