"""Qt settings and first-run dialog."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import threading

from PyQt6.QtCore import QEvent, QPoint, QObject, QSignalBlocker, Qt, QTimer, QUrl, pyqtSignal
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
    Config,
    ConfigError,
    resolve_screenshots_path,
    screenshots_path_health_warning,
    screenshots_path_validation_error,
)
from .metric_preferences import MetricPreferences


CredentialTester = Callable[[str, str, str], str]
SimpleAction = Callable[[], str]
UpdateAction = Callable[[], tuple[str, str | None]]
COMMON_WOW_RETAIL_ROOTS = (
    Path(r"C:\Games\World of Warcraft\_retail_"),
    Path(r"C:\Program Files (x86)\World of Warcraft\_retail_"),
    Path.home() / "World of Warcraft" / "_retail_",
)
WCL_CREATE_CLIENT_EXAMPLE_PATH = (
    Path(__file__).with_name("assets") / "wcl_create_client_example.jpg"
)
WCL_CREATE_CLIENT_APP_NAME = "ApplicantScout"
WCL_CREATE_CLIENT_REDIRECT_URL = "http://localhost"
SUPPORT_URL = "https://ko-fi.com/antrakt92"
APP_ICON_PATH = Path(__file__).with_name("assets") / "app_icon.ico"


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


@dataclass(frozen=True)
class SettingsValues:
    wcl_client_id: str
    wcl_client_secret: str
    region: str
    screenshots_path: str
    metric_preferences: MetricPreferences
    sync_with_wow: bool


@dataclass(frozen=True)
class _AsyncActionResult:
    button: QAbstractButton | QAction
    message: str
    error: bool = False
    open_url: str | None = None
    success_payload: object | None = None


class _AsyncSignals(QObject):
    finished = pyqtSignal(object)


class ReleaseNotesDialog(QDialog):
    def __init__(self, release_notes: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
        return str(resolve_screenshots_path(cfg))
    except ConfigError:
        pass
    for retail_root in COMMON_WOW_RETAIL_ROOTS:
        candidate = retail_root / "Screenshots"
        if screenshots_path_health_warning(candidate) is None:
            return str(candidate)
    return ""


class SettingsDialog(QDialog):
    valuesChanged = pyqtSignal(object)
    credentialsValidated = pyqtSignal(object)
    hideRequested = pyqtSignal()
    quitRequested = pyqtSignal()
    updateStarted = pyqtSignal()
    updateFinished = pyqtSignal(bool)
    updateCompleted = pyqtSignal()
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
        self._first_run = first_run
        self._hide_to_tray_on_close = hide_to_tray_on_close
        self._update_in_progress = False
        self._credential_tester = credential_tester
        self._open_logs = open_logs
        self._clear_cache = clear_cache
        self._check_updates = check_updates
        self._signals = _AsyncSignals(self)
        self._signals.finished.connect(self._finish_async_action)
        self._title_drag_offset: QPoint | None = None
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(700)
        self._autosave_timer.timeout.connect(self._emit_values_changed_if_valid)

        window_title = _settings_window_title(first_run=first_run)
        self.setWindowTitle(window_title)
        self.setModal(first_run)
        self.setMinimumWidth(520)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        outer.addWidget(self._build_title_bar(window_title))

        body = QWidget(self)
        body.setObjectName("settingsBody")
        root = QVBoxLayout(body)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        if first_run:
            intro = QLabel(
                "Enter your Warcraft Logs API credentials and the WoW Screenshots folder."
            )
            intro.setWordWrap(True)
            root.addWidget(intro)

        wcl_link_row = QWidget(self)
        wcl_link_layout = QHBoxLayout(wcl_link_row)
        wcl_link_layout.setContentsMargins(0, 0, 0, 0)
        wcl_link_layout.setSpacing(8)
        self.wcl_clients_link = QLabel(
            '<a href="https://www.warcraftlogs.com/api/clients/">Warcraft Logs API clients</a>'
        )
        self.wcl_clients_link.setObjectName("wclClientsLink")
        self.wcl_clients_link.setOpenExternalLinks(True)
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
        self.wcl_example_button.setToolTip(
            "Show the Warcraft Logs Create Client form values to copy."
        )
        self.wcl_example_button.clicked.connect(self._show_wcl_setup_example)
        wcl_link_layout.addWidget(self.wcl_example_button)
        wcl_link_layout.addStretch(1)
        root.addWidget(wcl_link_row)
        credentials_help = QLabel(
            "Create a Warcraft Logs API client with Redirect URL "
            "http://localhost and leave Public Client unchecked. Copy the "
            "generated Client ID and Client Secret into the fields below."
        )
        credentials_help.setWordWrap(True)
        root.addWidget(credentials_help)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        root.addLayout(form)

        display_client_id = getattr(cfg, "draft_wcl_client_id", "") or cfg.wcl_client_id
        display_client_secret = (
            getattr(cfg, "draft_wcl_client_secret", "") or cfg.wcl_client_secret
        )

        self.client_id_edit = QLineEdit(display_client_id)
        self.client_id_edit.setObjectName("wclClientId")
        form.addRow("WCL Client ID", self.client_id_edit)

        self.client_secret_edit = QLineEdit(display_client_secret)
        self.client_secret_edit.setObjectName("wclClientSecret")
        self.client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("WCL Client Secret", self.client_secret_edit)

        self.region_combo = QComboBox()
        self.region_combo.setObjectName("region")
        self.region_combo.addItems(["EU", "US", "KR", "TW", "CN"])
        region_idx = self.region_combo.findText((cfg.region or "EU").upper())
        self.region_combo.setCurrentIndex(max(0, region_idx))
        form.addRow("Region fallback", self.region_combo)

        path_row = QWidget(self)
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
        self.screenshots_edit.textChanged.connect(self._handle_screenshots_text_changed)
        path_layout.addWidget(self.screenshots_edit, stretch=1)
        browse_button = QPushButton("Browse")
        browse_button.setObjectName("browseScreenshots")
        browse_button.setToolTip("Browse to WoW's in-game Screenshots folder.")
        browse_button.clicked.connect(self._browse_screenshots)
        path_layout.addWidget(browse_button)
        form.addRow("WoW Screenshots folder", path_row)

        metrics_row = QWidget(self)
        metrics_layout = QHBoxLayout(metrics_row)
        metrics_layout.setContentsMargins(0, 0, 0, 0)
        metrics_layout.setSpacing(8)
        prefs = cfg.metric_preferences
        self.mplus_check = QCheckBox("M+")
        self.mplus_check.setObjectName("fetchMplus")
        self.mplus_check.setToolTip("Fetch and show Mythic+ dungeon parses.")
        self.mplus_check.setChecked(prefs.mplus)
        self.raid_normal_check = QCheckBox("Raid N")
        self.raid_normal_check.setObjectName("fetchRaidNormal")
        self.raid_normal_check.setToolTip("Fetch and show Normal raid parses.")
        self.raid_normal_check.setChecked(prefs.raid_normal)
        self.raid_heroic_check = QCheckBox("Raid H")
        self.raid_heroic_check.setObjectName("fetchRaidHeroic")
        self.raid_heroic_check.setToolTip("Fetch and show Heroic raid parses.")
        self.raid_heroic_check.setChecked(prefs.raid_heroic)
        self.raid_mythic_check = QCheckBox("Raid M")
        self.raid_mythic_check.setObjectName("fetchRaidMythic")
        self.raid_mythic_check.setToolTip("Fetch and show Mythic raid parses.")
        self.raid_mythic_check.setChecked(prefs.raid_mythic)
        for checkbox in (
            self.raid_normal_check,
            self.raid_heroic_check,
            self.raid_mythic_check,
            self.mplus_check,
        ):
            metrics_layout.addWidget(checkbox)
        metrics_layout.addStretch(1)
        form.addRow("WCL data", metrics_row)

        self.sync_with_wow_check = QCheckBox("Start and stop with WoW")
        self.sync_with_wow_check.setObjectName("syncWithWow")
        self.sync_with_wow_check.setToolTip(
            "Start ApplicantScout when WoW starts and quit it when WoW closes."
        )
        self.sync_with_wow_check.setChecked(cfg.sync_with_wow)
        form.addRow("", self.sync_with_wow_check)

        root.addStretch(1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("settingsStatus")
        self.status_label.setWordWrap(True)
        footer = QWidget(self)
        footer.setObjectName("settingsFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(8)
        self.support_button = QToolButton(footer)
        self.support_button.setObjectName("supportApplicantScout")
        self.support_button.setText("♡")
        self.support_button.setToolTip("Support ApplicantScout on Ko-fi.")
        self.support_button.setFixedSize(26, 24)
        self.support_button.setStyleSheet(
            "QToolButton {"
            "background: transparent;"
            "color: #ff6b7a;"
            "border: 1px solid transparent;"
            "border-radius: 4px;"
            "font-size: 17px;"
            "font-weight: 600;"
            "padding-bottom: 1px;"
            "}"
            "QToolButton:hover {"
            "background: #24131a;"
            "color: #ff8a95;"
            "border-color: #7a3340;"
            "}"
            "QToolButton:pressed {"
            "background: #1b0f14;"
            "}"
        )
        self.support_button.clicked.connect(self._open_support)
        footer_layout.addWidget(self.support_button)
        footer_layout.addWidget(self.status_label, stretch=1)
        self.test_button = QPushButton("Test WCL", footer)
        self.test_button.setObjectName("testWcl")
        self.test_button.setToolTip("Validate the current Warcraft Logs credentials.")
        self.test_button.clicked.connect(self._test_credentials)
        footer_layout.addWidget(self.test_button)
        footer_layout.addWidget(self._build_more_actions_button(footer))
        root.addWidget(footer)

        if first_run:
            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            buttons.addStretch(1)
            start_button = QPushButton("Start companion")
            start_button.setObjectName("startCompanion")
            start_button.clicked.connect(self.accept)
            buttons.addWidget(start_button)
            quit_button = QPushButton("Quit setup")
            quit_button.setObjectName("quitApplicantScout")
            quit_button.clicked.connect(self.reject)
            buttons.addWidget(quit_button)
            root.addLayout(buttons)
        outer.addWidget(body)
        self._connect_value_change_signals()
        self._update_screenshots_warning(self.screenshots_edit.text())

    def _build_title_bar(self, title: str) -> QWidget:
        title_bar = QWidget(self)
        self.title_bar = title_bar
        title_bar.setObjectName("settingsTitleBar")
        title_bar.setStyleSheet(
            "#settingsTitleBar {"
            "background: #242424;"
            "border-bottom: 1px solid #343434;"
            "}"
            "#settingsTitleBar QLabel#settingsTitle {"
            "color: #f0f0f0;"
            "font-weight: 500;"
            "}"
        )
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
        self.update_button.setToolTip("Install available ApplicantScout update.")
        self.update_button.setFixedSize(30, 26)
        self.update_button.setStyleSheet(
            "QToolButton {"
            "background: transparent;"
            "color: #4da3ff;"
            "border: 1px solid transparent;"
            "border-radius: 4px;"
            "padding: 3px;"
            "}"
            "QToolButton:hover {"
            "background: #10203a;"
            "color: #74baff;"
            "border-color: #2f5f9e;"
            "}"
            "QToolButton:pressed {"
            "background: #0b172b;"
            "}"
            "QToolButton:disabled {"
            "background: transparent;"
            "color: #315f91;"
            "border-color: transparent;"
            "}"
        )
        self.update_button.hide()
        self.update_button.clicked.connect(self._check_for_updates)
        title_layout.addWidget(self.update_button)

        self.close_button = QToolButton(title_bar)
        self.close_button.setObjectName("settingsClose")
        self.close_button.setText("×")
        self.close_button.setToolTip(
            "Close ApplicantScout setup."
            if self._first_run
            else (
                "Hide ApplicantScout settings to tray."
                if self._hide_to_tray_on_close
                else "Quit ApplicantScout."
            )
        )
        self.close_button.setFixedSize(30, 26)
        self.close_button.setStyleSheet(
            "QToolButton {"
            "background: transparent;"
            "color: #b8b8b8;"
            "border: 1px solid transparent;"
            "border-radius: 4px;"
            "font-size: 18px;"
            "padding-bottom: 2px;"
            "}"
            "QToolButton:hover {"
            "background: #3a2424;"
            "color: #ffffff;"
            "border-color: #704040;"
            "}"
            "QToolButton:pressed {"
            "background: #2b1717;"
            "}"
        )
        self.close_button.clicked.connect(self.close)
        title_layout.addWidget(self.close_button)

        for widget in (title_bar, self.title_icon, self.title_label):
            widget.installEventFilter(self)
        return title_bar

    def _build_more_actions_button(self, parent: QWidget) -> QToolButton:
        self.more_actions_button = QToolButton(parent)
        self.more_actions_button.setObjectName("settingsMoreActions")
        self.more_actions_button.setText("More")
        self.more_actions_button.setToolTip(
            "Open logs, view the changelog, reset cached data, or quit ApplicantScout."
        )
        self.more_actions_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        actions_menu = QMenu(self.more_actions_button)
        self.logs_action = QAction("Open logs", self.more_actions_button)
        self.logs_action.setObjectName("openLogs")
        self.logs_action.triggered.connect(self._open_log_folder)
        actions_menu.addAction(self.logs_action)
        self.changelog_action = QAction("View changelog", self.more_actions_button)
        self.changelog_action.setObjectName("viewChangelog")
        self.changelog_action.triggered.connect(
            lambda *_args: self.changelogRequested.emit()
        )
        actions_menu.addAction(self.changelog_action)
        self.cache_action = QAction("Reset cached data", self.more_actions_button)
        self.cache_action.setObjectName("clearCache")
        self.cache_action.triggered.connect(self._clear_cache_dir)
        actions_menu.addAction(self.cache_action)
        actions_menu.addSeparator()
        self.quit_action = QAction("Quit ApplicantScout", self.more_actions_button)
        self.quit_action.setObjectName("quitApplicantScout")
        self.quit_action.triggered.connect(self._request_full_quit)
        actions_menu.addAction(self.quit_action)
        self.more_actions_button.setMenu(actions_menu)
        return self.more_actions_button

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
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
                self._title_drag_offset = None
        return super().eventFilter(watched, event)

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
        if latest_version:
            self.update_button.setToolTip(
                f"Install ApplicantScout Companion {latest_version}."
            )
            self.update_button.show()
            return
        self.update_button.hide()
        self.update_button.setToolTip("Install available ApplicantScout update.")

    def set_update_in_progress(self, in_progress: bool) -> None:
        self._update_in_progress = in_progress
        self.update_button.setEnabled(not in_progress)
        if in_progress:
            self.update_button.show()
            self.update_button.setToolTip("Installing ApplicantScout update...")
        elif self.update_button.isHidden():
            self.update_button.setToolTip("Install available ApplicantScout update.")

    def accept(self) -> None:  # type: ignore[override]
        values = self.values()
        error = self._hard_validation_error(values)
        if error is not None:
            self._set_status(error, error=True)
            return
        super().accept()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._first_run:
            super().closeEvent(event)
            return
        if self._update_in_progress:
            event.ignore()
            self._set_status("Update is installing. Wait for it to finish before closing.", error=True)
            return
        if not self._hide_to_tray_on_close:
            super().closeEvent(event)
            self.quitRequested.emit()
            return
        event.ignore()
        self._hide_to_tray()

    def _request_full_quit(self) -> None:
        if self._update_in_progress:
            self._set_status(
                "Update is installing. Wait for it to finish before closing.",
                error=True,
            )
            return
        if self._first_run:
            self.reject()
            return
        self.quitRequested.emit()

    def set_status(self, text: str, *, error: bool = False) -> None:
        self._set_status(text, error=error)

    def flush_pending_values(self) -> bool:
        if not self._autosave_timer.isActive():
            return True
        self._autosave_timer.stop()
        return self._emit_values_changed_if_valid()

    def _hard_validation_error(self, values: SettingsValues) -> str | None:
        if not values.wcl_client_id or not values.wcl_client_secret:
            return "WCL Client ID and Secret are required."
        screenshots_path = values.screenshots_path
        if screenshots_path and Path(screenshots_path).is_file():
            return "Screenshots path points to a file, not a folder."
        if not values.metric_preferences.any_enabled:
            return "Select at least one WCL data type."
        if screenshots_path:
            warning = screenshots_path_validation_error(Path(screenshots_path))
            if warning is not None:
                return warning
        return None

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
        values = self.values()
        error = self._hard_validation_error(values)
        if error is not None:
            self._set_status(error, error=True)
            return False
        self.valuesChanged.emit(values)
        return True

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
        self._update_screenshots_warning(raw_path)
        self._schedule_values_changed()

    def _hide_to_tray(self) -> None:
        self.hide()
        self.hideRequested.emit()

    def _update_screenshots_warning(self, raw_path: str) -> None:
        path = raw_path.strip()
        if not path:
            if self.status_label.text().startswith("Screenshots folder warning:"):
                self._set_status("")
            return
        warning = screenshots_path_health_warning(Path(path))
        current = self.status_label.text()
        if warning:
            self._set_status(warning, error=True)
        elif current.startswith("Screenshots folder warning:"):
            self._set_status("")

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #ff6666;" if error else "color: #9edc8a;")

    def _start_async_action(
        self,
        *,
        button: QAbstractButton | QAction,
        busy_text: str,
        error_prefix: str,
        action: Callable[[], str | tuple[str, str | None]],
        success_payload: object | None = None,
    ) -> None:
        button.setEnabled(False)
        self._set_status(busy_text)

        def _worker() -> None:
            try:
                result = action()
                if isinstance(result, tuple):
                    message, open_url = result
                else:
                    message, open_url = result, None
                outcome = _AsyncActionResult(
                    button,
                    message,
                    open_url=open_url,
                    success_payload=success_payload,
                )
            except Exception as exc:  # noqa: BLE001
                outcome = _AsyncActionResult(
                    button,
                    f"{error_prefix}: {exc}",
                    error=True,
                )
            self._signals.finished.emit(outcome)

        threading.Thread(target=_worker, name="SettingsAction", daemon=True).start()

    def _finish_async_action(self, raw: object) -> None:
        if not isinstance(raw, _AsyncActionResult):
            return
        raw.button.setEnabled(True)
        self._set_status(raw.message, error=raw.error)
        if raw.button is self.update_button:
            self.updateFinished.emit(raw.error)
            if not raw.error:
                self.set_update_available(None)
                self.updateCompleted.emit()
        if not raw.error and raw.open_url:
            QDesktopServices.openUrl(QUrl(raw.open_url))
        if not raw.error and isinstance(raw.success_payload, SettingsValues):
            current = self.values()
            if (
                current.wcl_client_id != raw.success_payload.wcl_client_id
                or current.wcl_client_secret != raw.success_payload.wcl_client_secret
            ):
                self._set_status("Credentials changed during test; test WCL again.", error=True)
                return
            error = self._hard_validation_error(current)
            if error is not None:
                self._set_status(error, error=True)
                return
            self.credentialsValidated.emit(current)

    def _show_wcl_setup_example(self) -> None:
        self._build_wcl_setup_example_dialog().exec()

    def _build_wcl_setup_example_dialog(self) -> QDialog:
        popup = QDialog(self)
        popup.setWindowTitle("Warcraft Logs API client example")
        popup.setModal(True)
        popup.setMinimumWidth(720)

        layout = QVBoxLayout(popup)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        text = QLabel(
            "On the Warcraft Logs Create Client page, use Redirect URL "
            "http://localhost and leave Public Client unchecked. Then click "
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
        values_form.addRow("Public Client", public_client)
        layout.addWidget(copy_status)

        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setObjectName("wclSetupExampleImage")
        pixmap = QPixmap(str(WCL_CREATE_CLIENT_EXAMPLE_PATH))
        if pixmap.isNull():
            image.setText(
                "Example screenshot is unavailable. Use Redirect URL "
                "http://localhost and leave Public Client unchecked."
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
        copy_button.setToolTip(f"Copy {label}.")
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
            self.screenshots_edit.setText(selected)

    def _test_credentials(self) -> None:
        if self._credential_tester is None:
            self._set_status("Credential test is unavailable.", error=True)
            return
        credential_tester = self._credential_tester
        values = self.values()
        if not values.wcl_client_id or not values.wcl_client_secret:
            self._set_status("Enter WCL Client ID and Secret first.", error=True)
            return
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
        try:
            self._set_status(self._clear_cache())
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Could not clear cache: {exc}", error=True)

    def _check_for_updates(self) -> None:
        if self._check_updates is None:
            self._set_status("Update check is unavailable.", error=True)
            return
        if not self.update_button.isEnabled():
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


def open_folder(path: Path) -> bool:
    path.mkdir(parents=True, exist_ok=True)
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
