"""Real tab clicks preserve the supplied panel evidence and manual actions."""

import pytest
from PySide6.QtCore import Qt

from applicant_scout.overlay import ApplicantInfoPanel
from applicant_scout.scoring import CandidateFit, CONTEXT_MPLUS
from applicant_scout.state import AppState, WoWPlayer
from test_info_panel_widget import _app, _raid_listing
from test_overlay_fetch_identity import _QueuedPool, _window


def _click(qtbot, panel, mode):
    qtbot.mouseClick(panel._detail_buttons[mode], Qt.MouseButton.LeftButton)


@pytest.mark.parametrize("roundtrip", [False, True])
@pytest.mark.parametrize("action", ["load", "detail_retry", "summary_retry"])
def test_real_tab_click_preserves_manual_action(qtbot, roundtrip, action):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.show()
    app = _app(
        fetch_status="error" if action == "summary_retry" else "ready",
        error_message="Temporary outage" if action == "summary_retry" else "",
    )
    panel.setApplicantData(
        app,
        _raid_listing(),
        raid_detail_status="Boss details not loaded"
        if action == "load"
        else "Details failed",
        raid_detail_status_error=action == "detail_retry",
        raid_detail_retry_available=action != "summary_retry",
        wcl_retry_available=action == "summary_retry",
    )
    expected_status = panel._status_label.text()
    expected_action = panel._wcl_retry_button.text()
    expected_accessibility = (
        panel._wcl_retry_button.accessibleName(),
        panel._wcl_retry_button.accessibleDescription(),
    )
    assert not panel._wcl_retry_button.isHidden()
    if roundtrip:
        _click(qtbot, panel, "mplus")
    _click(qtbot, panel, "raid")
    assert panel._status_label.text() == expected_status
    assert not panel._wcl_retry_button.isHidden()
    assert panel._wcl_retry_button.text() == expected_action
    assert (
        panel._wcl_retry_button.accessibleName(),
        panel._wcl_retry_button.accessibleDescription(),
    ) == expected_accessibility
    received = []
    panel.wclRetryRequested.connect(lambda: received.append("requested"))
    qtbot.mouseClick(panel._wcl_retry_button, Qt.MouseButton.LeftButton)
    assert received == ["requested"]


@pytest.mark.parametrize("roundtrip", [False, True])
def test_tab_click_preserves_supplied_fit_instead_of_recomputing(qtbot, roundtrip):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.show()
    fit = CandidateFit(
        context=CONTEXT_MPLUS,
        score=91,
        label="TOP",
        display="91",
        colour="#ffffff",
        target_key=19,
        source="rio",
    )
    panel.setApplicantData(_app(), _raid_listing(), fit=fit)
    expected = panel._metric_labels["Fit"].text()
    assert "+19" in expected
    if roundtrip:
        _click(qtbot, panel, "mplus")
    _click(qtbot, panel, "raid")
    assert panel._metric_labels["Fit"].text() == expected


@pytest.mark.parametrize("replacement", ["placeholder", "new_row"])
def test_replacement_clears_previous_manual_action(qtbot, replacement):
    panel = ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.show()
    panel.setApplicantData(
        _app(),
        _raid_listing(),
        raid_detail_status="Boss details not loaded",
        raid_detail_retry_available=True,
    )
    assert not panel._wcl_retry_button.isHidden()
    if replacement == "placeholder":
        panel.setPlaceholder()
    else:
        panel.setApplicantData(
            _app(name="Other-Realm", applicant_id="other"), _raid_listing()
        )
    _click(qtbot, panel, "raid")
    assert panel._wcl_retry_button.isHidden()
    assert "Boss details not loaded" not in panel._status_label.text()


def test_integrated_tab_switches_never_launch_manual_detail_tokens(qtbot, tmp_path):
    state = AppState()
    state.player = WoWPlayer(full_name="Host-Realm")
    state.listing = _raid_listing()
    app = _app(raid_boss_parses={})
    state.add_or_update(app)
    window, client = _window(qtbot, tmp_path, state)
    window._pool = _QueuedPool()
    try:
        window._refresh_table()
        window._pinned_id = app.applicant_id
        window._refresh_panel()
        for mode in ("raid", "mplus", "raid", "raid"):
            _click(qtbot, window._panel, mode)
        assert window._pool.tasks == []
        assert not window._panel._wcl_retry_button.isHidden()
        qtbot.mouseClick(window._panel._wcl_retry_button, Qt.MouseButton.LeftButton)
        assert len(window._pool.tasks) == 1
    finally:
        client.close()
