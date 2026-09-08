"""Regression coverage for separating WCL evidence from Fit recommendations."""

from __future__ import annotations

import pytest
from PyQt6.QtGui import QColor

from applicant_scout import overlay, overlay_presenters, scoring
from applicant_scout.constants import CURRENT_RAID_ENCOUNTERS, percentile_colour
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.state import AppState, Applicant, Listing, RosterMember


class _Client:
    last_quota = None
    region = "EU"

    def quota_reset_remaining_seconds(self):
        return None


def _app(**overrides):
    values = dict(
        applicant_id="1:1",
        name="Scout-Realm",
        cls="WARRIOR",
        spec_id=71,
        role="DAMAGER",
        ilvl=310,
        score=2700,
        fetch_status="ready",
        raid_normal=87.0,
        raid_normal_median=82.0,
        raid_heroic=67.0,
        raid_heroic_median=62.0,
        raid_mythic=27.0,
        raid_mythic_median=25.0,
        mplus_dps=58.0,
        mplus_dps_median=66.0,
        mplus_dps_breakdown=[
            dict(
                name="Pit of Saron",
                parse_percent=58.0,
                median_percent=66.0,
                key_level=12,
                run_count=3,
            )
        ],
    )
    values.update(overrides)
    return Applicant(**values)


def _listing(kind):
    if kind == "unknown":
        return None
    raid = kind == "raid"
    return Listing(
        activity_id=401,
        dungeon_name="Raid" if raid else "Pit of Saron",
        listing_name="Heroic" if raid else "+12",
        comment="",
        key_level=0 if raid else 12,
        category_id=3 if raid else 2,
        difficulty_id=15 if raid else 8,
    )


@pytest.mark.parametrize("kind", ["raid", "mplus", "unknown"])
def test_mplus_raw_evidence_has_same_percentile_and_colour_in_every_context(kind):
    text, _fg, bg = overlay._mplus_cell_visuals(_app(), _listing(kind))

    assert text == "58/66 +12"
    assert bg == percentile_colour(58.0)


@pytest.mark.parametrize("kind", ["raid", "mplus"])
@pytest.mark.parametrize("tab", ["applicants", "party"])
def test_table_preserves_all_raw_parses_and_has_separate_fit(
    qtbot,
    tmp_path,
    kind,
    tab,
):
    state = AppState()
    state.listing = _listing(kind)
    app = _app()
    if tab == "party":
        app = RosterMember(**vars(app), unit_index=1, is_raid_member=kind == "raid")
        state.party_members[app.applicant_id] = app
    else:
        state.applicants[app.applicant_id] = app
    window = overlay.OverlayWindow(state, _Client(), object(), tmp_path)
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    window._launch_fetch = lambda _applicant: None
    window._launch_raid_boss_fetch_if_needed = lambda _applicant: False
    window.apply_metric_preferences(
        MetricPreferences(
            mplus=True,
            raid_normal=True,
            raid_heroic=True,
            raid_mythic=True,
        ),
        refetch_missing=False,
    )
    # Preference changes invalidate prior metrics even when refetch is disabled.
    app = _app()
    if tab == "party":
        app = RosterMember(**vars(app), unit_index=1, is_raid_member=kind == "raid")
        state.party_members[app.applicant_id] = app
    else:
        state.applicants[app.applicant_id] = app
    window._active_tab = tab

    window._refresh_table()

    for column, text, percentile in (
        (overlay.COL_N, "87/82", 87.0),
        (overlay.COL_H, "67/62", 67.0),
        (overlay.COL_M, "27/25", 27.0),
        (overlay.COL_MPLUS, "58/66 +12", 58.0),
    ):
        item = window._table.item(0, column)
        assert item.text() == text
        assert item.background().color() == QColor(percentile_colour(percentile))
    fit_item = window._table.item(0, overlay.COL_FIT)
    assert fit_item.text()
    assert not window._table.isColumnHidden(overlay.COL_FIT)
    assert window._table.horizontalHeaderItem(overlay.COL_FIT).text().startswith("Fit")


@pytest.mark.parametrize(
    "status,text",
    [
        ("loading", "…"),
        ("pending", "…"),
        ("error", "?"),
        ("not_found", "—"),
        ("restricted", "—"),
    ],
)
def test_raw_parse_status_never_displays_stale_evidence_or_fit(status, text):
    app = _app(fetch_status=status)
    for kind in ("raid", "mplus", "unknown"):
        rendered, _fg, bg = overlay._mplus_cell_visuals(app, _listing(kind))
        assert rendered == text
        assert bg is None
    assert overlay_presenters.raid_cell_visuals(87, 82, status)[0] == text


def test_zero_parse_is_visible_and_missing_median_does_not_invent_a_sample_size():
    assert overlay_presenters.raid_cell_visuals(0, None, "ready")[0] == "0"
    assert overlay_presenters.raid_cell_visuals(None, None, "ready")[0] == "—"
    assert overlay_presenters.mplus_metric_display_text(0, None, 0) == "0"
    assert overlay_presenters.mplus_metric_display_text(80, None, 0) == "80"


def test_single_run_summary_stays_numeric_and_details_keep_sample_count():
    assert overlay_presenters.mplus_metric_display_text(80, None, 1) == "80 1 run"
    assert (
        overlay_presenters.mplus_metric_display_text(
            80,
            None,
            1,
            headline=True,
        )
        == "80"
    )
    assert overlay_presenters.mplus_metric_display_text(80, 62, 3) == "80/62"
    app = _app(
        mplus_dps=80,
        mplus_dps_median=None,
        mplus_dps_breakdown=[
            dict(
                name=name,
                parse_percent=80,
                median_percent=None,
                key_level=key,
                run_count=1,
            )
            for name, key in (("Pit of Saron", 14), ("Skyreach", 12))
        ],
    )
    assert overlay._mplus_cell_visuals(app, _listing("raid"))[0] == "80 +14"


def test_boss_kills_and_overall_ilvl_parses_have_distinct_notation():
    encounter = CURRENT_RAID_ENCOUNTERS[0][1]
    app = _app(
        rio_raid_progress={"N": {"boss_kills": [2]}, "H": {"boss_kills": [1]}},
        raid_boss_parses={
            "N": [{"encounter_id": encounter, "overall": 91, "ilvl": 80}]
        },
    )

    row = overlay_presenters.raid_boss_rows_for_display(app, ["N", "H"])[0]

    assert row["rio_text"] == "N×2 · H×1"
    assert row["value"] == "N 91 / 80"


@pytest.mark.parametrize("kind", ["raid", "mplus"])
def test_supplied_fit_and_raw_cells_do_not_recompute_scoring(monkeypatch, kind):
    app = _app()
    listing = _listing(kind)
    fit = scoring.candidate_fit(app, listing)

    def unexpected_score(*_args, **_kwargs):
        pytest.fail("Rendering raw evidence or a supplied Fit repeated scoring")

    monkeypatch.setattr(overlay, "candidate_fit", unexpected_score)
    monkeypatch.setattr(scoring, "candidate_fit", unexpected_score)

    assert overlay._mplus_cell_visuals(app, listing)[0] == "58/66 +12"
    assert overlay._fit_cell_visuals(app, listing, fit=fit)[0]
