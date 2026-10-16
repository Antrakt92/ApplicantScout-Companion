"""Adversarial transitions and evidence boundaries for recruitment display."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QTableWidgetItem

from applicant_scout import overlay, overlay_presenters, overlay_rows, scoring
from applicant_scout.constants import (
    CLASS_COLOURS, CURRENT_RAID_ENCOUNTERS, MPLUS_ENCOUNTERS,
    PERCENTILE_BUCKETS, percentile_colour, rio_score_colour,
)
from applicant_scout.rio_score_tiers import RIO_SCORE_BUCKETS
from applicant_scout.state import AppState, Applicant, Listing
from applicant_scout.wcl import CharacterCache, CharacterRanks, WCL_ERROR_RESTRICTED
from applicant_scout.wcl_availability import RaidBossDetails
from applicant_scout.metric_preferences import MetricPreferences


def _listing(context="mplus"):
    raid = context == "raid"
    return Listing(0, "Raid" if raid else MPLUS_ENCOUNTERS[0][2],
                   "Heroic" if raid else "+16", "", key_level=0 if raid else 16,
                   category_id=3 if raid else 2, difficulty_id=15 if raid else 8)


def _app(aid="1:1", *, context="mplus", known=True, key=16, value=60):
    app = Applicant(aid, f"Player{aid}-Realm", "WARRIOR", 71, 300, 3000,
                    "DAMAGER", fetch_status="ready")
    if context == "mplus" and known:
        app.rio_dungeons = [dict(name=MPLUS_ENCOUNTERS[0][2], key_level=key)]
    elif context == "raid" and known:
        app.raid_heroic = app.raid_heroic_median = value
    return app


def _window(qtbot, tmp_path, applicants, listing):
    state = AppState()
    state.listing = listing
    state.applicants = {app.applicant_id: app for app in applicants}
    client = SimpleNamespace(region="EU", last_quota=None,
                             quota_reset_remaining_seconds=lambda: None)
    window = overlay.OverlayWindow(state, client, object(), tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    window._resolve_hover_from_cursor = lambda: None
    window._launch_fetch = lambda _applicant: None
    window._refresh_table()
    return window


def _relative_luminance(colour):
    channels = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


@pytest.mark.parametrize("background", sorted({
    *CLASS_COLOURS.values(), *(colour for _threshold, colour in PERCENTILE_BUCKETS),
}))
def test_class_and_rarity_badge_text_meets_small_text_contrast(background):
    foreground = overlay_presenters.text_colour_for_bg(background)
    low, high = sorted((_relative_luminance(background), _relative_luminance(foreground)))
    assert (high + 0.05) / (low + 0.05) >= 4.5


@pytest.mark.parametrize("value", [0, 24.49, 24.5, 24.51, 49.49, 49.5,
                                   74.5, 94.5, 94.51, 98.5, 98.51, 99.5, 100])
def test_known_fit_palette_matches_rounded_printed_number(value):
    app = _app()
    fit = scoring.CandidateFit(context=scoring.CONTEXT_MPLUS, is_available=True, score=value)
    text, foreground, background = overlay._fit_cell_visuals(app, _listing(), fit=fit)
    assert text == f"~{round(value)}"
    assert background == percentile_colour(round(value))
    assert foreground == overlay_presenters.text_colour_for_bg(background)
    package = scoring.PackageFit(context=scoring.CONTEXT_MPLUS, score=value, size=2,
                                 is_complete=True, available_count=2)
    assert overlay._package_fit_colour(package) == background


@pytest.mark.parametrize("context", ["mplus", "raid"])
@pytest.mark.parametrize("descending", [True, False])
def test_complete_observed_zero_precedes_partial_group_in_manual_fit_sort(context, descending):
    listing = _listing(context)
    known = _app(context=context, key=5, value=0)
    missing = _app("1:2", context=context, known=False)
    zero = _app("2:1", context=context, key=5, value=0)
    rows, packages, fits = overlay_rows.sort_applicants_grouped_with_package_fits(
        [missing, known, zero], listing,
    )
    assert fits[zero.applicant_id].is_available and fits[zero.applicant_id].score == 0
    assert not packages["1"].is_complete and packages["2"].is_complete
    assert rows[0] is zero
    window = SimpleNamespace(_active_manual_sort=lambda: (overlay.COL_FIT, descending),
                             _active_tab="applicants")
    result = overlay.OverlayWindow._sort_rows_by_selected_column(window, rows, listing, packages, fits)
    assert result[0] is zero
    assert overlay._fit_cell(zero, listing, fit=fits[zero.applicant_id]).text() == "~0"


def test_reused_fit_item_drops_group_lanes_and_colour_when_becoming_unknown():
    app = _app()
    listing = _listing()
    partner = replace(app, applicant_id="1:2")
    package = scoring.package_fit([app, partner], listing)
    item = QTableWidgetItem()
    overlay._mplus_group_cell(package, app, listing, item=item)
    assert item.data(overlay.MPLUS_PACKAGE_TEXT_ROLE)
    unknown = replace(app, rio_dungeons=[])
    overlay._fit_cell(unknown, listing, item=item)
    assert item.text() == "—"
    assert item.background().style() == Qt.BrushStyle.NoBrush
    assert item.data(overlay.MPLUS_PACKAGE_TEXT_ROLE) is None
    assert item.data(overlay.MPLUS_INDIVIDUAL_TEXT_ROLE) is None


def test_accessible_fit_values_distinguish_observed_zero_and_unknown(qtbot, tmp_path):
    known_zero = _app("1:1", key=5)
    unknown = _app("2:1", known=False)
    window = _window(qtbot, tmp_path, [known_zero, unknown], _listing())
    zero_item = window._table.item(window._row_for_id[known_zero.applicant_id], overlay.COL_FIT)
    missing_item = window._table.item(window._row_for_id[unknown.applicant_id], overlay.COL_FIT)
    assert "approximately 0" in zero_item.data(Qt.ItemDataRole.AccessibleTextRole)
    assert "Target evidence unknown" in missing_item.data(Qt.ItemDataRole.AccessibleTextRole)


@pytest.mark.parametrize("status", ["pending", "loading"])
def test_missing_mplus_fit_waits_on_relevant_wcl_evidence(status):
    app = _app(known=False)
    app.fetch_status = status
    assert not scoring.candidate_fit(app, _listing()).is_available
    assert overlay._mplus_cell_visuals(app)[0] == "…"
    assert overlay._fit_cell_visuals(app, _listing())[0] == "…"
    assert overlay._fit_cell_visuals(app, _listing("raid"))[0] == "…"


@pytest.mark.parametrize("blocked", ["unknown_spec", "disabled", "private"])
def test_missing_mplus_fit_does_not_spin_for_unusable_wcl_scope(blocked):
    app = _app(known=False)
    app.fetch_status = "loading"
    if blocked == "unknown_spec":
        app.spec_id = 0
    elif blocked == "disabled":
        app.wcl_metric_preferences = MetricPreferences(mplus=False)
    else:
        app.wcl_availability = {"mplus": "restricted"}
    assert not scoring.candidate_fit(app, _listing()).is_available
    assert overlay._fit_cell_visuals(app, _listing())[0] == "—"


def test_restricted_raid_scope_hides_stale_numbers_in_table_and_badges(qtbot, tmp_path):
    applicant = _app(context="raid", value=24)
    applicant.raid_mythic = applicant.raid_mythic_median = 99
    applicant.wcl_availability = {"raid_heroic": "available", "raid_mythic": "restricted"}
    listing = _listing("raid")
    fit = scoring.candidate_fit(applicant, listing)
    assert fit.evidence_source == "H"
    window = _window(qtbot, tmp_path, [applicant], listing)
    row = window._row_for_id[applicant.applicant_id]
    assert window._table.item(row, overlay.COL_M).text() == "—"
    assert window._table.item(row, overlay.COL_M).background().style() == Qt.BrushStyle.NoBrush
    assert window._table.item(row, overlay.COL_H).text() == "24/24"
    window._panel.setApplicantData(applicant, listing, fit=fit)
    assert window._panel._metric_labels["M"].isHidden()
    assert not window._panel._metric_labels["H"].isHidden()


@pytest.mark.parametrize("descending", [True, False])
def test_manual_parse_sort_treats_restricted_scope_as_missing(descending):
    private = _app("1:1", context="raid", value=99)
    private.wcl_availability = {"raid_heroic": "restricted"}
    available = _app("2:1", context="raid", value=24)
    window = SimpleNamespace(_active_manual_sort=lambda: (overlay.COL_H, descending),
                             _active_tab="applicants")
    result = overlay.OverlayWindow._sort_rows_by_selected_column(
        window, [private, available], _listing("raid"), {}, {},
    )
    assert result == [available, private]


def test_restricted_mplus_scope_hides_wcl_only_and_preserves_timed_fit(qtbot, tmp_path):
    applicant = _app()
    applicant.mplus_dps = applicant.mplus_dps_median = 99
    applicant.mplus_dps_breakdown = [dict(name=MPLUS_ENCOUNTERS[0][2], key_level=20,
                                        parse_percent=99, median_percent=99, run_count=3)]
    applicant.wcl_availability = {"mplus": "restricted"}
    fit = scoring.candidate_fit(applicant, _listing())
    assert fit.is_available
    window = _window(qtbot, tmp_path, [applicant], _listing())
    row = window._row_for_id[applicant.applicant_id]
    assert window._table.item(row, overlay.COL_MPLUS).text() == "—"
    assert window._table.item(row, overlay.COL_FIT).text().startswith("~")
    window._panel.setApplicantData(applicant, _listing(), fit=fit)
    assert window._panel._metric_labels["M+"].isHidden()
    assert not window._panel._metric_labels["Fit"].isHidden()
    assert overlay_presenters.wcl_dungeon_rows_by_name(applicant, _listing()) == {}


def test_private_boss_scope_excludes_stale_parse_but_keeps_rio_progress():
    app = _app(context="raid")
    boss_id = CURRENT_RAID_ENCOUNTERS[0][1]
    app.raid_boss_parses = {"H": [dict(encounter_id=boss_id, overall=99, ilvl=99)]}
    app.raid_boss_availability = {"H": "restricted"}
    app.rio_raid_progress = {"H": dict(boss_encounter_ids=[row[1] for row in CURRENT_RAID_ENCOUNTERS[:1]], boss_kills=[3])}
    row = overlay_presenters.raid_boss_rows_for_display(app, ["H"])[0]
    assert row["rio_text"] == "H×3"
    assert row["value"] == "" and row["segments"] == []


@pytest.mark.parametrize("global_private", [False, True])
def test_new_aggregate_privacy_revokes_old_detail_route_but_later_manual_data_can_return(
    qtbot, tmp_path, global_private,
):
    app = _app(context="raid")
    app.name = "Scout-Ravencrest"
    boss_id = CURRENT_RAID_ENCOUNTERS[0][1]
    app.raid_boss_parses = {"H": [dict(encounter_id=boss_id, overall=99, ilvl=99)]}
    app.raid_boss_availability = {"H": "available"}
    app.rio_raid_progress = {"H": dict(boss_encounter_ids=[row[1] for row in CURRENT_RAID_ENCOUNTERS[:1]], boss_kills=[3])}
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    resolved = overlay._fetch_identity_for_applicant(
        app, window._state.player.full_name, "EU", window._metric_preferences,
        window._wcl_runtime_generation, window._listing_session_generation,
    )
    assert resolved is not None
    identity = replace(resolved[0], operation=overlay.FetchOperation())
    window._mark_fetch_in_flight(identity)
    window._mark_fetch_waiting_on_target(identity)
    old_detail = replace(identity, metric_preferences=window._raid_detail_preferences(),
                         operation=overlay.FetchOperation())
    window._mark_raid_boss_fetch_in_flight(old_detail)
    window._mark_raid_boss_fetch_waiting_on_target(old_detail)
    ranks = CharacterRanks(raid_normal=None, raid_normal_median=None,
                           raid_heroic=None, raid_heroic_median=None,
                           mplus_dps=None, mplus_hps=None,
                           raid_mythic=60, raid_mythic_median=60,
                           availability={"raid_heroic": "restricted", "raid_mythic": "available"})
    if global_private:
        ranks.error_kind = WCL_ERROR_RESTRICTED
        ranks.error = "Rankings are private on Warcraft Logs"
        ranks.availability = {scope: "restricted" for scope in
                              ("mplus", "raid_normal", "raid_heroic", "raid_mythic")}
    window._on_fetch_done(identity, ranks)
    if not global_private:
        assert app.raid_boss_availability.get("H") == "restricted"
    assert "H" not in app.raid_boss_parses
    assert not old_detail.operation.is_active()
    row = overlay_presenters.raid_boss_rows_for_display(app, ["H"])[0]
    assert row["rio_text"] == "H×3"
    assert row["value"] == "" and row["segments"] == []
    old_rows = RaidBossDetails({"H": [dict(encounter_id=boss_id, overall=99, ilvl=99)]},
                               availability={"H": "available"})
    window._on_raid_boss_fetch_done(old_detail, old_rows, "")
    assert "H" not in app.raid_boss_parses
    fresh_detail = replace(old_detail, operation=overlay.FetchOperation())
    window._mark_raid_boss_fetch_in_flight(fresh_detail)
    window._mark_raid_boss_fetch_waiting_on_target(fresh_detail)
    fresh_rows = RaidBossDetails({"H": [dict(encounter_id=boss_id, overall=24, ilvl=20)]},
                                 availability={"H": "available"})
    window._on_raid_boss_fetch_done(fresh_detail, fresh_rows, "")
    assert app.wcl_availability["raid_heroic"] == "restricted"
    assert overlay_presenters.raid_boss_rows_for_display(app, ["H"])[0]["value"] == "H 24 / 20"


@pytest.mark.parametrize("state", ["empty", "error", "unavailable", "partial"])
def test_nonprivate_aggregate_state_keeps_independent_available_boss_detail(state):
    app = _app(context="raid")
    boss_id = CURRENT_RAID_ENCOUNTERS[0][1]
    app.raid_boss_parses = {"H": [dict(encounter_id=boss_id, overall=24, ilvl=20)]}
    app.raid_boss_availability = {"H": "available"}
    app.wcl_availability = {"raid_heroic": state}
    assert overlay_presenters.raid_boss_rows_for_display(app, ["H"])[0]["value"] == "H 24 / 20"


def test_aggregate_privacy_retires_other_coalesced_same_character_waiter(qtbot, tmp_path):
    first = _app("1:1", context="raid")
    first.name = "Scout-Ravencrest"
    boss_id = CURRENT_RAID_ENCOUNTERS[0][1]
    first.raid_boss_parses = {"H": [dict(encounter_id=boss_id, overall=99, ilvl=99)]}
    first.raid_boss_availability = {"H": "available"}
    second = replace(first, applicant_id="2:1",
                     raid_boss_parses={"H": [dict(encounter_id=boss_id, overall=99, ilvl=99)]},
                     raid_boss_availability={"H": "available"})
    window = _window(qtbot, tmp_path, [first, second], _listing("raid"))
    resolved = overlay._fetch_identity_for_applicant(first, "", "EU", window._metric_preferences)
    assert resolved is not None
    aggregate = replace(resolved[0], operation=overlay.FetchOperation())
    window._mark_fetch_in_flight(aggregate)
    window._mark_fetch_waiting_on_target(aggregate)
    old_detail = replace(aggregate, metric_preferences=window._raid_detail_preferences(),
                         operation=overlay.FetchOperation())
    window._mark_raid_boss_fetch_in_flight(old_detail)
    window._mark_raid_boss_fetch_waiting_on_target(old_detail)
    peer_detail = replace(old_detail, applicant_id=second.applicant_id, operation=None)
    assert window._coalesce_raid_boss_fetch_if_target_in_flight(peer_detail)
    ranks = CharacterRanks(None, None, 60, None, None, 60, None, None,
                           availability={"raid_heroic": "restricted", "raid_mythic": "available"})
    window._on_fetch_done(aggregate, ranks)
    assert not old_detail.operation.is_active()
    assert window._raid_boss_fetches_in_flight == {}
    old_rows = RaidBossDetails({"H": [dict(encounter_id=boss_id, overall=99, ilvl=99)]},
                               availability={"H": "available"})
    window._on_raid_boss_fetch_done(old_detail, old_rows, "")
    assert "H" not in second.raid_boss_parses
    assert second.raid_boss_availability.get("H") == "restricted"


@pytest.mark.parametrize("global_private", [False, True])
def test_older_aggregate_private_keeps_newer_manual_scope_and_later_live_request(
    qtbot, tmp_path, global_private,
):
    app = _app(context="raid")
    app.name = "Scout-Ravencrest"
    boss_id = CURRENT_RAID_ENCOUNTERS[0][1]
    app.raid_boss_parses = {"H": [dict(encounter_id=boss_id, overall=24, ilvl=20)]}
    app.raid_boss_availability = {"H": "available"}
    app.raid_boss_publication_epochs = {"H": 1}
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    resolved = overlay._fetch_identity_for_applicant(app, "", "EU", window._metric_preferences)
    assert resolved is not None
    aggregate = replace(resolved[0], operation=overlay.FetchOperation(), cache_publication_epoch=0)
    window._mark_fetch_in_flight(aggregate)
    window._mark_fetch_waiting_on_target(aggregate)
    newer_detail = replace(aggregate, metric_preferences=window._raid_detail_preferences(),
                           operation=overlay.FetchOperation(), cache_publication_epoch=2)
    window._mark_raid_boss_fetch_in_flight(newer_detail)
    window._mark_raid_boss_fetch_waiting_on_target(newer_detail)
    ranks = CharacterRanks(None, None, 60, None, None, 60, None, None,
                           availability={"raid_heroic": "restricted", "raid_mythic": "available"})
    if global_private:
        ranks.error = "Rankings are private on Warcraft Logs"
        ranks.error_kind = WCL_ERROR_RESTRICTED
        ranks.availability = {scope: "restricted" for scope in
                              ("mplus", "raid_normal", "raid_heroic", "raid_mythic")}
    window._on_fetch_done(aggregate, ranks)
    assert app.raid_boss_parses["H"][0]["overall"] == 24
    assert app.raid_boss_availability["H"] == "available"
    assert app.raid_boss_publication_epochs["H"] == 1
    assert newer_detail.operation.is_active()


_HEROIC = MetricPreferences(mplus=False, raid_normal=False, raid_heroic=True, raid_mythic=False)
_HEROIC_MYTHIC = replace(_HEROIC, raid_mythic=True)


def _boss_details(*, heroic=99, mythic=24):
    boss_id = CURRENT_RAID_ENCOUNTERS[0][1]
    return RaidBossDetails(
        {"H": [dict(encounter_id=boss_id, name=CURRENT_RAID_ENCOUNTERS[0][2], overall=heroic, ilvl=heroic)],
         "M": [dict(encounter_id=boss_id, name=CURRENT_RAID_ENCOUNTERS[0][2], overall=mythic, ilvl=mythic)]},
        availability={"H": "available", "M": "available"},
    )


def _owned_detail_identity(window, app):
    resolved = overlay._fetch_identity_for_applicant(
        app, "", "EU", window._raid_detail_preferences(),
        window._wcl_runtime_generation, window._listing_session_generation,
    )
    assert resolved is not None
    identity = replace(resolved[0], operation=overlay.FetchOperation(),
                       cache_publication_epoch=window._cache.publication_epoch)
    window._mark_raid_boss_fetch_in_flight(identity)
    window._mark_raid_boss_fetch_waiting_on_target(identity)
    return identity


@pytest.mark.parametrize("preferences,evict", [(_HEROIC, False), (_HEROIC_MYTHIC, False),
                                               (_HEROIC, True)])
@pytest.mark.usefixtures("qapp")
def test_manual_worker_emits_safe_composed_cache_view_after_privacy_publication(
    tmp_path, preferences, evict,
):
    cache = CharacterCache(tmp_path)
    identity = overlay._FetchIdentity("1:1", "scout", "ravencrest", "EU", 71, "DAMAGER",
                                      metric_preferences=preferences)

    class Client:
        def fetch_character_raid_boss_details(self, *_args, **_kwargs):
            private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
            assert cache.put("Scout", "ravencrest", "EU", 71, private,
                             metric_preferences=_HEROIC)
            if evict:
                cache.MAX_ENTRIES = 1
                cache.put("Other", "ravencrest", "EU", 71, CharacterRanks.empty(),
                          metric_preferences=_HEROIC)
            return _boss_details()

    task = overlay._RaidBossFetchTask(identity, "Scout", Client(), cache)
    delivered = []
    observed = []
    task.signals.done.connect(lambda *args: delivered.append(args))
    task.signals.networkDone.connect(lambda *args: observed.append(args))
    try:
        task.run()
        assert len(delivered) == 1
        assert len(observed) == 1 and observed[0][1]["H"][0]["overall"] == 99
        _identity, rows, error, error_kind = delivered[0]
        assert not rows.get("H")
        if evict:
            assert error and error_kind == overlay._RAID_DETAIL_SUPERSEDED
        else:
            assert not error
            assert rows.availability["H"] == "restricted"
            assert rows.publication_epochs["H"] > 0
        if preferences.raid_mythic:
            assert rows["M"][0]["overall"] == 24
    finally:
        cache.close()


def test_queued_manual_completion_rechecks_latest_private_scope_for_all_coalesced_rows(qtbot, tmp_path):
    first = _app("1:1", context="raid")
    first.name = "Scout-Ravencrest"
    second = replace(first, applicant_id="2:1")
    window = _window(qtbot, tmp_path, [first, second], _listing("raid"))
    window._metric_preferences = _HEROIC_MYTHIC
    cache = CharacterCache(tmp_path / "cache")
    window._cache = cache
    identity = _owned_detail_identity(window, first)
    peer = replace(identity, applicant_id=second.applicant_id, operation=None)
    assert window._coalesce_raid_boss_fetch_if_target_in_flight(peer)
    try:
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _boss_details(),
                                           metric_preferences=_HEROIC_MYTHIC)
        queued_rows = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                                  metric_preferences=_HEROIC_MYTHIC)
        assert queued_rows is not None and queued_rows["H"][0]["overall"] == 99
        private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
        assert cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=_HEROIC)
        window._on_raid_boss_fetch_done(identity, queued_rows, "")
        for app in (first, second):
            assert "H" not in app.raid_boss_parses
            assert app.raid_boss_availability["H"] == "restricted"
            assert app.raid_boss_parses["M"][0]["overall"] == 24
            assert app.raid_boss_publication_epochs["H"] > app.raid_boss_publication_epochs["M"]
        assert not identity.operation.is_active()
        assert window._raid_boss_fetches_in_flight == {}
        assert window._raid_boss_fetch_waiters_by_target == {}
    finally:
        cache.close()


def test_rejected_manual_without_current_cache_completes_waiter_and_allows_manual_retry(qtbot, tmp_path):
    app = _app(context="raid")
    app.name = "Scout-Ravencrest"
    app.raid_boss_parses = {"H": _boss_details()["H"]}
    app.raid_boss_availability = {"H": "available"}
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    window._metric_preferences = _HEROIC
    window._cache = SimpleNamespace(publication_epoch=0)
    identity = _owned_detail_identity(window, app)
    window._on_raid_boss_fetch_done(identity, {}, "Boss details changed; load them again.",
                                    overlay._RAID_DETAIL_SUPERSEDED)
    assert not app.raid_boss_parses.get("H")
    assert app.raid_boss_availability["H"] == "unavailable"
    assert not identity.operation.is_active()
    assert window._raid_boss_fetches_in_flight == {}
    assert window._raid_boss_manual_retry_available(identity)


@pytest.mark.parametrize("global_private", [False, True])
def test_old_aggregate_keeps_newer_manual_cache_proof_before_manual_gui_delivery(
    qtbot, tmp_path, global_private,
):
    app = _app(context="raid")
    app.name = "Scout-Ravencrest"
    app.raid_boss_parses = {"H": _boss_details()["H"]}
    app.raid_boss_availability = {"H": "available"}
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    cache = CharacterCache(tmp_path / "cache")
    window._cache = cache
    resolved = overlay._fetch_identity_for_applicant(app, "", "EU", window._metric_preferences)
    assert resolved is not None
    aggregate = replace(resolved[0], operation=overlay.FetchOperation(), cache_publication_epoch=0)
    window._mark_fetch_in_flight(aggregate)
    window._mark_fetch_waiting_on_target(aggregate)
    try:
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71, _boss_details(heroic=24),
                                           metric_preferences=_HEROIC)
        ranks = replace(CharacterRanks.empty(),
                        availability={"raid_heroic": "restricted", "raid_mythic": "available"})
        if global_private:
            ranks.error, ranks.error_kind = "Rankings are private on Warcraft Logs", WCL_ERROR_RESTRICTED
            ranks.availability = {scope: "restricted" for scope in
                                  ("mplus", "raid_normal", "raid_heroic", "raid_mythic")}
        assert cache.put("Scout", "ravencrest", "EU", 71, ranks,
                         metric_preferences=aggregate.metric_preferences,
                         expected_publication_epoch=0)
        window._on_fetch_done(aggregate, ranks)
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        assert app.raid_boss_availability["H"] == "available"
        assert app.raid_boss_publication_epochs["H"] == 1
    finally:
        cache.close()


def test_rejected_old_not_found_worker_preserves_newer_manual_evidence_at_gui_completion(qtbot, tmp_path):
    app = _app(context="raid")
    app.name = "Scout-Ravencrest"
    app.raid_boss_parses = {"H": _boss_details(heroic=24)["H"]}
    app.raid_boss_availability = {"H": "available"}
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    cache = CharacterCache(tmp_path / "cache")
    window._cache = cache
    resolved = overlay._fetch_identity_for_applicant(app, "", "EU", window._metric_preferences)
    assert resolved is not None
    aggregate = replace(resolved[0], operation=overlay.FetchOperation(), cache_publication_epoch=0)
    window._mark_fetch_in_flight(aggregate)
    window._mark_fetch_waiting_on_target(aggregate)

    class Client:
        def fetch_character_ranks(self, *_args, **_kwargs):
            assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                               _boss_details(heroic=24), metric_preferences=_HEROIC)
            return replace(CharacterRanks.empty(), not_found=True)

    task = overlay._FetchTask(aggregate, "Scout", Client(), cache)
    delivered = []
    observed = []
    task.signals.done.connect(lambda *args: delivered.append(args))
    task.signals.networkDone.connect(lambda *args: observed.append(args))
    try:
        task.run()
        assert len(delivered) == 1
        assert len(observed) == 1 and observed[0][1].not_found
        identity, ranks = delivered[0]
        assert not ranks.not_found
        assert ranks.availability["raid_heroic"] == "unavailable"
        window._on_fetch_done(identity, ranks)
        assert app.fetch_status == "ready"
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        assert app.raid_boss_availability["H"] == "available"
        assert not identity.operation.is_active()
        assert window._fetches_in_flight == {}
    finally:
        cache.close()


@pytest.mark.usefixtures("qapp")
def test_rejected_aggregate_worker_drops_cache_projection_if_generation_changes_during_read():
    class Cache:
        generation = 0
        publication_epoch = 0

        def get(self, *_args):
            self.generation += 1
            return replace(CharacterRanks.empty(), raid_heroic=99)

        def put(self, *_args, **_kwargs):
            return False

    client = SimpleNamespace(fetch_character_ranks=lambda *_args, **_kwargs:
                             replace(CharacterRanks.empty(), raid_heroic=24))
    identity = overlay._FetchIdentity("1:1", "scout", "ravencrest", "EU", 71, "DAMAGER",
                                      metric_preferences=_HEROIC)
    task = overlay._FetchTask(identity, "Scout", client, Cache())
    delivered = []
    task.signals.done.connect(lambda *args: delivered.append(args))
    task.run()
    assert len(delivered) == 1
    assert delivered[0][1].raid_heroic is None
    assert delivered[0][1].availability["raid_heroic"] == "unavailable"


def test_queued_old_not_found_does_not_clear_a_later_applied_manual_publication(qtbot, tmp_path):
    app = _app(context="raid")
    app.name = "Scout-Ravencrest"
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    window._metric_preferences = _HEROIC
    cache = CharacterCache(tmp_path / "cache")
    window._cache = cache
    resolved = overlay._fetch_identity_for_applicant(app, "", "EU", window._metric_preferences)
    assert resolved is not None
    aggregate = replace(resolved[0], operation=overlay.FetchOperation(), cache_publication_epoch=0)
    window._mark_fetch_in_flight(aggregate)
    window._mark_fetch_waiting_on_target(aggregate)
    negative = replace(CharacterRanks.empty(), not_found=True)
    try:
        assert cache.put("Scout", "ravencrest", "EU", 71, negative,
                         metric_preferences=_HEROIC, expected_publication_epoch=0)
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                           _boss_details(heroic=24), metric_preferences=_HEROIC,
                                           expected_publication_epoch=cache.publication_epoch)
        detail_identity = _owned_detail_identity(window, app)
        public_rows = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                                  metric_preferences=_HEROIC)
        assert public_rows is not None
        window._on_raid_boss_fetch_done(detail_identity, public_rows, "")
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        window._on_fetch_done(aggregate, negative)
        assert app.fetch_status == "ready"
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        assert app.raid_boss_availability["H"] == "available"
        assert not aggregate.operation.is_active()
        assert window._fetches_in_flight == {}
    finally:
        cache.close()


def test_queued_negative_without_newer_source_keeps_verified_not_found_status(qtbot, tmp_path):
    app = _app(context="raid")
    app.name = "Scout-Ravencrest"
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    window._metric_preferences = _HEROIC
    cache = CharacterCache(tmp_path / "cache")
    window._cache = cache
    resolved = overlay._fetch_identity_for_applicant(app, "", "EU", window._metric_preferences)
    assert resolved is not None
    aggregate = replace(resolved[0], operation=overlay.FetchOperation(), cache_publication_epoch=0)
    window._mark_fetch_in_flight(aggregate)
    window._mark_fetch_waiting_on_target(aggregate)
    negative = replace(CharacterRanks.empty(), not_found=True)
    try:
        assert cache.put("Scout", "ravencrest", "EU", 71, negative,
                         metric_preferences=_HEROIC, expected_publication_epoch=0)
        window._on_fetch_done(aggregate, negative)
        assert app.fetch_status == "not_found"
        assert not aggregate.operation.is_active()
        assert window._fetches_in_flight == {}
    finally:
        cache.close()


def test_queued_manual_cache_receipt_after_generation_clear_keeps_valid_active_result(qtbot, tmp_path):
    app = _app(context="raid")
    app.name = "Scout-Ravencrest"
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    window._metric_preferences = _HEROIC
    cache = CharacterCache(tmp_path / "cache")
    window._cache = cache
    identity = _owned_detail_identity(window, app)
    try:
        assert cache.put_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                           _boss_details(heroic=24), metric_preferences=_HEROIC)
        queued = cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                             metric_preferences=_HEROIC)
        assert queued is not None
        cache.clear()
        window._on_raid_boss_fetch_done(identity, queued, "")
        assert app.raid_boss_parses["H"][0]["overall"] == 24
        assert not identity.operation.is_active()
        assert window._raid_boss_fetches_in_flight == {}
        assert cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                           metric_preferences=_HEROIC) is None
    finally:
        cache.close()


@pytest.mark.parametrize("newer_private", [False, True])
def test_queued_receiptless_generation_fallback_rechecks_source_before_gui_delivery(
    qtbot, tmp_path, newer_private,
):
    app = _app(context="raid")
    app.name = "Scout-Ravencrest"
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    window._metric_preferences = _HEROIC
    cache = CharacterCache(tmp_path / "cache")
    window._cache = cache
    identity = _owned_detail_identity(window, app)
    client = SimpleNamespace(fetch_character_raid_boss_details=lambda *_args, **_kwargs:
                             _boss_details(heroic=99))
    task = overlay._RaidBossFetchTask(identity, "Scout", client, cache)
    queued = []
    task.signals.done.connect(lambda *args: queued.append(args))
    try:
        cache.clear()
        task.run()
        assert len(queued) == 1 and not queued[0][2]
        assert queued[0][1]["H"][0]["overall"] == 99
        assert queued[0][1].publication_epochs == {}
        if newer_private:
            private = replace(CharacterRanks.empty(), availability={"raid_heroic": "restricted"})
            assert cache.put("Scout", "ravencrest", "EU", 71, private, metric_preferences=_HEROIC)
        window._on_raid_boss_fetch_done(*queued[0])
        if newer_private:
            assert not app.raid_boss_parses.get("H")
            assert app.raid_boss_availability["H"] == "restricted"
        else:
            assert app.raid_boss_parses["H"][0]["overall"] == 99
            assert cache.get_raid_boss_details("Scout", "ravencrest", "EU", 71,
                                               metric_preferences=_HEROIC) is None
        assert not identity.operation.is_active()
        assert window._raid_boss_fetches_in_flight == {}
    finally:
        cache.close()


def test_unknown_key_default_parse_sort_does_not_rank_restricted_stale_damage():
    private = _app("1:1")
    private.mplus_dps = 99
    private.wcl_availability = {"mplus": "restricted"}
    available = _app("2:1")
    available.mplus_dps = 24
    listing = replace(_listing(), key_level=0)
    rows, _packages, _fits = overlay_rows.sort_applicants_grouped_with_package_fits(
        [private, available], listing,
    )
    assert rows == [available, private]


def test_cached_inplace_timing_retraction_changes_group_and_preserves_pinned_identity(qtbot, tmp_path):
    first = _app("1:1")
    second = _app("1:2")
    window = _window(qtbot, tmp_path, [first, second], _listing())
    window._pinned_id = second.applicant_id
    window._sync_delegate_and_panel()
    old_package = window._package_fit_by_raw["1"]
    assert old_package.is_complete
    assert not window._panel._metric_labels["Fit"].isHidden()
    second.rio_dungeons[0]["timed"] = False
    window._refresh_table()
    package = window._package_fit_by_raw["1"]
    assert package is not old_package and not package.is_complete
    assert package.available_count == 1
    assert window._pinned_id == second.applicant_id
    assert window._panel._current_applicant is second
    assert window._panel._metric_labels["Fit"].isHidden()
    item = window._table.item(window._row_for_id[second.applicant_id], overlay.COL_FIT)
    assert item.data(overlay.MPLUS_PACKAGE_TEXT_ROLE) == "G2 partial"
    assert item.data(overlay.MPLUS_INDIVIDUAL_TEXT_ROLE) == "—"
    assert item.data(overlay.MPLUS_INDIVIDUAL_BG_ROLE) == ""
    assert "individual Target evidence unknown" in item.data(Qt.ItemDataRole.AccessibleTextRole)
    first_item = window._table.item(window._row_for_id[first.applicant_id], overlay.COL_FIT)
    assert first_item.data(overlay.MPLUS_INDIVIDUAL_TEXT_ROLE).startswith("~")


def test_inplace_scope_retraction_clears_stale_numbers_and_zero_can_return(qtbot, tmp_path):
    app = _app(context="raid", value=99)
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    window._pinned_id = app.applicant_id
    window._sync_delegate_and_panel()
    original_item = window._table.item(0, overlay.COL_H)
    assert original_item.text() == "99/99"
    app.wcl_availability["raid_heroic"] = "restricted"
    window._refresh_table()
    assert window._table.item(0, overlay.COL_H) is original_item
    assert original_item.text() == "—"
    assert original_item.background().style() == Qt.BrushStyle.NoBrush
    assert window._table.item(0, overlay.COL_FIT).text() == "—"
    assert window._panel._metric_labels["Fit"].isHidden()
    app.wcl_availability["raid_heroic"] = "partial"
    app.raid_heroic = 0
    app.raid_heroic_median = None
    window._refresh_table()
    assert original_item.text() == "0"
    assert original_item.background().color().name() == percentile_colour(0)
    assert window._table.item(0, overlay.COL_FIT).text() == "~0"
    assert not window._panel._metric_labels["Fit"].isHidden()


def test_inplace_sample_metadata_updates_visible_and_accessible_scope(qtbot, tmp_path):
    app = _app(context="raid", value=82)
    window = _window(qtbot, tmp_path, [app], _listing("raid"))
    window._pinned_id = app.applicant_id
    window._sync_delegate_and_panel()
    badge = window._panel._metric_labels["Fit"]
    assert badge.text().endswith("sample unknown")
    app.raid_evidence["H"] = dict(boss_count=3, total_bosses=len(CURRENT_RAID_ENCOUNTERS), sample_known=True, kill_count=6)
    window._refresh_table()
    assert badge.text().endswith(f"H 3/{len(CURRENT_RAID_ENCOUNTERS)} zone bosses")
    assert "6 recorded kills" in badge.accessibleDescription()
    app.raid_evidence["H"]["kill_count"] = 12
    window._refresh_table()
    assert "12 recorded kills" in badge.accessibleDescription()
    assert "6 recorded kills" not in badge.toolTip()


@pytest.mark.parametrize("grouped", [False, True])
def test_inplace_nearby_wcl_updates_cached_fit_order_and_pinned_evidence(qtbot, tmp_path, grouped):
    def with_logs(aid, value):
        app = _app(aid)
        app.mplus_dps = app.mplus_dps_median = value
        app.mplus_dps_breakdown = [
            dict(name=name, key_level=16, parse_percent=value,
                 median_percent=value, run_count=3)
            for _alias, _encounter, name in MPLUS_ENCOUNTERS
        ]
        return app

    changing = [with_logs("1:1", 25)]
    if grouped:
        changing.append(with_logs("1:2", 25))
    stable = with_logs("2:1", 80)
    window = _window(qtbot, tmp_path, [*changing, stable], _listing())
    window._pinned_id = "1:1"
    window._sync_delegate_and_panel()
    badge = window._panel._metric_labels["Fit"]
    before = badge.text()
    assert window._id_by_row[0] == "2:1"
    for app in changing:
        app.mplus_dps = app.mplus_dps_median = 95
        for row in app.mplus_dps_breakdown:
            row.update(parse_percent=95, median_percent=95, run_count=4)
    window._refresh_table()
    assert window._id_by_row[0] == "1:1"
    assert window._pinned_id == "1:1"
    assert badge.text() != before
    assert "WCL 8/8" in badge.text()
    assert "32 WCL samples" in badge.accessibleDescription()
    assert "24 WCL samples" not in badge.toolTip()
    expected = scoring.candidate_fit(changing[0], _listing())
    assert f"~{round(expected.score)}" in badge.text()


def test_every_rio_provider_boundary_uses_the_enclosing_band():
    ascending = list(reversed(RIO_SCORE_BUCKETS))
    for index, (threshold, colour) in enumerate(ascending):
        assert rio_score_colour(threshold) == (colour if threshold else "#5d5d5d")
        if index and threshold > 1:
            assert rio_score_colour(threshold - 1) == ascending[index - 1][1]


@pytest.mark.parametrize("score", [2194, 2195, 3200, 0])
def test_inplace_rio_boundary_updates_table_and_panel_colour(qtbot, tmp_path, score):
    app = _app()
    window = _window(qtbot, tmp_path, [app], _listing())
    window._pinned_id = app.applicant_id
    window._sync_delegate_and_panel()
    app.score = score
    window._refresh_table()
    item = window._table.item(0, overlay.COL_RIO)
    assert item.text() == (str(score) if score else "—")
    assert item.foreground().color().name() == rio_score_colour(score)
    if score:
        assert rio_score_colour(score) in window._panel._rio_label.styleSheet()
    else:
        assert window._panel._rio_label.isHidden()


@pytest.mark.real_display
@pytest.mark.parametrize("context", ["mplus", "raid"])
def test_large_fit_badge_reflows_without_losing_numbers_or_scope(qtbot, context):
    app = _app(context=context, value=99)
    if context == "raid":
        app.raid_evidence = {"H": dict(boss_count=3, total_bosses=len(CURRENT_RAID_ENCOUNTERS), sample_known=True, kill_count=6)}
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setStyleSheet(overlay._STYLESHEET)
    panel.setApplicantData(app, _listing(context))
    badge = panel._metric_labels["Fit"]
    original_text = badge.text()
    badge.setStyleSheet(badge.styleSheet() + "font-size: 18pt;")
    panel.show()
    for width in (300, 650, 300):
        panel.resize(width, panel.height())
        QApplication.processEvents()
        panel.setFixedHeight(panel.target_height())
        QApplication.processEvents()
        assert badge.text() == original_text
        available = badge.contentsRect()
        text_rect = badge.fontMetrics().boundingRect(
            QRect(0, 0, available.width(), 10000),
            Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
            badge.text(),
        )
        assert text_rect.width() <= available.width()
        assert text_rect.height() <= available.height()
        badge_rect = QRect(badge.mapTo(panel, QPoint()), badge.size())
        assert panel.contentsRect().contains(badge_rect)
        assert "not a" in badge.accessibleDescription()
