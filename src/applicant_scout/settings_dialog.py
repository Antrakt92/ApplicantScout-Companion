"""Qt settings and first-run dialog."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QProcess,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .config import (
    COMMON_WOW_RETAIL_ROOTS,
    Config,
    ConfigError,
    screenshots_path_candidate,
)
from .metric_preferences import MetricPreferences
from .screenshots_path_probe import (
    SCREENSHOTS_PATH_PROBE_ARG,
    run_screenshots_path_probe_command as run_screenshots_path_probe_command,
    screenshots_path_probe_result_path as _screenshots_path_probe_result_path,
)
from .window_geometry import clamp_geometry_to_screens, clamp_rect_to_bounds


CredentialTester = Callable[[str, str, str], str]
SimpleAction = Callable[[], str]
WCL_CREATE_CLIENT_EXAMPLE_PATH = (
    Path(__file__).with_name("assets") / "wcl_create_client_example.jpg"
)
WCL_CREATE_CLIENT_APP_NAME = "ApplicantScout"
WCL_CREATE_CLIENT_REDIRECT_URL = "http://localhost"
WCL_CLIENTS_URL = "https://www.warcraftlogs.com/api/clients/"
SUPPORT_URL = "https://ko-fi.com/antrakt92"
APP_ICON_PATH = Path(__file__).with_name("assets") / "app_icon.ico"
SUPPORT_TOOLTIP = "Support ApplicantScout on Ko-fi."
UPDATE_ACCESSIBLE_NAME = "Install ApplicantScout update"
UPDATE_DEFAULT_TOOLTIP = "Install available ApplicantScout update."
UPDATE_INSTALLING_TOOLTIP = "Installing ApplicantScout update..."
CLOSE_SETUP_TOOLTIP = "Close ApplicantScout setup."
CLOSE_TRAY_TOOLTIP = "Hide ApplicantScout settings to tray."
CLOSE_QUIT_TOOLTIP = "Quit ApplicantScout."
SETTINGS_QUIT_BLOCKED_MESSAGE = (
    "Settings were not saved. Fix or revert the pending settings change before quitting."
)
UPDATE_BUSY_CLOSE_MESSAGE = "Update is installing. Wait for it to finish before closing."
CACHE_RESET_BUSY_MESSAGE = (
    "Cache reset is running. Wait for it to finish before closing."
)
CACHE_RESET_ACTION_BLOCKED_MESSAGE = (
    "Another settings action is still running. Wait for it to finish before resetting cache."
)
WCL_CREDENTIAL_TEST_BUSY_MESSAGE = (
    "WCL credential test is running. Wait for it to finish before continuing."
)
SCREENSHOTS_WARNING_DEBOUNCE_MS = 250
SCREENSHOTS_VALIDATION_PENDING_MESSAGE = "Checking Screenshots folder..."
SCREENSHOTS_PATH_PROBE_TIMEOUT_MS = 5000
SCREENSHOTS_PATH_PROBE_TIMEOUT_WARNING = (
    "Screenshots folder warning: path check timed out."
)
SCREENSHOTS_PATH_PROBE_FAILURE_WARNING = (
    "Screenshots folder warning: could not run the isolated path check."
)


_SETTINGS_STYLESHEET = """
QDialog#applicantScoutSettings,
QDialog#releaseNotesDialog,
QDialog#wclSetupExampleDialog {
    background-color: #090a0f;
    color: #eee8dd;
    font-family: 'Segoe UI', 'Cantarell', 'Helvetica Neue', sans-serif;
}
#settingsTitleBar {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 rgba(40, 38, 36, 255),
        stop: 0.48 rgba(27, 27, 31, 255),
        stop: 1 rgba(16, 18, 24, 255)
    );
    border-top: 1px solid #8b7049;
    border-left: 1px solid #6d583d;
    border-right: 1px solid #6d583d;
    border-bottom: 1px solid #785d3c;
}
#settingsTitle {
    color: #f0e8d7;
    font-weight: 600;
}
#settingsBody {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 rgba(16, 18, 25, 255),
        stop: 0.58 rgba(10, 12, 17, 255),
        stop: 1 rgba(6, 7, 11, 255)
    );
    border-left: 1px solid #6d583d;
    border-right: 1px solid #6d583d;
    border-bottom: 1px solid #6d583d;
}
#settingsScroll,
#settingsScroll > QWidget > QWidget {
    background: transparent;
    border: none;
}
#settingsHero {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 rgba(88, 49, 34, 238),
        stop: 0.62 rgba(44, 32, 28, 232),
        stop: 1 rgba(23, 25, 31, 224)
    );
    border: 1px solid rgba(205, 128, 82, 220);
    border-radius: 6px;
}
#settingsHeroEyebrow {
    color: #f0c77c;
    font-size: 10px;
    font-weight: 700;
}
#settingsHeroTitle {
    color: #fff5e8;
    font-size: 18px;
    font-weight: 700;
}
#settingsHeroText {
    color: #c8c0b5;
}
#warcraftLogsSection,
#scoutingSection {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 rgba(25, 27, 35, 244),
        stop: 1 rgba(14, 16, 22, 244)
    );
    border: 1px solid rgba(102, 84, 61, 215);
    border-radius: 6px;
}
#settingsSectionTitle {
    color: #f0c77c;
    font-size: 11px;
    font-weight: 700;
}
#settingsSectionHint,
#settingsAutosaveHint,
#releaseNotesSubtitle {
    color: #aaa59d;
}
#wclClientsToExampleArrow {
    color: #9b8765;
}
QLabel {
    color: #e8e1d5;
}
QLineEdit,
QComboBox,
QTextBrowser {
    color: #f2eadc;
    background-color: rgba(31, 34, 42, 238);
    border: 1px solid rgba(92, 85, 75, 220);
    border-radius: 4px;
    selection-background-color: #805038;
    selection-color: #ffffff;
    padding: 5px 7px;
}
QLineEdit:hover,
QComboBox:hover,
QTextBrowser:hover {
    border-color: rgba(156, 126, 84, 225);
}
QLineEdit:focus,
QComboBox:focus,
QTextBrowser:focus {
    border: 2px solid #e5cc80;
    padding: 4px 6px;
}
QLineEdit:disabled,
QComboBox:disabled,
QTextBrowser:disabled {
    color: #77746f;
    background-color: rgba(28, 30, 35, 210);
    border-color: rgba(67, 64, 59, 180);
}
QPushButton,
QToolButton {
    color: #e8ded0;
    background-color: rgba(43, 45, 53, 238);
    border: 1px solid rgba(103, 91, 74, 220);
    border-radius: 4px;
    padding: 4px 10px;
}
QPushButton:hover,
QToolButton:hover {
    color: #fff6e6;
    background-color: rgba(72, 63, 51, 244);
    border-color: rgba(185, 145, 83, 230);
}
QPushButton:pressed,
QToolButton:pressed {
    background-color: rgba(112, 69, 46, 250);
    border-color: #d7ad68;
}
QPushButton:focus,
QToolButton:focus,
QCheckBox:focus {
    border: 2px solid #e5cc80;
}
QPushButton:disabled,
QToolButton:disabled {
    color: #77746f;
    background-color: rgba(35, 37, 42, 210);
    border-color: rgba(68, 65, 60, 175);
}
#wclClientsLink {
    color: #d9bb78;
    background: transparent;
    border: 1px solid transparent;
    padding: 1px 2px;
    text-decoration: underline;
}
#wclClientsLink:hover {
    color: #fff2c8;
    background-color: rgba(73, 62, 48, 180);
    border-color: rgba(145, 122, 85, 180);
}
#wclClientsLink:focus {
    color: #fff2c8;
    background-color: rgba(73, 62, 48, 210);
    border: 2px solid #e5cc80;
    padding: 0 1px;
}
#supportApplicantScout {
    color: #ff7b88;
    background: transparent;
    border: 1px solid transparent;
    font-size: 17px;
    font-weight: 600;
    padding: 0 0 1px 0;
}
#supportApplicantScout:hover {
    color: #ff9aa4;
    background-color: #2b171d;
    border-color: #7a3d49;
}
#installUpdate {
    background: transparent;
    border: 1px solid transparent;
    padding: 3px;
}
#installUpdate:hover {
    background-color: #17283c;
    border-color: #4d7198;
}
#settingsClose {
    color: #c8c1b6;
    background: transparent;
    border: 1px solid transparent;
    font-size: 18px;
    padding: 0 0 2px 0;
}
#settingsClose:hover {
    color: #ffffff;
    background-color: #46282a;
    border-color: #8b4a4f;
}
#supportApplicantScout:focus,
#installUpdate:focus,
#settingsClose:focus {
    border: 2px solid #e5cc80;
}
#wclClientsLink:disabled,
#supportApplicantScout:disabled,
#settingsClose:disabled {
    color: #686661;
    background: transparent;
    border-color: transparent;
}
#startCompanion {
    color: #fff7ed;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 rgba(151, 83, 56, 255),
        stop: 1 rgba(91, 49, 37, 255)
    );
    border-color: #de8b60;
    font-weight: 700;
    padding: 6px 14px;
}
#startCompanion:hover {
    background-color: #a85b3d;
    border-color: #edb47d;
}
QCheckBox {
    color: #dfd8cc;
    spacing: 6px;
    padding: 2px;
}
QCheckBox:disabled {
    color: #77746f;
}
#settingsFooter {
    border-top: 1px solid rgba(91, 74, 54, 175);
}
#settingsStatus {
    border-radius: 4px;
    padding: 4px 7px;
}
#settingsStatus[statusState="success"] {
    background-color: rgba(39, 91, 58, 150);
    border: 1px solid rgba(84, 185, 121, 170);
}
#settingsStatus[statusState="warning"] {
    background-color: rgba(78, 60, 29, 180);
    border: 1px solid rgba(170, 132, 61, 190);
}
#settingsStatus[statusState="busy"] {
    background-color: rgba(31, 57, 77, 180);
    border: 1px solid rgba(76, 124, 157, 195);
}
#settingsStatus[statusState="error"] {
    background-color: rgba(82, 35, 39, 185);
    border: 1px solid rgba(171, 72, 79, 195);
}
QMenu {
    color: #eee8dd;
    background-color: #15171d;
    border: 1px solid #8b7049;
    padding: 4px;
}
QMenu::item {
    padding: 5px 22px 5px 9px;
    border-radius: 3px;
}
QMenu::item:selected {
    color: #fff5e8;
    background-color: #684431;
}
QMenu::item:disabled {
    color: #77746f;
}
QScrollBar:vertical {
    background-color: #090b10;
    width: 10px;
    margin: 1px 0;
    border-left: 1px solid rgba(74, 64, 51, 150);
}
QScrollBar::handle:vertical {
    background-color: rgba(105, 91, 72, 230);
    min-height: 28px;
    border: 1px solid rgba(153, 123, 78, 200);
    border-radius: 4px;
}
QScrollBar::handle:vertical:hover {
    background-color: rgba(142, 116, 79, 240);
    border-color: rgba(202, 158, 92, 225);
}
QScrollBar::add-line,
QScrollBar::sub-line {
    width: 0;
    height: 0;
    background: none;
    border: none;
}
QScrollBar::add-page,
QScrollBar::sub-page {
    background: none;
}
QToolTip {
    color: #eee8dd;
    background-color: #15171d;
    border: 1px solid #8b7049;
    padding: 6px;
    opacity: 255;
}
"""


def _screenshots_path_probe_program_args(
    path: str,
    token: str,
) -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return sys.executable, [SCREENSHOTS_PATH_PROBE_ARG, path, token]
    return sys.executable, [
        "-m",
        "applicant_scout.screenshots_path_probe",
        SCREENSHOTS_PATH_PROBE_ARG,
        path,
        token,
    ]


def _decode_screenshots_path_probe_output(raw: bytes) -> str | None:
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"warning"}:
        raise ValueError("unexpected path probe payload")
    warning = payload["warning"]
    if warning is not None and not isinstance(warning, str):
        raise ValueError("unexpected path probe warning")
    return warning


def run_bounded_screenshots_path_probe(
    path: Path,
    *,
    timeout_ms: int = SCREENSHOTS_PATH_PROBE_TIMEOUT_MS,
) -> str | None:
    """Validate a path in a killable child process and return its warning."""
    token = uuid.uuid4().hex
    result_path = _screenshots_path_probe_result_path(token)
    program, arguments = _screenshots_path_probe_program_args(str(path), token)
    try:
        completed = subprocess.run(
            [program, *arguments],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(timeout_ms, 1) / 1000,
        )
        if completed.returncode != 0:
            return SCREENSHOTS_PATH_PROBE_FAILURE_WARNING
        return _decode_screenshots_path_probe_output(result_path.read_bytes())
    except subprocess.TimeoutExpired:
        return SCREENSHOTS_PATH_PROBE_TIMEOUT_WARNING
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError):
        return SCREENSHOTS_PATH_PROBE_FAILURE_WARNING
    finally:
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass


def _settings_window_title(*, first_run: bool) -> str:
    if first_run:
        return f"ApplicantScout Companion · First-run setup · v{__version__}"
    return f"ApplicantScout Companion · v{__version__}"


def _download_icon(color: str = "#4da3ff") -> QIcon:
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidth(2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawLine(10, 3, 10, 12)
    painter.drawLine(6, 8, 10, 12)
    painter.drawLine(14, 8, 10, 12)
    painter.drawLine(5, 16, 15, 16)
    painter.drawLine(5, 13, 5, 16)
    painter.drawLine(15, 13, 15, 16)
    painter.end()
    return QIcon(pixmap)


def _set_tooltip_and_accessibility(
    button: QAbstractButton,
    *,
    tooltip: str,
    accessible_name: str,
    accessible_description: str | None = None,
) -> None:
    button.setToolTip(tooltip)
    button.setAccessibleName(accessible_name)
    button.setAccessibleDescription(accessible_description or tooltip)


def _set_action_help(
    action: QAction,
    *,
    tooltip: str,
    status_tip: str | None = None,
    whats_this: str | None = None,
) -> None:
    action.setToolTip(tooltip)
    action.setStatusTip(status_tip or tooltip)
    action.setWhatsThis(whats_this or status_tip or tooltip)


def _close_button_copy(*, first_run: bool, hide_to_tray: bool) -> tuple[str, str]:
    if first_run:
        return CLOSE_SETUP_TOOLTIP, "Close setup"
    if hide_to_tray:
        return CLOSE_TRAY_TOOLTIP, "Hide settings to tray"
    return CLOSE_QUIT_TOOLTIP, "Quit ApplicantScout"


def _settings_section(
    parent: QWidget,
    *,
    object_name: str,
    title: str,
    hint: str,
) -> tuple[QWidget, QVBoxLayout]:
    section = QWidget(parent)
    section.setObjectName(object_name)
    layout = QVBoxLayout(section)
    layout.setContentsMargins(14, 11, 14, 13)
    layout.setSpacing(8)

    section_title = QLabel(title, section)
    section_title.setObjectName("settingsSectionTitle")
    layout.addWidget(section_title)

    section_hint = QLabel(hint, section)
    section_hint.setObjectName("settingsSectionHint")
    section_hint.setWordWrap(True)
    layout.addWidget(section_hint)
    return section, layout


@dataclass(frozen=True)
class SettingsValues:
    wcl_client_id: str
    wcl_client_secret: str
    region: str
    screenshots_path: str
    metric_preferences: MetricPreferences
    sync_with_wow: bool


@dataclass(frozen=True)
class SettingsUpdateResult:
    message: str
    installer_handoff: bool = False
    installer_launch: object | None = None


ActionReturn = str | SettingsUpdateResult
UpdateAction = Callable[[], ActionReturn]


@dataclass(frozen=True)
class _AsyncActionResult:
    button: QAbstractButton | QAction
    message: str
    error: bool = False
    success_payload: object | None = None
    keep_disabled: bool = False
    installer_launch: object | None = None


class _AsyncSignals(QObject):
    finished = pyqtSignal(object)


class _SettingsScrollArea(QScrollArea):
    """Expose the full form as the preferred size while allowing runtime shrink."""

    def sizeHint(self) -> QSize:  # type: ignore[override]
        content = self.widget()
        if content is None:
            return super().sizeHint()
        hint = content.sizeHint()
        frame = self.frameWidth() * 2
        return QSize(hint.width() + frame, hint.height() + frame)


@dataclass(frozen=True)
class _ScreenshotsValidationResult:
    generation: int
    path: str
    warning: str | None


class ReleaseNotesDialog(QDialog):
    def __init__(self, release_notes: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("releaseNotesDialog")
        self.setStyleSheet(_SETTINGS_STYLESHEET)
        self.setWindowTitle("ApplicantScout Changelog")
        self.setModal(True)
        self.setMinimumSize(720, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("ApplicantScout Changelog")
        title.setObjectName("releaseNotesTitle")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        subtitle = QLabel("Latest companion release notes and earlier changes.")
        subtitle.setObjectName("releaseNotesSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.notes_browser = QTextBrowser(self)
        self.notes_browser.setObjectName("releaseNotesText")
        self.notes_browser.setReadOnly(True)
        self.notes_browser.setOpenExternalLinks(True)
        self.notes_browser.setMarkdown(release_notes)
        layout.addWidget(self.notes_browser, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def _initial_screenshots_path(cfg: Config) -> str:
    if cfg.screenshots_path is not None:
        return str(cfg.screenshots_path)
    try:
        return str(screenshots_path_candidate(cfg))
    except ConfigError:
        return str(COMMON_WOW_RETAIL_ROOTS[0] / "Screenshots")


class SettingsDialog(QDialog):
    valuesChanged = pyqtSignal(object)
    credentialsValidated = pyqtSignal(object)
    quitRequested = pyqtSignal()
    updateStarted = pyqtSignal()
    updateFinished = pyqtSignal(bool)
    updateCompleted = pyqtSignal()
    updateHandoffStarted = pyqtSignal(str, object)
    changelogRequested = pyqtSignal()
    def __init__(
        self,
        cfg: Config,
        *,
        first_run: bool = False,
        credential_tester: CredentialTester | None = None,
        open_logs: SimpleAction | None = None,
        clear_cache: SimpleAction | None = None,
        check_updates: UpdateAction | None = None,
        hide_to_tray_on_close: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("applicantScoutSettings")
        self.setStyleSheet(_SETTINGS_STYLESHEET)
        self._first_run = first_run
        self._hide_to_tray_on_close = hide_to_tray_on_close
        self._update_in_progress = False
        self._cache_action_in_progress = False
        self._credential_test_in_progress = False
        self.start_button: QPushButton | None = None
        self.setup_quit_button: QPushButton | None = None
        self._credential_tester = credential_tester
        self._open_logs = open_logs
        self._clear_cache = clear_cache
        self._check_updates = check_updates
        self._last_values_apply_succeeded = True
        self._latest_update_version: str | None = None
        self._signals = _AsyncSignals(self)
        self._signals.finished.connect(self._finish_async_action)
        self._title_drag_offset: QPoint | None = None
        gui_app = QApplication.instance()
        if isinstance(gui_app, QApplication):
            gui_app.screenRemoved.connect(self._on_screen_topology_changed)
            gui_app.primaryScreenChanged.connect(self._on_screen_topology_changed)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(700)
        self._autosave_timer.timeout.connect(self._emit_values_changed_if_valid)
        self._pending_screenshots_warning_path = ""
        self._screenshots_warning_path = ""
        self._screenshots_warning_text: str | None = None
        self._screenshots_validation_generation = 0
        self._screenshots_validation_started_generation: int | None = None
        self._screenshots_validation_ready_generation: int | None = None
        self._screenshots_validation_required_generation: int | None = None
        self._screenshots_validation_waiting_autosave = False
        self._screenshots_validation_process: QProcess | None = None
        self._screenshots_validation_process_generation: int | None = None
        self._screenshots_validation_process_path = ""
        self._screenshots_validation_process_result_path: Path | None = None
        self._screenshots_validation_process_timeout = QTimer(self)
        self._screenshots_validation_process_timeout.setSingleShot(True)
        self._screenshots_validation_process_timeout.setInterval(
            SCREENSHOTS_PATH_PROBE_TIMEOUT_MS
        )
        self._screenshots_validation_process_timeout.timeout.connect(
            self._handle_screenshots_validation_timeout
        )
        self.destroyed.connect(
            lambda _object=None: self._cancel_screenshots_validation_process()
        )
        self._screenshots_warning_timer = QTimer(self)
        self._screenshots_warning_timer.setSingleShot(True)
        self._screenshots_warning_timer.setInterval(SCREENSHOTS_WARNING_DEBOUNCE_MS)
        self._screenshots_warning_timer.timeout.connect(
            self._flush_screenshots_warning
        )

        window_title = _settings_window_title(first_run=first_run)
        self.setWindowTitle(window_title)
        self.setModal(first_run)
        self.setMinimumWidth(560)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        outer.addWidget(self._build_title_bar(window_title))

        body = QWidget(self)
        body.setObjectName("settingsBody")
        root = QVBoxLayout(body)
        root.setContentsMargins(14, 13, 14, 14)
        root.setSpacing(11)

        if first_run:
            hero = QWidget(body)
            hero.setObjectName("settingsHero")
            hero_layout = QVBoxLayout(hero)
            hero_layout.setContentsMargins(14, 11, 14, 12)
            hero_layout.setSpacing(3)
            hero_eyebrow = QLabel("FIRST-RUN SETUP", hero)
            hero_eyebrow.setObjectName("settingsHeroEyebrow")
            hero_layout.addWidget(hero_eyebrow)
            hero_title = QLabel("Connect ApplicantScout", hero)
            hero_title.setObjectName("settingsHeroTitle")
            hero_layout.addWidget(hero_title)
            intro = QLabel(
                "Add Warcraft Logs credentials and your WoW Screenshots folder. "
                "Every valid change saves automatically."
            )
            intro.setObjectName("settingsHeroText")
            intro.setWordWrap(True)
            hero_layout.addWidget(intro)
            root.addWidget(hero)

        wcl_section, wcl_root = _settings_section(
            body,
            object_name="warcraftLogsSection",
            title="WARCRAFT LOGS",
            hint="Connect the private API client used to fetch applicant performance.",
        )
        root.addWidget(wcl_section)

        wcl_link_row = QWidget(wcl_section)
        wcl_link_layout = QHBoxLayout(wcl_link_row)
        wcl_link_layout.setContentsMargins(0, 0, 0, 0)
        wcl_link_layout.setSpacing(8)
        self.wcl_clients_link = QPushButton("Warcraft Logs API clients")
        self.wcl_clients_link.setObjectName("wclClientsLink")
        self.wcl_clients_link.setFlat(True)
        self.wcl_clients_link.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.wcl_clients_link.setAccessibleName("Open Warcraft Logs API clients")
        self.wcl_clients_link.setAccessibleDescription(
            "Open the Warcraft Logs Create Client page in the default browser."
        )
        self.wcl_clients_link.clicked.connect(self._open_wcl_clients)
        wcl_link_layout.addWidget(self.wcl_clients_link)
        self.wcl_example_arrow = QLabel("→")
        self.wcl_example_arrow.setObjectName("wclClientsToExampleArrow")
        self.wcl_example_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.wcl_example_arrow.setToolTip(
            "Open the example to see exactly what to enter on Warcraft Logs."
        )
        wcl_link_layout.addWidget(self.wcl_example_arrow)
        self.wcl_example_button = QPushButton("Show example")
        self.wcl_example_button.setObjectName("showWclSetupExample")
        _set_tooltip_and_accessibility(
            self.wcl_example_button,
            tooltip="Show the Warcraft Logs Create Client form values to copy.",
            accessible_name="Show WCL setup example",
            accessible_description=(
                "Show the Warcraft Logs Create Client form values to copy."
            ),
        )
        self.wcl_example_button.clicked.connect(self._show_wcl_setup_example)
        wcl_link_layout.addWidget(self.wcl_example_button)
        wcl_link_layout.addStretch(1)
        wcl_root.addWidget(wcl_link_row)
        credentials_help = QLabel(
            "Create a Warcraft Logs API client with Redirect URL "
            f"{WCL_CREATE_CLIENT_REDIRECT_URL} and leave Public Client unchecked. Copy the "
            "generated Client ID and Client Secret into the fields below."
        )
        credentials_help.setWordWrap(True)
        wcl_root.addWidget(credentials_help)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        wcl_root.addLayout(form)

        display_client_id = getattr(cfg, "draft_wcl_client_id", "") or cfg.wcl_client_id
        display_client_secret = (
            getattr(cfg, "draft_wcl_client_secret", "") or cfg.wcl_client_secret
        )

        self.client_id_edit = QLineEdit(display_client_id)
        self.client_id_edit.setObjectName("wclClientId")
        self.client_id_edit.setPlaceholderText("Paste your Client ID")
        self.client_id_edit.setToolTip("Client ID generated by your Warcraft Logs API client.")
        self.client_id_edit.setAccessibleName("Warcraft Logs Client ID")
        self.client_id_edit.setAccessibleDescription(
            "Client ID generated by the private Warcraft Logs API client."
        )
        form.addRow("WCL Client ID", self.client_id_edit)

        self.client_secret_edit = QLineEdit(display_client_secret)
        self.client_secret_edit.setObjectName("wclClientSecret")
        self.client_secret_edit.setPlaceholderText("Paste your Client Secret")
        self.client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret_edit.setToolTip(
            "Client Secret generated by your Warcraft Logs API client."
        )
        self.client_secret_edit.setAccessibleName("Warcraft Logs Client Secret")
        self.client_secret_edit.setAccessibleDescription(
            "Secret generated by the private Warcraft Logs API client; the value is masked."
        )
        secret_row = QWidget(wcl_section)
        secret_layout = QHBoxLayout(secret_row)
        secret_layout.setContentsMargins(0, 0, 0, 0)
        secret_layout.setSpacing(6)
        secret_layout.addWidget(self.client_secret_edit, stretch=1)
        self.reveal_secret_button = QPushButton("Show", secret_row)
        self.reveal_secret_button.setObjectName("revealWclClientSecret")
        self.reveal_secret_button.setCheckable(True)
        self.reveal_secret_button.setAutoDefault(False)
        self.reveal_secret_button.setMinimumWidth(56)
        self.reveal_secret_button.toggled.connect(self._set_client_secret_visible)
        self._set_client_secret_visible(False)
        secret_layout.addWidget(self.reveal_secret_button)
        form.addRow("WCL Client Secret", secret_row)
        QWidget.setTabOrder(self.client_secret_edit, self.reveal_secret_button)

        self.region_combo = QComboBox()
        self.region_combo.setObjectName("region")
        self.region_combo.addItems(["EU", "US", "KR", "TW", "CN"])
        self.region_combo.setToolTip(
            "Fallback region used when an applicant name does not include a known realm."
        )
        self.region_combo.setAccessibleName("Warcraft Logs fallback region")
        self.region_combo.setAccessibleDescription(
            "Fallback region used when the applicant's character realm cannot determine it."
        )
        region_idx = self.region_combo.findText((cfg.region or "EU").upper())
        self.region_combo.setCurrentIndex(max(0, region_idx))
        form.addRow("Region fallback", self.region_combo)

        scouting_section, scouting_root = _settings_section(
            body,
            object_name="scoutingSection",
            title="SCOUTING",
            hint="Choose the local screenshot source and the data shown in applicant rows.",
        )
        root.addWidget(scouting_section)
        scouting_form = QFormLayout()
        scouting_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        scouting_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        scouting_form.setHorizontalSpacing(12)
        scouting_form.setVerticalSpacing(8)
        scouting_root.addLayout(scouting_form)

        path_row = QWidget(scouting_section)
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(6)
        self.screenshots_edit = QLineEdit(_initial_screenshots_path(cfg))
        self.screenshots_edit.setObjectName("screenshotsPath")
        self.screenshots_edit.setPlaceholderText(
            r"Example: C:\Program Files (x86)\World of Warcraft\_retail_\Screenshots"
        )
        self.screenshots_edit.setToolTip(
            r"Select WoW's in-game Screenshots folder under _retail_\Screenshots."
        )
        self.screenshots_edit.setAccessibleName("WoW Screenshots folder")
        self.screenshots_edit.setAccessibleDescription(
            r"Path to WoW's _retail_\Screenshots folder."
        )
        self.screenshots_edit.textChanged.connect(self._handle_screenshots_text_changed)
        path_layout.addWidget(self.screenshots_edit, stretch=1)
        self.browse_button = QPushButton("Browse")
        self.browse_button.setObjectName("browseScreenshots")
        _set_tooltip_and_accessibility(
            self.browse_button,
            tooltip="Browse to WoW's in-game Screenshots folder.",
            accessible_name="Browse WoW Screenshots folder",
        )
        self.browse_button.clicked.connect(self._browse_screenshots)
        path_layout.addWidget(self.browse_button)
        scouting_form.addRow("WoW Screenshots folder", path_row)

        metrics_row = QWidget(scouting_section)
        metrics_layout = QHBoxLayout(metrics_row)
        metrics_layout.setContentsMargins(0, 0, 0, 0)
        metrics_layout.setSpacing(8)
        prefs = cfg.metric_preferences
        self.mplus_check = QCheckBox("M+")
        self.mplus_check.setObjectName("fetchMplus")
        self.mplus_check.setToolTip("Fetch and show Mythic+ dungeon parses.")
        self.mplus_check.setAccessibleName("Fetch Mythic Plus data")
        self.mplus_check.setAccessibleDescription(
            "Fetch and show Mythic+ dungeon parses."
        )
        self.mplus_check.setChecked(prefs.mplus)
        self.raid_normal_check = QCheckBox("Raid N")
        self.raid_normal_check.setObjectName("fetchRaidNormal")
        self.raid_normal_check.setToolTip("Fetch and show Normal raid parses.")
        self.raid_normal_check.setAccessibleName("Fetch Normal raid data")
        self.raid_normal_check.setAccessibleDescription(
            "Fetch and show Normal raid parses."
        )
        self.raid_normal_check.setChecked(prefs.raid_normal)
        self.raid_heroic_check = QCheckBox("Raid H")
        self.raid_heroic_check.setObjectName("fetchRaidHeroic")
        self.raid_heroic_check.setToolTip("Fetch and show Heroic raid parses.")
        self.raid_heroic_check.setAccessibleName("Fetch Heroic raid data")
        self.raid_heroic_check.setAccessibleDescription(
            "Fetch and show Heroic raid parses."
        )
        self.raid_heroic_check.setChecked(prefs.raid_heroic)
        self.raid_mythic_check = QCheckBox("Raid M")
        self.raid_mythic_check.setObjectName("fetchRaidMythic")
        self.raid_mythic_check.setToolTip("Fetch and show Mythic raid parses.")
        self.raid_mythic_check.setAccessibleName("Fetch Mythic raid data")
        self.raid_mythic_check.setAccessibleDescription(
            "Fetch and show Mythic raid parses."
        )
        self.raid_mythic_check.setChecked(prefs.raid_mythic)
        for checkbox in (
            self.raid_normal_check,
            self.raid_heroic_check,
            self.raid_mythic_check,
            self.mplus_check,
        ):
            metrics_layout.addWidget(checkbox)
        metrics_layout.addStretch(1)
        scouting_form.addRow("WCL data", metrics_row)

        self.sync_with_wow_check = QCheckBox("Start and stop with WoW")
        self.sync_with_wow_check.setObjectName("syncWithWow")
        self.sync_with_wow_check.setToolTip(
            "Start ApplicantScout when WoW starts and quit it when WoW closes."
        )
        self.sync_with_wow_check.setAccessibleName("Synchronize with WoW")
        self.sync_with_wow_check.setAccessibleDescription(
            "Start ApplicantScout when WoW starts and quit it when WoW closes."
        )
        self.sync_with_wow_check.setChecked(cfg.sync_with_wow)
        scouting_form.addRow("", self.sync_with_wow_check)

        root.addStretch(1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("settingsStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Settings status")
        self.status_label.setAccessibleDescription("")
        self.status_label.setProperty("statusState", "idle")
        self.status_label.hide()
        footer = QWidget(self)
        footer.setObjectName("settingsFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 9, 0, 0)
        footer_layout.setSpacing(8)
        self.support_button = QToolButton(footer)
        self.support_button.setObjectName("supportApplicantScout")
        self.support_button.setText("♡")
        _set_tooltip_and_accessibility(
            self.support_button,
            tooltip=SUPPORT_TOOLTIP,
            accessible_name="Support ApplicantScout",
            accessible_description="Open Ko-fi support for ApplicantScout.",
        )
        self.support_button.setFixedSize(26, 24)
        self.support_button.clicked.connect(self._open_support)
        footer_layout.addWidget(self.support_button)
        self.autosave_hint = QLabel("Valid changes save automatically", footer)
        self.autosave_hint.setObjectName("settingsAutosaveHint")
        self.autosave_hint.setAccessibleName("Automatic save status")
        self.autosave_hint.setAccessibleDescription(
            "Valid settings changes save automatically."
        )
        footer_layout.addWidget(self.autosave_hint, stretch=1)
        footer_layout.addWidget(self.status_label, stretch=1)
        self.test_button = QPushButton("Test WCL", footer)
        self.test_button.setObjectName("testWcl")
        _set_tooltip_and_accessibility(
            self.test_button,
            tooltip="Validate the current Warcraft Logs credentials.",
            accessible_name="Test Warcraft Logs credentials",
        )
        self.test_button.clicked.connect(self._test_credentials)
        footer_layout.addWidget(self.test_button)
        footer_layout.addWidget(self._build_more_actions_button(footer))
        root.addWidget(footer)

        if first_run:
            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            buttons.addStretch(1)
            self.setup_quit_button = QPushButton("Quit setup")
            self.setup_quit_button.setObjectName("quitApplicantScout")
            _set_tooltip_and_accessibility(
                self.setup_quit_button,
                tooltip="Quit ApplicantScout without completing setup.",
                accessible_name="Quit ApplicantScout setup",
            )
            self.setup_quit_button.clicked.connect(self.reject)
            buttons.addWidget(self.setup_quit_button)
            self.start_button = QPushButton("Start companion")
            self.start_button.setObjectName("startCompanion")
            _set_tooltip_and_accessibility(
                self.start_button,
                tooltip="Validate these settings and start ApplicantScout.",
                accessible_name="Start ApplicantScout companion",
            )
            self.start_button.setDefault(True)
            self.start_button.clicked.connect(self.accept)
            buttons.addWidget(self.start_button)
            root.addLayout(buttons)
        self.body_scroll = _SettingsScrollArea(self)
        self.body_scroll.setObjectName("settingsScroll")
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.body_scroll.setWidget(body)
        outer.addWidget(self.body_scroll)
        self._connect_value_change_signals()
        self._schedule_screenshots_warning(self.screenshots_edit.text())
        self.client_id_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.installEventFilter(self)

    def _build_title_bar(self, title: str) -> QWidget:
        title_bar = QWidget(self)
        self.title_bar = title_bar
        title_bar.setObjectName("settingsTitleBar")
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(8, 4, 6, 4)
        title_layout.setSpacing(8)

        self.title_icon = QLabel(title_bar)
        self.title_icon.setObjectName("settingsTitleIcon")
        icon = QIcon(str(APP_ICON_PATH))
        if not icon.isNull():
            self.title_icon.setPixmap(icon.pixmap(16, 16))
        title_layout.addWidget(self.title_icon)

        self.title_label = QLabel(title, title_bar)
        self.title_label.setObjectName("settingsTitle")
        title_layout.addWidget(self.title_label, stretch=1)

        self.update_button = QToolButton(title_bar)
        self.update_button.setObjectName("installUpdate")
        self.update_button.setText("")
        self.update_button.setIcon(_download_icon())
        _set_tooltip_and_accessibility(
            self.update_button,
            tooltip=UPDATE_DEFAULT_TOOLTIP,
            accessible_name=UPDATE_ACCESSIBLE_NAME,
        )
        self.update_button.setFixedSize(30, 26)
        self.update_button.hide()
        self.update_button.clicked.connect(self._check_for_updates)
        title_layout.addWidget(self.update_button)

        self.close_button = QToolButton(title_bar)
        self.close_button.setObjectName("settingsClose")
        self.close_button.setText("×")
        close_tooltip, close_accessible_name = _close_button_copy(
            first_run=self._first_run,
            hide_to_tray=self._hide_to_tray_on_close,
        )
        _set_tooltip_and_accessibility(
            self.close_button,
            tooltip=close_tooltip,
            accessible_name=close_accessible_name,
        )
        self.close_button.setFixedSize(30, 26)
        self.close_button.clicked.connect(self.close)
        title_layout.addWidget(self.close_button)

        for widget in (title_bar, self.title_icon, self.title_label):
            widget.installEventFilter(self)
        return title_bar

    def _build_more_actions_button(self, parent: QWidget) -> QToolButton:
        self.more_actions_button = QToolButton(parent)
        self.more_actions_button.setObjectName("settingsMoreActions")
        self.more_actions_button.setText("More")
        _set_tooltip_and_accessibility(
            self.more_actions_button,
            tooltip=(
                "Open logs, view the changelog, reset cached data, or quit ApplicantScout."
            ),
            accessible_name="More settings actions",
        )
        self.more_actions_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        actions_menu = QMenu(self.more_actions_button)
        self.logs_action = QAction("Open logs", self.more_actions_button)
        self.logs_action.setObjectName("openLogs")
        _set_action_help(
            self.logs_action,
            tooltip="Open ApplicantScout logs.",
            status_tip="Open the ApplicantScout log folder.",
            whats_this="Open the folder containing companion logs.",
        )
        self.logs_action.triggered.connect(self._open_log_folder)
        actions_menu.addAction(self.logs_action)
        self.changelog_action = QAction("View changelog", self.more_actions_button)
        self.changelog_action.setObjectName("viewChangelog")
        _set_action_help(
            self.changelog_action,
            tooltip="View the ApplicantScout changelog.",
            status_tip="Open the ApplicantScout changelog.",
            whats_this="Open recent companion release notes and changelog entries.",
        )
        self.changelog_action.triggered.connect(
            lambda *_args: self.changelogRequested.emit()
        )
        actions_menu.addAction(self.changelog_action)
        self.cache_action = QAction("Reset cached data", self.more_actions_button)
        self.cache_action.setObjectName("clearCache")
        _set_action_help(
            self.cache_action,
            tooltip="Reset cached Warcraft Logs and RaiderIO data.",
            status_tip="Reset cached companion data.",
            whats_this="Clear cached Warcraft Logs, OAuth, and RaiderIO local data.",
        )
        self.cache_action.triggered.connect(self._clear_cache_dir)
        actions_menu.addAction(self.cache_action)
        actions_menu.addSeparator()
        self.quit_action = QAction("Quit ApplicantScout", self.more_actions_button)
        self.quit_action.setObjectName("quitApplicantScout")
        _set_action_help(
            self.quit_action,
            tooltip="Quit ApplicantScout.",
            status_tip="Quit ApplicantScout.",
            whats_this="Quit the companion instead of hiding settings to the tray.",
        )
        self.quit_action.triggered.connect(self._request_full_quit)
        actions_menu.addAction(self.quit_action)
        self.more_actions_button.setMenu(actions_menu)
        return self.more_actions_button

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if watched is self and event.type() in (
            QEvent.Type.Hide,
            QEvent.Type.WindowDeactivate,
        ):
            self.reveal_secret_button.setChecked(False)
        title_widgets = (
            getattr(self, "title_bar", None),
            getattr(self, "title_icon", None),
            getattr(self, "title_label", None),
        )
        if watched in title_widgets and isinstance(event, QMouseEvent):
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._title_drag_offset = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
                event.accept()
                return True
            if (
                event.type() == QEvent.Type.MouseMove
                and self._title_drag_offset is not None
                and event.buttons() & Qt.MouseButton.LeftButton
            ):
                self.move(event.globalPosition().toPoint() - self._title_drag_offset)
                event.accept()
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                was_dragging = self._title_drag_offset is not None
                self._title_drag_offset = None
                if was_dragging:
                    self._clamp_runtime_geometry()
        return super().eventFilter(watched, event)

    def _clamp_runtime_geometry(self) -> None:
        geometry = self.geometry()
        screens = tuple(QApplication.screens())
        clamped = clamp_geometry_to_screens(
            geometry.x(),
            geometry.y(),
            geometry.width(),
            geometry.height(),
            screens=screens,
            primary_screen=QApplication.primaryScreen(),
            preserve_grabbable_geometry=True,
            grabbable_height_px=self.title_bar.height(),
        )
        title_height = max(1, self.title_bar.height())
        best_title_overlap = 0
        best_bounds = None
        for screen in screens:
            bounds = screen.availableGeometry()
            overlap_x = max(
                0,
                min(geometry.right() + 1, bounds.right() + 1)
                - max(geometry.x(), bounds.x()),
            )
            overlap_y = max(
                0,
                min(geometry.y() + title_height, bounds.bottom() + 1)
                - max(geometry.y(), bounds.y()),
            )
            title_overlap = overlap_x * overlap_y
            if title_overlap > best_title_overlap:
                best_title_overlap = title_overlap
                best_bounds = bounds
        if best_bounds is not None and (
            geometry.width() > best_bounds.width()
            or geometry.height() > best_bounds.height()
        ):
            clamped = clamp_rect_to_bounds(
                geometry.x(),
                geometry.y(),
                geometry.width(),
                geometry.height(),
                best_bounds,
            )
        if clamped != (
            geometry.x(),
            geometry.y(),
            geometry.width(),
            geometry.height(),
        ):
            self.setGeometry(*clamped)

    def _on_screen_topology_changed(self, _screen: object | None = None) -> None:
        # Defer until Qt has published the replacement screens/primary screen.
        QTimer.singleShot(0, self._clamp_runtime_geometry)

    def showEvent(self, event):  # type: ignore[override]
        self._clamp_runtime_geometry()
        super().showEvent(event)

    def values(self) -> SettingsValues:
        return SettingsValues(
            wcl_client_id=self.client_id_edit.text().strip(),
            wcl_client_secret=self.client_secret_edit.text().strip(),
            region=self.region_combo.currentText().strip().upper() or "EU",
            screenshots_path=self.screenshots_edit.text().strip(),
            metric_preferences=MetricPreferences(
                mplus=self.mplus_check.isChecked(),
                raid_normal=self.raid_normal_check.isChecked(),
                raid_heroic=self.raid_heroic_check.isChecked(),
                raid_mythic=self.raid_mythic_check.isChecked(),
            ),
            sync_with_wow=self.sync_with_wow_check.isChecked(),
        )

    def set_update_available(self, latest_version: str | None) -> None:
        self._latest_update_version = latest_version
        if latest_version:
            _set_tooltip_and_accessibility(
                self.update_button,
                tooltip=f"Install ApplicantScout Companion {latest_version}.",
                accessible_name=UPDATE_ACCESSIBLE_NAME,
            )
            self.update_button.show()
            return
        self.update_button.hide()
        _set_tooltip_and_accessibility(
            self.update_button,
            tooltip=UPDATE_DEFAULT_TOOLTIP,
            accessible_name=UPDATE_ACCESSIBLE_NAME,
        )

    def set_update_in_progress(self, in_progress: bool) -> None:
        self._update_in_progress = in_progress
        if in_progress:
            self._autosave_timer.stop()
        self._refresh_settings_interaction_state()
        if in_progress:
            self.update_button.show()
            _set_tooltip_and_accessibility(
                self.update_button,
                tooltip=UPDATE_INSTALLING_TOOLTIP,
                accessible_name=UPDATE_ACCESSIBLE_NAME,
            )
        elif self.update_button.isHidden():
            _set_tooltip_and_accessibility(
                self.update_button,
                tooltip=UPDATE_DEFAULT_TOOLTIP,
                accessible_name=UPDATE_ACCESSIBLE_NAME,
            )
        else:
            self.set_update_available(self._latest_update_version)
            self._refresh_settings_interaction_state()

    def _settings_interactions_enabled(self) -> bool:
        return not self._update_in_progress and not self._cache_action_in_progress

    def _refresh_settings_interaction_state(self) -> None:
        enabled = self._settings_interactions_enabled()
        self.update_button.setEnabled(enabled)
        self._set_settings_controls_enabled(enabled)
        if (
            enabled
            and self._screenshots_validation_waiting_autosave
            and self._screenshots_validation_ready_generation
            == self._screenshots_validation_generation
        ):
            self._screenshots_validation_waiting_autosave = False
            self._autosave_timer.start()

    def _set_settings_controls_enabled(self, enabled: bool) -> None:
        if not enabled:
            self.reveal_secret_button.setChecked(False)
        for widget in (
            self.wcl_clients_link,
            self.wcl_example_button,
            self.client_id_edit,
            self.client_secret_edit,
            self.reveal_secret_button,
            self.region_combo,
            self.screenshots_edit,
            self.browse_button,
            self.mplus_check,
            self.raid_normal_check,
            self.raid_heroic_check,
            self.raid_mythic_check,
            self.sync_with_wow_check,
            self.support_button,
            self.test_button,
            self.more_actions_button,
            self.close_button,
        ):
            widget.setEnabled(enabled)
        for widget in (self.start_button, self.setup_quit_button):
            if widget is not None:
                widget.setEnabled(enabled)
        self.logs_action.setEnabled(enabled)
        self.changelog_action.setEnabled(enabled)
        self.cache_action.setEnabled(enabled and not self._cache_action_in_progress)

    def accept(self) -> None:  # type: ignore[override]
        if self._cache_action_in_progress:
            self._set_status(CACHE_RESET_BUSY_MESSAGE, error=True)
            return
        if self._update_in_progress:
            self._set_status(UPDATE_BUSY_CLOSE_MESSAGE, error=True)
            return
        if self._block_credential_test_in_progress():
            return
        values = self.values()
        error = self._hard_validation_error(
            values,
            require_screenshots_ready=True,
        )
        if error is not None:
            self._set_status(error, error=True)
            self._focus_invalid_field(values)
            return
        super().accept()

    def reject(self) -> None:  # type: ignore[override]
        if self._cache_action_in_progress:
            self._set_status(CACHE_RESET_BUSY_MESSAGE, error=True)
            return
        if self._update_in_progress:
            self._set_status(UPDATE_BUSY_CLOSE_MESSAGE, error=True)
            return
        if self._block_credential_test_in_progress():
            return
        super().reject()

    def done(self, result: int) -> None:  # type: ignore[override]
        self._cancel_screenshots_validation_process()
        super().done(result)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._cache_action_in_progress:
            event.ignore()
            self._set_status(CACHE_RESET_BUSY_MESSAGE, error=True)
            return
        if self._update_in_progress:
            event.ignore()
            self._set_status(UPDATE_BUSY_CLOSE_MESSAGE, error=True)
            return
        if self._first_run:
            if self._block_credential_test_in_progress():
                event.ignore()
                return
            super().closeEvent(event)
            return
        if not self._hide_to_tray_on_close:
            if not self.prepare_quit():
                event.ignore()
                return
            super().closeEvent(event)
            self.quitRequested.emit()
            return
        event.ignore()
        self._hide_to_tray()

    def _request_full_quit(self) -> None:
        if self._cache_action_in_progress:
            self._set_status(CACHE_RESET_BUSY_MESSAGE, error=True)
            return
        if self._update_in_progress:
            self._set_status(UPDATE_BUSY_CLOSE_MESSAGE, error=True)
            return
        if self._block_credential_test_in_progress():
            return
        if self._first_run:
            self.reject()
            return
        if not self.prepare_quit():
            return
        self.quitRequested.emit()

    def set_status(
        self,
        text: str,
        *,
        error: bool = False,
        warning: bool = False,
        busy: bool = False,
    ) -> None:
        self._set_status(text, error=error, warning=warning, busy=busy)

    def current_screenshots_warning(self) -> str | None:
        current_path = self.screenshots_edit.text().strip()
        if (
            current_path != self._screenshots_warning_path
            or self._screenshots_validation_ready_generation
            != self._screenshots_validation_generation
        ):
            return None
        return self._screenshots_warning_text

    def flush_pending_values(self) -> bool:
        if self._update_in_progress:
            self._autosave_timer.stop()
            return False
        if self._cache_action_in_progress:
            self._autosave_timer.stop()
            self._set_status(CACHE_RESET_BUSY_MESSAGE, error=True)
            return False
        if self._credential_test_in_progress:
            self._autosave_timer.stop()
            self._set_status(WCL_CREDENTIAL_TEST_BUSY_MESSAGE, error=True)
            return False
        if not self._autosave_timer.isActive():
            return self._last_values_apply_succeeded
        self._autosave_timer.stop()
        return self._emit_values_changed_if_valid()

    def prepare_quit(self) -> bool:
        status_before = self.status_label.text()
        if self.flush_pending_values():
            return True
        status_after = self.status_label.text()
        if not status_after or status_after == status_before:
            self._set_status(SETTINGS_QUIT_BLOCKED_MESSAGE, error=True)
        return False

    def report_values_apply_result(self, success: bool) -> None:
        self._last_values_apply_succeeded = success

    def _block_credential_test_in_progress(self) -> bool:
        if not self._credential_test_in_progress:
            return False
        self._set_status(WCL_CREDENTIAL_TEST_BUSY_MESSAGE, error=True)
        return True

    def _hard_validation_error(
        self,
        values: SettingsValues,
        *,
        require_screenshots_ready: bool = False,
    ) -> str | None:
        if not values.wcl_client_id or not values.wcl_client_secret:
            return "WCL Client ID and Secret are required."
        screenshots_path = values.screenshots_path
        if not values.metric_preferences.any_enabled:
            return "Select at least one WCL data type."
        if screenshots_path:
            ready, warning = self._current_screenshots_validation(
                screenshots_path,
                require_ready=require_screenshots_ready,
            )
            if not ready:
                return SCREENSHOTS_VALIDATION_PENDING_MESSAGE
            if warning is not None:
                return warning
        return None

    def _focus_invalid_field(self, values: SettingsValues) -> None:
        # Explicit submit may come from below the fold; autosave must never steal focus.
        if not values.wcl_client_id:
            field = self.client_id_edit
        elif not values.wcl_client_secret:
            field = self.client_secret_edit
        elif not values.metric_preferences.any_enabled:
            field = self.mplus_check
        else:
            field = self.screenshots_edit
        field.setFocus(Qt.FocusReason.OtherFocusReason)
        self.body_scroll.ensureWidgetVisible(field)

    def _set_client_secret_visible(self, visible: bool) -> None:
        self.client_secret_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        action = "Hide" if visible else "Show"
        self.reveal_secret_button.setText(action)
        _set_tooltip_and_accessibility(
            self.reveal_secret_button,
            tooltip=f"{action} the Client Secret to check the pasted value.",
            accessible_name=f"{action} Warcraft Logs Client Secret",
        )
        self.client_secret_edit.setAccessibleDescription(
            "Secret generated by the private Warcraft Logs API client; "
            + ("the value is visible." if visible else "the value is masked.")
        )

    def _current_screenshots_validation(
        self,
        raw_path: str,
        *,
        require_ready: bool,
    ) -> tuple[bool, str | None]:
        path = raw_path.strip()
        if (
            self._screenshots_validation_ready_generation
            == self._screenshots_validation_generation
            and self._screenshots_warning_path == path
        ):
            return True, self._screenshots_warning_text
        if (
            not require_ready
            and self._screenshots_validation_required_generation
            != self._screenshots_validation_generation
        ):
            return True, None
        self._start_screenshots_validation(path)
        return False, None

    def _connect_value_change_signals(self) -> None:
        for edit in (
            self.client_id_edit,
            self.client_secret_edit,
        ):
            edit.textChanged.connect(self._schedule_values_changed)
        self.region_combo.currentTextChanged.connect(self._emit_values_changed_if_valid)
        self.sync_with_wow_check.toggled.connect(self._emit_values_changed_if_valid)
        for checkbox in (
            self.raid_normal_check,
            self.raid_heroic_check,
            self.raid_mythic_check,
            self.mplus_check,
        ):
            checkbox.toggled.connect(self._handle_metric_checkbox_toggled)

    def _schedule_values_changed(self) -> None:
        self._autosave_timer.start()

    def _emit_values_changed_if_valid(self) -> bool:
        if self._update_in_progress:
            return False
        values = self.values()
        error = self._hard_validation_error(values)
        if error is not None:
            if error == SCREENSHOTS_VALIDATION_PENDING_MESSAGE:
                self._screenshots_validation_waiting_autosave = True
                self._set_status(error, warning=True)
            else:
                self._set_status(error, error=True)
            self._last_values_apply_succeeded = False
            return False
        self._last_values_apply_succeeded = True
        self.valuesChanged.emit(values)
        return self._last_values_apply_succeeded

    def _handle_metric_checkbox_toggled(self, checked: bool) -> None:
        if checked or self.values().metric_preferences.any_enabled:
            self._emit_values_changed_if_valid()
            return
        checkbox = self.sender()
        if isinstance(checkbox, QCheckBox):
            with QSignalBlocker(checkbox):
                checkbox.setChecked(True)
        self._set_status("Select at least one WCL data type.", error=True)

    def _handle_screenshots_text_changed(self, raw_path: str) -> None:
        self._schedule_screenshots_warning(raw_path, require_before_save=True)
        self._schedule_values_changed()

    def _schedule_screenshots_warning(
        self,
        raw_path: str,
        *,
        require_before_save: bool = False,
    ) -> None:
        self._pending_screenshots_warning_path = raw_path
        self._screenshots_validation_generation += 1
        self._screenshots_validation_started_generation = None
        self._screenshots_validation_ready_generation = None
        self._screenshots_validation_required_generation = (
            self._screenshots_validation_generation if require_before_save else None
        )
        path = raw_path.strip()
        if path:
            self._screenshots_warning_timer.start()
        else:
            self._screenshots_warning_timer.stop()
            self._screenshots_validation_waiting_autosave = False
            self._screenshots_warning_path = ""
            self._screenshots_warning_text = None
            self._screenshots_validation_ready_generation = (
                self._screenshots_validation_generation
            )
            if self.status_label.text().startswith("Screenshots folder warning:"):
                self._set_status("")

    def _flush_screenshots_warning(self) -> None:
        self._start_screenshots_validation(
            self._pending_screenshots_warning_path.strip()
        )

    def _hide_to_tray(self) -> None:
        self.hide()

    def _start_screenshots_validation(self, path: str) -> None:
        generation = self._screenshots_validation_generation
        if not path or self.screenshots_edit.text().strip() != path:
            return
        if self._screenshots_validation_started_generation == generation:
            return
        self._screenshots_warning_timer.stop()
        self._start_screenshots_validation_process(generation, path)

    def _start_screenshots_validation_process(
        self,
        generation: int,
        path: str,
    ) -> None:
        self._cancel_screenshots_validation_process()
        process = QProcess(self)
        self._screenshots_validation_process = process
        self._screenshots_validation_process_generation = generation
        self._screenshots_validation_process_path = path
        token = uuid.uuid4().hex
        result_path = _screenshots_path_probe_result_path(token)
        self._screenshots_validation_process_result_path = result_path
        self._screenshots_validation_started_generation = generation
        process.finished.connect(
            lambda exit_code, exit_status, active_process=process, output=result_path: (
                self._finish_screenshots_validation_process(
                    active_process,
                    output,
                    exit_code,
                    exit_status,
                )
            )
        )
        process.errorOccurred.connect(
            lambda error, active_process=process, output=result_path: (
                self._handle_screenshots_validation_process_error(
                    active_process,
                    output,
                    error,
                )
            )
        )
        program, arguments = _screenshots_path_probe_program_args(path, token)
        process.start(program, arguments)
        self._screenshots_validation_process_timeout.start()

    def _cancel_screenshots_validation_process(self) -> None:
        process = self._screenshots_validation_process
        if process is None:
            return
        self._screenshots_validation_process = None
        self._screenshots_validation_process_generation = None
        self._screenshots_validation_process_path = ""
        result_path = self._screenshots_validation_process_result_path
        self._screenshots_validation_process_result_path = None
        self._stop_screenshots_validation_timeout()
        try:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(1000)
            process.deleteLater()
        except RuntimeError:
            # The dialog's QObject teardown can delete child wrappers before a
            # queued QProcess callback reaches Python. State was cleared above.
            pass
        self._remove_screenshots_validation_result(result_path)

    def _stop_screenshots_validation_timeout(self) -> None:
        try:
            self._screenshots_validation_process_timeout.stop()
        except RuntimeError:
            # The timeout is a QObject child and may already be gone while the
            # process emits its final queued signal during dialog destruction.
            pass

    @staticmethod
    def _delete_screenshots_validation_process_later(process: QProcess) -> None:
        try:
            process.deleteLater()
        except RuntimeError:
            pass

    def _take_screenshots_validation_process(
        self,
        process: QProcess,
    ) -> tuple[int, str] | None:
        if process is not self._screenshots_validation_process:
            return None
        generation = self._screenshots_validation_process_generation
        path = self._screenshots_validation_process_path
        if generation is None:
            return None
        self._screenshots_validation_process = None
        self._screenshots_validation_process_generation = None
        self._screenshots_validation_process_path = ""
        self._screenshots_validation_process_result_path = None
        self._stop_screenshots_validation_timeout()
        self._delete_screenshots_validation_process_later(process)
        return generation, path

    @staticmethod
    def _remove_screenshots_validation_result(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _finish_screenshots_validation_process(
        self,
        process: QProcess,
        result_path: Path,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        request = self._take_screenshots_validation_process(process)
        if request is None:
            self._delete_screenshots_validation_process_later(process)
            self._remove_screenshots_validation_result(result_path)
            return
        generation, path = request
        warning = SCREENSHOTS_PATH_PROBE_FAILURE_WARNING
        if exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit:
            try:
                warning = _decode_screenshots_path_probe_output(
                    result_path.read_bytes()
                )
            except (OSError, UnicodeDecodeError, ValueError):
                warning = SCREENSHOTS_PATH_PROBE_FAILURE_WARNING
        self._remove_screenshots_validation_result(result_path)
        self._finish_screenshots_validation(
            _ScreenshotsValidationResult(
                generation,
                path,
                warning,
            )
        )

    def _handle_screenshots_validation_process_error(
        self,
        process: QProcess,
        result_path: Path,
        error: QProcess.ProcessError,
    ) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        request = self._take_screenshots_validation_process(process)
        if request is None:
            self._delete_screenshots_validation_process_later(process)
            self._remove_screenshots_validation_result(result_path)
            return
        generation, path = request
        self._remove_screenshots_validation_result(result_path)
        self._finish_screenshots_validation(
            _ScreenshotsValidationResult(
                generation,
                path,
                SCREENSHOTS_PATH_PROBE_FAILURE_WARNING,
            )
        )

    def _handle_screenshots_validation_timeout(self) -> None:
        process = self._screenshots_validation_process
        generation = self._screenshots_validation_process_generation
        path = self._screenshots_validation_process_path
        if process is None or generation is None:
            return
        self._cancel_screenshots_validation_process()
        self._finish_screenshots_validation(
            _ScreenshotsValidationResult(
                generation,
                path,
                SCREENSHOTS_PATH_PROBE_TIMEOUT_WARNING,
            )
        )

    def _finish_screenshots_validation(self, raw: object) -> None:
        if not isinstance(raw, _ScreenshotsValidationResult):
            return
        if (
            raw.generation != self._screenshots_validation_generation
            or raw.path != self.screenshots_edit.text().strip()
        ):
            self._start_screenshots_validation(
                self.screenshots_edit.text().strip()
            )
            return
        self._screenshots_warning_path = raw.path
        self._screenshots_warning_text = raw.warning
        self._screenshots_validation_ready_generation = raw.generation
        # Keep the result for the next save without replacing an active action's status.
        if not self._settings_interactions_enabled():
            return
        current = self.status_label.text()
        if raw.warning:
            self._set_status(raw.warning, error=True)
        elif current.startswith("Screenshots folder warning:") or (
            current == SCREENSHOTS_VALIDATION_PENDING_MESSAGE
        ):
            self._set_status("")
        if self._screenshots_validation_waiting_autosave:
            self._screenshots_validation_waiting_autosave = False
            self._autosave_timer.stop()
            self._emit_values_changed_if_valid()

    def _set_status(
        self,
        text: str,
        *,
        error: bool = False,
        warning: bool = False,
        busy: bool = False,
    ) -> None:
        self.status_label.setText(text)
        if error:
            colour = "#ff6666"
            state = "error"
        elif warning:
            colour = "#e5cc80"
            state = "warning"
        elif busy:
            colour = "#c4ddf1"
            state = "busy"
        else:
            colour = "#9edc8a"
            state = "success" if text else "idle"
        self.status_label.setProperty("statusState", state)
        self.status_label.setAccessibleDescription(text)
        self.status_label.setStyleSheet(f"color: {colour};")
        self.status_label.setVisible(bool(text))
        self.autosave_hint.setVisible(not bool(text))
        style = self.status_label.style()
        if style is not None:
            style.unpolish(self.status_label)
            style.polish(self.status_label)
        self.status_label.update()

    def _start_async_action(
        self,
        *,
        button: QAbstractButton | QAction,
        busy_text: str,
        error_prefix: str,
        action: Callable[[], ActionReturn],
        success_payload: object | None = None,
    ) -> None:
        button.setEnabled(False)
        self._set_status(busy_text, busy=True)

        def _worker() -> None:
            try:
                result = action()
                keep_disabled = False
                if isinstance(result, SettingsUpdateResult):
                    message = result.message
                    keep_disabled = result.installer_handoff
                    installer_launch = result.installer_launch
                else:
                    message = result
                    installer_launch = None
                outcome = _AsyncActionResult(
                    button,
                    message,
                    success_payload=success_payload,
                    keep_disabled=keep_disabled,
                    installer_launch=installer_launch,
                )
            except Exception as exc:  # noqa: BLE001
                outcome = _AsyncActionResult(
                    button,
                    f"{error_prefix}: {exc}",
                    error=True,
                    success_payload=success_payload,
                )
            self._signals.finished.emit(outcome)

        try:
            worker = threading.Thread(
                target=_worker,
                name="SettingsAction",
                daemon=True,
            )
            worker.start()
        except Exception as exc:  # noqa: BLE001 - restore the GUI action inline
            self._finish_async_action(
                _AsyncActionResult(
                    button,
                    f"{error_prefix}: {exc}",
                    error=True,
                    success_payload=success_payload,
                )
            )

    def _finish_async_action(self, raw: object) -> None:
        if not isinstance(raw, _AsyncActionResult):
            return
        if raw.button is self.test_button:
            self._credential_test_in_progress = False
        if raw.button is self.cache_action:
            self._cache_action_in_progress = False
            self._refresh_settings_interaction_state()
            if self._update_in_progress:
                return
            self._set_status(raw.message, error=raw.error)
            return
        if self._update_in_progress and raw.button is not self.update_button:
            return
        if isinstance(raw.success_payload, SettingsValues):
            current = self.values()
            if (
                current.wcl_client_id != raw.success_payload.wcl_client_id
                or current.wcl_client_secret != raw.success_payload.wcl_client_secret
                or current.region != raw.success_payload.region
            ):
                raw.button.setEnabled(True)
                self._set_status("Credentials changed during test; test WCL again.", error=True)
                return
        self._set_status(raw.message, error=raw.error)
        if raw.button is self.update_button:
            if raw.error:
                raw.button.setEnabled(True)
                self.updateFinished.emit(True)
            elif raw.keep_disabled:
                raw.button.setEnabled(False)
                self.update_button.show()
                _set_tooltip_and_accessibility(
                    self.update_button,
                    tooltip=UPDATE_INSTALLING_TOOLTIP,
                    accessible_name=UPDATE_ACCESSIBLE_NAME,
                )
                self.updateHandoffStarted.emit(raw.message, raw.installer_launch)
            else:
                raw.button.setEnabled(True)
                self.updateFinished.emit(False)
                self.set_update_available(None)
                self.updateCompleted.emit()
            return
        raw.button.setEnabled(True)
        if not raw.error and isinstance(raw.success_payload, SettingsValues):
            current = self.values()
            error = self._hard_validation_error(current)
            if error is not None:
                self._set_status(error, error=True)
                return
            self._autosave_timer.stop()
            self.credentialsValidated.emit(current)

    def _show_wcl_setup_example(self) -> None:
        self._build_wcl_setup_example_dialog().exec()

    def _build_wcl_setup_example_dialog(self) -> QDialog:
        popup = QDialog(self)
        popup.setObjectName("wclSetupExampleDialog")
        popup.setStyleSheet(_SETTINGS_STYLESHEET)
        popup.setWindowTitle("Warcraft Logs API client example")
        popup.setModal(True)
        popup.setMinimumWidth(720)

        layout = QVBoxLayout(popup)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        text = QLabel(
            "On the Warcraft Logs Create Client page, use Redirect URL "
            f"{WCL_CREATE_CLIENT_REDIRECT_URL} and leave Public Client unchecked. Then click "
            "Create and copy the generated Client ID and Client Secret back here."
        )
        text.setWordWrap(True)
        layout.addWidget(text)

        copy_status = QLabel("")
        copy_status.setObjectName("wclExampleCopyStatus")

        values_form = QFormLayout()
        values_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        values_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        layout.addLayout(values_form)

        app_name_edit = QLineEdit(WCL_CREATE_CLIENT_APP_NAME)
        app_name_edit.setObjectName("wclExampleApplicationName")
        app_name_edit.setReadOnly(True)
        app_name_edit.setToolTip("Copy this into the Warcraft Logs application name field.")
        app_name_edit.setAccessibleName("WCL application name")
        app_name_edit.setAccessibleDescription(
            "Application name to enter on the Warcraft Logs Create Client form."
        )
        values_form.addRow(
            "Application name",
            self._copyable_value_row(
                app_name_edit,
                WCL_CREATE_CLIENT_APP_NAME,
                "Application name",
                "copyWclExampleApplicationName",
                copy_status,
            ),
        )

        redirect_url_edit = QLineEdit(WCL_CREATE_CLIENT_REDIRECT_URL)
        redirect_url_edit.setObjectName("wclExampleRedirectUrl")
        redirect_url_edit.setReadOnly(True)
        redirect_url_edit.setToolTip("Copy this into the Warcraft Logs redirect URL field.")
        redirect_url_edit.setAccessibleName("WCL redirect URL")
        redirect_url_edit.setAccessibleDescription(
            "Redirect URL to enter on the Warcraft Logs Create Client form."
        )
        values_form.addRow(
            "Redirect URL",
            self._copyable_value_row(
                redirect_url_edit,
                WCL_CREATE_CLIENT_REDIRECT_URL,
                "Redirect URL",
                "copyWclExampleRedirectUrl",
                copy_status,
            ),
        )

        public_client = QCheckBox("Public Client unchecked")
        public_client.setObjectName("wclExamplePublicClientUnchecked")
        public_client.setChecked(False)
        public_client.setEnabled(False)
        public_client.setToolTip("Leave Public Client unchecked on Warcraft Logs.")
        public_client.setAccessibleName("WCL Public Client checkbox")
        public_client.setAccessibleDescription(
            "Leave Public Client unchecked on the Warcraft Logs Create Client form."
        )
        values_form.addRow("Public Client", public_client)
        layout.addWidget(copy_status)

        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setObjectName("wclSetupExampleImage")
        pixmap = QPixmap(str(WCL_CREATE_CLIENT_EXAMPLE_PATH))
        if pixmap.isNull():
            image.setText(
                "Example screenshot is unavailable. Use Redirect URL "
                f"{WCL_CREATE_CLIENT_REDIRECT_URL} and leave Public Client unchecked."
            )
            image.setWordWrap(True)
        else:
            image.setPixmap(
                pixmap.scaledToWidth(
                    900,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(image)
        layout.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(popup.reject)
        layout.addWidget(buttons)
        return popup

    def _copyable_value_row(
        self,
        value_edit: QLineEdit,
        value: str,
        label: str,
        button_name: str,
        status: QLabel,
    ) -> QWidget:
        row = QWidget(self)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(value_edit, stretch=1)
        copy_button = QPushButton("Copy")
        copy_button.setObjectName(button_name)
        accessible_label = "redirect URL" if label == "Redirect URL" else label.lower()
        _set_tooltip_and_accessibility(
            copy_button,
            tooltip=f"Copy {label}.",
            accessible_name=f"Copy WCL {accessible_label}",
        )
        copy_button.clicked.connect(
            lambda: self._copy_wcl_example_value(value, label, status)
        )
        row_layout.addWidget(copy_button)
        return row

    def _copy_wcl_example_value(self, value: str, label: str, status: QLabel) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            status.setText("Clipboard is unavailable.")
            return
        clipboard.setText(value)
        status.setText(f"Copied {label}.")

    def _browse_screenshots(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select WoW Screenshots folder",
            self.screenshots_edit.text().strip(),
        )
        if selected:
            if selected == self.screenshots_edit.text().strip():
                self._schedule_screenshots_warning(
                    selected,
                    require_before_save=True,
                )
                self._schedule_values_changed()
            else:
                self.screenshots_edit.setText(selected)

    def _test_credentials(self) -> None:
        if self._credential_tester is None:
            self._set_status("Credential test is unavailable.", error=True)
            return
        credential_tester = self._credential_tester
        values = self.values()
        if not values.wcl_client_id or not values.wcl_client_secret:
            self._set_status("Enter WCL Client ID and Secret first.", error=True)
            self._focus_invalid_field(values)
            return
        self._credential_test_in_progress = True
        self._start_async_action(
            button=self.test_button,
            busy_text="Testing WCL credentials...",
            error_prefix="WCL test failed",
            action=lambda: credential_tester(
                values.wcl_client_id,
                values.wcl_client_secret,
                values.region,
            ),
            success_payload=values,
        )

    def _open_log_folder(self) -> None:
        if self._open_logs is None:
            self._set_status("Log folder is unavailable.", error=True)
            return
        try:
            self._set_status(self._open_logs())
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Could not open logs: {exc}", error=True)

    def _clear_cache_dir(self) -> None:
        if self._clear_cache is None:
            self._set_status("Cache action is unavailable.", error=True)
            return
        if self._cache_action_in_progress:
            return
        if self._update_in_progress:
            self._set_status(
                "Update is installing. Wait for it to finish before resetting cache.",
                error=True,
            )
            return
        if not self.test_button.isEnabled():
            self._set_status(CACHE_RESET_ACTION_BLOCKED_MESSAGE, error=True)
            return
        if not self.flush_pending_values():
            return
        self._cache_action_in_progress = True
        self._refresh_settings_interaction_state()
        self._start_async_action(
            button=self.cache_action,
            busy_text="Resetting cached data...",
            error_prefix="Could not clear cache",
            action=self._clear_cache,
        )

    def _check_for_updates(self) -> None:
        if self._check_updates is None:
            self._set_status("Update check is unavailable.", error=True)
            return
        if not self.update_button.isEnabled():
            return
        if not self.flush_pending_values():
            return
        check_updates = self._check_updates
        self.updateStarted.emit()
        self._start_async_action(
            button=self.update_button,
            busy_text="Installing update...",
            error_prefix="Update failed",
            action=check_updates,
        )

    def _open_support(self) -> None:
        if not QDesktopServices.openUrl(QUrl(SUPPORT_URL)):
            self._set_status("Could not open support link.", error=True)

    def _open_wcl_clients(self) -> None:
        if not QDesktopServices.openUrl(QUrl(WCL_CLIENTS_URL)):
            self._set_status("Could not open Warcraft Logs API clients.", error=True)


def open_folder(path: Path) -> bool:
    path.mkdir(parents=True, exist_ok=True)
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
