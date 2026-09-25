"""P2 refactor coverage: overlay_health/fetch/table/row_renderer equivalence.

Additive only — existing test_overlay_* expectations are untouched. These
tests pin the extracted modules to the OverlayWindow behavior they were
moved from (same branches, same strings, same cells).
"""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidget

from applicant_scout import overlay_health as _health
from applicant_scout import overlay_fetch as _fetch
from applicant_scout import overlay_table as _table
from applicant_scout import overlay_row_renderer as _renderer
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.overlay import (
    COL_FIT,
    COL_H,
    COL_ILVL,
    COL_M,
    COL_MPLUS,
    COL_N,
    COL_NAME,
    COL_RIO,
    COL_SPEC,
    MPLUS_INDIVIDUAL_TEXT_ROLE,
    MPLUS_PACKAGE_TEXT_ROLE,
    ROW_BASE_ACCESSIBLE_DESCRIPTION_ROLE,
    OverlayWindow,
    _FetchIdentity,
    _bold_cell_font,
    _fit_cell,
    _metric_cell_font,
    _mplus_dual_cell,
    _mplus_group_cell,
    _raid_dual_cell,
    _role_icon,
    _set_cell_background,
    _set_cell_foreground,
    _stamp_cell_font_sig,
    _text_colour_for_bg,
    auth_chip_for,
)
from applicant_scout.scoring import candidate_fit
from applicant_scout.state import AppState, Applicant, Listing, RosterMember, WoWPlayer
from applicant_scout.wcl import (
    WCLAuth,
    WCLClient,
    CharacterCache,
    CharacterRanks,
    WCL_ERROR_AUTH,
)

FULL = MetricPreferences()
NARROW = MetricPreferences(
    mplus=True, raid_normal=False, raid_heroic=False, raid_mythic=False
)
DISABLED = MetricPreferences(
    mplus=False, raid_normal=False, raid_heroic=False, raid_mythic=False
)


def _identity(prefs: MetricPreferences, **overrides) -> _FetchIdentity:
    base = dict(
        applicant_id="42:1",
        charname_key="scout",
        server_slug="realma",
        region="EU",
        spec_id=71,
        metric_role="dps",
        row_source="applicants",
        runtime_generation=0,
        metric_preferences=prefs,
        listing_session_generation=0,
    )
    base.update(overrides)
    return _FetchIdentity(**base)


def _stub_applicant(fetch_status: str, covers: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        fetch_status=fetch_status,
        wcl_data_covers=lambda _prefs: covers,
    )


def _clean_ranks() -> CharacterRanks:
    return CharacterRanks(
        raid_normal=11.0,
        raid_heroic=22.0,
        raid_mythic=33.0,
        raid_normal_median=10.0,
        raid_heroic_median=20.0,
        raid_mythic_median=30.0,
        mplus_dps=77.0,
        mplus_hps=None,
    )


def _reconcile(**overrides) -> str:
    params = dict(
        applicant=_stub_applicant("ready", covers=True),
        fetched_identity=_identity(FULL),
        current=(_identity(NARROW), "Scout"),
        was_current=True,
        ranks=_clean_ranks(),
        metrics_enabled=True,
    )
    params.update(overrides)
    return _fetch.reconcile_fetch_done(**params).action


def test_reconcile_no_row_for_gone_applicant():
    assert _reconcile(applicant=None) == "no_row"


def test_reconcile_metrics_disabled_clears_to_ready():
    assert _reconcile(metrics_enabled=False) == "metrics_disabled"


def test_reconcile_missing_realm_when_current_is_none():
    assert _reconcile(current=None) == "missing_realm"


def test_reconcile_current_prefs_disabled_clears_to_ready():
    assert _reconcile(current=(_identity(DISABLED), "Scout")) == (
        "current_prefs_disabled"
    )


def test_reconcile_already_current_for_covered_ready_row():
    assert _reconcile(was_current=False) == "already_current"


def test_reconcile_stale_terminal_keeps_error_row():
    assert (
        _reconcile(
            was_current=False,
            applicant=_stub_applicant("error", covers=False),
            fetched_identity=_identity(NARROW),
            current=(_identity(FULL), "Scout"),
        )
        == "stale_terminal"
    )


def test_reconcile_stale_relaunch_for_changed_target():
    assert (
        _reconcile(
            was_current=False,
            applicant=_stub_applicant("ready", covers=False),
            fetched_identity=_identity(FULL, spec_id=72),
        )
        == "stale_relaunch"
    )


def test_reconcile_apply_branches_follow_ranks():
    not_found = _clean_ranks()
    not_found.not_found = True
    assert _reconcile(ranks=not_found) == "apply_not_found"

    restricted = _clean_ranks()
    restricted.error = "private"
    restricted.error_kind = "restricted"
    assert _reconcile(ranks=restricted) == "apply_restricted"

    failed = _clean_ranks()
    failed.error = "boom"
    failed.error_kind = "network"
    assert _reconcile(ranks=failed) == "apply_error"

    assert _reconcile() == "apply_ready"


def test_auth_chip_state_matches_table_lookup():
    for state, kind in [
        ("checking", ""),
        ("oauth_ready", ""),
        ("api_ready", ""),
        ("error", WCL_ERROR_AUTH),
        ("error", ""),
        ("unknown", ""),
    ]:
        status = SimpleNamespace(state=state, error_kind=kind)
        assert _health.auth_chip_state(status) == auth_chip_for(state, kind)


def test_auth_chip_state_legacy_client_without_status_api_is_neutral():
    assert _health.auth_chip_state(object()) == auth_chip_for("unknown", "")


def _health_kwargs(**overrides) -> dict:
    params = dict(
        restored_pending=False,
        restored_saved_at=None,
        restored_deadline=None,
        wire_rejects=0,
        wire_reject_threshold=3,
        wire_reject_message="newer format",
        wire_reject_active=False,
        failed_at=None,
        last_decode_time=None,
        failed_path="shot.png",
        failed_reason="reject",
        addon_warning=None,
        app_update_version=None,
        applicants_unavailable=False,
        roster_unavailable=False,
        lfg_unavailable=False,
        now=1000.0,
    )
    params.update(overrides)
    return params


def test_health_chip_restored_snapshot():
    chip = _health.health_chip_state(
        **_health_kwargs(
            restored_pending=True, restored_saved_at=900.0, restored_deadline=1100.0
        )
    )

    assert (chip.text, chip.chip_state) == ("Shot restored", "active")
    assert "Snapshot age" in chip.tooltip


def test_health_chip_wire_reject_streak():
    chip = _health.health_chip_state(
        **_health_kwargs(
            wire_rejects=3, wire_reject_active=True, failed_at=990.0
        )
    )

    assert (chip.text, chip.chip_state) == ("Companion update", "warning")


def test_health_chip_failed_decode():
    chip = _health.health_chip_state(**_health_kwargs(failed_at=990.0))

    assert (chip.text, chip.chip_state) == ("Shot failed", "critical")


def test_health_chip_addon_and_app_updates():
    addon = _health.health_chip_state(**_health_kwargs(addon_warning="warn"))
    assert (addon.text, addon.chip_state) == ("Addon update", "warning")

    app = _health.health_chip_state(**_health_kwargs(app_update_version="9.9.9"))
    assert (app.text, app.chip_state) == ("App update", "warning")
    assert "9.9.9" in app.tooltip


def test_health_chip_partial_names_stale_surfaces():
    chip = _health.health_chip_state(
        **_health_kwargs(
            last_decode_time=900.0,
            applicants_unavailable=True,
            roster_unavailable=True,
            lfg_unavailable=True,
        )
    )

    assert (chip.text, chip.chip_state) == ("Shot partial", "warning")
    assert "Group Finder listing and Applicants" in chip.tooltip
    assert "Party" in chip.tooltip


def test_health_chip_no_decode_yet_and_age():
    empty = _health.health_chip_state(**_health_kwargs())

    assert (empty.text, empty.chip_state) == ("Shot —", "neutral")
    assert empty.accessible == "No screenshot has been decoded yet."

    aged = _health.health_chip_state(**_health_kwargs(last_decode_time=940.0))
    assert aged.text.startswith("Shot ")
    assert aged.accessible.startswith("Last screenshot decoded ")


def _app(applicant_id: str = "42", **overrides) -> Applicant:
    base = Applicant(
        applicant_id=applicant_id,
        name="Scout-RealmA",
        cls="WARRIOR",
        spec_id=71,
        ilvl=480,
        score=2400,
        role="DAMAGER",
        fetch_status="ready",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _mplus_listing() -> Listing:
    return Listing(
        activity_id=401,
        dungeon_name="Pit of Saron",
        listing_name="+14 Pit of Saron",
        comment="",
        key_level=14,
        category_id=2,
        difficulty_id=8,
    )


def _window(qtbot, tmp_path, state: AppState) -> OverlayWindow:
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth, region="EU")
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(state, client, cache, tmp_path)
    window._pool = None
    qtbot.addWidget(window)
    return window


def test_refresh_table_model_builds_group_maps():
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    state.listing = _mplus_listing()
    state.add_or_update(_app("7:1"))
    state.add_or_update(_app("7:2"))

    model = _table.refresh_table_model(
        state, state.listing, FULL, active_tab="applicants"
    )

    assert [a.applicant_id for a in model.sorted_applicants] == ["7:1", "7:2"]
    assert model.group_size_by_raw == {"7": 2}
    assert model.group_position_by_id == {"7:1": 1, "7:2": 2}

    # Idempotent on an unchanged order.
    snapshot = (
        dict(model.group_size_by_raw),
        dict(model.group_position_by_id),
        dict(model.group_ready_by_raw),
    )
    _table.index_group_maps(model, state.listing)
    assert (
        dict(model.group_size_by_raw),
        dict(model.group_position_by_id),
        dict(model.group_ready_by_raw),
    ) == snapshot

    # Reindex follows the displayed (post-manual-sort) order.
    model.sorted_applicants = list(reversed(model.sorted_applicants))
    _table.index_group_maps(model, state.listing)
    assert model.group_position_by_id == {"7:1": 2, "7:2": 1}


def test_refresh_table_model_party_tab_prefetches_fits():
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    member = RosterMember(
        applicant_id="scout-realma",
        name="Scout-RealmA",
        cls="WARRIOR",
        spec_id=71,
        ilvl=480,
        score=2400,
        role="DAMAGER",
        fetch_status="ready",
    )
    state.add_or_update_party_member(member)

    model = _table.refresh_table_model(
        state, None, FULL, active_tab="party", compute_party_fits=True
    )

    assert [a.applicant_id for a in model.sorted_applicants] == ["scout-realma"]
    assert "scout-realma" in model.candidate_fit_by_id
    assert model.package_fit_by_raw == {}


def test_render_row_cells_matches_window_render(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-RealmA")
    state.listing = _mplus_listing()
    applicant = _app(fetch_status="ready", mplus_dps=80.0, mplus_dps_median=62.0)
    state.add_or_update(applicant)
    window = _window(qtbot, tmp_path, state)
    try:
        window._refresh_table()
        assert window._table.rowCount() == 1

        expected_texts = [
            window._table.item(0, col).text() for col in range(9)
        ]
        expected_accessible = [
            window._table.item(0, col).data(
                Qt.ItemDataRole.AccessibleDescriptionRole
            )
            for col in range(9)
        ]
        expected_tip = window._table.item(0, COL_RIO).toolTip()
        expected_height = window._table.rowHeight(0)

        fresh = QTableWidget(1, 9)
        qtbot.addWidget(fresh)
        listing = window._effective_listing()

        def reuse(row: int, col: int, text: str):
            item = fresh.item(row, col)
            if item is None:
                from PySide6.QtWidgets import QTableWidgetItem

                item = QTableWidgetItem(text)
                fresh.setItem(row, col, item)
            elif item.text() != text:
                item.setText(text)
            return item

        env = _renderer.RowRenderEnv(
            listing=listing,
            package_fit_by_raw=dict(window._package_fit_by_raw),
            group_size_by_raw=dict(window._group_size_by_raw),
            group_position_by_id=dict(window._group_position_by_id),
            group_ready_by_raw=dict(window._group_ready_by_raw),
            pinned_id=window._pinned_id,
            keyboard_id=window._keyboard_id,
            keyboard_preview_active=window._keyboard_preview_active,
            col_spec=COL_SPEC,
            col_name=COL_NAME,
            col_ilvl=COL_ILVL,
            col_rio=COL_RIO,
            col_n=COL_N,
            col_h=COL_H,
            col_m=COL_M,
            col_mplus=COL_MPLUS,
            col_fit=COL_FIT,
            package_text_role=MPLUS_PACKAGE_TEXT_ROLE,
            individual_text_role=MPLUS_INDIVIDUAL_TEXT_ROLE,
            row_base_role=ROW_BASE_ACCESSIBLE_DESCRIPTION_ROLE,
            reuse_item=reuse,
            set_data=OverlayWindow._set_item_data_if_changed,
            set_row_height=fresh.setRowHeight,
            rio_cell_height=OverlayWindow._rio_cell_height,
            table_font=fresh.font,
            role_icon=_role_icon,
            bold_font=_bold_cell_font,
            metric_font=lambda font, bold: _metric_cell_font(font, bold=bold),
            text_colour_for_bg=_text_colour_for_bg,
            set_cell_foreground=_set_cell_foreground,
            set_cell_background=_set_cell_background,
            stamp_cell_font_sig=_stamp_cell_font_sig,
            raid_dual_cell=_raid_dual_cell,
            mplus_dual_cell=_mplus_dual_cell,
            mplus_group_cell=_mplus_group_cell,
            fit_cell=_fit_cell,
        )
        _renderer.render_row_cells(
            0,
            applicant,
            candidate_fit(applicant, listing),
            table=fresh,
            env=env,
        )

        assert [fresh.item(0, col).text() for col in range(9)] == expected_texts
        assert [
            fresh.item(0, col).data(Qt.ItemDataRole.AccessibleDescriptionRole)
            for col in range(9)
        ] == expected_accessible
        assert fresh.item(0, COL_RIO).toolTip() == expected_tip
        assert fresh.rowHeight(0) == expected_height
    finally:
        window._wcl_client.close()
