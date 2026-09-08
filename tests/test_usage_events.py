from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from applicant_scout.metric_preferences import MetricPreferences
import applicant_scout.usage_events as events


class Recorder:
    def __init__(self, enabled=True):
        self.consent_enabled = enabled
        self.events = []

    def record(self, event):
        if not self.consent_enabled:
            return False
        self.events.append(event)
        return True


@pytest.fixture
def clock(monkeypatch):
    value = [1_800_000_000.0]
    monkeypatch.setattr(events.time, "time", lambda: value[0])
    monkeypatch.setattr(events.time, "monotonic", lambda: value[0])
    monkeypatch.setattr(events, "datetime", SimpleNamespace(
        now=lambda tz: datetime.fromtimestamp(value[0], tz=tz)))
    return value


def snapshot(clock, **changes):
    fields = dict(source=SimpleNamespace(mtime_ns=int(clock[0] * 1e9)),
                  applicants=[object()], roster=[], terminal_clear=False,
                  lfg_unavailable=False, applicants_unavailable=False, roster_unavailable=False)
    fields.update(changes)
    return SimpleNamespace(**fields)


def row(**changes):
    fields = dict(fetch_status="ready", mplus_dps=75.0, mplus_hps=None,
                  raid_normal=None, raid_heroic=None, raid_mythic=None,
                  mplus_dps_median=None, mplus_hps_median=None,
                  raid_normal_median=None, raid_heroic_median=None, raid_mythic_median=None,
                  wcl_metric_preferences=MetricPreferences(mplus=True, raid_normal=False,
                                                         raid_heroic=False, raid_mythic=False))
    fields.update(changes)
    return SimpleNamespace(**fields)


def test_no_consent_records_no_activity_or_retroactive_rows(clock):
    recorder = Recorder(False)
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock))
    activity.rows_rendered([row()], visible=True)
    assert recorder.events == []
    recorder.consent_enabled = True
    activity.reset()
    activity.rows_rendered([row()], visible=True)
    assert recorder.events == []


@pytest.mark.parametrize("changes", [
    {"source": None},
    {"source": SimpleNamespace(mtime_ns="yesterday")},
    {"source": SimpleNamespace(mtime_ns=True)},
    {"source": SimpleNamespace(mtime_ns=10**300)},
    {"source": SimpleNamespace(mtime_ns=-1)},
    {"terminal_clear": True},
    {"applicants": [], "roster": []},
    {"applicants_unavailable": True},
    {"lfg_unavailable": True},
    {"applicants": [], "roster": [object()], "roster_unavailable": True},
])
def test_invalid_or_non_authoritative_snapshot_does_not_count(clock, changes):
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock, **changes))
    activity.rows_rendered([row()], visible=True)
    assert recorder.events == []


@pytest.mark.parametrize("offset", [-121, 1])
def test_stale_or_future_screenshot_does_not_count(clock, offset):
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    source = SimpleNamespace(mtime_ns=int((clock[0] + offset) * 1e9))
    activity.snapshot_applied(snapshot(clock, source=source))
    assert recorder.events == []


def test_preconsent_recent_screenshot_is_not_replayed_after_enabling(clock):
    recorder = Recorder(False)
    activity = events.UsageActivity(recorder)
    old_snapshot = snapshot(clock)
    clock[0] += 5
    recorder.consent_enabled = True
    activity.reset()
    activity.snapshot_applied(old_snapshot)
    activity.rows_rendered([row()], visible=True)
    assert recorder.events == []
    activity.snapshot_applied(snapshot(clock))
    assert recorder.events == ["addon_received"]


@pytest.mark.parametrize("changes", [
    {},
    {"applicants": [], "roster": [object()]},
    {"applicants_unavailable": True, "roster": [object()]},
    {"roster_unavailable": True},
])
def test_fresh_authoritative_applicants_or_roster_count(clock, changes):
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock, **changes))
    assert recorder.events == ["addon_received"]


@pytest.mark.parametrize("changes", [
    {"fetch_status": "not_found"}, {"fetch_status": "error"}, {"fetch_status": "loading"},
    {"fetch_status": "restricted"}, {"wcl_metric_preferences": None},
    {"wcl_metric_preferences": MetricPreferences(mplus=False, raid_normal=False,
                                               raid_heroic=False, raid_mythic=False)},
    {"mplus_dps": None}, {"mplus_dps": float("nan")}, {"mplus_dps": float("inf")},
    {"mplus_dps": -1.0}, {"mplus_dps": 101.0}, {"mplus_dps": True},
])
def test_ready_without_actual_enabled_wcl_result_does_not_count(clock, changes):
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock))
    activity.rows_rendered([row(**changes)], visible=True)
    assert recorder.events == ["addon_received"]


@pytest.mark.parametrize("percentile", [0.0, 75.0, 100.0])
def test_visible_numeric_wcl_result_counts_once_per_day(clock, percentile):
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock))
    activity.rows_rendered([row(mplus_dps=percentile)], visible=False)
    assert recorder.events == ["addon_received"]
    activity.rows_rendered([row(mplus_dps=percentile)], visible=True)
    activity.rows_rendered([row(mplus_dps=percentile)], visible=True)
    assert recorder.events == ["addon_received", "wcl_result"]


@pytest.mark.parametrize("metric", ["raid_normal", "raid_heroic", "raid_mythic", "mplus_hps"])
def test_hidden_or_legacy_metrics_do_not_claim_a_displayed_result(clock, metric):
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock))

    activity.rows_rendered([row(mplus_dps=None, **{metric: 75.0})], visible=True)

    assert recorder.events == ["addon_received"]


@pytest.mark.parametrize("scope,metric", [
    ("mplus", "mplus_dps_median"),
    ("raid_normal", "raid_normal_median"),
    ("raid_heroic", "raid_heroic_median"),
    ("raid_mythic", "raid_mythic_median"),
])
def test_visible_median_without_best_counts_as_a_displayed_result(clock, scope, metric):
    preferences = dict(mplus=False, raid_normal=False, raid_heroic=False, raid_mythic=False)
    preferences[scope] = True
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock))

    activity.rows_rendered([row(
        mplus_dps=None, wcl_metric_preferences=MetricPreferences(**preferences),
        **{metric: 75.0},
    )], visible=True)

    assert recorder.events == ["addon_received", "wcl_result"]


@pytest.mark.parametrize("invalidate", ["reset", "revoke", "clear", "expiry"])
def test_prior_activity_cannot_authorize_later_rows_after_invalidation(clock, invalidate):
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock))
    if invalidate == "reset":
        activity.reset()
    elif invalidate == "revoke":
        recorder.consent_enabled = False
    elif invalidate == "clear":
        activity.snapshot_applied(snapshot(clock, terminal_clear=True))
    else:
        clock[0] += 121
    activity.rows_rendered([row()], visible=True)
    assert recorder.events == ["addon_received"]


@pytest.mark.parametrize("authoritative,other,changes", [
    ("party", "applicants", {"applicants_unavailable": True, "roster": [object()]}),
    ("applicants", "party", {"roster_unavailable": True, "roster": [object()]}),
])
def test_fresh_surface_cannot_authorize_stale_other_tab(clock, authoritative, other, changes):
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock, **changes))
    activity.rows_rendered([row()], visible=True, surface=other)
    assert recorder.events == ["addon_received"]
    activity.rows_rendered([row()], visible=True, surface=authoritative)
    assert recorder.events == ["addon_received", "wcl_result"]


def test_old_daily_activity_cannot_authorize_wcl_result_after_midnight(clock):
    clock[0] = 1_800_057_599.0  # One second before UTC midnight.
    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock))
    clock[0] += 2
    activity.rows_rendered([row()], visible=True)
    assert recorder.events == ["addon_received"]
    activity.snapshot_applied(snapshot(clock))
    activity.rows_rendered([row()], visible=True)
    assert recorder.events == ["addon_received", "addon_received", "wcl_result"]


def test_repeated_snapshot_and_paint_never_serialize_player_data(clock):
    class PrivateRow:
        fetch_status = "ready"
        wcl_metric_preferences = row().wcl_metric_preferences
        mplus_dps = 80.0

        @property
        def full_name(self):
            raise AssertionError("usage must not inspect player identity")

        @property
        def realm(self):
            raise AssertionError("usage must not inspect realm")

    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock, applicants=[PrivateRow()]))
    activity.rows_rendered([PrivateRow()], visible=True)
    assert recorder.events == ["addon_received", "wcl_result"]



def test_overlay_hook_excludes_role_filtered_rows(clock):
    from applicant_scout.overlay import OverlayWindow

    recorder = Recorder()
    activity = events.UsageActivity(recorder)
    activity.snapshot_applied(snapshot(clock))
    rows = {"visible": row(fetch_status="loading"), "hidden": row()}
    window = SimpleNamespace(
        usage_activity=activity,
        _active_tab="applicants",
        _active_row_map=lambda: rows,
        _id_by_row=["visible", "hidden"],
        _table=SimpleNamespace(isRowHidden=lambda index: index == 1),
        isVisible=lambda: True,
        restored_snapshot_pending=lambda: False,
    )
    OverlayWindow._record_usage_rows(window)
    assert recorder.events == ["addon_received"]
    window._table.isRowHidden = lambda _index: False
    OverlayWindow._record_usage_rows(window)
    assert recorder.events == ["addon_received", "wcl_result"]
