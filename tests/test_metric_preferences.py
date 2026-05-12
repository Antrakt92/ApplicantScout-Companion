from __future__ import annotations

from applicant_scout.metric_preferences import MetricPreferences


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
