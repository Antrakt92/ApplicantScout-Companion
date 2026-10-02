"""A table refresh freezes each rendered applicant once without stale evidence."""

from collections import Counter

import pytest

import applicant_scout.overlay as overlay
import applicant_scout.overlay_rows as rows
from applicant_scout.overlay_table import refresh_table_model
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.scoring import candidate_fit, package_fit
from applicant_scout.state import AppState, Listing
from test_info_panel_widget import _app, _listing, _raid_listing
from test_overlay_fetch_identity import _window


def test_actual_refresh_traverses_each_applicant_evidence_once(
    qtbot, tmp_path, monkeypatch
):
    state = AppState()
    state.listing = _listing()
    apps = [_app(applicant_id="42:1"), _app(applicant_id="42:2", name="Second-Realm")]
    for app in apps:
        state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    calls = Counter()
    original = rows.rendered_applicant_key

    def counted(app):
        calls[id(app)] += 1
        return original(app)

    monkeypatch.setattr(rows, "rendered_applicant_key", counted)
    try:
        window._refresh_table()
        assert calls == {id(app): 1 for app in apps}
        calls.clear()
        window._refresh_table()
        assert calls == {id(app): 1 for app in apps}
    finally:
        client.close()


def test_context_keys_are_detached_and_keyed_by_object_not_applicant_id():
    listing = _listing()
    app = _app(applicant_id="42:1")
    context = rows.RefreshRenderKeys(listing)
    original = rows.rendered_applicant_key(app)
    assert context.applicant(app) == original
    listing_key = context.listing()
    app.mplus_dps_breakdown[0]["parse_percent"] = 7
    listing.key_level += 1
    assert context.applicant(app) == original
    assert context.listing() == listing_key
    replacement = _app(applicant_id=app.applicant_id, score=123)
    assert context.applicant(replacement) == rows.rendered_applicant_key(replacement)
    assert context.applicant(replacement) != original
    fresh = rows.RefreshRenderKeys(listing)
    assert fresh.applicant(app) == rows.rendered_applicant_key(app) != original
    assert fresh.listing() != listing_key
    package = package_fit([app, replacement], listing)
    assert fresh.package(package) == rows.freeze_render_value(package)
    assert fresh.package(None) is None


@pytest.mark.parametrize("listing_factory", [_listing, lambda: None])
@pytest.mark.parametrize(
    "preferences", [MetricPreferences(), MetricPreferences(mplus=False)]
)
def test_cached_model_has_exact_order_fits_and_group_maps(listing_factory, preferences):
    state = AppState()
    listing = listing_factory()
    for app in (
        _app(applicant_id="42:1", score=111),
        _app(applicant_id="42:2", name="Second-Realm", score=3000),
        _app(applicant_id="other:1", name="Solo-Realm", raid_heroic=31),
    ):
        state.add_or_update(app)
    plain = refresh_table_model(
        state, listing, preferences, active_tab="applicants", package_fit_cache={}
    )
    cached = refresh_table_model(
        state,
        listing,
        preferences,
        active_tab="applicants",
        package_fit_cache={},
        refresh_keys=rows.RefreshRenderKeys(listing),
    )
    assert cached == plain


def test_refresh_keys_follow_nested_mutation_and_same_id_replacement(qtbot, tmp_path):
    state = AppState()
    state.listing = _listing()
    app = _app(applicant_id="42:1")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    try:
        window._refresh_table()
        original = window._row_render_key_by_id[app.applicant_id]
        app.mplus_dps_breakdown[0]["parse_percent"] = 13
        window._refresh_table()
        changed = window._row_render_key_by_id[app.applicant_id]
        assert changed != original
        assert changed == window._row_render_key(app, window._effective_listing())
        replacement = _app(applicant_id=app.applicant_id, name="Replacement-Realm")
        state.applicants[app.applicant_id] = replacement
        window._refresh_table()
        assert window._row_render_key_by_id[app.applicant_id] != changed
        assert window._row_render_key_by_id[app.applicant_id] == window._row_render_key(
            replacement, window._effective_listing()
        )
    finally:
        client.close()


def test_refresh_after_scoring_exception_uses_fresh_evidence(
    qtbot, tmp_path, monkeypatch
):
    state = AppState()
    state.listing = _listing()
    app = _app(applicant_id="42:1")
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    original = overlay.package_fit

    def fail(*_args, **_kwargs):
        raise RuntimeError("scoring interrupted")

    try:
        monkeypatch.setattr(overlay, "package_fit", fail)
        with pytest.raises(RuntimeError, match="scoring interrupted"):
            window._refresh_table()
        assert window._refresh_render_keys is None
        monkeypatch.setattr(overlay, "package_fit", original)
        app.score = 3555
        window._refresh_table()
        assert window._row_render_key_by_id[app.applicant_id] == window._row_render_key(
            app, window._effective_listing()
        )
        first = window._row_render_key_by_id[app.applicant_id]
        state.listing.key_level += 1
        window._refresh_table()
        assert window._row_render_key_by_id[app.applicant_id] != first
        second = window._row_render_key_by_id[app.applicant_id]
        window.apply_metric_preferences(MetricPreferences(mplus=False))
        window._refresh_table()
        assert window._row_render_key_by_id[app.applicant_id] != second
    finally:
        client.close()


def test_nested_refresh_restores_outer_context_on_success_and_exception(
    qtbot, tmp_path, monkeypatch
):
    state = AppState()
    state.listing = _listing()
    window, client = _window(qtbot, tmp_path, state)
    outer = rows.RefreshRenderKeys(None)
    window._refresh_render_keys = outer
    observed = []

    def nested(_listing):
        observed.append(window._refresh_render_keys)
        if len(observed) == 1:
            current = window._refresh_render_keys
            window._refresh_table()
            assert window._refresh_render_keys is current

    try:
        monkeypatch.setattr(window, "_refresh_table_with_keys", nested)
        window._refresh_table()
        assert len(observed) == 2 and observed[0] is not observed[1]
        assert all(context is not outer for context in observed)
        assert window._refresh_render_keys is outer

        def fail(_listing):
            raise RuntimeError("nested failure")

        monkeypatch.setattr(window, "_refresh_table_with_keys", fail)
        with pytest.raises(RuntimeError, match="nested failure"):
            window._refresh_table()
        assert window._refresh_render_keys is outer
    finally:
        window._refresh_render_keys = None
        client.close()


def test_canonical_fit_cache_ignores_ilvl_but_render_key_does_not():
    listing = _listing()
    app = _app(applicant_id="42:1")
    cache = {}
    first = rows.sort_applicants_grouped_with_package_fits(
        [app], listing, package_fit_cache=cache
    )
    first_package = first[1]["42"]
    first_candidate = first[2][app.applicant_id]
    old_render_key = rows.rendered_applicant_key(app)
    app.ilvl += 1
    second = rows.sort_applicants_grouped_with_package_fits(
        [app], listing, package_fit_cache=cache
    )
    assert rows.rendered_applicant_key(app) != old_render_key
    assert second[1]["42"] is first_package
    assert second[2][app.applicant_id] is first_candidate
    app.mplus_dps_breakdown[0]["parse_percent"] = 12
    third = rows.sort_applicants_grouped_with_package_fits(
        [app], listing, package_fit_cache=cache
    )
    assert third[1]["42"] is not first_package
    assert third[2][app.applicant_id] is not first_candidate


def test_injected_fit_function_retains_ilvl_in_cache_dependencies():
    from dataclasses import replace

    listing = _listing()
    app = _app(applicant_id="42:1")
    cache = {}
    calls = []

    def ilvl_fit(members, current_listing):
        members = list(members)
        calls.append(members[0].ilvl)
        return replace(
            package_fit(members, current_listing), score=float(members[0].ilvl)
        )

    first = rows.sort_applicants_grouped_with_package_fits(
        [app], listing, package_fit_cache=cache, package_fit_fn=ilvl_fit
    )
    app.ilvl += 1
    second = rows.sort_applicants_grouped_with_package_fits(
        [app], listing, package_fit_cache=cache, package_fit_fn=ilvl_fit
    )
    assert len(calls) == 2
    assert second[1]["42"].score == app.ilvl
    assert second[1]["42"] is not first[1]["42"]


@pytest.mark.parametrize("context", ["mplus", "raid", "unknown", "none"])
@pytest.mark.parametrize(
    "status", ["ready", "pending", "loading", "error", "not_found", "restricted"]
)
@pytest.mark.parametrize("role", ["DAMAGER", "HEALER", "TANK"])
def test_canonical_scoring_is_independent_of_item_level(context, status, role):
    listing = {
        "mplus": _listing(),
        "raid": _raid_listing(),
        "unknown": Listing(0, "", "Unknown", "", 0),
        "none": None,
    }[context]
    app = _app(applicant_id="42:1", fetch_status=status, role=role, ilvl=200)
    partner = _app(applicant_id="42:2", name="Partner-Realm", ilvl=250)
    before_candidate = candidate_fit(app, listing)
    before_package = package_fit([app, partner], listing)
    app.ilvl = 999
    partner.ilvl = 1
    assert candidate_fit(app, listing) == before_candidate
    assert package_fit([app, partner], listing) == before_package
