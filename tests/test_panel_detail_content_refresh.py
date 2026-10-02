from dataclasses import replace

import pytest

from applicant_scout.state import AppState, WoWPlayer
from test_info_panel_widget import _app, _raid_listing
from test_overlay_fetch_identity import _window


def _details(value):
    return {
        difficulty: [
            {
                "encounter_id": 3470,
                "name": "Synthetic boss",
                "overall": value,
                "ilvl": None,
            }
        ]
        for difficulty in ("N", "H", "M")
    }


@pytest.mark.parametrize("delivery", ["completion", "replacement", "in_place"])
def test_loaded_detail_replacement_refreshes_panel_without_rebuilding_table(
    qtbot, tmp_path, monkeypatch, delivery
):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-Ravencrest")
    state.listing = _raid_listing()
    app = _app(
        name="Scout-Ravencrest",
        raid_boss_parses=_details(94),
        raid_boss_availability={key: "available" for key in ("N", "H", "M")},
    )
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    window._panel._set_detail_mode("raid")
    table_renders = []
    try:
        window._refresh_table()
        first_value = window._panel._dungeon_rows[0][3]
        assert "94" in first_value.property("raidFullText")
        status = window._panel._status_label.text()
        original = window._render_row

        def counted(row, applicant, *, fit=None):
            table_renders.append(applicant.applicant_id)
            return original(row, applicant, fit=fit)

        monkeypatch.setattr(window, "_render_row", counted)
        if delivery == "completion":
            identity = window._current_fetch_identity_for(app)
            assert identity is not None
            window._on_raid_boss_fetch_done(
                replace(identity, metric_preferences=window._raid_detail_preferences()),
                _details(11),
                "",
            )
        elif delivery == "replacement":
            app.raid_boss_parses = _details(11)
        else:
            for records in app.raid_boss_parses.values():
                records[0]["overall"] = 11
        window._refresh_table()
        assert "11" in first_value.property("raidFullText")
        assert "94" not in first_value.property("raidFullText")
        assert window._panel._status_label.text() == status
        assert table_renders == []
    finally:
        window.shutdown_fetches()
        client.close()
