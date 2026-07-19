"""Entry point: wires config → screenshot watcher → state machine → WCL fetcher → overlay."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import ctypes
from dataclasses import dataclass, replace
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import subprocess
import sys
import threading
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, Protocol

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import __version__
from .atomic_io import apply_private_directory_mode, apply_private_file_mode
from .config import (
    Config,
    ConfigError,
    _parse_cache_ttl_seconds,
    is_config_ready,
    load_config,
    normalize_wcl_region,
    read_user_config_values,
    resolve_screenshots_path,
    save_config_values,
    screenshots_path_health_warning,
    user_log_dir,
    validate_metric_preferences,
)
from .constants import CLASS_ID_TO_NAME, REGION_ID_TO_WCL, ROLE_BYTE_TO_NAME
from .live_snapshot_cache import (
    LIVE_SNAPSHOT_RESTORE_GRACE_SECONDS,
    LiveSnapshotCacheWriter,
    clear_live_snapshot_if_saved_at,
    load_live_snapshot,
)
from .metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences
from .overlay import OverlayWindow
from .raiderio_local import (
    RaiderIOLocalReader,
    clear_lookup_payload_cache,
    retail_root_from_screenshots_path,
)
from .screenshot import (
    DecodedRosterMember,
    ScreenshotWatcher,
    Snapshot,
    clear_screenshot_manual_indexes,
    cleanup_appscout_screenshots,
    format_screenshot_cleanup_summary,
    is_placeholder_transport_identity,
    screenshot_cleanup_exit_code,
)
from .settings_dialog import (
    ReleaseNotesDialog,
    SCREENSHOTS_PATH_PROBE_ARG,
    SETTINGS_QUIT_BLOCKED_MESSAGE,
    SettingsDialog,
    SettingsUpdateResult,
    open_folder,
    run_screenshots_path_probe_command,
)
from .state import Applicant, AppState, LeaderKey, Listing, RosterMember, WoWPlayer
from .updater import (
    check_for_update,
    download_update_installer,
    launch_update_installer,
    UpdateResult,
)
from .wcl import (
    CharacterCache,
    WCLAuth,
    WCLClient,
    applicant_has_explicit_realm,
    default_realm_from_player,
    derive_server_slug,
    split_name_realm,
    wcl_metric_role,
)
from .wow_lifecycle import (
    WATCH_WOW_ARG,
    configure_wow_sync_startup,
    is_wow_foreground,
    is_wow_running,
    start_wow_sync_watcher,
    stop_current_session_watcher,
)


log = logging.getLogger("applicant_scout")
_RIO_LOOKUP_FAILED = object()
RIO_PRELOAD_REFRESH_INTERVAL_SECONDS = 30.0
APP_ICON_PATH = Path(__file__).with_name("assets") / "app_icon.ico"
APP_USER_MODEL_ID = "Antrakt.ApplicantScout.Companion"
CONTROL_SERVER_NAME = "Antrakt.ApplicantScout.Companion.Control"
CONTROL_SHUTDOWN_ARG = "--shutdown-running-instance"
SHOW_SETTINGS_ARG = "--show-settings"
CONTROL_QUIT_COMMAND = b"quit"
CONTROL_SHOW_SETTINGS_COMMAND = b"show-settings"
UPDATE_QUIT_BLOCKED_MESSAGE = "Update is installing. Wait for it to finish before quitting."
WOW_EXIT_POLL_MS = 5000
WOW_EXIT_MISSES_BEFORE_QUIT = 3
UPDATE_CHECK_INITIAL_MS = 1_000
UPDATE_CHECK_INTERVAL_MS = 60 * 60 * 1000
UPDATE_HANDOFF_POLL_INTERVAL_MS = 1_000
UPDATE_HANDOFF_RECOVERY_MS = 180_000
UPDATE_HANDOFF_INSTALLER_EXITED_MESSAGE = (
    "Update installer exited before closing ApplicantScout. "
    "You can retry the update or quit and install manually."
)
UPDATE_HANDOFF_TIMEOUT_MESSAGE = (
    "Update installer did not close ApplicantScout in time. "
    "Finish or cancel any Windows installer prompt, then quit and install manually."
)
_UPDATE_INSTALL_LOCK = threading.Lock()
_QT_APPLICATION_CLASS = QApplication
# SYNC: updater._default_update_download_dir stores installers under this cache child.
UPDATE_DOWNLOADS_DIR_NAME = "updates"


@dataclass(frozen=True)
class _ControlCommandResult:
    connected: bool
    written: bool
    response: bytes | None = None
    error: str | None = None


class _DuplicateInstanceFound(RuntimeError):
    pass


class _ControlServerUnavailable(RuntimeError):
    pass


class _DeferredGuiAction:
    def __init__(self) -> None:
        self._callback: Callable[[], None] | None = None
        self._pending = False

    def request(self) -> None:
        if self._callback is None:
            self._pending = True
            return
        QTimer.singleShot(0, self._callback)

    def set_callback(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        if not self._pending:
            return
        self._pending = False
        QTimer.singleShot(0, callback)


class _WCLRegionRuntime:
    def __init__(self, fallback_region: str):
        self._fallback_region = (fallback_region or "EU").upper()
        self._live_region: str | None = None

    @property
    def effective_region(self) -> str:
        return self._live_region or self._fallback_region

    def set_fallback(self, region: str) -> bool:
        before = self.effective_region
        self._fallback_region = (region or "EU").upper()
        return self.effective_region != before

    def set_live_region_id(self, region_id: int) -> bool:
        region = REGION_ID_TO_WCL.get(region_id)
        if region is None:
            return False
        before = self.effective_region
        self._live_region = region
        return self.effective_region != before


class _WowLifecycleSignals(QObject):
    checked = pyqtSignal(bool)
    checkFailed = pyqtSignal()


@dataclass(frozen=True)
class _SettingsApplyResult:
    cfg: Config
    auth: Any
    watcher: Any
    current_screenshots_dir: Path
    wow_exit_timer: Any
    overrides: list[str]


class TrayController:
    def __init__(
        self,
        *,
        app: QApplication,
        icon: QIcon,
        window: OverlayWindow,
        show_settings: Callable[[], None],
        open_logs: Callable[[], str],
        run_update: Callable[[], None],
        quit_app: Callable[[], None],
    ) -> None:
        self.tray = QSystemTrayIcon(icon, app)
        self.tray.setToolTip("ApplicantScout Companion is running")
        self.menu = QMenu()

        self.settings_action = _add_menu_action(self.menu, "Open settings")
        self.settings_action.triggered.connect(lambda *_args: show_settings())

        self.show_overlay_action = _add_menu_action(self.menu, "Show overlay")
        self.show_overlay_action.triggered.connect(
            lambda *_args: self._show_overlay(window)
        )

        self.hide_overlay_action = _add_menu_action(self.menu, "Hide overlay")
        self.hide_overlay_action.triggered.connect(
            lambda *_args: window.collapse_to_launcher()
        )

        self.update_action = _add_menu_action(self.menu, "Update")
        self.update_action.setEnabled(False)
        self.update_action.triggered.connect(lambda *_args: run_update())
        self._latest_update_version: str | None = None
        self._update_in_progress = False

        self.open_logs_action = _add_menu_action(self.menu, "Open logs")
        self.open_logs_action.triggered.connect(
            lambda *_args: self._open_logs(open_logs)
        )

        self.menu.addSeparator()
        self.quit_action = _add_menu_action(self.menu, "Quit ApplicantScout")
        self.quit_action.triggered.connect(lambda *_args: self._request_quit(quit_app))

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(
            lambda reason: self._handle_activation(reason, show_settings)
        )

    def show(self) -> None:
        self.tray.show()
        self.tray.showMessage(
            "ApplicantScout Companion",
            "Running in the system tray. Right-click for settings.",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def set_update_available(self, latest_version: str | None) -> None:
        self._latest_update_version = latest_version
        self._render_update_state()

    def set_update_in_progress(self, in_progress: bool) -> None:
        self._update_in_progress = in_progress
        self._render_update_state()

    def _render_update_state(self) -> None:
        if self._update_in_progress:
            self.update_action.setText("Installing update...")
            self.update_action.setEnabled(False)
            self.quit_action.setEnabled(False)
            self.tray.setToolTip("ApplicantScout Companion update is installing")
            return
        self.quit_action.setEnabled(True)
        if self._latest_update_version:
            self.update_action.setText(f"Update to {self._latest_update_version}")
            self.update_action.setEnabled(True)
            self.tray.setToolTip(
                "ApplicantScout Companion is running - update "
                f"{self._latest_update_version} is available"
            )
            return
        self.update_action.setText("Update")
        self.update_action.setEnabled(False)
        self.tray.setToolTip("ApplicantScout Companion is running")

    def _request_quit(self, quit_app: Callable[[], None]) -> None:
        if self._update_in_progress:
            self.show_update_quit_blocked()
            return
        quit_app()

    def show_update_quit_blocked(self) -> None:
        self.tray.showMessage(
            "ApplicantScout update",
            UPDATE_QUIT_BLOCKED_MESSAGE,
            QSystemTrayIcon.MessageIcon.Warning,
            7000,
        )

    def _show_overlay(self, window: OverlayWindow) -> None:
        window.restore_from_tray()

    def _open_logs(self, open_logs: Callable[[], str]) -> None:
        try:
            open_logs()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not open logs from tray: %s", exc)
            self.tray.showMessage(
                "ApplicantScout logs",
                f"Could not open logs: {exc}",
                QSystemTrayIcon.MessageIcon.Warning,
                7000,
            )

    def _handle_activation(
        self,
        reason: QSystemTrayIcon.ActivationReason,
        show_settings: Callable[[], None],
    ) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            show_settings()


def _add_menu_action(menu: QMenu, text: str) -> QAction:
    action = menu.addAction(text)
    if action is None:
        raise RuntimeError(f"Could not create tray menu action: {text}")
    return action


def _app_icon() -> QIcon:
    icon = QIcon(str(APP_ICON_PATH))
    return icon if not icon.isNull() else QIcon()


def _set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except (AttributeError, OSError, ValueError):
        log.debug("Could not set Windows AppUserModelID", exc_info=True)


def _send_control_command(
    command: bytes, *, timeout_ms: int = 2000
) -> _ControlCommandResult:
    socket = QLocalSocket()
    socket.connectToServer(CONTROL_SERVER_NAME)
    if not socket.waitForConnected(timeout_ms):
        return _ControlCommandResult(
            connected=False,
            written=False,
            error=socket.errorString(),
        )
    payload = command.rstrip() + b"\n"
    socket.write(payload)
    if not socket.waitForBytesWritten(timeout_ms):
        error = socket.errorString()
        socket.disconnectFromServer()
        return _ControlCommandResult(connected=True, written=False, error=error)
    response = None
    if socket.waitForReadyRead(500):
        response = socket.readAll().data().strip().lower()
    socket.disconnectFromServer()
    return _ControlCommandResult(connected=True, written=True, response=response)


def _shutdown_running_instance(timeout_ms: int = 2000) -> int:
    result = _send_control_command(CONTROL_QUIT_COMMAND, timeout_ms=timeout_ms)
    if not result.connected:
        log.info("No running ApplicantScout instance accepted the shutdown command.")
        return 0
    if not result.written:
        log.warning("Could not send shutdown command: %s", result.error or "unknown error")
        return 1
    if result.response == b"blocked":
        log.warning("Running ApplicantScout instance refused the shutdown command.")
        return 1
    if result.response != b"ok":
        log.warning(
            "Running ApplicantScout instance did not acknowledge shutdown: %r",
            result.response,
        )
        return 1
    return 0


def _control_command_acknowledged(result: _ControlCommandResult) -> bool:
    return result.connected and result.written and result.response == b"ok"


def _has_running_instance(timeout_ms: int = 200) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(CONTROL_SERVER_NAME)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.disconnectFromServer()
    return True


def _create_control_server(
    app: QApplication,
    *,
    quit_app: Callable[[], None],
    show_settings: Callable[[], None],
    can_quit: Callable[[], bool] | None = None,
    prepare_quit: Callable[[], bool] | None = None,
    quit_blocked: Callable[[], None] | None = None,
) -> QLocalServer:
    server = QLocalServer(app)
    if not server.listen(CONTROL_SERVER_NAME):
        active_owner = _send_control_command(CONTROL_SHOW_SETTINGS_COMMAND, timeout_ms=200)
        if _control_command_acknowledged(active_owner):
            raise _DuplicateInstanceFound
        if active_owner.connected and active_owner.written:
            log.warning(
                "Control server owner returned unexpected response while probing: %r",
                active_owner.response,
            )
            raise _DuplicateInstanceFound
        QLocalServer.removeServer(CONTROL_SERVER_NAME)
        if not server.listen(CONTROL_SERVER_NAME):
            raise _ControlServerUnavailable(server.errorString())

    server.newConnection.connect(
        lambda: _drain_control_connections(
            server,
            quit_app,
            show_settings,
            can_quit=can_quit,
            prepare_quit=prepare_quit,
            quit_blocked=quit_blocked,
        )
    )
    return server


def _drain_control_connections(
    server: QLocalServer,
    quit_app: Callable[[], None],
    show_settings: Callable[[], None],
    *,
    can_quit: Callable[[], bool] | None = None,
    prepare_quit: Callable[[], bool] | None = None,
    quit_blocked: Callable[[], None] | None = None,
) -> None:
    while server.hasPendingConnections():
        socket = server.nextPendingConnection()
        if socket is None:
            continue
        socket.readyRead.connect(
            lambda _socket=socket: _handle_control_command(
                _socket,
                quit_app,
                show_settings,
                can_quit=can_quit,
                prepare_quit=prepare_quit,
                quit_blocked=quit_blocked,
            )
        )
        socket.disconnected.connect(socket.deleteLater)
        if socket.bytesAvailable() > 0:
            _handle_control_command(
                socket,
                quit_app,
                show_settings,
                can_quit=can_quit,
                prepare_quit=prepare_quit,
                quit_blocked=quit_blocked,
            )


def _handle_control_command(
    socket: QLocalSocket,
    quit_app: Callable[[], None],
    show_settings: Callable[[], None] | None = None,
    *,
    can_quit: Callable[[], bool] | None = None,
    prepare_quit: Callable[[], bool] | None = None,
    quit_blocked: Callable[[], None] | None = None,
) -> None:
    command = socket.readAll().data().strip().lower()
    if command == CONTROL_QUIT_COMMAND:
        if can_quit is not None and not can_quit():
            socket.write(b"blocked\n")
            socket.flush()
            socket.waitForBytesWritten(100)
            socket.disconnectFromServer()
            if quit_blocked is not None:
                QTimer.singleShot(0, quit_blocked)
            return
        if prepare_quit is not None and not prepare_quit():
            socket.write(b"blocked\n")
            socket.flush()
            socket.waitForBytesWritten(100)
            socket.disconnectFromServer()
            return
        socket.write(b"ok\n")
        socket.flush()
        socket.waitForBytesWritten(100)
        socket.disconnectFromServer()
        QTimer.singleShot(0, quit_app)
        return
    if command == CONTROL_SHOW_SETTINGS_COMMAND and show_settings is not None:
        socket.write(b"ok\n")
        socket.flush()
        socket.waitForBytesWritten(100)
        socket.disconnectFromServer()
        QTimer.singleShot(0, show_settings)
        return
    socket.write(b"unknown\n")
    socket.flush()
    socket.disconnectFromServer()


def _create_tray_controller(
    app: QApplication,
    *,
    icon: QIcon,
    window: OverlayWindow,
    show_settings: Callable[[], None],
    open_logs: Callable[[], str],
    run_update: Callable[[], None],
    quit_app: Callable[[], None],
) -> TrayController | None:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("System tray is unavailable; settings are only available in overlay.")
        return None
    app.setQuitOnLastWindowClosed(False)
    controller = TrayController(
        app=app,
        icon=icon,
        window=window,
        show_settings=show_settings,
        open_logs=open_logs,
        run_update=run_update,
        quit_app=quit_app,
    )
    controller.show()
    return controller


def _validate_oauth_async(client: WCLClient) -> threading.Thread | None:
    """Start a fresh, generation-safe OAuth probe off the GUI thread."""
    validation = client.begin_auth_validation()
    if validation is None:
        return None
    worker = threading.Thread(
        target=lambda: client.run_auth_validation(validation),
        name="WCLAuthValidator",
        daemon=True,
    )
    worker.start()
    return worker


class StateMachine(QObject):
    """Applies decoded Snapshot to AppState via diff; emits per-applicant signals.

    Snapshot is idempotent (full state per shot) — companion holds 'previous'
    state, computes added/updated/removed sets, emits matching signals so
    OverlayWindow's existing slots fire identically to the old chatlog flow.
    Preserves WCL percentile cache when an applicant's spec_id is unchanged."""

    applicantAdded = pyqtSignal(Applicant)
    applicantUpdated = pyqtSignal(Applicant)
    applicantRemoved = pyqtSignal(str)
    listingChanged = pyqtSignal()
    cleared = pyqtSignal()
    rosterChanged = pyqtSignal()
    # Region change → main wires to wcl_client.region so non-EU users don't
    # silently get "Server not found" with default config.
    versionUpdated = pyqtSignal(int)
    _rioPreloadCompleted = pyqtSignal(str, int, int)

    def __init__(
        self,
        state: AppState,
        parent=None,
        rio_reader: Any | None = None,
        *,
        rio_preload_monotonic: Callable[[], float] = time.monotonic,
    ):
        super().__init__(parent)
        self._state = state
        self._rio_reader = rio_reader
        self._rio_reader_generation = 0
        self._rio_preload_monotonic = rio_preload_monotonic
        self._rio_preload_serial = 0
        self._rio_preload_current_key: tuple[int, str] | None = None
        self._rio_preload_active: tuple[int, str, int] | None = None
        self._rio_preload_completed_key: tuple[int, str] | None = None
        self._rio_preload_refresh_after = 0.0
        self._rioPreloadCompleted.connect(self._on_rio_preload_completed)

    def set_rio_reader(self, rio_reader: Any | None) -> None:
        self._rio_reader = rio_reader
        self._rio_reader_generation += 1

    def _preload_local_rio_region(self, region_token: str | None) -> None:
        if self._rio_reader is None or region_token is None:
            return
        preload = getattr(self._rio_reader, "preload_region_async", None)
        if not callable(preload):
            return
        generation = self._rio_reader_generation
        request_key = (generation, region_token)
        key_changed = request_key != self._rio_preload_current_key
        active = self._rio_preload_active
        if not key_changed and active is not None and active[:2] == request_key:
            return
        now = self._rio_preload_monotonic()
        if (
            not key_changed
            and self._rio_preload_completed_key == request_key
            and now < self._rio_preload_refresh_after
        ):
            return
        self._rio_preload_current_key = request_key
        self._rio_preload_serial += 1
        serial = self._rio_preload_serial
        request = (generation, region_token, serial)
        self._rio_preload_active = request

        def _on_loaded() -> None:
            self._rioPreloadCompleted.emit(region_token, generation, serial)

        try:
            preload(region_token, on_loaded=_on_loaded)
        except TypeError:
            try:
                preload(region_token)
            except Exception:
                if self._rio_preload_active == request:
                    self._rio_preload_active = None
                raise
            if self._rio_preload_active == request:
                self._rio_preload_active = None
                self._rio_preload_completed_key = request_key
                self._rio_preload_refresh_after = (
                    self._rio_preload_monotonic()
                    + RIO_PRELOAD_REFRESH_INTERVAL_SECONDS
                )
        except Exception:
            if self._rio_preload_active == request:
                self._rio_preload_active = None
            raise

    def _on_rio_preload_completed(
        self, region_token: str, generation: int, serial: int
    ) -> None:
        request = (generation, region_token, serial)
        if request != self._rio_preload_active:
            return
        self._rio_preload_active = None
        self._rio_preload_completed_key = (generation, region_token)
        self._rio_preload_refresh_after = (
            self._rio_preload_monotonic() + RIO_PRELOAD_REFRESH_INTERVAL_SECONDS
        )
        if generation != self._rio_reader_generation:
            return
        current_region = REGION_ID_TO_WCL.get(self._state.player.region_id)
        if region_token != current_region:
            return
        self._reenrich_local_rio_rows()

    def _reenrich_local_rio_rows(self) -> None:
        if self._rio_reader is None:
            return
        for applicant in list(self._state.applicants.values()):
            if self._apply_current_local_rio_context(
                applicant, preserve_on_failure=True
            ):
                self.applicantUpdated.emit(applicant)
        roster_changed = False
        for member in list(self._state.party_members.values()):
            roster_changed = (
                self._apply_current_local_rio_context(
                    member, preserve_on_failure=True
                )
                or roster_changed
            )
        if roster_changed:
            self.rosterChanged.emit()

    def _rio_profile_for(self, decoded_name: str):
        if self._rio_reader is None:
            return None
        default_realm = default_realm_from_player(self._state.player.full_name)
        name, realm = split_name_realm(decoded_name, default_realm)
        region = REGION_ID_TO_WCL.get(self._state.player.region_id)
        try:
            try:
                profile = self._rio_reader.lookup_profile(
                    name,
                    realm,
                    region,
                    allow_load=False,
                )
            except TypeError:
                profile = self._rio_reader.lookup_profile(name, realm, region)
        except (OSError, ValueError) as exc:
            log.warning("Local RaiderIO lookup failed for %s-%s: %s", name, realm, exc)
            return _RIO_LOOKUP_FAILED
        return profile

    @staticmethod
    def _rio_dungeon_rows_from_profile(profile, decoded_rows: list[dict]) -> list[dict]:
        rows = [dict(row) for row in decoded_rows]
        if profile is None or profile is _RIO_LOOKUP_FAILED:
            return rows
        if not getattr(profile, "has_mplus_profile", True):
            return rows
        return [dict(row) for row in profile.dungeons]

    @staticmethod
    def _rio_raid_progress_from_profile(profile) -> dict[str, dict]:
        if profile is None or profile is _RIO_LOOKUP_FAILED:
            return {}
        progress = getattr(profile, "raid_progress", None)
        if not isinstance(progress, dict):
            return {}
        return {
            str(difficulty): dict(data)
            for difficulty, data in progress.items()
            if isinstance(data, dict)
        }

    @staticmethod
    def _rio_score_from_profile(profile, decoded_score: int) -> int:
        score = decoded_score if isinstance(decoded_score, int) else 0
        if profile is None or profile is _RIO_LOOKUP_FAILED:
            return score
        profile_score = getattr(profile, "current_score", 0)
        if isinstance(profile_score, bool) or not isinstance(profile_score, int):
            return score
        return max(score, profile_score)

    @staticmethod
    def _rio_profile_flag_from_profile(profile, decoded_flag: bool) -> bool:
        if decoded_flag:
            return True
        if profile is None or profile is _RIO_LOOKUP_FAILED:
            return False
        return bool(getattr(profile, "has_mplus_profile", True))

    @staticmethod
    def _record_rio_transport_fields(
        target: Applicant,
        *,
        score: int,
        rio_profile: bool,
        rio_dungeons: list[dict],
    ) -> None:
        target.rio_transport_score = score
        target.rio_transport_profile = rio_profile
        target.rio_transport_dungeons = [dict(row) for row in rio_dungeons]

    @staticmethod
    def _rio_transport_fields(
        source: Applicant,
    ) -> tuple[int, bool, list[dict]]:
        score = source.rio_transport_score
        if score is None:
            score = source.score if isinstance(source.score, int) else 0
        rio_profile = source.rio_transport_profile
        if rio_profile is None:
            rio_profile = bool(source.rio_profile)
        rio_dungeons = source.rio_transport_dungeons
        if rio_dungeons is None:
            rio_dungeons = source.rio_dungeons
        return score, rio_profile, [dict(row) for row in rio_dungeons]

    def _apply_current_local_rio_context(
        self,
        target: Applicant,
        *,
        preserve_on_failure: bool,
    ) -> bool:
        profile = self._rio_profile_for(target.name)
        if profile is _RIO_LOOKUP_FAILED:
            if preserve_on_failure:
                return False
            # The previous local profile belongs to a different identity
            # context. Fall back to transport evidence even when the replacement
            # database lookup fails; stale region/realm evidence is not valid.
            profile = None
        transport_score, transport_profile, transport_rows = (
            self._rio_transport_fields(target)
        )
        rows = self._rio_dungeon_rows_from_profile(profile, transport_rows)
        raid_progress = self._rio_raid_progress_from_profile(profile)
        score = self._rio_score_from_profile(profile, transport_score)
        rio_profile = self._rio_profile_flag_from_profile(
            profile, transport_profile
        )
        if (
            rows == target.rio_dungeons
            and raid_progress == target.rio_raid_progress
            and score == target.score
            and rio_profile == target.rio_profile
        ):
            return False
        target.rio_dungeons = rows
        target.rio_raid_progress = raid_progress
        target.score = score
        target.rio_profile = rio_profile
        return True

    @staticmethod
    def _roster_key(name: str) -> str:
        return name.strip().lower()

    @staticmethod
    def _copy_wcl_data(source: Applicant, target: Applicant) -> None:
        target.fetch_status = source.fetch_status
        target.error_message = source.error_message
        target.wcl_error_kind = source.wcl_error_kind
        target.raid_normal = source.raid_normal
        target.raid_heroic = source.raid_heroic
        target.raid_mythic = source.raid_mythic
        target.raid_normal_median = source.raid_normal_median
        target.raid_heroic_median = source.raid_heroic_median
        target.raid_mythic_median = source.raid_mythic_median
        target.mplus_dps = source.mplus_dps
        target.mplus_hps = source.mplus_hps
        target.mplus_dps_median = source.mplus_dps_median
        target.mplus_hps_median = source.mplus_hps_median
        target.mplus_dps_breakdown = list(source.mplus_dps_breakdown)
        target.mplus_hps_breakdown = list(source.mplus_hps_breakdown)
        target.raid_boss_parses = {
            difficulty: [dict(row) for row in rows]
            for difficulty, rows in source.raid_boss_parses.items()
        }
        target.wcl_metric_preferences = source.wcl_metric_preferences

    @staticmethod
    def _preserve_known_transport_fields(source: Applicant, target: Applicant) -> None:
        if target.name == source.name:
            if target.spec_id <= 0 < source.spec_id:
                target.spec_id = source.spec_id
            if target.ilvl <= 0 < source.ilvl:
                target.ilvl = source.ilvl

    @staticmethod
    def _capture_local_rio_fields(
        source: Applicant,
    ) -> tuple[int, bool, list[dict], dict[str, dict]]:
        score = source.score if isinstance(source.score, int) else 0
        return (
            score,
            bool(source.rio_profile),
            [dict(row) for row in source.rio_dungeons],
            {
                str(difficulty): dict(data)
                for difficulty, data in source.rio_raid_progress.items()
                if isinstance(data, dict)
            },
        )

    @staticmethod
    def _preserve_local_rio_fields(
        fields: tuple[int, bool, list[dict], dict[str, dict]],
        target: Applicant,
    ) -> None:
        score, rio_profile, rio_dungeons, rio_raid_progress = fields
        target_score = target.score if isinstance(target.score, int) else 0
        if score > target_score:
            target.score = score
        if rio_profile:
            target.rio_profile = True
        if rio_dungeons:
            target.rio_dungeons = [dict(row) for row in rio_dungeons]
        if rio_raid_progress:
            target.rio_raid_progress = {
                str(difficulty): dict(data)
                for difficulty, data in rio_raid_progress.items()
            }

    @staticmethod
    def _local_rio_identity_stable(
        source_name: str,
        target_name: str,
        *,
        region_identity_changed: bool,
        default_realm_changed: bool,
    ) -> bool:
        if source_name != target_name:
            return False
        return not StateMachine._identity_context_changed(
            target_name,
            region_identity_changed=region_identity_changed,
            default_realm_changed=default_realm_changed,
        )

    @staticmethod
    def _identity_context_changed(
        name: str,
        *,
        region_identity_changed: bool,
        default_realm_changed: bool,
    ) -> bool:
        return region_identity_changed or (
            default_realm_changed and not applicant_has_explicit_realm(name)
        )

    def _refresh_preserved_identity_rows(
        self,
        *,
        applicants: bool,
        roster: bool,
        region_identity_changed: bool,
        default_realm_changed: bool,
    ) -> None:
        if not region_identity_changed and not default_realm_changed:
            return
        if applicants:
            for applicant in list(self._state.applicants.values()):
                if not self._identity_context_changed(
                    applicant.name,
                    region_identity_changed=region_identity_changed,
                    default_realm_changed=default_realm_changed,
                ):
                    continue
                applicant.clear_wcl_data()
                self._apply_current_local_rio_context(
                    applicant, preserve_on_failure=False
                )
                self.applicantUpdated.emit(applicant)
        if roster:
            roster_changed = False
            for member in list(self._state.party_members.values()):
                if not self._identity_context_changed(
                    member.name,
                    region_identity_changed=region_identity_changed,
                    default_realm_changed=default_realm_changed,
                ):
                    continue
                member.clear_wcl_data()
                self._apply_current_local_rio_context(
                    member, preserve_on_failure=False
                )
                roster_changed = True
            if roster_changed:
                self.rosterChanged.emit()

    def _roster_member_from_decoded(
        self,
        decoded: DecodedRosterMember,
        *,
        rio_summary_target_key: int = 0,
        rio_profile=None,
    ) -> RosterMember:
        cls_name = CLASS_ID_TO_NAME.get(decoded.class_id, "?")
        role_name = ROLE_BYTE_TO_NAME.get(decoded.role, "DAMAGER")
        member = RosterMember(
            applicant_id=self._roster_key(decoded.name),
            name=decoded.name,
            cls=cls_name,
            spec_id=decoded.spec_id,
            ilvl=decoded.ilvl,
            score=self._rio_score_from_profile(rio_profile, decoded.score),
            role=role_name,
            main_score=decoded.main_score,
            rio_profile=self._rio_profile_flag_from_profile(
                rio_profile, decoded.rio_profile
            ),
            rio_best_key=decoded.rio_best_key,
            rio_best_dungeon_key=decoded.rio_best_dungeon_key,
            rio_timed_at_or_above=decoded.rio_timed_at_or_above,
            rio_timed_at_or_above_minus1=decoded.rio_timed_at_or_above_minus1,
            rio_timed_at_or_above_minus2=decoded.rio_timed_at_or_above_minus2,
            rio_completed_at_or_above_minus1=decoded.rio_completed_at_or_above_minus1,
            rio_dungeon_count=decoded.rio_dungeon_count,
            rio_summary_target_key=rio_summary_target_key,
            rio_dungeons=self._rio_dungeon_rows_from_profile(
                rio_profile, decoded.rio_dungeons
            ),
            rio_raid_progress=self._rio_raid_progress_from_profile(rio_profile),
            unit_index=decoded.unit_index,
            subgroup=decoded.subgroup,
            is_self=decoded.is_self,
            is_raid_member=decoded.is_raid_member,
        )
        self._record_rio_transport_fields(
            member,
            score=decoded.score,
            rio_profile=decoded.rio_profile,
            rio_dungeons=decoded.rio_dungeons,
        )
        return member

    def _apply_roster_snapshot(
        self,
        roster: list[DecodedRosterMember],
        *,
        region_identity_changed: bool = False,
        default_realm_changed: bool = False,
        rio_summary_target_key: int = 0,
        emit_signal: bool = True,
    ) -> bool:
        new_by_id = {
            self._roster_key(decoded.name): decoded
            for decoded in roster
            if decoded.name.strip()
        }
        old_ids = set(self._state.party_members)
        new_ids = set(new_by_id)
        changed = bool(old_ids ^ new_ids)

        for member_id in old_ids - new_ids:
            self._state.remove_party_member(member_id)

        for member_id, decoded in new_by_id.items():
            existing = self._state.party_members.get(member_id)
            rio_profile = self._rio_profile_for(decoded.name)
            member = self._roster_member_from_decoded(
                decoded,
                rio_summary_target_key=rio_summary_target_key,
                rio_profile=rio_profile,
            )
            if existing is not None:
                previous_local_rio = self._capture_local_rio_fields(existing)
                local_rio_identity_stable = self._local_rio_identity_stable(
                    existing.name,
                    member.name,
                    region_identity_changed=region_identity_changed,
                    default_realm_changed=default_realm_changed,
                )
                self._preserve_known_transport_fields(existing, member)
                needs_refetch = (
                    existing.spec_id != member.spec_id
                    or existing.name != member.name
                    or wcl_metric_role(existing.role) != wcl_metric_role(member.role)
                    or self._identity_context_changed(
                        member.name,
                        region_identity_changed=region_identity_changed,
                        default_realm_changed=default_realm_changed,
                    )
                )
                if not needs_refetch:
                    self._copy_wcl_data(existing, member)
                if rio_profile is _RIO_LOOKUP_FAILED and local_rio_identity_stable:
                    self._preserve_local_rio_fields(previous_local_rio, member)
                changed = changed or member != existing
            else:
                changed = True
            self._state.add_or_update_party_member(member)

        if changed and emit_signal:
            self.rosterChanged.emit()
        return changed

    def expire_restored_snapshot_surfaces(
        self,
        *,
        applicants: bool,
        roster: bool,
    ) -> None:
        """Discard restored domains that never received fresh transport proof."""
        if applicants:
            old_listing = self._state.listing
            old_leader_key = self._state.leader_key
            had_applicants = bool(self._state.applicants)
            self._state.listing = None
            self._state.leader_key = None
            self._state.clear_all()
            if old_listing is not None or old_leader_key is not None:
                self.listingChanged.emit()
            if old_listing is not None or had_applicants:
                self.cleared.emit()

        if roster and self._apply_roster_snapshot([], emit_signal=False):
            self.rosterChanged.emit()

    def apply_snapshot(self, snap: Snapshot) -> None:
        region_identity_changed = False
        default_realm_changed = False
        # ─── Version ───
        if snap.version is not None:
            old_player = self._state.player
            new_player = WoWPlayer(
                addon_version=snap.version.addon_version,
                game_version=snap.version.game_version,
                region_id=snap.version.region_id,
                full_name=snap.version.player_name,
            )
            old_region_token = REGION_ID_TO_WCL.get(old_player.region_id)
            new_region_token = REGION_ID_TO_WCL.get(new_player.region_id)
            region_identity_changed = (
                new_region_token is not None and new_region_token != old_region_token
            )
            old_realm_slug = derive_server_slug(
                default_realm_from_player(old_player.full_name)
            )
            new_realm_slug = derive_server_slug(
                default_realm_from_player(new_player.full_name)
            )
            default_realm_changed = bool(new_realm_slug) and (
                new_realm_slug != old_realm_slug
            )
            self._state.player = new_player
            if snap.version.region_id != old_player.region_id:
                # Direct Qt slots update the WCL client synchronously. Do this
                # before local-RIO preload because supported readers may invoke
                # their completion callback before preload_region_async returns.
                self.versionUpdated.emit(snap.version.region_id)
            self._preload_local_rio_region(new_region_token)
            log.info(
                "Player: %s (region=%d)",
                snap.version.player_name,
                snap.version.region_id,
            )

        if not snap.terminal_clear and not snap.lfg_unavailable:
            self._refresh_preserved_identity_rows(
                applicants=False,
                roster=snap.roster_unavailable,
                region_identity_changed=region_identity_changed,
                default_realm_changed=default_realm_changed,
            )

        # ─── Leader keystone ───
        old_leader_key = self._state.leader_key
        new_leader_key: LeaderKey | None = None
        if snap.terminal_clear:
            new_leader_key = None
        elif snap.leader_key is not None and snap.leader_key.key_level > 0:
            new_leader_key = LeaderKey(
                key_level=snap.leader_key.key_level,
                challenge_map_id=snap.leader_key.challenge_map_id,
                player_name=snap.leader_key.player_name,
            )
        elif snap.lfg_unavailable and not snap.terminal_clear:
            new_leader_key = old_leader_key
        leader_key_changed = new_leader_key != old_leader_key
        self._state.leader_key = new_leader_key

        # ─── Listing ───
        new_listing: Listing | None = None
        if snap.listing is not None:
            new_listing = Listing(
                activity_id=snap.listing.activity_id,
                dungeon_name=snap.listing.dungeon_name or "?",
                listing_name=snap.listing.listing_name,
                comment=snap.listing.comment,
                key_level=snap.listing.key_level,
                category_id=snap.listing.category_id,
                difficulty_id=snap.listing.difficulty_id,
            )

        old_listing = self._state.listing
        had_applicants = bool(self._state.applicants)

        # Explicit terminal clear is authoritative even if a malformed or
        # future producer accidentally includes listing/applicant/roster blocks.
        if snap.terminal_clear:
            self._state.listing = None
            self._state.clear_all()
            roster_changed = self._apply_roster_snapshot(
                [],
                region_identity_changed=region_identity_changed,
                default_realm_changed=default_realm_changed,
                rio_summary_target_key=0,
                emit_signal=False,
            )
            if old_listing is not None or leader_key_changed:
                self.listingChanged.emit()
            if old_listing is not None or had_applicants:
                self.cleared.emit()
            if roster_changed:
                self.rosterChanged.emit()
            return

        # Partial LFG snapshot: chat/LFG lockdown kept roster transport alive, but
        # listing and applicant reads were not authoritative. Preserve LFG state
        # and apply only version/leader/roster data from this snapshot.
        if snap.lfg_unavailable and not snap.terminal_clear:
            listing_changed = False
            effective_listing = old_listing
            if effective_listing is None and new_listing is not None:
                self._state.listing = new_listing
                effective_listing = new_listing
                listing_changed = True

            rio_summary_target_key = 0
            if new_leader_key is not None:
                rio_summary_target_key = new_leader_key.key_level
            elif effective_listing is not None and effective_listing.key_level > 0:
                rio_summary_target_key = effective_listing.key_level

            if not snap.roster_unavailable:
                self._apply_roster_snapshot(
                    snap.roster,
                    region_identity_changed=region_identity_changed,
                    default_realm_changed=default_realm_changed,
                    rio_summary_target_key=rio_summary_target_key,
                )
            self._refresh_preserved_identity_rows(
                applicants=True,
                roster=snap.roster_unavailable,
                region_identity_changed=region_identity_changed,
                default_realm_changed=default_realm_changed,
            )
            if listing_changed or leader_key_changed:
                self.listingChanged.emit()
            return

        # NOLISTING-equivalent: snap arrived with has_listing=0 AND we had one.
        # Clear all applicants + emit cleared signal so overlay hides.
        if new_listing is None and old_listing is not None:
            self._state.listing = None
            self._state.clear_all()
            roster_changed = False
            if not snap.roster_unavailable:
                roster_changed = self._apply_roster_snapshot(
                    snap.roster,
                    region_identity_changed=region_identity_changed,
                    default_realm_changed=default_realm_changed,
                    rio_summary_target_key=(
                        new_leader_key.key_level if new_leader_key is not None else 0
                    ),
                    emit_signal=False,
                )
            self.listingChanged.emit()
            self.cleared.emit()
            if roster_changed:
                self.rosterChanged.emit()
            return

        # No listing in snap AND no prior listing → roster/version can still update.
        if new_listing is None:
            if not snap.roster_unavailable:
                self._apply_roster_snapshot(
                    snap.roster,
                    region_identity_changed=region_identity_changed,
                    default_realm_changed=default_realm_changed,
                    rio_summary_target_key=(
                        new_leader_key.key_level if new_leader_key is not None else 0
                    ),
                )
            if leader_key_changed:
                self.listingChanged.emit()
            return

        rio_summary_target_key = 0
        if new_leader_key is not None:
            rio_summary_target_key = new_leader_key.key_level
        elif new_listing.key_level > 0:
            rio_summary_target_key = new_listing.key_level

        # Listing changed (dungeon/key/comment) — fire signal so overlay re-titles
        if new_listing != old_listing:
            self._state.listing = new_listing
            log.info(
                "Listing: %s +%d cat=%d diff=%d (%d applicant rows in snapshot)",
                new_listing.dungeon_name,
                new_listing.key_level,
                new_listing.category_id,
                new_listing.difficulty_id,
                len(snap.applicants),
            )
            self.listingChanged.emit()
        elif leader_key_changed:
            self.listingChanged.emit()

        # ─── Applicants diff ───
        # Composite key f"{applicant_id}:{member_idx}" — required for multi-
        # member group apps (one LFG application can have up to 5 members,
        # all sharing applicant_id but with distinct member_idx 1..N). Solo
        # apps + legacy v0x01 payloads decode with member_idx=1, producing
        # keys like "42:1" — same shape, no special-casing needed.
        valid_applicants = [
            a for a in snap.applicants
            if (name := a.name.strip()) and not is_placeholder_transport_identity(name)
        ]
        new_by_id = {f"{a.applicant_id}:{a.member_idx}": a for a in valid_applicants}
        # Diagnostic: per-applicant_id member-count distribution. Helps verify
        # multi-member group emit is reaching the companion (expect aid_groups
        # like {42: 2, 99: 1} when a 2-person group + a solo apply together).
        if valid_applicants:
            aid_groups: dict[int, int] = {}
            for a in valid_applicants:
                aid_groups[a.applicant_id] = aid_groups.get(a.applicant_id, 0) + 1
            multi_member = {aid: c for aid, c in aid_groups.items() if c > 1}
            if multi_member:
                log.info(
                    "Snapshot: %d applicant rows across %d apps; multi-member groups: %s",
                    len(snap.applicants),
                    len(aid_groups),
                    multi_member,
                )
        old_ids = set(self._state.applicants.keys())
        new_ids = set(new_by_id.keys())

        # Removed
        for aid in old_ids - new_ids:
            self._state.remove(aid)
            self.applicantRemoved.emit(aid)

        # Added or updated
        for aid, da in new_by_id.items():
            cls_name = CLASS_ID_TO_NAME.get(da.class_id, "?")
            role_name = ROLE_BYTE_TO_NAME.get(da.role, "DAMAGER")
            existing = self._state.applicants.get(aid)
            rio_profile = self._rio_profile_for(da.name)
            if existing is None:
                applicant = Applicant(
                    applicant_id=aid,
                    name=da.name,
                    cls=cls_name,
                    spec_id=da.spec_id,
                    ilvl=da.ilvl,
                    score=self._rio_score_from_profile(rio_profile, da.score),
                    role=role_name,
                    main_score=da.main_score,
                    rio_profile=self._rio_profile_flag_from_profile(
                        rio_profile, da.rio_profile
                    ),
                    rio_best_key=da.rio_best_key,
                    rio_best_dungeon_key=da.rio_best_dungeon_key,
                    rio_timed_at_or_above=da.rio_timed_at_or_above,
                    rio_timed_at_or_above_minus1=da.rio_timed_at_or_above_minus1,
                    rio_timed_at_or_above_minus2=da.rio_timed_at_or_above_minus2,
                    rio_completed_at_or_above_minus1=(
                        da.rio_completed_at_or_above_minus1
                    ),
                    rio_dungeon_count=da.rio_dungeon_count,
                    rio_summary_target_key=rio_summary_target_key,
                    rio_dungeons=self._rio_dungeon_rows_from_profile(
                        rio_profile, da.rio_dungeons
                    ),
                    rio_raid_progress=self._rio_raid_progress_from_profile(
                        rio_profile
                    ),
                )
                self._record_rio_transport_fields(
                    applicant,
                    score=da.score,
                    rio_profile=da.rio_profile,
                    rio_dungeons=da.rio_dungeons,
                )
                self._state.add_or_update(applicant)
                log.info(
                    "Applicant added: %s (%s, ilvl %d, score %d, main %d) "
                    "[key=%s aid=%d m=%d]",
                    da.name,
                    cls_name,
                    da.ilvl,
                    da.score,
                    da.main_score,
                    aid,
                    da.applicant_id,
                    da.member_idx,
                )
                self.applicantAdded.emit(applicant)
            else:
                previous_state = replace(existing)
                previous_local_rio = self._capture_local_rio_fields(existing)
                local_rio_identity_stable = self._local_rio_identity_stable(
                    existing.name,
                    da.name,
                    region_identity_changed=region_identity_changed,
                    default_realm_changed=default_realm_changed,
                )
                # Preserve WCL percentiles only while the WCL result shape stays
                # valid for this row. Gear/score changes are safe; character,
                # spec, and DPS-vs-HEALER metric-role changes are not.
                incoming_spec_id = (
                    da.spec_id if da.spec_id > 0 else existing.spec_id
                )
                incoming_ilvl = da.ilvl if da.ilvl > 0 else existing.ilvl
                needs_refetch = (
                    existing.spec_id != incoming_spec_id
                    or existing.name != da.name
                    or wcl_metric_role(existing.role) != wcl_metric_role(role_name)
                    or self._identity_context_changed(
                        da.name,
                        region_identity_changed=region_identity_changed,
                        default_realm_changed=default_realm_changed,
                    )
                )
                existing.name = da.name
                existing.cls = cls_name
                existing.spec_id = incoming_spec_id
                existing.ilvl = incoming_ilvl
                existing.score = self._rio_score_from_profile(rio_profile, da.score)
                existing.role = role_name
                existing.main_score = da.main_score
                existing.rio_profile = self._rio_profile_flag_from_profile(
                    rio_profile, da.rio_profile
                )
                existing.rio_best_key = da.rio_best_key
                existing.rio_best_dungeon_key = da.rio_best_dungeon_key
                existing.rio_timed_at_or_above = da.rio_timed_at_or_above
                existing.rio_timed_at_or_above_minus1 = da.rio_timed_at_or_above_minus1
                existing.rio_timed_at_or_above_minus2 = da.rio_timed_at_or_above_minus2
                existing.rio_completed_at_or_above_minus1 = (
                    da.rio_completed_at_or_above_minus1
                )
                existing.rio_dungeon_count = da.rio_dungeon_count
                existing.rio_summary_target_key = rio_summary_target_key
                existing.rio_dungeons = self._rio_dungeon_rows_from_profile(
                    rio_profile, da.rio_dungeons
                )
                existing.rio_raid_progress = self._rio_raid_progress_from_profile(
                    rio_profile
                )
                self._record_rio_transport_fields(
                    existing,
                    score=da.score,
                    rio_profile=da.rio_profile,
                    rio_dungeons=da.rio_dungeons,
                )
                if rio_profile is _RIO_LOOKUP_FAILED and local_rio_identity_stable:
                    self._preserve_local_rio_fields(previous_local_rio, existing)
                if needs_refetch:
                    existing.clear_wcl_data()
                # The transport intentionally sends one redundant settled
                # snapshot instead of an ACK. Keep that resend idempotent so it
                # does not enqueue a duplicate overlay refresh for every row.
                # Identity changes still need a signal even when the row was
                # already pending: the overlay owns starting the replacement
                # WCL request against the new region/spec/metric-role context.
                if existing != previous_state or needs_refetch:
                    self.applicantUpdated.emit(existing)

        if not snap.roster_unavailable:
            self._apply_roster_snapshot(
                snap.roster,
                region_identity_changed=region_identity_changed,
                default_realm_changed=default_realm_changed,
                rio_summary_target_key=rio_summary_target_key,
            )


class UpdateSignals(QObject):
    checked = pyqtSignal(int, object)
    completed = pyqtSignal(object)


@dataclass(frozen=True)
class _UpdateCompletion:
    message: str
    error: bool = False
    installer_handoff: bool = False
    installer_launch: object | None = None


class _UpdateQuitGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._update_in_progress = False
        self._installer_handoff_started = False

    @property
    def update_in_progress(self) -> bool:
        with self._lock:
            return self._update_in_progress

    def set_update_in_progress(self, in_progress: bool) -> None:
        with self._lock:
            self._update_in_progress = in_progress
            if not in_progress:
                self._installer_handoff_started = False

    def mark_installer_handoff_started(self) -> bool:
        with self._lock:
            if not self._update_in_progress:
                return False
            self._installer_handoff_started = True
            return True

    def rollback_installer_handoff(self) -> None:
        with self._lock:
            if self._update_in_progress:
                self._installer_handoff_started = False

    def can_user_quit(self) -> bool:
        with self._lock:
            return not self._update_in_progress

    def can_control_quit(self) -> bool:
        with self._lock:
            return not self._update_in_progress or self._installer_handoff_started

    def prepare_control_quit(self, normal_prepare: Callable[[], bool]) -> bool:
        with self._lock:
            if self._update_in_progress and self._installer_handoff_started:
                return True
        return normal_prepare()


class _UpdateHandoffRecoveryController:
    def __init__(
        self,
        parent: QObject | None,
        *,
        on_recover: Callable[[str, bool], None],
        timer_factory: Callable[[QObject | None], Any] = QTimer,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_ms: int = UPDATE_HANDOFF_RECOVERY_MS,
        poll_interval_ms: int = UPDATE_HANDOFF_POLL_INTERVAL_MS,
    ) -> None:
        self._parent = parent
        self._on_recover = on_recover
        self._timer_factory = timer_factory
        self._monotonic = monotonic
        self._timeout_s = timeout_ms / 1000
        self._poll_interval_ms = poll_interval_ms
        self._timer: Any | None = None
        self._installer_launch: object | None = None
        self._started_at: float | None = None

    def arm(self, installer_launch: object | None, _message: str) -> None:
        self.disarm()
        self._installer_launch = installer_launch
        self._started_at = self._monotonic()
        timer = self._timer_factory(self._parent)
        timer.setInterval(self._poll_interval_ms)
        timer.timeout.connect(self._tick)
        timer.start()
        self._timer = timer

    def disarm(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._timer = None
        self._installer_launch = None
        self._started_at = None

    def _tick(self) -> None:
        if self._started_at is None:
            return
        poll = getattr(self._installer_launch, "poll", None)
        if callable(poll):
            try:
                if poll() is not None:
                    self._recover(UPDATE_HANDOFF_INSTALLER_EXITED_MESSAGE, True)
                    return
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not poll update installer process: %s", exc)
                self._recover(UPDATE_HANDOFF_INSTALLER_EXITED_MESSAGE, True)
                return
        if self._monotonic() - self._started_at >= self._timeout_s:
            self._recover(UPDATE_HANDOFF_TIMEOUT_MESSAGE, False)

    def _recover(self, message: str, retry_available: bool) -> None:
        self.disarm()
        self._on_recover(message, retry_available)


class _UpdateCheckCoordinator:
    def __init__(self) -> None:
        self._latest_generation = 0

    def next_generation(self) -> int:
        self._latest_generation += 1
        return self._latest_generation

    def is_current(self, generation: int) -> bool:
        return generation == self._latest_generation


@dataclass(frozen=True)
class _UpdateCheckDecision:
    is_current: bool
    action: Literal["ignore", "set", "clear", "preserve"]
    pending_update_version: str | None


_TRANSIENT_UPDATE_UNAVAILABLE_REASONS = frozenset(
    {
        "client_error",
        "http_error",
        "network_error",
        "malformed_json",
        "unexpected_response",
        "unexpected_exception",
    }
)


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._apply_private_modes()

    def _apply_private_modes(self) -> None:
        base_path = Path(self.baseFilename)
        if base_path.exists():
            apply_private_file_mode(base_path)
        for index in range(1, self.backupCount + 1):
            backup_path = Path(f"{self.baseFilename}.{index}")
            if backup_path.exists():
                apply_private_file_mode(backup_path)

    def doRollover(self) -> None:  # noqa: N802 - stdlib logging API name.
        try:
            super().doRollover()
        except PermissionError:
            # WHY: Windows can reject rollover when another companion process holds
            # the log or a backup; keep logging without noisy handler tracebacks.
            if self.stream is None and not self.delay:
                self.stream = self._open()
            return
        self._apply_private_modes()


def _resolve_update_check_result(
    coordinator: _UpdateCheckCoordinator,
    generation: int,
    result: object,
    *,
    previous_pending_update_version: str | None = None,
) -> _UpdateCheckDecision:
    if not coordinator.is_current(generation):
        return _UpdateCheckDecision(
            is_current=False,
            action="ignore",
            pending_update_version=previous_pending_update_version,
        )

    latest_version = getattr(result, "latest_version", None)
    if getattr(result, "status", None) == "available" and _update_result_has_installable_asset(
        result
    ):
        return _UpdateCheckDecision(
            is_current=True,
            action="set",
            pending_update_version=str(latest_version or "available"),
        )
    if (
        getattr(result, "status", None) == "unavailable"
        and getattr(result, "reason", None) in _TRANSIENT_UPDATE_UNAVAILABLE_REASONS
        and previous_pending_update_version is not None
    ):
        return _UpdateCheckDecision(
            is_current=True,
            action="preserve",
            pending_update_version=previous_pending_update_version,
        )
    return _UpdateCheckDecision(
        is_current=True,
        action="clear",
        pending_update_version=None,
    )


def _setup_logging(log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    target_log_dir = log_dir or user_log_dir()
    try:
        target_log_dir.mkdir(parents=True, exist_ok=True)
        apply_private_directory_mode(target_log_dir)
        file_handler = _PrivateRotatingFileHandler(
            target_log_dir / "applicant-scout.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("Could not initialize file logging: %s", exc)


def _show_config_error(message: str) -> None:
    QMessageBox.critical(None, "ApplicantScout setup", message)


def _clear_cache_dir(
    cache_dir: Path,
    character_cache: CharacterCache | None = None,
    auth: WCLAuth | None = None,
    *,
    live_snapshot_writer: LiveSnapshotCacheWriter | None = None,
) -> str:
    try:
        if live_snapshot_writer is not None:
            live_snapshot_writer.invalidate()
        if character_cache is not None:
            character_cache.clear()
        cache_dir.mkdir(parents=True, exist_ok=True)
        for child in cache_dir.iterdir():
            if child.name in {UPDATE_DOWNLOADS_DIR_NAME, "raiderio-local"}:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        clear_screenshot_manual_indexes(cache_dir)
        clear_lookup_payload_cache(cache_dir)
    finally:
        if auth is not None:
            auth.invalidate()
    return "Cache cleared."


def _open_log_dir(log_dir: Path) -> str:
    if not open_folder(log_dir):
        raise RuntimeError(f"Could not open {log_dir}")
    return f"Opened log folder: {log_dir}"


def _release_notes_candidate_paths() -> tuple[Path, ...]:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        executable = getattr(sys, "executable", "")
        if executable:
            candidates.append(Path(executable).resolve().parent / "RELEASE_NOTES.md")
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root).resolve() / "RELEASE_NOTES.md")
    candidates.append(Path(__file__).resolve().parents[2] / "RELEASE_NOTES.md")

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).casefold() if sys.platform == "win32" else str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


def _load_release_notes_text() -> str:
    last_error: OSError | UnicodeError | None = None
    candidates = _release_notes_candidate_paths()
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            last_error = exc
    searched = ", ".join(str(path) for path in candidates)
    if last_error is not None:
        raise RuntimeError(f"Could not read RELEASE_NOTES.md from {searched}: {last_error}") from last_error
    raise FileNotFoundError(f"Could not find RELEASE_NOTES.md in {searched}")


def _show_release_notes_dialog(parent: Any | None = None) -> None:
    try:
        release_notes = _load_release_notes_text()
    except (OSError, RuntimeError, UnicodeError) as exc:
        log.warning("Could not open ApplicantScout changelog: %s", exc)
        QMessageBox.warning(
            parent,
            "ApplicantScout changelog",
            f"Could not open changelog: {exc}",
        )
        return
    dialog = ReleaseNotesDialog(release_notes, parent=parent)
    dialog.exec()


def _connect_release_notes_dialog_action(dialog: Any) -> None:
    changelog_requested = getattr(dialog, "changelogRequested", None)
    if changelog_requested is None:
        return
    changelog_requested.connect(lambda: _show_release_notes_dialog(dialog))


def _test_wcl_credentials(cache_dir: Path, client_id: str, client_secret: str, _region: str) -> str:
    with tempfile.TemporaryDirectory(dir=cache_dir.parent) as temp_dir:
        auth = WCLAuth(client_id, client_secret, Path(temp_dir))
        auth.get_token()
    return "WCL credentials are valid."


def _safe_check_for_update(current_version: str) -> UpdateResult:
    try:
        return check_for_update(current_version)
    except Exception as exc:  # noqa: BLE001
        log.warning("GitHub update check failed unexpectedly: %s", exc)
        return UpdateResult(
            status="unavailable",
            message=f"GitHub update check failed: {exc}",
            current_version=current_version,
            reason="unexpected_exception",
        )


def _check_updates(
    *, update_quit_gate: _UpdateQuitGate
) -> SettingsUpdateResult | tuple[str, str | None]:
    if not _UPDATE_INSTALL_LOCK.acquire(blocking=False):
        raise RuntimeError("Update is already in progress.")
    try:
        result = _safe_check_for_update(__version__)
        status = getattr(result, "status", None)
        message = getattr(result, "message", "Update check failed.")
        if status == "unavailable":
            raise RuntimeError(str(message))
        if status != "available":
            return str(message), None
        if not _update_result_has_installable_asset(result):
            raise RuntimeError(str(message))
        installer = download_update_installer(result)
        if not update_quit_gate.mark_installer_handoff_started():
            raise RuntimeError("Update installer handoff is not active.")
        # WHY: current broad releases are unsigned by policy; the installer and
        # matching .sha256 sidecar are the in-app update gate, not publisher ID.
        try:
            installer_launch = launch_update_installer(
                installer,
                require_trusted_signature=False,
            )
        except BaseException:
            update_quit_gate.rollback_installer_handoff()
            raise
        return SettingsUpdateResult(
            message=(
                f"Installing ApplicantScout Companion {getattr(result, 'latest_version', 'update')}. "
                "The companion may close and reopen during the update."
            ),
            installer_handoff=True,
            installer_launch=installer_launch,
        )
    finally:
        _UPDATE_INSTALL_LOCK.release()


def _should_show_settings_on_start(
    args: list[str], *, startup_settings_shown: bool, wow_watch_mode: bool
) -> bool:
    if startup_settings_shown:
        return False
    return SHOW_SETTINGS_ARG in args or not wow_watch_mode


def _duplicate_launch_command(_args: list[str], *, wow_watch_mode: bool) -> bytes | None:
    if wow_watch_mode:
        return None
    return CONTROL_SHOW_SETTINGS_COMMAND


def _should_show_wow_start_update_prompt(
    *,
    wow_watch_mode: bool,
    startup_update_prompt_pending: bool,
    pending_update_version: str | None,
) -> bool:
    return bool(
        wow_watch_mode and startup_update_prompt_pending and pending_update_version
    )


def _wow_start_update_prompt_message(latest_version: str) -> str:
    return (
        f"Update {latest_version} is available. Click the blue download icon in the "
        "title bar to install it."
    )


def _flush_settings_before_update(settings_dialog: object | None) -> bool:
    if settings_dialog is None:
        return True
    flush = getattr(settings_dialog, "flush_pending_values", None)
    if not callable(flush):
        return True
    return bool(flush())


def _prepare_settings_before_quit(settings_dialog: object | None) -> bool:
    if settings_dialog is None:
        return True
    prepare = getattr(settings_dialog, "prepare_quit", None)
    if callable(prepare):
        return bool(prepare())
    flush = getattr(settings_dialog, "flush_pending_values", None)
    if not callable(flush):
        return True
    return bool(flush())


def _remove_wow_sync_startup_best_effort() -> None:
    try:
        configure_wow_sync_startup(False)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log.warning("Could not remove stale WoW lifecycle startup shortcut: %s", exc)


def _prepare_wow_watch_mode(args: list[str]) -> tuple[list[str], bool, int | None]:
    if WATCH_WOW_ARG not in args:
        return args, False, None
    clean_args = [arg for arg in args if arg != WATCH_WOW_ARG]
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("Could not load config for WoW lifecycle mode: %s", exc)
        return clean_args, False, 1
    if not cfg.sync_with_wow:
        log.info("WoW lifecycle mode is disabled; exiting startup shortcut run.")
        _remove_wow_sync_startup_best_effort()
        return clean_args, False, 0
    log.info("WoW lifecycle mode waiting for WoW to start.")
    while not is_wow_running():
        try:
            cfg = load_config()
        except ConfigError as exc:
            log.error("Could not reload config for WoW lifecycle mode: %s", exc)
            return clean_args, False, 1
        if not cfg.sync_with_wow:
            log.info("WoW lifecycle mode was disabled while waiting; exiting.")
            _remove_wow_sync_startup_best_effort()
            return clean_args, False, 0
        time.sleep(WOW_EXIT_POLL_MS / 1000)
    if _has_running_instance():
        log.info("Companion is already running; exiting WoW lifecycle shortcut run.")
        return clean_args, False, 0
    log.info("WoW detected; starting companion.")
    return clean_args, True, None


def _start_wow_lifecycle_timer(
    app: QApplication,
    *,
    has_seen_wow: bool,
    quit_app: Callable[[], None] | None = None,
    can_quit: Callable[[], bool] | None = None,
    prepare_quit: Callable[[], bool] | None = None,
    running_checker: Callable[[], bool | None] | None = None,
    async_runner: Callable[[Callable[[], None]], None] | None = None,
) -> QTimer:
    timer = QTimer(app)
    timer.setInterval(WOW_EXIT_POLL_MS)
    observed_wow = has_seen_wow
    missing_wow_scans = 0
    quit_callback = quit_app or app.quit
    can_quit_callback = can_quit or (lambda: True)
    prepare_quit_callback = prepare_quit or (lambda: True)

    def _default_wow_running_checker() -> bool | None:
        return is_wow_running(unknown_on_error=True)

    check_wow_running = running_checker or _default_wow_running_checker
    signals = _WowLifecycleSignals()
    state = {"checking": False, "active": True, "rearm_failed": False}
    run_async = async_runner or (
        lambda worker: threading.Thread(
            target=worker,
            name="ApplicantScoutWoWLifecycleCheck",
            daemon=True,
        ).start()
    )

    def _handle_wow_running(running: bool) -> None:
        nonlocal observed_wow, missing_wow_scans
        state["checking"] = False
        if not state["active"]:
            return
        if running:
            observed_wow = True
            missing_wow_scans = 0
            state["rearm_failed"] = False
            return
        if observed_wow:
            missing_wow_scans += 1
            if missing_wow_scans < WOW_EXIT_MISSES_BEFORE_QUIT:
                log.info(
                    "WoW process not visible (%d/%d); waiting before quitting.",
                    missing_wow_scans,
                    WOW_EXIT_MISSES_BEFORE_QUIT,
                )
                return
            if not can_quit_callback():
                return
            if not prepare_quit_callback():
                return
            if not state["active"]:
                return
            try:
                start_wow_sync_watcher(check_existing=True)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                if not state["rearm_failed"]:
                    log.warning("Could not re-arm WoW lifecycle watcher: %s", exc)
                state["rearm_failed"] = True
                return
            state["rearm_failed"] = False
            log.info("WoW is no longer running; quitting companion.")
            quit_callback()

    def _handle_wow_check_failed() -> None:
        nonlocal missing_wow_scans
        state["checking"] = False
        # WHY: an unknown scan breaks the consecutive evidence that WoW exited.
        missing_wow_scans = 0

    signals.checked.connect(_handle_wow_running)
    signals.checkFailed.connect(_handle_wow_check_failed)

    def _quit_after_wow_cycle() -> None:
        if state["checking"]:
            return
        state["checking"] = True

        def _worker() -> None:
            try:
                running = check_wow_running()
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not check WoW lifecycle state: %s", exc)
                signals.checkFailed.emit()
                return
            if running is None:
                signals.checkFailed.emit()
                return
            signals.checked.emit(running)

        run_async(_worker)

    setattr(timer, "_applicant_scout_wow_lifecycle_signals", signals)
    setattr(timer, "_applicant_scout_wow_lifecycle_state", state)
    timer.timeout.connect(_quit_after_wow_cycle)
    timer.start()
    return timer


def _apply_wow_sync_runtime(
    app: QApplication,
    enabled: bool,
    current_timer: QTimer | None,
    *,
    quit_app: Callable[[], None] | None = None,
    can_quit: Callable[[], bool] | None = None,
    prepare_quit: Callable[[], bool] | None = None,
    configure_startup: bool = True,
) -> QTimer | None:
    if configure_startup:
        configure_wow_sync_startup(enabled)
    if enabled:
        try:
            start_wow_sync_watcher(check_existing=False)
            if current_timer is None:
                return _start_wow_lifecycle_timer(
                    app,
                    has_seen_wow=False,
                    quit_app=quit_app,
                    can_quit=can_quit,
                    prepare_quit=prepare_quit,
            )
            return current_timer
        except Exception:
            if configure_startup:
                configure_wow_sync_startup(False)
            raise
    _stop_current_session_watcher_best_effort()
    if current_timer is not None:
        state = getattr(current_timer, "_applicant_scout_wow_lifecycle_state", None)
        if isinstance(state, dict):
            state["active"] = False
        current_timer.stop()
        current_timer.deleteLater()
    return None


def _stop_current_session_watcher_best_effort() -> None:
    try:
        stopped = stop_current_session_watcher()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log.warning("Could not stop current-session WoW lifecycle watcher: %s", exc)
        return
    if not stopped:
        log.warning("Could not stop current-session WoW lifecycle watcher.")


class _WatcherSignalGate:
    def __init__(self) -> None:
        self._generation = 0
        self._pending_generation: int | None = None

    @property
    def generation(self) -> int:
        return self._generation

    def prepare_next(self) -> int:
        self._pending_generation = self._generation + 1
        return self._pending_generation

    def commit(self, generation: int) -> None:
        self._generation = generation
        self._pending_generation = None

    def cancel(self, generation: int) -> None:
        if self._pending_generation == generation:
            self._pending_generation = None

    def restore(self, generation: int) -> None:
        self._generation = generation
        self._pending_generation = None

    def invalidate(self) -> None:
        self._generation = max(
            self._generation,
            self._pending_generation or self._generation,
        ) + 1
        self._pending_generation = None

    def is_current(self, generation: int) -> bool:
        return generation == self._generation or generation == self._pending_generation


class _SnapshotSourceGate:
    def __init__(self) -> None:
        self._latest_mtime_ns: int | None = None
        self._accepted_latest_ids: set[tuple[str, int]] = set()

    def accept(self, source: object | None, *, advance: bool = True) -> bool:
        if source is None:
            return True
        mtime_ns = getattr(source, "mtime_ns", None)
        file_id = getattr(source, "file_id", None)
        size = getattr(source, "size", None)
        if not isinstance(mtime_ns, int) or not isinstance(file_id, str):
            return True
        if not isinstance(size, int):
            return True
        identity = (file_id, size)
        if not advance:
            if self._latest_mtime_ns is None:
                return True
            if mtime_ns < self._latest_mtime_ns:
                return False
            if mtime_ns == self._latest_mtime_ns and identity in self._accepted_latest_ids:
                return False
            return True
        if self._latest_mtime_ns is None or mtime_ns > self._latest_mtime_ns:
            self._latest_mtime_ns = mtime_ns
            self._accepted_latest_ids = {identity}
            return True
        if mtime_ns < self._latest_mtime_ns:
            return False
        if identity in self._accepted_latest_ids:
            return False
        self._accepted_latest_ids.add(identity)
        return True


class _SnapshotApplier(Protocol):
    def apply_snapshot(self, snap: Snapshot) -> None: ...


def _schedule_snapshot_apply(callback: Callable[[], None]) -> None:
    if QApplication.instance() is None:
        callback()
        return
    QTimer.singleShot(0, callback)


class _WowSyncStartupConfigurator(QObject):
    _notificationReady = pyqtSignal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        configure: Callable[[bool], object] | None = None,
        runner: Callable[[Callable[[], None]], None] | None = None,
        notify: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._configure = configure or configure_wow_sync_startup
        self._runner = runner
        self._state_lock = threading.Lock()
        self._work_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._generation = 0
        self._desired_enabled: bool | None = None
        self._applied_enabled: bool | None = None
        self._closed = False
        self._close_error: Exception | None = None
        self._threads: set[threading.Thread] = set()
        self._notificationReady.connect(self._run_notification)
        self._notify = notify or self._notificationReady.emit

    @staticmethod
    def _run_notification(callback: Callable[[], None]) -> None:
        callback()

    def request(
        self,
        enabled: bool,
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> int:
        with self._close_lock:
            return self._request_locked(enabled, on_error=on_error)

    def _request_locked(
        self,
        enabled: bool,
        *,
        on_error: Callable[[Exception], None] | None,
    ) -> int:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("WoW startup shortcut configurator is closed")
            self._generation += 1
            generation = self._generation
            self._desired_enabled = enabled

        def _worker() -> None:
            self._apply_generation(generation, enabled, on_error)

        if self._runner is not None:
            self._runner(_worker)
            return generation

        def _tracked_worker() -> None:
            try:
                _worker()
            finally:
                with self._state_lock:
                    self._threads.discard(thread)

        thread = threading.Thread(
            target=_tracked_worker,
            name="ApplicantScoutStartupShortcut",
            daemon=False,
        )
        with self._state_lock:
            self._threads.add(thread)
        try:
            thread.start()
        except Exception:
            with self._state_lock:
                self._threads.discard(thread)
            raise
        return generation

    def _is_current(self, generation: int) -> bool:
        with self._state_lock:
            return not self._closed and self._generation == generation

    def _apply_generation(
        self,
        generation: int,
        enabled: bool,
        on_error: Callable[[Exception], None] | None,
    ) -> None:
        with self._work_lock:
            if not self._is_current(generation):
                return
            with self._state_lock:
                if self._applied_enabled == enabled:
                    return
            try:
                self._configure(enabled)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                if not self._is_current(generation):
                    return
                log.warning("Could not configure WoW lifecycle startup shortcut: %s", exc)
                if on_error is not None:
                    callback = on_error
                    error = exc

                    def _deliver_current_error() -> None:
                        if self._is_current(generation):
                            callback(error)

                    self._notify(_deliver_current_error)
                return
            with self._state_lock:
                self._applied_enabled = enabled

    def close(self) -> Exception | None:
        with self._close_lock:
            with self._state_lock:
                if self._closed:
                    return self._close_error
                self._closed = True
                self._generation += 1
                desired_enabled = self._desired_enabled

            with self._work_lock:
                with self._state_lock:
                    applied_enabled = self._applied_enabled
                if (
                    desired_enabled is not None
                    and applied_enabled != desired_enabled
                ):
                    try:
                        self._configure(desired_enabled)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                        self._close_error = exc
                        log.warning(
                            "Could not reconcile WoW lifecycle startup shortcut during shutdown: %s",
                            exc,
                        )
                    else:
                        with self._state_lock:
                            self._applied_enabled = desired_enabled

            while True:
                with self._state_lock:
                    threads = tuple(self._threads)
                if not threads:
                    break
                for thread in threads:
                    thread.join()
            return self._close_error


def _stop_screenshot_watcher_async(
    watcher: ScreenshotWatcher,
    *,
    stop_runner: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    request_stop = getattr(watcher, "request_stop", None)
    if callable(request_stop):
        request_stop()

    def _worker() -> None:
        try:
            watcher.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not stop previous screenshot watcher: %s", exc)

    if stop_runner is not None:
        stop_runner(_worker)
        return
    threading.Thread(
        target=_worker,
        name="ApplicantScoutWatcherStop",
        daemon=True,
    ).start()


def _quiesce_screenshot_ingestion(
    watcher: ScreenshotWatcher | None,
    signal_gate: _WatcherSignalGate,
    live_snapshot_writer: LiveSnapshotCacheWriter | None,
) -> None:
    if watcher is not None:
        request_stop = getattr(watcher, "request_stop", None)
        if callable(request_stop):
            try:
                request_stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not request screenshot watcher stop: %s", exc)
        apply_queue = getattr(watcher, "_applicant_scout_apply_queue", None)
        flush = getattr(apply_queue, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not flush pending screenshot snapshot: %s", exc)
    signal_gate.invalidate()
    if live_snapshot_writer is not None:
        try:
            live_snapshot_writer.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not close live snapshot cache writer: %s", exc)


def _shutdown_runtime(
    watcher: ScreenshotWatcher | None,
    window: OverlayWindow,
    cache: CharacterCache,
    live_snapshot_writer: LiveSnapshotCacheWriter | None,
    wcl_client: WCLClient,
) -> None:
    """Stop producers, drain WCL work, persist cache, then close shared clients."""
    if watcher is not None:
        try:
            watcher.stop()
        except Exception as exc:  # noqa: BLE001 - terminal cleanup boundary
            log.warning("Could not stop screenshot watcher during shutdown: %s", exc)
    fetches_drained = False
    try:
        fetches_drained = window.shutdown_fetches()
        if not fetches_drained:
            log.error("WCL fetch pool did not fully drain during shutdown.")
    except Exception as exc:  # noqa: BLE001 - terminal cleanup boundary
        log.error("Could not drain WCL fetch pool during shutdown: %s", exc)
    if fetches_drained:
        try:
            if not cache.close():
                log.warning("Could not persist the final WCL character cache snapshot.")
        except Exception as exc:  # noqa: BLE001 - terminal cleanup boundary
            log.warning("Could not close WCL character cache: %s", exc)
    if live_snapshot_writer is not None:
        try:
            live_snapshot_writer.close()
        except Exception as exc:  # noqa: BLE001 - terminal cleanup boundary
            log.warning("Could not close live snapshot cache writer: %s", exc)
    if fetches_drained:
        try:
            wcl_client.close()
        except Exception as exc:  # noqa: BLE001 - terminal cleanup boundary
            log.warning("Could not close WCL client: %s", exc)


def _run_application_event_loop(
    app: QApplication,
    *,
    wow_sync_startup_configurator: _WowSyncStartupConfigurator,
    sync_with_wow: bool,
    watcher: ScreenshotWatcher | None,
    window: OverlayWindow,
    cache: CharacterCache,
    live_snapshot_writer: LiveSnapshotCacheWriter | None,
    wcl_client: WCLClient,
) -> int:
    try:
        wow_sync_startup_configurator.request(sync_with_wow)
    except RuntimeError as exc:
        log.warning("Could not schedule WoW lifecycle startup reconciliation: %s", exc)
    rc = app.exec()
    wow_sync_startup_configurator.close()
    _shutdown_runtime(watcher, window, cache, live_snapshot_writer, wcl_client)
    return rc


_SNAPSHOT_AUTHORITY_LFG = 1 << 0
_SNAPSHOT_AUTHORITY_ROSTER = 1 << 1
_SNAPSHOT_AUTHORITY_VERSION = 1 << 2
_SNAPSHOT_AUTHORITY_LEADER = 1 << 3
_SNAPSHOT_AUTHORITY_LISTING_SEED = 1 << 4


def _snapshot_carries_leader_update(snap: object) -> bool:
    if bool(getattr(snap, "terminal_clear", False)) or not bool(
        getattr(snap, "lfg_unavailable", False)
    ):
        return True
    leader_key = getattr(snap, "leader_key", None)
    key_level = getattr(leader_key, "key_level", None)
    return isinstance(key_level, int) and key_level > 0


def _snapshot_authority_mask(snap: object) -> int:
    terminal_clear = bool(getattr(snap, "terminal_clear", False))
    lfg_unavailable = bool(getattr(snap, "lfg_unavailable", False))
    roster_unavailable = bool(getattr(snap, "roster_unavailable", False))
    authority = 0
    if terminal_clear or not lfg_unavailable:
        authority |= (
            _SNAPSHOT_AUTHORITY_LFG
            | _SNAPSHOT_AUTHORITY_LEADER
            | _SNAPSHOT_AUTHORITY_LISTING_SEED
        )
    else:
        # A restricted LFG read can still carry independently useful context.
        if _snapshot_carries_leader_update(snap):
            authority |= _SNAPSHOT_AUTHORITY_LEADER
        if getattr(snap, "listing", None) is not None:
            authority |= _SNAPSHOT_AUTHORITY_LISTING_SEED
    if terminal_clear or not roster_unavailable:
        authority |= _SNAPSHOT_AUTHORITY_ROSTER
    if getattr(snap, "version", None) is not None:
        authority |= _SNAPSHOT_AUTHORITY_VERSION
    return authority


def _compact_snapshot_segment(snapshots: tuple[object, ...]) -> tuple[object, ...]:
    """Keep the newest observation plus each older snapshot with unique authority."""
    covered = 0
    retained_reversed: list[object] = []
    for snap in reversed(snapshots):
        authority = _snapshot_authority_mask(snap)
        if not retained_reversed or authority & ~covered:
            retained_reversed.append(snap)
            covered |= authority
    retained_reversed.reverse()
    return tuple(retained_reversed)


def _append_pending_snapshot(
    pending: tuple[object, ...],
    snap: object,
) -> tuple[object, ...]:
    # A clear invalidates every earlier state event, but must itself survive a
    # later partial/full frame so listing-session and waiter cleanup still runs.
    if bool(getattr(snap, "terminal_clear", False)):
        return (snap,)
    if pending and bool(getattr(pending[0], "terminal_clear", False)):
        return (pending[0],) + _compact_snapshot_segment(pending[1:] + (snap,))
    return _compact_snapshot_segment(pending + (snap,))


def _merge_snapshot_segment(snapshots: tuple[Snapshot, ...]) -> Snapshot:
    """Compose final in-memory authority without fabricating a cache snapshot."""
    latest = snapshots[-1]
    lfg_source = next(
        (snap for snap in reversed(snapshots) if not snap.lfg_unavailable),
        None,
    )
    roster_source = next(
        (snap for snap in reversed(snapshots) if not snap.roster_unavailable),
        None,
    )
    version = next(
        (snap.version for snap in reversed(snapshots) if snap.version is not None),
        None,
    )
    leader_source = next(
        (
            snap
            for snap in reversed(snapshots)
            if _snapshot_carries_leader_update(snap)
        ),
        None,
    )
    listing_seed_source = None
    if lfg_source is None:
        listing_seed_source = next(
            (snap for snap in reversed(snapshots) if snap.listing is not None),
            None,
        )
    return replace(
        latest,
        listing=(
            lfg_source.listing
            if lfg_source is not None
            else (
                listing_seed_source.listing
                if listing_seed_source is not None
                else None
            )
        ),
        version=version,
        leader_key=(leader_source.leader_key if leader_source is not None else None),
        applicants=list(lfg_source.applicants) if lfg_source is not None else [],
        roster=list(roster_source.roster) if roster_source is not None else [],
        terminal_clear=False,
        lfg_unavailable=lfg_source is None,
        roster_unavailable=roster_source is None,
    )


def _snapshot_application_plan(
    snapshots: tuple[object, ...],
    cache_snapshots: tuple[Snapshot, ...],
) -> tuple[tuple[object, tuple[object, ...]], ...]:
    """Build state-application steps while retaining original cache inputs."""
    typed_snapshots = tuple(
        snap for snap in snapshots if isinstance(snap, Snapshot)
    )
    if len(typed_snapshots) != len(snapshots):
        return tuple((snap, (snap,)) for snap in snapshots)

    steps: list[tuple[object, tuple[object, ...]]] = []
    segment = typed_snapshots
    cache_segment = cache_snapshots
    if segment and segment[0].terminal_clear:
        terminal = segment[0]
        segment = segment[1:]
        cache_terminal: tuple[object, ...] = ()
        if cache_segment and cache_segment[0].terminal_clear:
            cache_terminal = (cache_segment[0],)
            cache_segment = cache_segment[1:]
        planned_terminal = terminal
        if segment and any(snap.version is not None for snap in segment):
            planned_terminal = replace(terminal, version=None)
        steps.append((planned_terminal, cache_terminal))
    if segment:
        steps.append((_merge_snapshot_segment(segment), tuple(cache_segment)))
    return tuple(steps)


class _SnapshotApplyQueue:
    def __init__(
        self,
        machine: _SnapshotApplier,
        window: OverlayWindow,
        decode_failed_callback: Callable[[str, str], None],
        *,
        signal_gate: _WatcherSignalGate,
        generation: int,
        live_snapshot_cache_writer: LiveSnapshotCacheWriter | None = None,
        scheduler: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._machine = machine
        self._window = window
        self._decode_failed_callback = decode_failed_callback
        self._signal_gate = signal_gate
        self._generation = generation
        self._live_snapshot_cache_writer = live_snapshot_cache_writer
        self._scheduler = scheduler or _schedule_snapshot_apply
        self._pending: tuple[str, tuple[object, ...]] | None = None
        self._pending_cache_snapshots: tuple[Snapshot, ...] = ()
        self._flush_pending = False

    def enqueue_snapshot(self, snap: object) -> None:
        pending_snapshots: tuple[object, ...] = ()
        if self._pending is not None and self._pending[0] == "snapshot":
            pending_snapshots = self._pending[1]
        else:
            self._pending_cache_snapshots = ()
        if isinstance(snap, Snapshot):
            if snap.terminal_clear:
                self._pending_cache_snapshots = (snap,)
            else:
                self._pending_cache_snapshots += (snap,)
        self._pending = (
            "snapshot",
            _append_pending_snapshot(pending_snapshots, snap),
        )
        self._schedule_flush()

    def enqueue_decode_failed(self, path: str, reason: str) -> None:
        # WHY: a decode failure has no usable state. If a valid snapshot is
        # already waiting for the GUI flush, keep it rather than turning a
        # good-frame-plus-bad-frame burst into no state update.
        if self._pending is not None and self._pending[0] == "snapshot":
            return
        self._pending = ("decode_failed", (path, reason))
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_pending:
            return
        self._flush_pending = True
        self._scheduler(self.flush)

    def flush(self) -> None:
        if self._pending is None:
            self._flush_pending = False
            return
        kind, args = self._pending
        cache_snapshots = self._pending_cache_snapshots
        self._pending = None
        self._pending_cache_snapshots = ()
        self._flush_pending = False
        if not self._signal_gate.is_current(self._generation):
            return
        if kind == "snapshot":
            latest_snap = args[-1]
            getattr(self._window, "note_decode", lambda *_args: None)(latest_snap)
            for snap, step_cache_snapshots in _snapshot_application_plan(
                args,
                cache_snapshots,
            ):
                if not self._signal_gate.is_current(self._generation):
                    return
                getattr(self._machine, "apply_snapshot", lambda *_args: None)(snap)
                if not self._signal_gate.is_current(self._generation):
                    return
                if self._live_snapshot_cache_writer is not None:
                    for cache_snap in step_cache_snapshots:
                        if isinstance(cache_snap, Snapshot):
                            self._live_snapshot_cache_writer.submit(cache_snap)
                getattr(
                    self._window,
                    "note_snapshot_applied",
                    lambda *_args: None,
                )(snap)
            return
        path, reason = args
        self._decode_failed_callback(str(path), str(reason))
        getattr(self._window, "note_decode_failed", lambda *_args: None)(
            str(path),
            str(reason),
        )


def _connect_screenshot_watcher(
    watcher: ScreenshotWatcher,
    machine: _SnapshotApplier,
    window: OverlayWindow,
    decode_failed_callback: Callable[[str, str], None],
    *,
    signal_gate: _WatcherSignalGate,
    source_gate: _SnapshotSourceGate,
    generation: int,
    live_snapshot_cache_writer: LiveSnapshotCacheWriter | None = None,
    scheduler: Callable[[Callable[[], None]], None] | None = None,
) -> _SnapshotApplyQueue:
    apply_queue = _SnapshotApplyQueue(
        machine,
        window,
        decode_failed_callback,
        signal_gate=signal_gate,
        generation=generation,
        live_snapshot_cache_writer=live_snapshot_cache_writer,
        scheduler=scheduler,
    )

    def _snapshot_if_current(snap: object) -> None:
        if not signal_gate.is_current(generation):
            return
        if not source_gate.accept(getattr(snap, "source", None)):
            return
        apply_queue.enqueue_snapshot(snap)

    def _decode_failed_if_current(
        path: str,
        reason: str,
        source: object | None = None,
    ) -> None:
        if not signal_gate.is_current(generation):
            return
        if not source_gate.accept(source, advance=False):
            return
        apply_queue.enqueue_decode_failed(path, reason)

    watcher.snapshotReceived.connect(_snapshot_if_current)
    watcher.decodeFailed.connect(_decode_failed_if_current)
    return apply_queue


def _replace_screenshot_watcher(
    current_watcher: ScreenshotWatcher | None,
    screenshots_dir: Path,
    machine: _SnapshotApplier,
    window: OverlayWindow,
    decode_failed_callback: Callable[[str, str], None],
    *,
    cache_dir: Path | None = None,
    signal_gate: _WatcherSignalGate,
    live_snapshot_cache_writer: LiveSnapshotCacheWriter | None = None,
    stop_runner: Callable[[Callable[[], None]], None] | None = None,
) -> ScreenshotWatcher:
    previous_generation = signal_gate.generation
    if cache_dir is None:
        new_watcher = ScreenshotWatcher(screenshots_dir)
    else:
        new_watcher = ScreenshotWatcher(screenshots_dir, cache_dir=cache_dir)
    generation = signal_gate.prepare_next()
    source_gate = _SnapshotSourceGate()
    apply_queue = _connect_screenshot_watcher(
        new_watcher,
        machine,
        window,
        decode_failed_callback,
        signal_gate=signal_gate,
        source_gate=source_gate,
        generation=generation,
        live_snapshot_cache_writer=live_snapshot_cache_writer,
    )
    setattr(new_watcher, "_applicant_scout_apply_queue", apply_queue)
    try:
        new_watcher.start()
    except Exception:
        signal_gate.restore(previous_generation)
        try:
            new_watcher.stop()
        except Exception as cleanup_exc:  # noqa: BLE001
            log.warning("Could not clean up failed screenshot watcher: %s", cleanup_exc)
        raise
    signal_gate.commit(generation)
    if current_watcher is not None:
        # WHY: watchdog Observer.stop()+join can block for seconds on Windows
        # storage paths. The generation gate above already ignores stale signals.
        _stop_screenshot_watcher_async(current_watcher, stop_runner=stop_runner)
    return new_watcher


def _replace_screenshots_runtime(
    current_watcher: ScreenshotWatcher | None,
    screenshots_dir: Path,
    machine: StateMachine,
    window: OverlayWindow,
    decode_failed_callback: Callable[[str, str], None],
    *,
    cache_dir: Path | None = None,
    signal_gate: _WatcherSignalGate,
    live_snapshot_cache_writer: LiveSnapshotCacheWriter | None = None,
    stop_runner: Callable[[Callable[[], None]], None] | None = None,
) -> ScreenshotWatcher:
    previous_reader = getattr(machine, "_rio_reader", None)
    if cache_dir is None:
        next_reader = _raiderio_reader_for_screenshots_path(screenshots_dir)
    else:
        next_reader = _raiderio_reader_for_screenshots_path(
            screenshots_dir,
            cache_dir=cache_dir,
        )
    try:
        watcher = _replace_screenshot_watcher(
            current_watcher,
            screenshots_dir,
            _ReaderBoundMachine(machine, next_reader),
            window,
            decode_failed_callback,
            cache_dir=cache_dir,
            signal_gate=signal_gate,
            live_snapshot_cache_writer=live_snapshot_cache_writer,
            stop_runner=stop_runner,
        )
    except Exception:
        machine.set_rio_reader(previous_reader)
        raise
    machine.set_rio_reader(next_reader)
    _preload_machine_rio_region(machine)
    return watcher


def _raiderio_reader_for_screenshots_path(
    path: Path,
    *,
    cache_dir: Path | None = None,
) -> RaiderIOLocalReader | None:
    retail_root = retail_root_from_screenshots_path(path)
    if retail_root is None:
        return None
    return RaiderIOLocalReader(retail_root, cache_dir=cache_dir)


class _ReaderBoundMachine:
    def __init__(self, machine: StateMachine, rio_reader: Any | None):
        self._machine = machine
        self._rio_reader = rio_reader

    def apply_snapshot(self, snap: Snapshot) -> None:
        current_reader = getattr(self._machine, "_rio_reader", None)
        switched = current_reader is not self._rio_reader
        if switched:
            self._machine.set_rio_reader(self._rio_reader)
        try:
            self._machine.apply_snapshot(snap)
        finally:
            if switched:
                self._machine.set_rio_reader(current_reader)


def _preload_machine_rio_region(machine: StateMachine) -> None:
    player = getattr(getattr(machine, "_state", None), "player", None)
    region_token = REGION_ID_TO_WCL.get(getattr(player, "region_id", 0))
    preload = getattr(machine, "_preload_local_rio_region", None)
    if callable(preload):
        preload(region_token)


def _restore_live_snapshot_cache(
    cache_dir: Path,
    machine: StateMachine,
    window: OverlayWindow,
    *,
    grace_seconds: float = LIVE_SNAPSHOT_RESTORE_GRACE_SECONDS,
) -> bool:
    restored = load_live_snapshot(cache_dir)
    if restored is None:
        return False
    getattr(window, "note_restored_snapshot", lambda *_args: None)(
        restored.snapshot,
        restored.saved_at,
        grace_seconds,
    )
    machine.apply_snapshot(restored.snapshot)

    def _expire_restored_snapshot() -> None:
        if not window.restored_snapshot_pending():
            return
        applicants_pending, roster_pending = (
            window.restored_snapshot_pending_surfaces()
        )
        machine.expire_restored_snapshot_surfaces(
            applicants=bool(applicants_pending),
            roster=bool(roster_pending),
        )
        getattr(window, "note_restored_snapshot_expired", lambda: None)()
        clear_live_snapshot_if_saved_at(cache_dir, restored.saved_at)

    QTimer.singleShot(max(0, int(grace_seconds * 1000)), _expire_restored_snapshot)
    return True


def _update_result_has_installable_asset(result: object) -> bool:
    asset_name = getattr(result, "asset_name", None)
    if not isinstance(asset_name, str):
        return False
    asset_url = getattr(result, "asset_url", None)
    checksum_name = getattr(result, "checksum_name", None)
    checksum_url = getattr(result, "checksum_url", None)
    metadata = (asset_name, asset_url, checksum_name, checksum_url)
    if not all(isinstance(value, str) and value.strip() for value in metadata):
        return False
    if "/" in asset_name or "\\" in asset_name:
        return False
    normalized = asset_name.lower()
    return normalized.startswith("applicantscoutcompanionsetup-") and normalized.endswith(
        ".exe"
    )


def _settings_env_override_keys() -> list[str]:
    keys = (
        "WCL_CLIENT_ID",
        "WCL_CLIENT_SECRET",
        "APSCOUT_DRAFT_WCL_CLIENT_ID",
        "APSCOUT_DRAFT_WCL_CLIENT_SECRET",
        "APSCOUT_REGION",
        "APSCOUT_SCREENSHOTS_PATH",
        "APSCOUT_FETCH_MPLUS",
        "APSCOUT_FETCH_RAID_NORMAL",
        "APSCOUT_FETCH_RAID_HEROIC",
        "APSCOUT_FETCH_RAID_MYTHIC",
        "APSCOUT_CACHE_TTL_SECONDS",
        "APSCOUT_SYNC_WITH_WOW",
    )
    return [key for key in keys if os.environ.get(key) is not None]


def _process_env_bool_override(key: str, current: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return current
    value = raw.strip().lower()
    if not value:
        return current
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be one of: 1, 0, true, false, yes, no, on, off")


def _parse_saved_bool(key: str, raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be one of: 1, 0, true, false, yes, no, on, off")


def _saved_config_value_for_process_override(
    saved_values: dict[str, str],
    key: str,
    current: str,
) -> str:
    if os.environ.get(key) is None:
        return current
    return saved_values.get(key, "").strip()


def _saved_config_bool_for_process_override(
    saved_values: dict[str, str],
    key: str,
    current: bool,
    *,
    default: bool,
) -> bool:
    if os.environ.get(key) is None:
        return current
    try:
        return _parse_saved_bool(key, saved_values.get(key), default=default)
    except ConfigError:
        return current


def _saved_config_cache_ttl_for_process_override(
    saved_values: dict[str, str],
    current: int | None,
) -> int | None:
    if os.environ.get("APSCOUT_CACHE_TTL_SECONDS") is None:
        return current
    try:
        return _parse_cache_ttl_seconds(saved_values.get("APSCOUT_CACHE_TTL_SECONDS"))
    except ConfigError:
        return None


def _apply_process_env_overrides_to_config(cfg: Config) -> Config:
    screenshots_override = os.environ.get("APSCOUT_SCREENSHOTS_PATH")
    region_override = os.environ.get("APSCOUT_REGION")
    screenshots_path = cfg.screenshots_path
    if screenshots_override is not None:
        clean_screenshots_path = screenshots_override.strip()
        screenshots_path = Path(clean_screenshots_path) if clean_screenshots_path else None
    metric_preferences = validate_metric_preferences(
        MetricPreferences(
            mplus=_process_env_bool_override(
                "APSCOUT_FETCH_MPLUS", cfg.metric_preferences.mplus
            ),
            raid_normal=_process_env_bool_override(
                "APSCOUT_FETCH_RAID_NORMAL", cfg.metric_preferences.raid_normal
            ),
            raid_heroic=_process_env_bool_override(
                "APSCOUT_FETCH_RAID_HEROIC", cfg.metric_preferences.raid_heroic
            ),
            raid_mythic=_process_env_bool_override(
                "APSCOUT_FETCH_RAID_MYTHIC", cfg.metric_preferences.raid_mythic
            ),
        )
    )
    return replace(
        cfg,
        wcl_client_id=os.environ.get("WCL_CLIENT_ID", cfg.wcl_client_id).strip(),
        wcl_client_secret=os.environ.get(
            "WCL_CLIENT_SECRET", cfg.wcl_client_secret
        ).strip(),
        draft_wcl_client_id=os.environ.get(
            "APSCOUT_DRAFT_WCL_CLIENT_ID", cfg.draft_wcl_client_id
        ).strip(),
        draft_wcl_client_secret=os.environ.get(
            "APSCOUT_DRAFT_WCL_CLIENT_SECRET", cfg.draft_wcl_client_secret
        ).strip(),
        region=normalize_wcl_region(
            region_override if region_override is not None else cfg.region
        ),
        screenshots_path=screenshots_path,
        metric_preferences=metric_preferences,
        sync_with_wow=_process_env_bool_override(
            "APSCOUT_SYNC_WITH_WOW", cfg.sync_with_wow
        ),
    )


_PATH_WARNING_UNSET = object()


def _settings_saved_status(
    values,
    override_keys: list[str],
    *,
    path_warning: str | None | object = _PATH_WARNING_UNSET,
) -> tuple[str, bool]:
    if path_warning is _PATH_WARNING_UNSET:
        resolved_path_warning = (
            screenshots_path_health_warning(Path(values.screenshots_path))
            if values.screenshots_path
            else None
        )
    elif isinstance(path_warning, str):
        resolved_path_warning = path_warning
    else:
        resolved_path_warning = None
    if override_keys:
        message = (
            "Saved for this app session, but environment overrides are active: "
            + ", ".join(override_keys)
        )
        if resolved_path_warning:
            message = f"{message}. {resolved_path_warning}"
        return message, True
    if resolved_path_warning:
        return resolved_path_warning, True
    return "Saved.", False


def _has_pending_wcl_credentials(cfg: Config) -> bool:
    return bool(cfg.draft_wcl_client_id or cfg.draft_wcl_client_secret)


def _settings_autosave_status(
    values,
    override_keys: list[str],
    cfg: Config,
    *,
    path_warning: str | None | object = _PATH_WARNING_UNSET,
) -> tuple[str, bool, bool]:
    saved_text, saved_error = _settings_saved_status(
        values,
        override_keys,
        path_warning=path_warning,
    )
    if not _has_pending_wcl_credentials(cfg):
        return saved_text, saved_error, False
    pending_text = (
        "WCL credential changes are pending validation; "
        "click Test WCL to activate them."
    )
    return f"{saved_text} {pending_text}", saved_error, not saved_error


def _settings_wcl_test_success_status(
    values,
    override_keys: list[str],
    *,
    path_warning: str | None | object = _PATH_WARNING_UNSET,
) -> tuple[str, bool]:
    saved_text, saved_error = _settings_saved_status(
        values,
        override_keys,
        path_warning=path_warning,
    )
    if not saved_error:
        return "WCL credentials are valid.", False
    session_override_prefix = "Saved for this app session, but "
    if saved_text.startswith(session_override_prefix):
        return (
            "WCL credentials are valid, but "
            + saved_text.removeprefix(session_override_prefix),
            True,
        )
    return f"WCL credentials are valid. {saved_text}", True


def _persist_settings_values(
    cfg: Config,
    values,
    *,
    apply_credentials: bool = True,
) -> Path:
    saved_values = read_user_config_values(cfg.config_path) if cfg.config_path else {}
    wcl_env_blocks_active_credentials = (
        os.environ.get("WCL_CLIENT_ID") is not None
        or os.environ.get("WCL_CLIENT_SECRET") is not None
    )
    credentials_changed = (
        values.wcl_client_id != cfg.wcl_client_id
        or values.wcl_client_secret != cfg.wcl_client_secret
    )
    if apply_credentials and wcl_env_blocks_active_credentials:
        active_client_id = cfg.wcl_client_id
        active_client_secret = cfg.wcl_client_secret
        draft_client_id = values.wcl_client_id
        draft_client_secret = values.wcl_client_secret
    elif apply_credentials:
        active_client_id = values.wcl_client_id
        active_client_secret = values.wcl_client_secret
        draft_client_id = ""
        draft_client_secret = ""
    elif credentials_changed:
        active_client_id = cfg.wcl_client_id
        active_client_secret = cfg.wcl_client_secret
        draft_client_id = values.wcl_client_id
        draft_client_secret = values.wcl_client_secret
    else:
        active_client_id = cfg.wcl_client_id
        active_client_secret = cfg.wcl_client_secret
        draft_client_id = ""
        draft_client_secret = ""
    active_client_id = _saved_config_value_for_process_override(
        saved_values,
        "WCL_CLIENT_ID",
        active_client_id,
    )
    active_client_secret = _saved_config_value_for_process_override(
        saved_values,
        "WCL_CLIENT_SECRET",
        active_client_secret,
    )
    draft_client_id = _saved_config_value_for_process_override(
        saved_values,
        "APSCOUT_DRAFT_WCL_CLIENT_ID",
        draft_client_id,
    )
    draft_client_secret = _saved_config_value_for_process_override(
        saved_values,
        "APSCOUT_DRAFT_WCL_CLIENT_SECRET",
        draft_client_secret,
    )
    region = _saved_config_value_for_process_override(
        saved_values,
        "APSCOUT_REGION",
        values.region,
    )
    screenshots_path = _saved_config_value_for_process_override(
        saved_values,
        "APSCOUT_SCREENSHOTS_PATH",
        values.screenshots_path,
    )
    metric_preferences = MetricPreferences(
        mplus=_saved_config_bool_for_process_override(
            saved_values,
            "APSCOUT_FETCH_MPLUS",
            values.metric_preferences.mplus,
            default=DEFAULT_METRIC_PREFERENCES.mplus,
        ),
        raid_normal=_saved_config_bool_for_process_override(
            saved_values,
            "APSCOUT_FETCH_RAID_NORMAL",
            values.metric_preferences.raid_normal,
            default=DEFAULT_METRIC_PREFERENCES.raid_normal,
        ),
        raid_heroic=_saved_config_bool_for_process_override(
            saved_values,
            "APSCOUT_FETCH_RAID_HEROIC",
            values.metric_preferences.raid_heroic,
            default=DEFAULT_METRIC_PREFERENCES.raid_heroic,
        ),
        raid_mythic=_saved_config_bool_for_process_override(
            saved_values,
            "APSCOUT_FETCH_RAID_MYTHIC",
            values.metric_preferences.raid_mythic,
            default=DEFAULT_METRIC_PREFERENCES.raid_mythic,
        ),
    )
    sync_with_wow = _saved_config_bool_for_process_override(
        saved_values,
        "APSCOUT_SYNC_WITH_WOW",
        values.sync_with_wow,
        default=False,
    )
    cache_ttl_seconds = _saved_config_cache_ttl_for_process_override(
        saved_values,
        cfg.cache_ttl_seconds,
    )
    chatlog_path = (
        _saved_config_value_for_process_override(
            saved_values,
            "APSCOUT_CHATLOG_PATH",
            str(cfg.chatlog_path),
        )
        if not screenshots_path
        else ""
    )
    return save_config_values(
        wcl_client_id=active_client_id,
        wcl_client_secret=active_client_secret,
        draft_wcl_client_id=draft_client_id,
        draft_wcl_client_secret=draft_client_secret,
        region=region,
        screenshots_path=screenshots_path,
        cache_ttl_seconds=cache_ttl_seconds,
        metric_preferences=metric_preferences,
        sync_with_wow=sync_with_wow,
        chatlog_path=chatlog_path,
        config_path=cfg.config_path,
    )


def _persist_config_snapshot(cfg: Config) -> Path:
    saved_values = read_user_config_values(cfg.config_path) if cfg.config_path else {}
    cache_ttl_seconds = _saved_config_cache_ttl_for_process_override(
        saved_values,
        cfg.cache_ttl_seconds,
    )
    return save_config_values(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        draft_wcl_client_id=cfg.draft_wcl_client_id,
        draft_wcl_client_secret=cfg.draft_wcl_client_secret,
        region=cfg.region,
        screenshots_path=str(cfg.screenshots_path) if cfg.screenshots_path else "",
        cache_ttl_seconds=cache_ttl_seconds,
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=cfg.sync_with_wow,
        chatlog_path="" if cfg.screenshots_path else str(cfg.chatlog_path),
        config_path=cfg.config_path,
    )


def _settings_values_to_config(
    cfg: Config,
    values,
    *,
    apply_credentials: bool = True,
) -> Config:
    credentials_changed = (
        values.wcl_client_id != cfg.wcl_client_id
        or values.wcl_client_secret != cfg.wcl_client_secret
    )
    if apply_credentials:
        active_client_id = values.wcl_client_id
        active_client_secret = values.wcl_client_secret
        draft_client_id = ""
        draft_client_secret = ""
    elif credentials_changed:
        active_client_id = cfg.wcl_client_id
        active_client_secret = cfg.wcl_client_secret
        draft_client_id = values.wcl_client_id
        draft_client_secret = values.wcl_client_secret
    else:
        active_client_id = cfg.wcl_client_id
        active_client_secret = cfg.wcl_client_secret
        draft_client_id = ""
        draft_client_secret = ""
    screenshots_text = str(values.screenshots_path or "").strip()
    return Config(
        wcl_client_id=active_client_id,
        wcl_client_secret=active_client_secret,
        chatlog_path=cfg.chatlog_path,
        region=normalize_wcl_region(values.region),
        cache_dir=cfg.cache_dir,
        config_dir=cfg.config_dir,
        screenshots_path=Path(screenshots_text) if screenshots_text else None,
        cache_ttl_seconds=cfg.cache_ttl_seconds,
        config_path=cfg.config_path,
        log_dir=cfg.log_dir,
        metric_preferences=values.metric_preferences,
        sync_with_wow=bool(values.sync_with_wow),
        draft_wcl_client_id=draft_client_id,
        draft_wcl_client_secret=draft_client_secret,
    )


def _apply_settings_change(
    *,
    app: QApplication,
    cfg: Config,
    values,
    apply_credentials: bool,
    auth,
    wcl_client,
    region_runtime: _WCLRegionRuntime,
    window,
    watcher,
    current_screenshots_dir: Path,
    machine,
    decode_failed_callback: Callable[[str, str], None],
    signal_gate: _WatcherSignalGate,
    wow_exit_timer,
    quit_app: Callable[[], None],
    can_quit: Callable[[], bool],
    prepare_quit: Callable[[], bool] | None = None,
    live_snapshot_cache_writer: LiveSnapshotCacheWriter | None = None,
) -> _SettingsApplyResult:
    old_cfg = cfg
    new_cfg = _settings_values_to_config(
        old_cfg,
        values,
        apply_credentials=apply_credentials,
    )
    new_cfg = _apply_process_env_overrides_to_config(new_cfg)
    new_screenshots_dir = resolve_screenshots_path(new_cfg)
    _persist_settings_values(
        old_cfg,
        values,
        apply_credentials=apply_credentials,
    )
    new_wow_exit_timer = wow_exit_timer
    wow_sync_changed = old_cfg.sync_with_wow != new_cfg.sync_with_wow
    new_watcher = watcher
    try:
        if wow_sync_changed:
            new_wow_exit_timer = _apply_wow_sync_runtime(
                app,
                new_cfg.sync_with_wow,
                wow_exit_timer,
                quit_app=quit_app,
                can_quit=can_quit,
                prepare_quit=prepare_quit,
                configure_startup=False,
            )
        if new_screenshots_dir != current_screenshots_dir:
            new_watcher = _replace_screenshots_runtime(
                watcher,
                new_screenshots_dir,
                machine,
                window,
                decode_failed_callback,
                cache_dir=new_cfg.cache_dir,
                signal_gate=signal_gate,
                live_snapshot_cache_writer=live_snapshot_cache_writer,
            )
    except Exception:
        if wow_sync_changed:
            try:
                _apply_wow_sync_runtime(
                    app,
                    old_cfg.sync_with_wow,
                    new_wow_exit_timer,
                    quit_app=quit_app,
                    can_quit=can_quit,
                    prepare_quit=prepare_quit,
                    configure_startup=False,
                )
            except Exception as rollback_exc:  # noqa: BLE001
                log.warning("Could not roll back WoW sync runtime: %s", rollback_exc)
        try:
            _persist_config_snapshot(old_cfg)
        except Exception as rollback_exc:  # noqa: BLE001
            log.warning("Could not roll back settings file: %s", rollback_exc)
        raise

    credentials_promoted = apply_credentials and (
        old_cfg.wcl_client_id != new_cfg.wcl_client_id
        or old_cfg.wcl_client_secret != new_cfg.wcl_client_secret
    )
    credentials_validated_for_active = apply_credentials and (
        values.wcl_client_id.strip() == new_cfg.wcl_client_id
        and values.wcl_client_secret.strip() == new_cfg.wcl_client_secret
    )
    region_effective_changed = region_runtime.set_fallback(new_cfg.region)
    wcl_runtime_changed = credentials_promoted or region_effective_changed
    new_auth = auth
    if new_cfg.wcl_client_id and new_cfg.wcl_client_secret and credentials_promoted:
        new_auth = WCLAuth(
            new_cfg.wcl_client_id,
            new_cfg.wcl_client_secret,
            new_cfg.cache_dir,
        )
        wcl_client.reconfigure_auth(new_auth, validated=True)
    elif credentials_validated_for_active:
        wcl_client.mark_active_auth_validated()
    if wcl_runtime_changed:
        wcl_client.region = region_runtime.effective_region
        window.apply_metric_preferences(
            new_cfg.metric_preferences,
            refetch_missing=False,
        )
        window.bump_wcl_runtime_generation()
    else:
        window.apply_metric_preferences(new_cfg.metric_preferences)

    return _SettingsApplyResult(
        cfg=new_cfg,
        auth=new_auth,
        watcher=new_watcher,
        current_screenshots_dir=new_screenshots_dir,
        wow_exit_timer=new_wow_exit_timer,
        overrides=_settings_env_override_keys(),
    )


def _run_first_run_settings(
    cfg: Config,
    *,
    update_quit_gate: _UpdateQuitGate,
    character_cache: CharacterCache | None = None,
) -> bool:
    def _check_updates_with_handoff() -> SettingsUpdateResult | tuple[str, str | None]:
        update_quit_gate.set_update_in_progress(True)
        keep_handoff = False
        try:
            result = _check_updates(update_quit_gate=update_quit_gate)
            keep_handoff = (
                isinstance(result, SettingsUpdateResult) and result.installer_handoff
            )
            return result
        finally:
            if not keep_handoff:
                update_quit_gate.set_update_in_progress(False)

    dialog = SettingsDialog(
        cfg,
        first_run=True,
        credential_tester=lambda client_id, client_secret, region: _test_wcl_credentials(
            cfg.cache_dir,
            client_id,
            client_secret,
            region,
        ),
        open_logs=lambda: _open_log_dir(cfg.log_dir or user_log_dir()),
        clear_cache=lambda: _clear_cache_dir(cfg.cache_dir, character_cache),
        check_updates=_check_updates_with_handoff,
    )
    dialog.setWindowIcon(_app_icon())
    _connect_release_notes_dialog_action(dialog)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    values = dialog.values()
    try:
        configure_wow_sync_startup(values.sync_with_wow)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log.warning("Could not configure WoW lifecycle startup shortcut: %s", exc)
        if not values.sync_with_wow:
            QMessageBox.warning(
                None,
                "ApplicantScout settings",
                "Settings were saved, but the WoW startup shortcut could not be "
                f"updated: {exc}",
            )
            _persist_settings_values(cfg, values)
            _stop_current_session_watcher_best_effort()
            return True
        QMessageBox.warning(
            None,
            "ApplicantScout settings",
            f"Settings were not saved because the WoW startup shortcut could not be updated: {exc}",
        )
        return False
    try:
        _persist_settings_values(cfg, values)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist first-run settings: %s", exc)
        rollback_error = ""
        try:
            configure_wow_sync_startup(cfg.sync_with_wow)
        except Exception as rollback_exc:  # noqa: BLE001
            rollback_error = (
                " The WoW startup shortcut rollback also failed: "
                f"{rollback_exc}"
            )
            log.warning(
                "Could not roll back WoW lifecycle startup shortcut after "
                "settings save failure: %s",
                rollback_exc,
            )
        QMessageBox.warning(
            None,
            "ApplicantScout settings",
            "Settings were not saved because the config file could not be "
            f"updated: {exc}.{rollback_error}",
        )
        return False
    if values.sync_with_wow:
        try:
            start_wow_sync_watcher(check_existing=False)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            log.warning("Could not start WoW lifecycle watcher: %s", exc)
            QMessageBox.warning(
                None,
                "ApplicantScout settings",
                "Settings were saved, but the current-session WoW watcher "
                f"could not be started: {exc}",
            )
    else:
        _stop_current_session_watcher_best_effort()
    return True


def _run_settings_dialog(
    cfg: Config,
    *,
    first_run: bool,
    update_quit_gate: _UpdateQuitGate,
    character_cache: CharacterCache | None = None,
) -> bool:
    if not first_run:
        raise RuntimeError("Normal settings are modeless; use _show_settings.")
    return _run_first_run_settings(
        cfg,
        update_quit_gate=update_quit_gate,
        character_cache=character_cache,
    )


def _load_startup_config(
    *, update_quit_gate: _UpdateQuitGate
) -> tuple[Config, Path, bool] | None:
    startup_settings_shown = False
    try:
        cfg = load_config()
    except ConfigError as exc:
        _show_config_error(str(exc))
        return None
    while True:
        if not is_config_ready(cfg):
            if not _run_settings_dialog(
                cfg,
                first_run=True,
                update_quit_gate=update_quit_gate,
            ):
                return None
            startup_settings_shown = True
            try:
                cfg = load_config()
            except ConfigError as exc:
                _show_config_error(str(exc))
                return None
            continue
        try:
            return cfg, resolve_screenshots_path(cfg), startup_settings_shown
        except ConfigError as exc:
            if os.environ.get("APSCOUT_SCREENSHOTS_PATH") is not None:
                _show_config_error(
                    f"{exc}\n\n"
                    "APSCOUT_SCREENSHOTS_PATH is set in the process environment. "
                    "Correct or remove that environment override before saved "
                    "settings can take effect."
                )
                return None
            QMessageBox.warning(None, "ApplicantScout setup", str(exc))
            if not _run_settings_dialog(
                cfg,
                first_run=True,
                update_quit_gate=update_quit_gate,
            ):
                return None
            startup_settings_shown = True
            try:
                cfg = load_config()
            except ConfigError as load_exc:
                _show_config_error(str(load_exc))
                return None


def _positive_cleanup_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _system_exit_code(code: object) -> int:
    return code if isinstance(code, int) else 1


def _run_cleanup_screenshots_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="applicant-scout cleanup-screenshots")
    parser.add_argument("screenshots_dir", nargs="?")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--limit", type=_positive_cleanup_limit)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return _system_exit_code(exc.code)

    if args.screenshots_dir:
        screenshots_dir = Path(args.screenshots_dir)
    else:
        try:
            screenshots_dir = resolve_screenshots_path(load_config())
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    try:
        summary = cleanup_appscout_screenshots(
            screenshots_dir,
            delete=args.delete,
            limit=args.limit,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(format_screenshot_cleanup_summary(summary, delete=args.delete))
    return screenshot_cleanup_exit_code(summary)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == SCREENSHOTS_PATH_PROBE_ARG:
        if len(args) != 3:
            return 2
        return run_screenshots_path_probe_command(args[1], args[2])
    _setup_logging()
    if args and args[0] == "cleanup-screenshots":
        return _run_cleanup_screenshots_command(args[1:])
    if CONTROL_SHUTDOWN_ARG in args:
        return _shutdown_running_instance()
    args, wow_watch_mode, early_exit = _prepare_wow_watch_mode(args)
    if early_exit is not None:
        return early_exit
    duplicate_command = _duplicate_launch_command(args, wow_watch_mode=wow_watch_mode)
    if duplicate_command is not None:
        result = _send_control_command(duplicate_command, timeout_ms=200)
        if _control_command_acknowledged(result):
            return 0
        if result.connected and result.written and result.response is None:
            log.warning(
                "Running ApplicantScout instance did not acknowledge %r; "
                "refusing to start a duplicate instance.",
                duplicate_command,
            )
            return 1
        elif result.connected and result.written:
            log.warning(
                "Running ApplicantScout instance returned unexpected response "
                "for %r: %r",
                duplicate_command,
                result.response,
            )
            return 1
        if result.connected and not result.written:
            log.warning(
                "Could not send duplicate-launch control command: %s",
                result.error or "unknown error",
            )
            return 1
    _set_windows_app_user_model_id()
    app = QApplication([sys.argv[0], *args])
    app.setApplicationName("ApplicantScout")
    if isinstance(app, _QT_APPLICATION_CLASS):
        app.setWindowIcon(_app_icon())
    wow_sync_startup_configurator = _WowSyncStartupConfigurator(
        app if isinstance(app, QObject) else None
    )

    settings_dialog: SettingsDialog | None = None
    window_ref: dict[str, OverlayWindow] = {}
    show_settings_action = _DeferredGuiAction()
    pending_update_version: str | None = None
    startup_update_prompt_pending = wow_watch_mode
    tray_controller: TrayController | None = None
    update_quit_gate = _UpdateQuitGate()
    update_handoff_recovery: _UpdateHandoffRecoveryController | None = None
    watcher: ScreenshotWatcher | None = None
    watcher_signal_gate = _WatcherSignalGate()
    live_snapshot_writer: LiveSnapshotCacheWriter | None = None

    def _check_updates_with_handoff() -> SettingsUpdateResult | tuple[str, str | None]:
        return _check_updates(update_quit_gate=update_quit_gate)

    def _flush_before_quit() -> None:
        _quiesce_screenshot_ingestion(
            watcher,
            watcher_signal_gate,
            live_snapshot_writer,
        )
        if settings_dialog is not None:
            settings_dialog.flush_pending_values()
        if active_window := window_ref.get("window"):
            active_window.flush_geometry()

    def _quit_application() -> None:
        if update_handoff_recovery is not None:
            update_handoff_recovery.disarm()
        _flush_before_quit()
        app.quit()

    def _can_quit_application() -> bool:
        return update_quit_gate.can_user_quit()

    def _can_control_quit_application() -> bool:
        return update_quit_gate.can_control_quit()

    def _show_update_quit_blocked() -> None:
        if tray_controller is not None:
            tray_controller.show_update_quit_blocked()
            return
        if settings_dialog is not None:
            settings_dialog.set_status(UPDATE_QUIT_BLOCKED_MESSAGE, error=True)
            return
        log.info(UPDATE_QUIT_BLOCKED_MESSAGE)

    def _show_settings_quit_blocked() -> None:
        if settings_dialog is not None:
            settings_dialog.show()
            settings_dialog.raise_()
            settings_dialog.activateWindow()
            return
        log.info(SETTINGS_QUIT_BLOCKED_MESSAGE)

    def _prepare_quit_application() -> bool:
        if not _can_quit_application():
            _show_update_quit_blocked()
            return False
        if not _prepare_settings_before_quit(settings_dialog):
            _show_settings_quit_blocked()
            return False
        return True

    def _prepare_control_quit_application() -> bool:
        if not _can_control_quit_application():
            _show_update_quit_blocked()
            return False
        return update_quit_gate.prepare_control_quit(_prepare_quit_application)

    def _request_quit_application() -> None:
        if not _prepare_quit_application():
            return
        _quit_application()

    about_to_quit = getattr(app, "aboutToQuit", None)
    if about_to_quit is not None:
        about_to_quit.connect(_flush_before_quit)

    try:
        control_server = _create_control_server(
            app,
            quit_app=_quit_application,
            show_settings=show_settings_action.request,
            can_quit=_can_control_quit_application,
            prepare_quit=_prepare_control_quit_application,
            quit_blocked=_show_update_quit_blocked,
        )
    except _DuplicateInstanceFound:
        log.info("ApplicantScout Companion is already running; exiting duplicate launch.")
        return 0
    except _ControlServerUnavailable as exc:
        log.error(
            "Could not establish single-instance ownership; refusing startup: %s",
            exc,
        )
        return 1
    setattr(app, "_applicant_scout_control_server", control_server)

    loaded = _load_startup_config(update_quit_gate=update_quit_gate)
    if loaded is None:
        return 1

    cfg, screenshots_dir, startup_settings_shown = loaded
    region = cfg.region or REGION_ID_TO_WCL.get(3, "EU")  # default EU
    region_runtime = _WCLRegionRuntime(region)
    log.info("Screenshots: %s", screenshots_dir)
    path_warning = screenshots_path_health_warning(screenshots_dir)
    if path_warning:
        log.warning(path_warning)
    log.info("Region: %s (overridden by addon's VERSION snapshot if different)", region)
    log.info("Logs: %s", cfg.log_dir or user_log_dir())
    log.info("WCL metric preferences: %s", cfg.metric_preferences.cache_key())

    auth = WCLAuth(cfg.wcl_client_id, cfg.wcl_client_secret, cfg.cache_dir)
    cache = CharacterCache(
        cfg.cache_dir,
        ttl_seconds=cfg.cache_ttl_seconds,
        defer_saves=True,
    )
    live_snapshot_writer = LiveSnapshotCacheWriter(cfg.cache_dir, defer_saves=True)
    wcl_client = WCLClient(
        auth,
        region=region_runtime.effective_region,
        metric_preferences=cfg.metric_preferences,
    )

    # Ctrl+C → graceful quit (PyQt's C event loop swallows SIGINT by default;
    # the no-op timer wakes Python every 500 ms so signal handlers actually run)
    import signal as _signal

    _signal.signal(_signal.SIGINT, lambda *_: _request_quit_application())
    _ctrlc_timer = QTimer()
    _ctrlc_timer.start(500)
    _ctrlc_timer.timeout.connect(lambda: None)

    state = AppState()
    machine = StateMachine(
        state,
        rio_reader=_raiderio_reader_for_screenshots_path(
            screenshots_dir,
            cache_dir=cfg.cache_dir,
        ),
    )
    current_screenshots_dir = screenshots_dir
    wow_exit_timer: QTimer | None = None

    def _set_update_in_progress(in_progress: bool) -> None:
        update_quit_gate.set_update_in_progress(in_progress)
        if tray_controller is not None:
            tray_controller.set_update_available(pending_update_version)
            tray_controller.set_update_in_progress(in_progress)
        if settings_dialog is not None:
            settings_dialog.set_update_in_progress(in_progress)

    def _recover_update_handoff(message: str, retry_available: bool) -> None:
        nonlocal pending_update_version
        if not update_quit_gate.update_in_progress:
            return
        if not retry_available:
            pending_update_version = None
        _set_update_in_progress(False)
        if settings_dialog is not None:
            settings_dialog.set_update_available(pending_update_version)
            settings_dialog.set_status(message, error=True)
        if tray_controller is not None:
            tray_controller.set_update_available(pending_update_version)
            tray_controller.tray.showMessage(
                "ApplicantScout update",
                message,
                QSystemTrayIcon.MessageIcon.Warning,
                7000,
            )

    def _handle_update_handoff_started(
        message: str, installer_launch: object | None
    ) -> None:
        update_quit_gate.mark_installer_handoff_started()
        if update_handoff_recovery is not None:
            update_handoff_recovery.arm(installer_launch, message)
        if tray_controller is not None:
            tray_controller.tray.showMessage(
                "ApplicantScout update",
                message,
                QSystemTrayIcon.MessageIcon.Information,
                7000,
            )

    def _show_settings() -> None:
        nonlocal auth
        nonlocal cfg
        nonlocal current_screenshots_dir
        nonlocal settings_dialog
        nonlocal watcher
        nonlocal wow_exit_timer
        if settings_dialog is not None:
            settings_dialog.show()
            settings_dialog.raise_()
            settings_dialog.activateWindow()
            return

        dialog = SettingsDialog(
            cfg,
            first_run=False,
            credential_tester=lambda client_id, client_secret, region: _test_wcl_credentials(
                cfg.cache_dir,
                client_id,
                client_secret,
                region,
            ),
            open_logs=lambda: _open_log_dir(cfg.log_dir or user_log_dir()),
            clear_cache=lambda: _clear_cache_dir(
                cfg.cache_dir,
                cache,
                auth,
                live_snapshot_writer=live_snapshot_writer,
            ),
            check_updates=_check_updates_with_handoff,
            hide_to_tray_on_close=tray_controller is not None,
            parent=window,
        )
        dialog.setWindowIcon(_app_icon())
        dialog.set_update_available(pending_update_version)
        _connect_release_notes_dialog_action(dialog)
        settings_dialog = dialog

        def _forget_dialog() -> None:
            nonlocal settings_dialog
            settings_dialog = None

        def _apply_settings_values(values, *, apply_credentials: bool) -> None:
            nonlocal auth
            nonlocal cfg
            nonlocal current_screenshots_dir
            nonlocal watcher
            nonlocal wow_exit_timer
            dialog.set_status("Saving...")
            try:
                result = _apply_settings_change(
                    app=app,
                    cfg=cfg,
                    values=values,
                    apply_credentials=apply_credentials,
                    auth=auth,
                    wcl_client=wcl_client,
                    region_runtime=region_runtime,
                    window=window,
                    watcher=watcher,
                    current_screenshots_dir=current_screenshots_dir,
                    machine=machine,
                    decode_failed_callback=_log_decode_failed,
                    signal_gate=watcher_signal_gate,
                    wow_exit_timer=wow_exit_timer,
                    quit_app=_request_quit_application,
                    can_quit=_can_quit_application,
                    prepare_quit=_prepare_quit_application,
                    live_snapshot_cache_writer=live_snapshot_writer,
                )
            except (ConfigError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                log.warning("Could not apply settings change: %s", exc)
                dialog.report_values_apply_result(False)
                dialog.set_status(f"Could not save/apply settings: {exc}", error=True)
                return
            startup_shortcut_changed = cfg.sync_with_wow != result.cfg.sync_with_wow
            dialog.report_values_apply_result(True)
            cfg = result.cfg
            auth = result.auth
            watcher = result.watcher
            current_screenshots_dir = result.current_screenshots_dir
            wow_exit_timer = result.wow_exit_timer
            overrides = result.overrides
            path_warning = dialog.current_screenshots_warning()
            if startup_shortcut_changed:
                desired_sync_with_wow = cfg.sync_with_wow

                def _report_startup_shortcut_error(exc: Exception) -> None:
                    if settings_dialog is not dialog:
                        return
                    dialog.set_status(
                        "Settings saved, but the WoW startup shortcut could "
                        f"not be updated: {exc}",
                        error=True,
                    )

                wow_sync_startup_configurator.request(
                    desired_sync_with_wow,
                    on_error=_report_startup_shortcut_error,
                )
            if apply_credentials:
                status_text, status_error = _settings_wcl_test_success_status(
                    values,
                    overrides,
                    path_warning=path_warning,
                )
            else:
                status_text, status_error, status_warning = _settings_autosave_status(
                    values,
                    overrides,
                    cfg,
                    path_warning=path_warning,
                )
                dialog.set_status(
                    status_text,
                    error=status_error,
                    warning=status_warning,
                )
                return
            dialog.set_status(status_text, error=status_error)

        def _handle_values_changed(values) -> None:
            _apply_settings_values(values, apply_credentials=False)

        def _handle_credentials_validated(values) -> None:
            _apply_settings_values(values, apply_credentials=True)

        def _handle_dialog_update_completed() -> None:
            nonlocal pending_update_version
            pending_update_version = None
            if tray_controller is not None:
                tray_controller.set_update_available(None)

        dialog.valuesChanged.connect(_handle_values_changed)
        dialog.credentialsValidated.connect(_handle_credentials_validated)
        dialog.updateStarted.connect(window.flush_geometry)
        dialog.updateStarted.connect(lambda: _set_update_in_progress(True))
        dialog.updateFinished.connect(lambda _error: _set_update_in_progress(False))
        dialog.updateCompleted.connect(_handle_dialog_update_completed)
        dialog.updateHandoffStarted.connect(_handle_update_handoff_started)
        dialog.hideRequested.connect(lambda: None)
        dialog.quitRequested.connect(_request_quit_application)
        dialog.destroyed.connect(lambda *_args: _forget_dialog())
        dialog.set_update_in_progress(update_quit_gate.update_in_progress)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    window = OverlayWindow(
        state,
        wcl_client,
        cache,
        cfg.config_dir,
        metric_preferences=cfg.metric_preferences,
        show_settings=_show_settings,
        game_foreground_probe=is_wow_foreground,
    )
    _validate_oauth_async(wcl_client)
    window_ref["window"] = window
    window.setWindowIcon(_app_icon())
    show_settings_action.set_callback(_show_settings)
    update_signals = UpdateSignals(app)
    update_check_coordinator = _UpdateCheckCoordinator()
    update_handoff_recovery = _UpdateHandoffRecoveryController(
        app,
        on_recover=_recover_update_handoff,
    )

    def _run_update() -> None:
        if update_quit_gate.update_in_progress:
            return
        if not _flush_settings_before_update(settings_dialog):
            return
        window.flush_geometry()
        _set_update_in_progress(True)

        def _worker() -> None:
            try:
                result = _check_updates_with_handoff()
                if isinstance(result, SettingsUpdateResult):
                    completion = _UpdateCompletion(
                        message=result.message,
                        installer_handoff=result.installer_handoff,
                        installer_launch=result.installer_launch,
                    )
                else:
                    message, _url = result
                    completion = _UpdateCompletion(message=message)
                update_signals.completed.emit(completion)
            except Exception as exc:  # noqa: BLE001
                update_signals.completed.emit(
                    _UpdateCompletion(f"Update failed: {exc}", error=True)
                )

        threading.Thread(target=_worker, name="ApplicantScoutUpdater", daemon=True).start()

    def _handle_update_completed(completion: object) -> None:
        nonlocal pending_update_version
        if not isinstance(completion, _UpdateCompletion):
            log.warning("Ignored unexpected update completion payload: %r", completion)
            return
        if completion.error:
            update_handoff_recovery.disarm()
            _set_update_in_progress(False)
            QMessageBox.warning(window, "ApplicantScout update", completion.message)
            return
        if completion.installer_handoff:
            _handle_update_handoff_started(
                completion.message,
                completion.installer_launch,
            )
            return
        update_handoff_recovery.disarm()
        pending_update_version = None
        if not completion.installer_handoff:
            _set_update_in_progress(False)
            if settings_dialog is not None:
                settings_dialog.set_update_available(None)
            if tray_controller is not None:
                tray_controller.set_update_available(None)
        if tray_controller is not None:
            tray_controller.tray.showMessage(
                "ApplicantScout update",
                completion.message,
                QSystemTrayIcon.MessageIcon.Information,
                7000,
            )

    def _run_silent_update_check() -> None:
        generation = update_check_coordinator.next_generation()

        def _worker() -> None:
            result = _safe_check_for_update(__version__)
            update_signals.checked.emit(generation, result)

        threading.Thread(
            target=_worker,
            name="ApplicantScoutUpdateCheck",
            daemon=True,
        ).start()

    def _handle_update_checked(generation: int, result: object) -> None:
        nonlocal pending_update_version
        nonlocal startup_update_prompt_pending
        decision = _resolve_update_check_result(
            update_check_coordinator,
            generation,
            result,
            previous_pending_update_version=pending_update_version,
        )
        if not decision.is_current:
            return
        pending_update_version = decision.pending_update_version
        if tray_controller is not None:
            tray_controller.set_update_available(pending_update_version)
        if settings_dialog is not None:
            settings_dialog.set_update_available(pending_update_version)
            if update_quit_gate.update_in_progress:
                settings_dialog.set_update_in_progress(True)
        if update_quit_gate.update_in_progress:
            startup_update_prompt_pending = False
            return
        if _should_show_wow_start_update_prompt(
            wow_watch_mode=wow_watch_mode,
            startup_update_prompt_pending=startup_update_prompt_pending,
            pending_update_version=pending_update_version,
        ):
            startup_update_prompt_pending = False
            _show_settings()
            if settings_dialog is not None and pending_update_version is not None:
                settings_dialog.set_status(
                    _wow_start_update_prompt_message(pending_update_version)
                )
            return
        startup_update_prompt_pending = False

    update_signals.checked.connect(_handle_update_checked)
    update_signals.completed.connect(_handle_update_completed)
    tray_controller = _create_tray_controller(
        app,
        icon=_app_icon(),
        window=window,
        show_settings=_show_settings,
        open_logs=lambda: _open_log_dir(cfg.log_dir or user_log_dir()),
        run_update=_run_update,
        quit_app=_request_quit_application,
    )
    if tray_controller is None:
        log.info("System tray indicator disabled.")
    if cfg.sync_with_wow:
        wow_exit_timer = _start_wow_lifecycle_timer(
            app,
            has_seen_wow=wow_watch_mode,
            quit_app=_request_quit_application,
            can_quit=_can_quit_application,
            prepare_quit=_prepare_quit_application,
        )
        setattr(app, "_applicant_scout_wow_exit_timer", wow_exit_timer)
    update_timer = QTimer(app)
    update_timer.setInterval(UPDATE_CHECK_INTERVAL_MS)
    update_timer.timeout.connect(_run_silent_update_check)
    update_timer.start()
    setattr(app, "_applicant_scout_update_timer", update_timer)
    QTimer.singleShot(UPDATE_CHECK_INITIAL_MS, _run_silent_update_check)
    if _should_show_settings_on_start(
        args,
        startup_settings_shown=startup_settings_shown,
        wow_watch_mode=wow_watch_mode,
    ):
        QTimer.singleShot(0, _show_settings)

    # Wire state-machine signals → overlay slots (unchanged from chatlog pipeline —
    # OverlayWindow API is transport-agnostic)
    machine.applicantAdded.connect(window.on_applicant_added)
    machine.applicantUpdated.connect(window.on_applicant_updated)
    machine.applicantRemoved.connect(window.on_applicant_removed)
    machine.listingChanged.connect(window.on_listing_changed)
    machine.cleared.connect(window.on_cleared)
    machine.rosterChanged.connect(window.on_roster_changed)

    def _sync_region_to_wcl(region_id: int) -> None:
        old_region = wcl_client.region
        if region_runtime.set_live_region_id(region_id):
            log.info(
                "Region updated from VERSION: %s -> %s",
                old_region,
                region_runtime.effective_region,
            )
            wcl_client.region = region_runtime.effective_region

    machine.versionUpdated.connect(_sync_region_to_wcl)

    def _log_decode_failed(path: str, reason: str) -> None:
        log.warning("decode failed for %s: %s", path, reason)

    if _restore_live_snapshot_cache(cfg.cache_dir, machine, window):
        log.info("Restored last live ApplicantScout snapshot; waiting for fresh QR.")

    # Start screenshot watcher: it scans recent backlog (last 60s of WoWScrnShot
    # files) on start and applies the most recent valid snapshot — handles the
    # "companion started mid-session" case. Then watches Screenshots/ folder
    # via watchdog Observer for new files.
    watcher = _replace_screenshot_watcher(
        None,
        screenshots_dir,
        machine,
        window,
        _log_decode_failed,
        cache_dir=cfg.cache_dir,
        signal_gate=watcher_signal_gate,
        live_snapshot_cache_writer=live_snapshot_writer,
    )

    log.info("Ready. Overlay will appear when applicants are present.")

    return _run_application_event_loop(
        app,
        wow_sync_startup_configurator=wow_sync_startup_configurator,
        sync_with_wow=cfg.sync_with_wow,
        watcher=watcher,
        window=window,
        cache=cache,
        live_snapshot_writer=live_snapshot_writer,
        wcl_client=wcl_client,
    )


if __name__ == "__main__":
    sys.exit(main())
