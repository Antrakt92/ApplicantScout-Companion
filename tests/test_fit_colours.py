"""Fit uses the percentile palette while keeping estimate and missing-data semantics."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QImage

from applicant_scout import overlay, scoring
from applicant_scout.constants import percentile_colour
from applicant_scout.state import Applicant, AppState, Listing


def _app():
    return Applicant(
        "1:1", "Player-Realm", "MAGE", 62, 700, 2500, "DAMAGER",
        fetch_status="ready", mplus_dps=85, mplus_dps_median=75,
        mplus_dps_breakdown=[dict(
            name="Skyreach", key_level=15, parse_percent=85,
            median_percent=75, run_count=4,
        )],
        rio_dungeons=[dict(name="Skyreach", key_level=15)],
    )


def _listing(key=15):
    return Listing(0, "Skyreach", "", "", key_level=key, category_id=2)


@pytest.mark.parametrize("score", [0, 24.4, 24.6, 49.6, 74.6, 94.6, 98.6, 99.6, 105])
def test_fit_table_and_detail_use_the_displayed_estimate_colour(qtbot, score):
    app = _app()
    fit = scoring.CandidateFit(
        context=scoring.CONTEXT_MPLUS, score=score, display="estimate", target_key=15,
    )
    expected = percentile_colour(round(score))
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(app, _listing(), fit=fit)
    item = overlay._fit_cell(app, _listing(), fit=fit)

    assert item.text() == f"~{round(score)}"
    assert item.background().color().name() == expected
    assert expected in panel._metric_labels["Fit"].styleSheet()
    assert "not a WCL parse percentile" in panel._metric_labels["Fit"].toolTip()


def test_group_fit_colours_both_group_summary_and_individual(qtbot):
    app = _app()
    fit = scoring.CandidateFit(
        context=scoring.CONTEXT_MPLUS, score=80, display="estimate", target_key=15,
    )
    package = scoring.PackageFit(
        context=scoring.CONTEXT_MPLUS, size=2, score=60, display="G2 60",
        member_fits=(fit, fit),
    )
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(app, _listing(), package, fit=fit)
    item = overlay._mplus_group_cell(package, app, _listing(), fit=fit)

    assert item.data(overlay.MPLUS_PACKAGE_BG_ROLE) == percentile_colour(60)
    assert item.data(overlay.MPLUS_INDIVIDUAL_BG_ROLE) == percentile_colour(80)
    assert percentile_colour(60) in panel._package_label.styleSheet()
    assert percentile_colour(80) in panel._metric_labels["Fit"].styleSheet()


@pytest.mark.parametrize("status", ["pending", "loading", "error", "not_found", "restricted", "ready"])
def test_missing_fit_stays_neutral_and_has_no_badge(qtbot, status):
    app = replace(
        _app(), fetch_status=status, score=0, mplus_dps=None,
        mplus_dps_median=None, mplus_dps_breakdown=[], rio_dungeons=[],
    )
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(app, _listing())
    item = overlay._fit_cell(app, _listing())
    assert item.background().style() == Qt.BrushStyle.NoBrush
    assert panel._metric_labels["Fit"].isHidden()


def test_target_key_change_updates_fit_colour_without_recolouring_wcl(qtbot, tmp_path):
    state = AppState()
    app = _app()
    app.mplus_dps_breakdown = [
        dict(name=f"Dungeon {index}", key_level=15, parse_percent=85,
             median_percent=75, run_count=4) for index in range(8)
    ]
    app.rio_dungeons = [dict(name=f"Dungeon {index}", key_level=15) for index in range(8)]
    state.party_members[app.applicant_id] = app
    window = overlay.OverlayWindow(
        state, SimpleNamespace(region="EU", partition=None), SimpleNamespace(), tmp_path,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    window._launch_fetch = lambda _app: None
    window._launch_raid_boss_fetch_if_needed = lambda _app: False
    window._on_source_tab_changed("party")
    window._on_cell_clicked(0, overlay.COL_NAME)
    colours = []
    wcl = []
    for key in (5, 25):
        window._tab_bar._key_spin.setValue(key)
        item = window._table.item(0, overlay.COL_FIT)
        expected = percentile_colour(round(scoring.candidate_fit(app, window._effective_listing()).score))
        colours.append(item.background().color().name())
        assert colours[-1] == expected
        assert expected in window._panel._metric_labels["Fit"].styleSheet()
        wcl_item = window._table.item(0, overlay.COL_MPLUS)
        wcl.append((wcl_item.text(), wcl_item.background().color().name()))
    assert colours[0] != colours[1]
    assert wcl[0] == wcl[1]


def test_launcher_rounded_corners_are_transparent_and_not_clickable(qtbot):
    launcher = overlay.OverlayLauncher()
    qtbot.addWidget(launcher)
    launcher.setStyleSheet(overlay._STYLESHEET)
    assert launcher.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    image = QImage(launcher.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    launcher.render(image)
    for point in (QPoint(0, 0), QPoint(launcher.width() - 1, 0),
                  QPoint(0, launcher.height() - 1), launcher.rect().bottomRight()):
        assert image.pixelColor(point).alpha() == 0
        assert not launcher.mask().contains(point)
        assert not launcher.hitButton(point)
    assert image.pixelColor(launcher.rect().center()).alpha() > 0
    assert launcher.hitButton(launcher.rect().center())


def test_unknown_group_fit_does_not_gain_percentile_colour(qtbot):
    app = replace(
        _app(), score=0, mplus_dps=None, mplus_dps_median=None,
        mplus_dps_breakdown=[], rio_dungeons=[],
    )
    package = scoring.package_fit([app, replace(app, applicant_id="1:2")], _listing())
    assert not any(fit.display for fit in package.member_fits)
    item = overlay._mplus_group_cell(package, app, _listing())
    panel = overlay.ApplicantInfoPanel(None)
    qtbot.addWidget(panel)
    panel.setApplicantData(app, _listing(), package)
    assert item.data(overlay.MPLUS_PACKAGE_BG_ROLE) == overlay.FIT_GROUP_BACKGROUND
    assert overlay.FIT_GROUP_BACKGROUND in panel._package_label.styleSheet()


def test_launcher_ignores_corner_press_but_center_still_restores(qtbot):
    launcher = overlay.OverlayLauncher()
    qtbot.addWidget(launcher)
    clicks = []
    launcher.clicked.connect(lambda: clicks.append(True))
    qtbot.mouseClick(launcher, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
    assert not launcher.is_dragging()
    assert clicks == []
    qtbot.mouseClick(launcher, Qt.MouseButton.LeftButton, pos=launcher.rect().center())
    assert clicks == [True]
    assert not launcher.is_dragging()
