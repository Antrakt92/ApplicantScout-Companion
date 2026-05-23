"""Entry point: wires config → screenshot watcher → state machine → WCL fetcher → overlay."""

from __future__ import annotations

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

from PyQt6.QtCore import QObject, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import __version__
from .atomic_io import apply_private_file_mode
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
from .metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences
from .overlay import OverlayWindow
from .raiderio_local import RaiderIOLocalReader, retail_root_from_screenshots_path
from .screenshot import DecodedRosterMember, ScreenshotWatcher, Snapshot
from .settings_dialog import (
    ReleaseNotesDialog,
    SettingsDialog,
    SettingsUpdateResult,
    open_folder,
)
from .state import Applicant, AppState, Listing, RosterMember, WoWPlayer
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
)


log = logging.getLogger("applicant_scout")
APP_ICON_PATH = Path(__file__).with_name("assets") / "app_icon.ico"
APP_USER_MODEL_ID = "Antrakt.ApplicantScout.Companion"
CONTROL_SERVER_NAME = "Antrakt.ApplicantScout.Companion.Control"
CONTROL_SHUTDOWN_ARG = "--shutdown-running-instance"
SHOW_SETTINGS_ARG = "--show-settings"
CONTROL_QUIT_COMMAND = b"quit"
CONTROL_SHOW_SETTINGS_COMMAND = b"show-settings"
UPDATE_QUIT_BLOCKED_MESSAGE = "Update is installing. Wait for it to finish before quitting."
WOW_EXIT_POLL_MS = 5000
UPDATE_CHECK_INITIAL_MS = 1_000
UPDATE_CHECK_INTERVAL_MS = 60 * 60 * 1000
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
        window.restore_from_launcher()

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
    quit_blocked: Callable[[], None] | None = None,
) -> QLocalServer | None:
    server = QLocalServer(app)
    if not server.listen(CONTROL_SERVER_NAME):
        active_owner = _send_control_command(CONTROL_SHOW_SETTINGS_COMMAND, timeout_ms=200)
        if active_owner.connected and active_owner.written:
            raise _DuplicateInstanceFound
        QLocalServer.removeServer(CONTROL_SERVER_NAME)
        if not server.listen(CONTROL_SERVER_NAME):
            log.warning("Could not start control server: %s", server.errorString())
            return None

    server.newConnection.connect(
        lambda: _drain_control_connections(
            server,
            quit_app,
            show_settings,
            can_quit=can_quit,
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
                quit_blocked=quit_blocked,
            )


def _handle_control_command(
    socket: QLocalSocket,
    quit_app: Callable[[], None],
    show_settings: Callable[[], None] | None = None,
    *,
    can_quit: Callable[[], bool] | None = None,
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


def _validate_oauth_async(auth: WCLAuth) -> None:
    """First-run OAuth validation off the GUI thread.

    Previously synchronous: 500ms-2s blocking HTTP roundtrip before
    OverlayWindow.show() — startup felt frozen on first run / expired-token
    refresh. Now fires a daemon thread that calls get_token(); failure surfaces
    via the existing fetch-error path (overlay cell shows red `?` with WCL
    error message in tooltip) the first time a real applicant fetch runs. Lazy
    is OK because get_token() is also called from each QRunnable, so a missing
    token can't propagate downstream — the failed fetch caps the blast radius."""

    def _worker() -> None:
        try:
            auth.get_token()
            log.info("WCL OAuth: OK")
        except Exception as e:  # noqa: BLE001
            log.error("WCL OAuth failed (will surface on first fetch): %s", e)

    threading.Thread(target=_worker, name="WCLAuthValidator", daemon=True).start()


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
    _rioPreloadCompleted = pyqtSignal(str, int)

    def __init__(self, state: AppState, parent=None, rio_reader: Any | None = None):
        super().__init__(parent)
        self._state = state
        self._rio_reader = rio_reader
        self._rio_reader_generation = 0
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

        def _on_loaded() -> None:
            self._rioPreloadCompleted.emit(region_token, generation)

        try:
            preload(region_token, on_loaded=_on_loaded)
        except TypeError:
            preload(region_token)

    def _on_rio_preload_completed(self, region_token: str, generation: int) -> None:
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
            rows = self._rio_dungeon_rows_for(applicant.name, applicant.rio_dungeons)
            if rows == applicant.rio_dungeons:
                continue
            applicant.rio_dungeons = rows
            self.applicantUpdated.emit(applicant)
        roster_changed = False
        for member in list(self._state.party_members.values()):
            rows = self._rio_dungeon_rows_for(member.name, member.rio_dungeons)
            if rows == member.rio_dungeons:
                continue
            member.rio_dungeons = rows
            roster_changed = True
        if roster_changed:
            self.rosterChanged.emit()

    def _rio_dungeon_rows_for(self, decoded_name: str, decoded_rows: list[dict]) -> list[dict]:
        rows = [dict(row) for row in decoded_rows]
        if self._rio_reader is None:
            return rows
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
            profile = None
        if profile is None:
            return rows
        return [dict(row) for row in profile.dungeons]

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
        target.wcl_metric_preferences = source.wcl_metric_preferences

    def _roster_member_from_decoded(
        self, decoded: DecodedRosterMember, *, rio_summary_target_key: int = 0
    ) -> RosterMember:
        cls_name = CLASS_ID_TO_NAME.get(decoded.class_id, "?")
        role_name = ROLE_BYTE_TO_NAME.get(decoded.role, "DAMAGER")
        return RosterMember(
            applicant_id=self._roster_key(decoded.name),
            name=decoded.name,
            cls=cls_name,
            spec_id=decoded.spec_id,
            ilvl=decoded.ilvl,
            score=decoded.score,
            role=role_name,
            main_score=decoded.main_score,
            rio_profile=decoded.rio_profile,
            rio_best_key=decoded.rio_best_key,
            rio_best_dungeon_key=decoded.rio_best_dungeon_key,
            rio_timed_at_or_above=decoded.rio_timed_at_or_above,
            rio_timed_at_or_above_minus1=decoded.rio_timed_at_or_above_minus1,
            rio_timed_at_or_above_minus2=decoded.rio_timed_at_or_above_minus2,
            rio_completed_at_or_above_minus1=decoded.rio_completed_at_or_above_minus1,
            rio_dungeon_count=decoded.rio_dungeon_count,
            rio_summary_target_key=rio_summary_target_key,
            rio_dungeons=self._rio_dungeon_rows_for(
                decoded.name, decoded.rio_dungeons
            ),
            unit_index=decoded.unit_index,
            subgroup=decoded.subgroup,
            is_self=decoded.is_self,
            is_raid_member=decoded.is_raid_member,
        )

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
            member = self._roster_member_from_decoded(
                decoded,
                rio_summary_target_key=rio_summary_target_key,
            )
            if existing is not None:
                needs_refetch = (
                    existing.spec_id != member.spec_id
                    or existing.name != member.name
                    or wcl_metric_role(existing.role) != wcl_metric_role(member.role)
                    or region_identity_changed
                    or (
                        default_realm_changed
                        and not applicant_has_explicit_realm(member.name)
                    )
                )
                if not needs_refetch:
                    self._copy_wcl_data(existing, member)
                changed = changed or member != existing
            else:
                changed = True
            self._state.add_or_update_party_member(member)

        if changed and emit_signal:
            self.rosterChanged.emit()
        return changed

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
            self._preload_local_rio_region(new_region_token)
            log.info(
                "Player: %s (region=%d)",
                snap.version.player_name,
                snap.version.region_id,
            )
            if snap.version.region_id != old_player.region_id:
                self.versionUpdated.emit(snap.version.region_id)

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

        # NOLISTING-equivalent: snap arrived with has_listing=0 AND we had one.
        # Clear all applicants + emit cleared signal so overlay hides.
        if new_listing is None and old_listing is not None:
            self._state.listing = None
            self._state.clear_all()
            roster_changed = self._apply_roster_snapshot(
                snap.roster,
                region_identity_changed=region_identity_changed,
                default_realm_changed=default_realm_changed,
                rio_summary_target_key=0,
                emit_signal=False,
            )
            self.listingChanged.emit()
            self.cleared.emit()
            if roster_changed:
                self.rosterChanged.emit()
            return

        # No listing in snap AND no prior listing → roster/version can still update.
        if new_listing is None:
            self._apply_roster_snapshot(
                snap.roster,
                region_identity_changed=region_identity_changed,
                default_realm_changed=default_realm_changed,
                rio_summary_target_key=0,
            )
            return

        rio_summary_target_key = new_listing.key_level if new_listing.key_level > 0 else 0

        # Listing changed (dungeon/key/comment) — fire signal so overlay re-titles
        if new_listing != old_listing:
            self._state.listing = new_listing
            log.info(
                "Listing: %s +%d cat=%d diff=%d (%d apps in snapshot)",
                new_listing.dungeon_name,
                new_listing.key_level,
                new_listing.category_id,
                new_listing.difficulty_id,
                len(snap.applicants),
            )
            self.listingChanged.emit()

        # ─── Applicants diff ───
        # Composite key f"{applicant_id}:{member_idx}" — required for multi-
        # member group apps (one LFG application can have up to 5 members,
        # all sharing applicant_id but with distinct member_idx 1..N). Solo
        # apps + legacy v0x01 payloads decode with member_idx=1, producing
        # keys like "42:1" — same shape, no special-casing needed.
        valid_applicants = [
            a for a in snap.applicants if (name := a.name.strip()) and name != "?"
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
                    "Snapshot: %d applicants across %d apps; multi-member groups: %s",
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
            if existing is None:
                applicant = Applicant(
                    applicant_id=aid,
                    name=da.name,
                    cls=cls_name,
                    spec_id=da.spec_id,
                    ilvl=da.ilvl,
                    score=da.score,
                    role=role_name,
                    main_score=da.main_score,
                    rio_profile=da.rio_profile,
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
                    rio_dungeons=self._rio_dungeon_rows_for(da.name, da.rio_dungeons),
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
                # Preserve WCL percentiles only while the WCL result shape stays
                # valid for this row. Gear/score changes are safe; character,
                # spec, and DPS-vs-HEALER metric-role changes are not.
                needs_refetch = (
                    existing.spec_id != da.spec_id
                    or existing.name != da.name
                    or wcl_metric_role(existing.role) != wcl_metric_role(role_name)
                    or region_identity_changed
                    or (
                        default_realm_changed
                        and not applicant_has_explicit_realm(da.name)
                    )
                )
                existing.name = da.name
                existing.cls = cls_name
                existing.spec_id = da.spec_id
                existing.ilvl = da.ilvl
                existing.score = da.score
                existing.role = role_name
                existing.main_score = da.main_score
                existing.rio_profile = da.rio_profile
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
                existing.rio_dungeons = self._rio_dungeon_rows_for(
                    da.name, da.rio_dungeons
                )
                if needs_refetch:
                    existing.clear_wcl_data()
                self.applicantUpdated.emit(existing)

        self._apply_roster_snapshot(
            snap.roster,
            region_identity_changed=region_identity_changed,
            default_realm_changed=default_realm_changed,
            rio_summary_target_key=rio_summary_target_key,
        )


class UpdateSignals(QObject):
    checked = pyqtSignal(int, object)
    completed = pyqtSignal(str, bool, bool)


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
) -> str:
    try:
        if character_cache is not None:
            character_cache.clear()
        cache_dir.mkdir(parents=True, exist_ok=True)
        for child in cache_dir.iterdir():
            if child.name == UPDATE_DOWNLOADS_DIR_NAME:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
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


def _check_updates() -> SettingsUpdateResult | tuple[str, str | None]:
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
        launch_update_installer(installer)
        return SettingsUpdateResult(
            message=(
                f"Installing ApplicantScout Companion {getattr(result, 'latest_version', 'update')}. "
                "The companion may close and reopen during the update."
            ),
            installer_handoff=True,
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
    running_checker: Callable[[], bool] | None = None,
    async_runner: Callable[[Callable[[], None]], None] | None = None,
) -> QTimer:
    timer = QTimer(app)
    timer.setInterval(WOW_EXIT_POLL_MS)
    observed_wow = has_seen_wow
    quit_callback = quit_app or app.quit
    can_quit_callback = can_quit or (lambda: True)
    check_wow_running = running_checker or is_wow_running
    signals = _WowLifecycleSignals()
    state = {"checking": False, "active": True}
    run_async = async_runner or (
        lambda worker: threading.Thread(
            target=worker,
            name="ApplicantScoutWoWLifecycleCheck",
            daemon=True,
        ).start()
    )

    def _handle_wow_running(running: bool) -> None:
        nonlocal observed_wow
        state["checking"] = False
        if not state["active"]:
            return
        if running:
            observed_wow = True
            return
        if observed_wow:
            if not can_quit_callback():
                return
            log.info("WoW is no longer running; quitting companion.")
            try:
                start_wow_sync_watcher()
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                log.warning("Could not re-arm WoW lifecycle watcher: %s", exc)
            quit_callback()

    def _handle_wow_check_failed() -> None:
        state["checking"] = False

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
) -> QTimer | None:
    configure_wow_sync_startup(enabled)
    if enabled:
        try:
            start_wow_sync_watcher()
            if current_timer is None:
                if can_quit is not None:
                    return _start_wow_lifecycle_timer(
                        app,
                        has_seen_wow=is_wow_running(),
                        quit_app=quit_app,
                        can_quit=can_quit,
                    )
                return _start_wow_lifecycle_timer(
                    app,
                    has_seen_wow=is_wow_running(),
                    quit_app=quit_app,
                )
            return current_timer
        except Exception:
            configure_wow_sync_startup(False)
            raise
    if current_timer is not None:
        state = getattr(current_timer, "_applicant_scout_wow_lifecycle_state", None)
        if isinstance(state, dict):
            state["active"] = False
        current_timer.stop()
        current_timer.deleteLater()
    return None


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

    def activate_next(self) -> int:
        generation = self.prepare_next()
        self.commit(generation)
        return generation

    def restore(self, generation: int) -> None:
        self._generation = generation
        self._pending_generation = None

    def is_current(self, generation: int) -> bool:
        return generation == self._generation or generation == self._pending_generation


class _SnapshotApplier(Protocol):
    def apply_snapshot(self, snap: Snapshot) -> None: ...


def _connect_screenshot_watcher(
    watcher: ScreenshotWatcher,
    machine: _SnapshotApplier,
    window: OverlayWindow,
    decode_failed_callback: Callable[[str, str], None],
    *,
    signal_gate: _WatcherSignalGate,
    generation: int,
) -> None:
    def _apply_snapshot_if_current(snap: object) -> None:
        if not signal_gate.is_current(generation):
            return
        getattr(machine, "apply_snapshot", lambda *_args: None)(snap)

    def _note_decode_if_current(snap: object) -> None:
        if not signal_gate.is_current(generation):
            return
        getattr(window, "note_decode", lambda *_args: None)(snap)

    def _decode_failed_if_current(path: str, reason: str) -> None:
        if not signal_gate.is_current(generation):
            return
        decode_failed_callback(path, reason)
        getattr(window, "note_decode_failed", lambda *_args: None)(path, reason)

    watcher.snapshotReceived.connect(_apply_snapshot_if_current)
    watcher.snapshotReceived.connect(_note_decode_if_current)
    watcher.decodeFailed.connect(_decode_failed_if_current)


def _replace_screenshot_watcher(
    current_watcher: ScreenshotWatcher | None,
    screenshots_dir: Path,
    machine: _SnapshotApplier,
    window: OverlayWindow,
    decode_failed_callback: Callable[[str, str], None],
    *,
    signal_gate: _WatcherSignalGate,
) -> ScreenshotWatcher:
    new_watcher = ScreenshotWatcher(screenshots_dir)
    generation = signal_gate.prepare_next()
    _connect_screenshot_watcher(
        new_watcher,
        machine,
        window,
        decode_failed_callback,
        signal_gate=signal_gate,
        generation=generation,
    )
    try:
        new_watcher.start()
    except Exception:
        signal_gate.cancel(generation)
        raise
    signal_gate.commit(generation)
    if current_watcher is not None:
        current_watcher.stop()
    return new_watcher


def _replace_screenshots_runtime(
    current_watcher: ScreenshotWatcher | None,
    screenshots_dir: Path,
    machine: StateMachine,
    window: OverlayWindow,
    decode_failed_callback: Callable[[str, str], None],
    *,
    signal_gate: _WatcherSignalGate,
) -> ScreenshotWatcher:
    previous_reader = getattr(machine, "_rio_reader", None)
    next_reader = _raiderio_reader_for_screenshots_path(screenshots_dir)
    try:
        watcher = _replace_screenshot_watcher(
            current_watcher,
            screenshots_dir,
            _ReaderBoundMachine(machine, next_reader),
            window,
            decode_failed_callback,
            signal_gate=signal_gate,
        )
    except Exception:
        machine.set_rio_reader(previous_reader)
        raise
    machine.set_rio_reader(next_reader)
    _preload_machine_rio_region(machine)
    return watcher


def _raiderio_reader_for_screenshots_path(path: Path) -> RaiderIOLocalReader | None:
    retail_root = retail_root_from_screenshots_path(path)
    if retail_root is None:
        return None
    return RaiderIOLocalReader(retail_root)


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


def _settings_saved_status(values, override_keys: list[str]) -> tuple[str, bool]:
    path_warning = (
        screenshots_path_health_warning(Path(values.screenshots_path))
        if values.screenshots_path
        else None
    )
    if override_keys:
        message = (
            "Saved for this app session, but environment overrides are active: "
            + ", ".join(override_keys)
        )
        if path_warning:
            message = f"{message}. {path_warning}"
        return message, True
    if path_warning:
        return path_warning, True
    return "Saved.", False


def _has_pending_wcl_credentials(cfg: Config) -> bool:
    return bool(cfg.draft_wcl_client_id or cfg.draft_wcl_client_secret)


def _settings_autosave_status(
    values,
    override_keys: list[str],
    cfg: Config,
) -> tuple[str, bool, bool]:
    saved_text, saved_error = _settings_saved_status(values, override_keys)
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
) -> tuple[str, bool]:
    saved_text, saved_error = _settings_saved_status(values, override_keys)
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
            )
        if new_screenshots_dir != current_screenshots_dir:
            new_watcher = _replace_screenshots_runtime(
                watcher,
                new_screenshots_dir,
                machine,
                window,
                decode_failed_callback,
                signal_gate=signal_gate,
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
    region_effective_changed = region_runtime.set_fallback(new_cfg.region)
    wcl_runtime_changed = credentials_promoted or region_effective_changed
    new_auth = auth
    if new_cfg.wcl_client_id and new_cfg.wcl_client_secret and credentials_promoted:
        new_auth = WCLAuth(
            new_cfg.wcl_client_id,
            new_cfg.wcl_client_secret,
            new_cfg.cache_dir,
        )
        wcl_client.reconfigure_auth(new_auth)
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
    character_cache: CharacterCache | None = None,
) -> bool:
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
        check_updates=_check_updates,
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
            return True
        QMessageBox.warning(
            None,
            "ApplicantScout settings",
            f"Settings were not saved because the WoW startup shortcut could not be updated: {exc}",
        )
        return False
    _persist_settings_values(cfg, values)
    if values.sync_with_wow:
        try:
            start_wow_sync_watcher()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            log.warning("Could not start WoW lifecycle watcher: %s", exc)
            QMessageBox.warning(
                None,
                "ApplicantScout settings",
                "Settings were saved, but the current-session WoW watcher "
                f"could not be started: {exc}",
            )
    return True


def _run_settings_dialog(
    cfg: Config,
    *,
    first_run: bool,
    character_cache: CharacterCache | None = None,
) -> bool:
    if not first_run:
        raise RuntimeError("Normal settings are modeless; use _show_settings.")
    return _run_first_run_settings(cfg, character_cache=character_cache)


def _load_startup_config() -> tuple[Config, Path, bool] | None:
    startup_settings_shown = False
    try:
        cfg = load_config()
    except ConfigError as exc:
        _show_config_error(str(exc))
        return None
    while True:
        if not is_config_ready(cfg):
            if not _run_settings_dialog(cfg, first_run=True):
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
            if not _run_settings_dialog(cfg, first_run=True):
                return None
            startup_settings_shown = True
            try:
                cfg = load_config()
            except ConfigError as load_exc:
                _show_config_error(str(load_exc))
                return None


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = sys.argv[1:] if argv is None else argv
    if CONTROL_SHUTDOWN_ARG in args:
        return _shutdown_running_instance()
    args, wow_watch_mode, early_exit = _prepare_wow_watch_mode(args)
    if early_exit is not None:
        return early_exit
    duplicate_command = _duplicate_launch_command(args, wow_watch_mode=wow_watch_mode)
    if duplicate_command is not None:
        result = _send_control_command(duplicate_command, timeout_ms=200)
        if result.connected and result.written:
            if result.response == b"unknown":
                log.info(
                    "Running ApplicantScout instance did not recognize %r; "
                    "exiting duplicate launch.",
                    duplicate_command,
                )
            return 0
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

    settings_dialog: SettingsDialog | None = None
    window_ref: dict[str, OverlayWindow] = {}
    show_settings_action = _DeferredGuiAction()
    pending_update_version: str | None = None
    startup_update_prompt_pending = wow_watch_mode
    tray_controller: TrayController | None = None
    update_in_progress = False

    def _flush_before_quit() -> None:
        if settings_dialog is not None:
            settings_dialog.flush_pending_values()
        if active_window := window_ref.get("window"):
            active_window.flush_geometry()

    def _quit_application() -> None:
        _flush_before_quit()
        app.quit()

    def _can_quit_application() -> bool:
        return not update_in_progress

    def _show_update_quit_blocked() -> None:
        if tray_controller is not None:
            tray_controller.show_update_quit_blocked()
            return
        if settings_dialog is not None:
            settings_dialog.set_status(UPDATE_QUIT_BLOCKED_MESSAGE, error=True)
            return
        log.info(UPDATE_QUIT_BLOCKED_MESSAGE)

    def _request_quit_application() -> None:
        if not _can_quit_application():
            _show_update_quit_blocked()
            return
        _quit_application()

    about_to_quit = getattr(app, "aboutToQuit", None)
    if about_to_quit is not None:
        about_to_quit.connect(_flush_before_quit)

    try:
        control_server = _create_control_server(
            app,
            quit_app=_request_quit_application,
            show_settings=show_settings_action.request,
            can_quit=_can_quit_application,
            quit_blocked=_show_update_quit_blocked,
        )
    except _DuplicateInstanceFound:
        log.info("ApplicantScout Companion is already running; exiting duplicate launch.")
        return 0
    if control_server is not None:
        setattr(app, "_applicant_scout_control_server", control_server)

    loaded = _load_startup_config()
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
    # OAuth validation now runs on a daemon thread so the overlay paints
    # immediately. If credentials are bad, the first applicant's WCL fetch
    # will surface the error in its cell tooltip via the existing fetch-error
    # path — no special UI plumbing needed.
    _validate_oauth_async(auth)

    cache = CharacterCache(cfg.cache_dir, ttl_seconds=cfg.cache_ttl_seconds)
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
        rio_reader=_raiderio_reader_for_screenshots_path(screenshots_dir),
    )
    watcher: ScreenshotWatcher | None = None
    watcher_signal_gate = _WatcherSignalGate()
    current_screenshots_dir = screenshots_dir
    wow_exit_timer: QTimer | None = None

    def _set_update_in_progress(in_progress: bool) -> None:
        nonlocal update_in_progress
        update_in_progress = in_progress
        if tray_controller is not None:
            tray_controller.set_update_available(pending_update_version)
            tray_controller.set_update_in_progress(in_progress)
        if settings_dialog is not None:
            settings_dialog.set_update_in_progress(in_progress)

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
            clear_cache=lambda: _clear_cache_dir(cfg.cache_dir, cache, auth),
            check_updates=_check_updates,
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
                )
            except (ConfigError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                log.warning("Could not apply settings change: %s", exc)
                dialog.report_values_apply_result(False)
                dialog.set_status(f"Could not save/apply settings: {exc}", error=True)
                return
            dialog.report_values_apply_result(True)
            cfg = result.cfg
            auth = result.auth
            watcher = result.watcher
            current_screenshots_dir = result.current_screenshots_dir
            wow_exit_timer = result.wow_exit_timer
            overrides = result.overrides
            if apply_credentials:
                status_text, status_error = _settings_wcl_test_success_status(
                    values,
                    overrides,
                )
            else:
                status_text, status_error, status_warning = _settings_autosave_status(
                    values,
                    overrides,
                    cfg,
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
        dialog.hideRequested.connect(lambda: None)
        dialog.quitRequested.connect(_request_quit_application)
        dialog.destroyed.connect(lambda *_args: _forget_dialog())
        dialog.set_update_in_progress(update_in_progress)
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
    window_ref["window"] = window
    window.setWindowIcon(_app_icon())
    show_settings_action.set_callback(_show_settings)
    update_signals = UpdateSignals(app)
    update_check_coordinator = _UpdateCheckCoordinator()

    def _run_update() -> None:
        if update_in_progress:
            return
        if not _flush_settings_before_update(settings_dialog):
            return
        window.flush_geometry()
        _set_update_in_progress(True)

        def _worker() -> None:
            try:
                result = _check_updates()
                if isinstance(result, SettingsUpdateResult):
                    message = result.message
                    installer_handoff = result.installer_handoff
                else:
                    message, _url = result
                    installer_handoff = False
                update_signals.completed.emit(message, False, installer_handoff)
            except Exception as exc:  # noqa: BLE001
                update_signals.completed.emit(f"Update failed: {exc}", True, False)

        threading.Thread(target=_worker, name="ApplicantScoutUpdater", daemon=True).start()

    def _handle_update_completed(
        message: str, error: bool, installer_handoff: bool
    ) -> None:
        nonlocal pending_update_version
        if error:
            _set_update_in_progress(False)
            QMessageBox.warning(window, "ApplicantScout update", message)
            return
        pending_update_version = None
        if not installer_handoff:
            _set_update_in_progress(False)
            if settings_dialog is not None:
                settings_dialog.set_update_available(None)
            if tray_controller is not None:
                tray_controller.set_update_available(None)
        if tray_controller is not None:
            tray_controller.tray.showMessage(
                "ApplicantScout update",
                message,
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
            if update_in_progress:
                settings_dialog.set_update_in_progress(True)
        if update_in_progress:
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
            has_seen_wow=wow_watch_mode or is_wow_running(),
            quit_app=_request_quit_application,
            can_quit=_can_quit_application,
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
        signal_gate=watcher_signal_gate,
    )

    log.info("Ready. Overlay will appear when applicants are present.")

    rc = app.exec()

    # Stop screenshot ingestion before draining WCL workers. Otherwise a late
    # backlog/watchdog signal can enqueue a fresh fetch after waitForDone().
    if watcher is not None:
        watcher.stop()

    # Drain in-flight WCL fetch tasks before closing the httpx client.
    # Without this, a QRunnable mid-fetch hits "Cannot send a request, as
    # the client has been closed." (caught by its except, but produces
    # noisy traceback at exit). 2s ceiling = WCL p99 fetch headroom.
    pool = QThreadPool.globalInstance()
    if pool is not None:
        pool.waitForDone(2000)
    wcl_client.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
