"""Visual QA smoke checks for the representative overlay fixture."""

from __future__ import annotations

import os

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

import applicant_scout.overlay as overlay_mod
from applicant_scout.constants import ALL_ROLES
from applicant_scout.overlay import COL_H, COL_M, COL_MPLUS, COL_N
from scripts import render_overlay_fixture
from scripts.overlay_visual_fixture import (
    DEFAULT_VISUAL_FIXTURE_SCENARIO,
    OVERLAY_VISUAL_SCENARIOS,
    OVERLAY_VISUAL_BASELINE_PATH,
    VISUAL_FIXTURE_PINNED_ID,
    VISUAL_FIXTURE_REGEN_COMMAND,
    compare_overlay_visual_images,
    create_overlay_visual_window,
    grab_overlay_visual_image,
    show_overlay_visual_window,
)


def test_visual_fixture_scenarios_are_small_and_unique():
    assert DEFAULT_VISUAL_FIXTURE_SCENARIO == "applicants-default"
    assert set(OVERLAY_VISUAL_SCENARIOS) == {
        "applicants-default",
        "party-manual-key",
        "party-no-listing-manual-key",
        "metrics-raid-only",
        "raid-listing",
    }
    assert (
        OVERLAY_VISUAL_SCENARIOS[DEFAULT_VISUAL_FIXTURE_SCENARIO].baseline_path
        == OVERLAY_VISUAL_BASELINE_PATH
    )
    assert len(
        {scenario.baseline_path for scenario in OVERLAY_VISUAL_SCENARIOS.values()}
    ) == len(OVERLAY_VISUAL_SCENARIOS)


def test_render_overlay_fixture_cli_defaults_to_single_default_scenario():
    args = render_overlay_fixture.parse_args([])

    assert args.scenario == DEFAULT_VISUAL_FIXTURE_SCENARIO
    assert not args.all


def test_render_overlay_fixture_cli_accepts_all_scenarios():
    args = render_overlay_fixture.parse_args(["--check", "--all"])

    assert args.check
    assert args.all


def test_render_overlay_fixture_cli_rejects_output_with_all():
    with pytest.raises(SystemExit):
        render_overlay_fixture.parse_args(["--all", "--output", "out.png"])


def test_visual_fixture_regen_command_refreshes_all_scenarios():
    assert "--all" in VISUAL_FIXTURE_REGEN_COMMAND


def _sampled_colours(image: QImage) -> set[int]:
    x_step = max(1, image.width() // 10)
    y_step = max(1, image.height() // 10)
    colours: set[int] = set()
    for x in range(0, image.width(), x_step):
        for y in range(0, image.height(), y_step):
            colours.add(image.pixelColor(x, y).rgba())
    return colours


def test_overlay_visual_fixture_renders_representative_state(qtbot, tmp_path):
    state, window, client = create_overlay_visual_window(tmp_path)
    qtbot.addWidget(window)

    try:
        show_overlay_visual_window(window, process_events=QApplication.processEvents)

        screenshot_path = tmp_path / "overlay-polish-fixture.png"
        pixmap = grab_overlay_visual_image(window)
        assert not pixmap.isNull()
        assert pixmap.save(str(screenshot_path))
        assert screenshot_path.stat().st_size > 0

        image = QImage(str(screenshot_path))
        dpr = pixmap.devicePixelRatio()
        assert image.width() == round(window.size().width() * dpr)
        assert image.height() == round(window.size().height() * dpr)
        assert len(_sampled_colours(image)) > 1

        assert OVERLAY_VISUAL_BASELINE_PATH.exists()
        assert OVERLAY_VISUAL_BASELINE_PATH.stat().st_size > 0
        baseline = QImage(str(OVERLAY_VISUAL_BASELINE_PATH))
        assert not baseline.isNull()
        if os.environ.get("APPLICANT_SCOUT_VISUAL_BASELINE") != "smoke":
            diff = compare_overlay_visual_images(baseline, image)
            assert diff.passed, diff.message

        assert window._table.rowCount() == len(state.applicants)
        assert window._pinned_id == VISUAL_FIXTURE_PINNED_ID
        assert window._hover_id is None
        assert window._panel._name_label.text() == "Bloomwell"
        assert window._panel.height() == window._panel.target_height()
        assert window._panel.minimumHeight() == window._panel.target_height()
        screen = window.screen()
        if screen is not None:
            assert window.geometry().top() >= screen.availableGeometry().top()
        assert window._role_filter_bar._active == set(ALL_ROLES)
        assert not window._role_filter_bar._reset_btn.isHidden()
        assert all(
            not window._table.isRowHidden(row)
            for row in range(window._table.rowCount())
        )
    finally:
        client.close()


@pytest.mark.parametrize("scenario_name", sorted(OVERLAY_VISUAL_SCENARIOS))
def test_overlay_visual_fixture_scenarios_render_nonblank(
    qtbot, tmp_path, scenario_name
):
    _state, window, client = create_overlay_visual_window(tmp_path, scenario_name)
    qtbot.addWidget(window)

    try:
        show_overlay_visual_window(
            window,
            scenario_name,
            process_events=QApplication.processEvents,
        )

        pixmap = grab_overlay_visual_image(window)
        assert not pixmap.isNull()
        image = pixmap.toImage()
        assert len(_sampled_colours(image)) > 1
    finally:
        client.close()


def test_party_manual_key_visual_scenario_uses_manual_override_path(qtbot, tmp_path):
    _state, window, client = create_overlay_visual_window(tmp_path, "party-manual-key")
    qtbot.addWidget(window)

    try:
        show_overlay_visual_window(
            window,
            "party-manual-key",
            process_events=QApplication.processEvents,
        )

        assert window._active_tab == "party"
        assert window._manual_target_key == 16
        assert window._tab_bar._key_spin.value() == 16
        listing = window._effective_listing()
        assert listing is not None
        assert listing.key_level == 16
        assert window._table.rowCount() == len(window._state.party_members)
    finally:
        client.close()


def test_party_no_listing_manual_key_visual_scenario_synthesizes_listing(
    qtbot, tmp_path
):
    _state, window, client = create_overlay_visual_window(
        tmp_path, "party-no-listing-manual-key"
    )
    qtbot.addWidget(window)

    try:
        show_overlay_visual_window(
            window,
            "party-no-listing-manual-key",
            process_events=QApplication.processEvents,
        )

        listing = window._effective_listing()
        assert window._active_tab == "party"
        assert window._manual_target_key == 14
        assert listing is not None
        assert listing.dungeon_name == "Mythic+"
        assert listing.key_level == 14
    finally:
        client.close()


def test_metrics_raid_only_visual_scenario_hides_disabled_columns(qtbot, tmp_path):
    _state, window, client = create_overlay_visual_window(tmp_path, "metrics-raid-only")
    qtbot.addWidget(window)

    try:
        show_overlay_visual_window(
            window,
            "metrics-raid-only",
            process_events=QApplication.processEvents,
        )

        assert not window._table.isColumnHidden(COL_N)
        assert window._table.isColumnHidden(COL_H)
        assert not window._table.isColumnHidden(COL_M)
        assert window._table.isColumnHidden(COL_MPLUS)
        assert not window._panel._metric_labels["N"].isHidden()
        assert window._panel._metric_labels["H"].isHidden()
        assert not window._panel._metric_labels["M"].isHidden()
        assert window._panel._metric_labels["M+"].isHidden()
    finally:
        client.close()


def test_raid_listing_visual_scenario_covers_raid_context(qtbot, tmp_path):
    state, window, client = create_overlay_visual_window(tmp_path, "raid-listing")
    qtbot.addWidget(window)

    try:
        show_overlay_visual_window(
            window,
            "raid-listing",
            process_events=QApplication.processEvents,
        )

        listing = window._effective_listing()
        assert listing is not None
        assert listing.category_id == 3
        assert listing.difficulty_id == 15
        assert listing.key_level == 0
        assert window._title_bar.title_label.text() == (
            "Raid Applicants — Manaforge Omega (3)"
        )
        assert len(state.applicants) == window._table.rowCount()
        assert not window._table.isColumnHidden(COL_H)
        assert not window._table.isColumnHidden(COL_MPLUS)
        assert window._panel._detail_mode == "raid"
        assert not window._panel._detail_buttons["raid"].isHidden()
        assert not window._panel._detail_buttons["mplus"].isHidden()
        assert window._panel._dungeon_rows[0][0].text()
        assert window._panel._dungeon_rows[0][1].text()
        assert window._panel._dungeon_rows[0][3].text()
        assert window._panel._dungeon_rows[1][0].text() == "Vorasius"
        assert window._panel._dungeon_rows[1][3].text() == "H 72-58 · M 39-52"
        assert window._panel.height() == window._panel.target_height()
        assert window._raid_boss_fetches_in_flight == {}
    finally:
        client.close()


def test_visual_fixture_disabled_tracking_blocks_cursor_hover(
    qtbot, monkeypatch, tmp_path
):
    _state, window, client = create_overlay_visual_window(tmp_path)
    qtbot.addWidget(window)

    try:
        show_overlay_visual_window(window, process_events=QApplication.processEvents)

        viewport = window._table.viewport()
        row0_rect = window._table.visualRect(window._table.model().index(0, 0))
        cursor_pos = viewport.mapToGlobal(row0_rect.center())

        class FakeCursor:
            @staticmethod
            def pos() -> QPoint:
                return cursor_pos

        monkeypatch.setattr(overlay_mod, "QCursor", FakeCursor)

        assert not window._table.hasMouseTracking()
        assert not viewport.hasMouseTracking()

        window._reresolve_hover_from_cursor()

        assert window._hover_id is None
        assert window._pinned_id == VISUAL_FIXTURE_PINNED_ID
        assert window._panel._name_label.text() == "Bloomwell"
    finally:
        client.close()


def _solid_image(width: int, height: int, color: QColor) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    return image


def test_visual_fixture_diff_accepts_identical_images():
    image = _solid_image(4, 4, QColor(10, 20, 30, 255))

    diff = compare_overlay_visual_images(image, image)

    assert diff.passed
    assert diff.changed_pixels == 0


def test_visual_fixture_diff_rejects_dimension_mismatch():
    expected = _solid_image(4, 4, QColor(10, 20, 30, 255))
    actual = _solid_image(5, 4, QColor(10, 20, 30, 255))

    diff = compare_overlay_visual_images(expected, actual)

    assert not diff.passed
    assert "dimension mismatch" in diff.message


def test_visual_fixture_diff_accepts_uniform_dpi_scaling():
    expected = _solid_image(10, 10, QColor(10, 20, 30, 255))
    actual = _solid_image(8, 8, QColor(10, 20, 30, 255))

    diff = compare_overlay_visual_images(expected, actual)

    assert diff.passed


def test_visual_fixture_diff_allows_minor_antialiasing_noise():
    expected = _solid_image(4, 4, QColor(10, 20, 30, 255))
    actual = _solid_image(4, 4, QColor(10, 20, 30, 255))
    actual.setPixelColor(0, 0, QColor(14, 24, 34, 255))

    diff = compare_overlay_visual_images(expected, actual)

    assert diff.passed


def test_visual_fixture_diff_rejects_broad_layout_drift():
    expected = _solid_image(20, 20, QColor(10, 20, 30, 255))
    actual = _solid_image(20, 20, QColor(10, 20, 30, 255))
    for x in range(5):
        for y in range(5):
            actual.setPixelColor(x, y, QColor(240, 240, 240, 255))

    diff = compare_overlay_visual_images(expected, actual)

    assert not diff.passed
    assert "changed pixels" in diff.message
