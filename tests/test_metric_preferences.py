from __future__ import annotations

import pytest

from applicant_scout.metric_preferences import (
    DEFAULT_METRIC_PREFERENCES,
    MetricPreferences,
    effective_wcl_preferences_for_spec,
)


def test_default_metric_preferences_enable_only_mplus():
    assert DEFAULT_METRIC_PREFERENCES == MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )


def test_metric_preferences_cache_key_round_trips():
    prefs = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=True,
    )

    assert MetricPreferences.from_cache_key(prefs.cache_key()) == prefs


@pytest.mark.parametrize(
    "raw",
    ["", "mp1.n1.h1", "mp2.n1.h1.m1", "mp1.n1.h1.m1.extra", "bad"],
)
def test_metric_preferences_from_cache_key_rejects_malformed(raw):
    assert MetricPreferences.from_cache_key(raw) is None


def test_metric_preferences_covers_enabled_subset():
    broad = MetricPreferences(
        mplus=True,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=True,
    )
    narrow = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=True,
    )

    assert broad.covers(narrow)
    assert narrow.covers(narrow)
    assert not narrow.covers(broad)


def test_metric_preferences_covers_independent_metrics():
    mplus_only = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )
    heroic_only = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )

    assert not mplus_only.covers(heroic_only)
    assert not heroic_only.covers(mplus_only)


def test_effective_wcl_preferences_for_known_spec_preserves_requested_scope():
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )

    assert effective_wcl_preferences_for_spec(71, prefs) == prefs


def test_effective_wcl_preferences_for_unknown_spec_disables_mplus_only():
    prefs = MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )

    assert effective_wcl_preferences_for_spec(0, prefs) == MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
