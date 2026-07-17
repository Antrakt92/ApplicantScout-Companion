"""Shared Settings dialog visual fixture used by tests and screenshot rendering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from applicant_scout.config import Config
from applicant_scout.metric_preferences import (
    DEFAULT_METRIC_PREFERENCES,
    MetricPreferences,
)
from applicant_scout.settings_dialog import SettingsDialog
from scripts.visual_fixture_checks import VisualFixtureDiff, compare_visual_images

if TYPE_CHECKING:
    from PyQt6.QtGui import QImage, QPixmap


SETTINGS_DIALOG_VISUAL_BASELINE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "visual"
    / "settings-dialog-fixture.png"
)
SETTINGS_VISUAL_FIXTURE_REGEN_COMMAND = (
    r".\.venv\Scripts\python scripts\render_settings_dialog_fixture.py --all"
)
SETTINGS_VISUAL_DIFF_CHANNEL_TOLERANCE = 12
SETTINGS_VISUAL_DIFF_MAX_PIXEL_RATIO = 0.005
SETTINGS_VISUAL_DIFF_SCALE_RATIO_TOLERANCE = 0.01
DEFAULT_SETTINGS_VISUAL_SCENARIO = "normal-default"


@dataclass(frozen=True)
class SettingsDialogVisualScenario:
    name: str
    baseline_path: Path
    first_run: bool = False
    blank_credentials: bool = False
    prepare_dialog: Callable[[SettingsDialog], None] | None = None


def _baseline_path(name: str) -> Path:
    if name == DEFAULT_SETTINGS_VISUAL_SCENARIO:
        return SETTINGS_DIALOG_VISUAL_BASELINE_PATH
    return SETTINGS_DIALOG_VISUAL_BASELINE_PATH.with_name(
        f"settings-dialog-fixture-{name}.png"
    )


def _visual_config(
    *, first_run: bool = False, blank_credentials: bool = False
) -> Config:
    metric_preferences = (
        DEFAULT_METRIC_PREFERENCES
        if first_run
        else MetricPreferences(
            mplus=True,
            raid_normal=True,
            raid_heroic=True,
            raid_mythic=True,
        )
    )
    return Config(
        wcl_client_id="" if blank_credentials else "fixture-client-id",
        wcl_client_secret="" if blank_credentials else "fixture-client-secret",
        chatlog_path=Path(
            r"C:\ApplicantScoutFixture\World of Warcraft\_retail_\Logs\WoWChatLog.txt"
        ),
        region="EU",
        cache_dir=Path(r"C:\ApplicantScoutFixture\cache"),
        config_dir=Path(r"C:\ApplicantScoutFixture\config"),
        metric_preferences=metric_preferences,
        screenshots_path=None,
        sync_with_wow=False,
    )


def _stabilize_dialog_inputs(dialog: SettingsDialog, *, first_run: bool) -> None:
    stable_title = (
        "ApplicantScout Companion · First-run setup · Visual QA"
        if first_run
        else "ApplicantScout Companion · Visual QA"
    )
    dialog.setWindowTitle(stable_title)
    dialog.title_label.setText(stable_title)
    # Avoid committing machine-specific WoW paths from _initial_screenshots_path().
    dialog.screenshots_edit.setText("")
    dialog.set_status("")
    dialog._autosave_timer.stop()


def _prepare_update_available(dialog: SettingsDialog) -> None:
    dialog.set_update_available("v0.8.0")


def _prepare_update_installing_disabled(dialog: SettingsDialog) -> None:
    dialog.set_update_available("v0.8.0")
    dialog.set_update_in_progress(True)
    dialog.set_status("Installing update.")


SETTINGS_DIALOG_VISUAL_SCENARIOS: dict[str, SettingsDialogVisualScenario] = {
    DEFAULT_SETTINGS_VISUAL_SCENARIO: SettingsDialogVisualScenario(
        name=DEFAULT_SETTINGS_VISUAL_SCENARIO,
        baseline_path=_baseline_path(DEFAULT_SETTINGS_VISUAL_SCENARIO),
    ),
    "first-run": SettingsDialogVisualScenario(
        name="first-run",
        baseline_path=_baseline_path("first-run"),
        first_run=True,
        blank_credentials=True,
    ),
    "update-available": SettingsDialogVisualScenario(
        name="update-available",
        baseline_path=_baseline_path("update-available"),
        prepare_dialog=_prepare_update_available,
    ),
    "update-installing-disabled": SettingsDialogVisualScenario(
        name="update-installing-disabled",
        baseline_path=_baseline_path("update-installing-disabled"),
        prepare_dialog=_prepare_update_installing_disabled,
    ),
}


def resolve_settings_visual_scenario(
    scenario: str | SettingsDialogVisualScenario = DEFAULT_SETTINGS_VISUAL_SCENARIO,
) -> SettingsDialogVisualScenario:
    if isinstance(scenario, SettingsDialogVisualScenario):
        return scenario
    try:
        return SETTINGS_DIALOG_VISUAL_SCENARIOS[scenario]
    except KeyError as exc:
        names = ", ".join(sorted(SETTINGS_DIALOG_VISUAL_SCENARIOS))
        raise ValueError(
            f"unknown Settings dialog visual fixture scenario {scenario!r}; "
            f"choices: {names}"
        ) from exc


def create_settings_visual_dialog(
    scenario: str | SettingsDialogVisualScenario = DEFAULT_SETTINGS_VISUAL_SCENARIO,
    *,
    hide_to_tray_on_close: bool = True,
) -> SettingsDialog:
    resolved = resolve_settings_visual_scenario(scenario)
    dialog = SettingsDialog(
        _visual_config(
            first_run=resolved.first_run,
            blank_credentials=resolved.blank_credentials,
        ),
        first_run=resolved.first_run,
        hide_to_tray_on_close=hide_to_tray_on_close,
    )
    _stabilize_dialog_inputs(dialog, first_run=resolved.first_run)
    if resolved.prepare_dialog is not None:
        resolved.prepare_dialog(dialog)
    return dialog


def show_settings_visual_dialog(
    dialog: SettingsDialog,
    *,
    process_events: Callable[[], None],
) -> None:
    dialog.adjustSize()
    dialog.show()
    for _ in range(8):
        process_events()
        if dialog.isVisible() and dialog.width() > 0 and dialog.height() > 0:
            return
    raise RuntimeError("Settings dialog visual fixture did not settle before screenshot")


def grab_settings_visual_image(dialog: SettingsDialog) -> "QPixmap":
    return dialog.grab()


def compare_settings_visual_images(
    expected: "QImage",
    actual: "QImage",
) -> VisualFixtureDiff:
    return compare_visual_images(
        expected,
        actual,
        label="settings dialog visual fixture",
        regen_command=SETTINGS_VISUAL_FIXTURE_REGEN_COMMAND,
        channel_tolerance=SETTINGS_VISUAL_DIFF_CHANNEL_TOLERANCE,
        max_pixel_ratio=SETTINGS_VISUAL_DIFF_MAX_PIXEL_RATIO,
        scale_ratio_tolerance=SETTINGS_VISUAL_DIFF_SCALE_RATIO_TOLERANCE,
    )
