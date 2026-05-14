"""Entry point: wires config → screenshot watcher → state machine → WCL fetcher → overlay."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
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

from PyQt6.QtCore import QObject, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import __version__
from .config import (
    Config,
    ConfigError,
    is_config_ready,
    load_config,
    resolve_screenshots_path,
    save_config_values,
    screenshots_path_health_warning,
    user_log_dir,
)
from .constants import CLASS_ID_TO_NAME, REGION_ID_TO_WCL, ROLE_BYTE_TO_NAME
from .overlay import OverlayWindow
from .screenshot import ScreenshotWatcher, Snapshot
from .settings_dialog import SettingsDialog, open_folder
from .state import Applicant, AppState, Listing, WoWPlayer
from .updater import check_for_update, download_update_installer, launch_update_installer
from .wcl import (
    CharacterCache,
    WCLAuth,
    WCLClient,
    applicant_has_explicit_realm,
    default_realm_from_player,
    derive_server_slug,
    wcl_metric_role,
)
from .wow_lifecycle import (
    WATCH_WOW_ARG,
    configure_wow_sync_startup,
    is_wow_running,
    start_wow_sync_watcher,
)


log = logging.getLogger("applicant_scout")
APP_ICON_PATH = Path(__file__).with_name("assets") / "app_icon.ico"
APP_USER_MODEL_ID = "Antrakt.ApplicantScout.Companion"
CONTROL_SERVER_NAME = "Antrakt.ApplicantScout.Companion.Control"
CONTROL_SHUTDOWN_ARG = "--shutdown-running-instance"
SHOW_SETTINGS_ARG = "--show-settings"
WOW_EXIT_POLL_MS = 5000
UPDATE_CHECK_INITIAL_MS = 30_000
UPDATE_CHECK_INTERVAL_MS = 60 * 60 * 1000
_UPDATE_INSTALL_LOCK = threading.Lock()
_QT_APPLICATION_CLASS = QApplication


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
        self.hide_overlay_action.triggered.connect(lambda *_args: window.hide())

        self.update_action = _add_menu_action(self.menu, "Update")
        self.update_action.setEnabled(False)
        self.update_action.triggered.connect(lambda *_args: run_update())

        self.open_logs_action = _add_menu_action(self.menu, "Open logs")
        self.open_logs_action.triggered.connect(
            lambda *_args: self._open_logs(open_logs)
        )

        self.menu.addSeparator()
        self.quit_action = _add_menu_action(self.menu, "Quit ApplicantScout")
        self.quit_action.triggered.connect(lambda *_args: quit_app())

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
        if latest_version:
            self.update_action.setText(f"Update to {latest_version}")
            self.update_action.setEnabled(True)
            self.tray.setToolTip(
                f"ApplicantScout Companion is running - update {latest_version} is available"
            )
            return
        self.update_action.setText("Update")
        self.update_action.setEnabled(False)
        self.tray.setToolTip("ApplicantScout Companion is running")

    def _show_overlay(self, window: OverlayWindow) -> None:
        window.show()
        window.raise_()
        window.activateWindow()

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


def _shutdown_running_instance(timeout_ms: int = 2000) -> int:
    socket = QLocalSocket()
    socket.connectToServer(CONTROL_SERVER_NAME)
    if not socket.waitForConnected(timeout_ms):
        log.info("No running ApplicantScout instance accepted the shutdown command.")
        return 0
    socket.write(b"quit\n")
    if not socket.waitForBytesWritten(timeout_ms):
        log.warning("Could not send shutdown command: %s", socket.errorString())
        return 1
    socket.waitForReadyRead(500)
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
) -> QLocalServer | None:
    server = QLocalServer(app)
    if not server.listen(CONTROL_SERVER_NAME):
        QLocalServer.removeServer(CONTROL_SERVER_NAME)
        if not server.listen(CONTROL_SERVER_NAME):
            log.warning("Could not start control server: %s", server.errorString())
            return None

    server.newConnection.connect(lambda: _drain_control_connections(server, quit_app))
    return server


def _drain_control_connections(
    server: QLocalServer,
    quit_app: Callable[[], None],
) -> None:
    while server.hasPendingConnections():
        socket = server.nextPendingConnection()
        if socket is None:
            continue
        socket.readyRead.connect(
            lambda _socket=socket: _handle_control_command(_socket, quit_app)
        )
        socket.disconnected.connect(socket.deleteLater)
        if socket.bytesAvailable() > 0:
            _handle_control_command(socket, quit_app)


def _handle_control_command(socket: QLocalSocket, quit_app: Callable[[], None]) -> None:
    command = socket.readAll().data().strip().lower()
    if command == b"quit":
        socket.write(b"ok\n")
        socket.flush()
        socket.waitForBytesWritten(100)
        socket.disconnectFromServer()
        QTimer.singleShot(0, quit_app)
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
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("System tray is unavailable; settings are only available in overlay.")
        return None
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
    # Region change → main wires to wcl_client.region so non-EU users don't
    # silently get "Server not found" with default config.
    versionUpdated = pyqtSignal(int)

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self._state = state

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
            self.listingChanged.emit()
            self.cleared.emit()
            return

        # No listing in snap AND no prior listing → nothing to update
        if new_listing is None:
            return

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
        new_by_id = {f"{a.applicant_id}:{a.member_idx}": a for a in snap.applicants}
        # Diagnostic: per-applicant_id member-count distribution. Helps verify
        # multi-member group emit is reaching the companion (expect aid_groups
        # like {42: 2, 99: 1} when a 2-person group + a solo apply together).
        if snap.applicants:
            aid_groups: dict[int, int] = {}
            for a in snap.applicants:
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
                if needs_refetch:
                    existing.clear_wcl_data()
                self.applicantUpdated.emit(existing)


class UpdateSignals(QObject):
    checked = pyqtSignal(object)
    completed = pyqtSignal(str, bool)


def _setup_logging(log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
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
        file_handler = RotatingFileHandler(
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


def _clear_cache_dir(cache_dir: Path, character_cache: CharacterCache | None = None) -> str:
    if character_cache is not None:
        character_cache.clear()
    cache_dir.mkdir(parents=True, exist_ok=True)
    for child in cache_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    return "Cache cleared."


def _open_log_dir(log_dir: Path) -> str:
    if not open_folder(log_dir):
        raise RuntimeError(f"Could not open {log_dir}")
    return f"Opened log folder: {log_dir}"


def _test_wcl_credentials(cache_dir: Path, client_id: str, client_secret: str, _region: str) -> str:
    with tempfile.TemporaryDirectory(dir=cache_dir.parent) as temp_dir:
        auth = WCLAuth(client_id, client_secret, Path(temp_dir))
        auth.get_token()
    return "WCL credentials are valid."


def _check_updates() -> tuple[str, str | None]:
    if not _UPDATE_INSTALL_LOCK.acquire(blocking=False):
        raise RuntimeError("Update is already in progress.")
    try:
        result = check_for_update(__version__)
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
        return (
            f"Installing ApplicantScout Companion {getattr(result, 'latest_version', 'update')}. "
            "The companion may close and reopen during the update.",
            None,
        )
    finally:
        _UPDATE_INSTALL_LOCK.release()


def _should_show_settings_on_start(
    args: list[str], *, startup_settings_shown: bool
) -> bool:
    return SHOW_SETTINGS_ARG in args and not startup_settings_shown


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
) -> QTimer:
    timer = QTimer(app)
    timer.setInterval(WOW_EXIT_POLL_MS)
    observed_wow = has_seen_wow
    quit_callback = quit_app or app.quit

    def _quit_after_wow_cycle() -> None:
        nonlocal observed_wow
        if is_wow_running():
            observed_wow = True
            return
        if observed_wow:
            log.info("WoW is no longer running; quitting companion.")
            try:
                start_wow_sync_watcher()
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                log.warning("Could not re-arm WoW lifecycle watcher: %s", exc)
            quit_callback()

    timer.timeout.connect(_quit_after_wow_cycle)
    timer.start()
    return timer


def _apply_wow_sync_runtime(
    app: QApplication,
    enabled: bool,
    current_timer: QTimer | None,
    *,
    quit_app: Callable[[], None] | None = None,
) -> QTimer | None:
    configure_wow_sync_startup(enabled)
    if enabled:
        start_wow_sync_watcher()
        if current_timer is None:
            return _start_wow_lifecycle_timer(
                app,
                has_seen_wow=is_wow_running(),
                quit_app=quit_app,
            )
        return current_timer
    if current_timer is not None:
        current_timer.stop()
        current_timer.deleteLater()
    return None


def _connect_screenshot_watcher(
    watcher: ScreenshotWatcher,
    machine: StateMachine,
    window: OverlayWindow,
    decode_failed_callback: Callable[[str, str], None],
) -> None:
    watcher.snapshotReceived.connect(
        getattr(machine, "apply_snapshot", lambda *_args: None)
    )
    watcher.snapshotReceived.connect(getattr(window, "note_decode", lambda *_args: None))
    watcher.decodeFailed.connect(decode_failed_callback)


def _replace_screenshot_watcher(
    current_watcher: ScreenshotWatcher | None,
    screenshots_dir: Path,
    machine: StateMachine,
    window: OverlayWindow,
    decode_failed_callback: Callable[[str, str], None],
) -> ScreenshotWatcher:
    new_watcher = ScreenshotWatcher(screenshots_dir)
    _connect_screenshot_watcher(
        new_watcher,
        machine,
        window,
        decode_failed_callback,
    )
    new_watcher.start()
    if current_watcher is not None:
        current_watcher.stop()
    return new_watcher


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
        "APSCOUT_SYNC_WITH_WOW",
    )
    return [key for key in keys if os.environ.get(key) is not None]


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
    return save_config_values(
        wcl_client_id=active_client_id,
        wcl_client_secret=active_client_secret,
        draft_wcl_client_id=draft_client_id,
        draft_wcl_client_secret=draft_client_secret,
        region=values.region,
        screenshots_path=values.screenshots_path,
        cache_ttl_seconds=cfg.cache_ttl_seconds,
        metric_preferences=values.metric_preferences,
        sync_with_wow=values.sync_with_wow,
        chatlog_path="" if values.screenshots_path else str(cfg.chatlog_path),
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
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    values = dialog.values()
    _persist_settings_values(cfg, values)
    try:
        configure_wow_sync_startup(values.sync_with_wow)
        if values.sync_with_wow:
            start_wow_sync_watcher()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log.warning("Could not configure WoW lifecycle startup shortcut: %s", exc)
        QMessageBox.warning(
            None,
            "ApplicantScout settings",
            f"Settings were saved, but the WoW startup shortcut could not be updated: {exc}",
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

    _set_windows_app_user_model_id()
    app = QApplication([sys.argv[0], *args])
    app.setApplicationName("ApplicantScout")
    if isinstance(app, _QT_APPLICATION_CLASS):
        app.setWindowIcon(_app_icon())
    if _has_running_instance():
        log.info("ApplicantScout Companion is already running; exiting duplicate launch.")
        return 0

    settings_dialog: SettingsDialog | None = None
    pending_update_version: str | None = None

    def _flush_settings_before_quit() -> None:
        if settings_dialog is not None:
            settings_dialog.flush_pending_values()

    def _quit_application() -> None:
        _flush_settings_before_quit()
        app.quit()

    about_to_quit = getattr(app, "aboutToQuit", None)
    if about_to_quit is not None:
        about_to_quit.connect(_flush_settings_before_quit)

    loaded = _load_startup_config()
    if loaded is None:
        return 1
    control_server = _create_control_server(app, quit_app=_quit_application)
    if control_server is not None:
        setattr(app, "_applicant_scout_control_server", control_server)

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

    _signal.signal(_signal.SIGINT, lambda *_: _quit_application())
    _ctrlc_timer = QTimer()
    _ctrlc_timer.start(500)
    _ctrlc_timer.timeout.connect(lambda: None)

    state = AppState()
    machine = StateMachine(state)
    watcher: ScreenshotWatcher | None = None
    current_screenshots_dir = screenshots_dir
    wow_exit_timer: QTimer | None = None

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
            clear_cache=lambda: _clear_cache_dir(cfg.cache_dir, cache),
            check_updates=_check_updates,
            parent=window,
        )
        dialog.setWindowIcon(_app_icon())
        dialog.set_update_available(pending_update_version)
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
            old_cfg = cfg
            dialog.set_status("Saving...")
            try:
                _persist_settings_values(
                    cfg,
                    values,
                    apply_credentials=apply_credentials,
                )
                new_cfg = load_config()
                if old_cfg.sync_with_wow != new_cfg.sync_with_wow:
                    wow_exit_timer = _apply_wow_sync_runtime(
                        app,
                        new_cfg.sync_with_wow,
                        wow_exit_timer,
                        quit_app=_quit_application,
                    )
                credentials_promoted = apply_credentials and (
                    old_cfg.wcl_client_id != new_cfg.wcl_client_id
                    or old_cfg.wcl_client_secret != new_cfg.wcl_client_secret
                )
                region_effective_changed = region_runtime.set_fallback(new_cfg.region)
                wcl_runtime_changed = credentials_promoted or region_effective_changed
                if new_cfg.wcl_client_id and new_cfg.wcl_client_secret:
                    if credentials_promoted:
                        auth = WCLAuth(
                            new_cfg.wcl_client_id,
                            new_cfg.wcl_client_secret,
                            new_cfg.cache_dir,
                        )
                        wcl_client.reconfigure_auth(auth)
                if wcl_runtime_changed:
                    wcl_client.region = region_runtime.effective_region
                if wcl_runtime_changed:
                    window.apply_metric_preferences(
                        new_cfg.metric_preferences,
                        refetch_missing=False,
                    )
                    window.bump_wcl_runtime_generation()
                else:
                    window.apply_metric_preferences(new_cfg.metric_preferences)

                new_screenshots_dir = (
                    Path(new_cfg.screenshots_path)
                    if new_cfg.screenshots_path is not None
                    else resolve_screenshots_path(new_cfg)
                )
                if new_screenshots_dir != current_screenshots_dir:
                    watcher = _replace_screenshot_watcher(
                        watcher,
                        new_screenshots_dir,
                        machine,
                        window,
                        _log_decode_failed,
                    )
                    current_screenshots_dir = new_screenshots_dir

                cfg = new_cfg
                overrides = _settings_env_override_keys()
            except (ConfigError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                log.warning("Could not apply settings change: %s", exc)
                dialog.set_status(f"Could not save/apply settings: {exc}", error=True)
                return
            if apply_credentials:
                status_text, status_error = _settings_wcl_test_success_status(
                    values,
                    overrides,
                )
            else:
                status_text, status_error = _settings_saved_status(values, overrides)
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
        dialog.updateCompleted.connect(_handle_dialog_update_completed)
        dialog.hideRequested.connect(lambda: None)
        dialog.quitRequested.connect(_quit_application)
        dialog.destroyed.connect(lambda *_args: _forget_dialog())
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
    )
    window.setWindowIcon(_app_icon())
    update_signals = UpdateSignals(app)

    def _run_update() -> None:
        def _worker() -> None:
            try:
                message, _url = _check_updates()
                update_signals.completed.emit(message, False)
            except Exception as exc:  # noqa: BLE001
                update_signals.completed.emit(f"Update failed: {exc}", True)

        threading.Thread(target=_worker, name="ApplicantScoutUpdater", daemon=True).start()

    def _handle_update_completed(message: str, error: bool) -> None:
        nonlocal pending_update_version
        if error:
            QMessageBox.warning(window, "ApplicantScout update", message)
            return
        pending_update_version = None
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
        def _worker() -> None:
            result = check_for_update(__version__)
            update_signals.checked.emit(result)

        threading.Thread(
            target=_worker,
            name="ApplicantScoutUpdateCheck",
            daemon=True,
        ).start()

    def _handle_update_checked(result: object) -> None:
        nonlocal pending_update_version
        latest_version = getattr(result, "latest_version", None)
        if getattr(result, "status", None) == "available" and _update_result_has_installable_asset(
            result
        ):
            pending_update_version = str(latest_version or "available")
        else:
            pending_update_version = None
        if tray_controller is not None:
            tray_controller.set_update_available(pending_update_version)
        if settings_dialog is not None:
            settings_dialog.set_update_available(pending_update_version)

    update_signals.checked.connect(_handle_update_checked)
    update_signals.completed.connect(_handle_update_completed)
    tray_controller = _create_tray_controller(
        app,
        icon=_app_icon(),
        window=window,
        show_settings=_show_settings,
        open_logs=lambda: _open_log_dir(cfg.log_dir or user_log_dir()),
        run_update=_run_update,
        quit_app=_quit_application,
    )
    if tray_controller is None:
        log.info("System tray indicator disabled.")
    if cfg.sync_with_wow:
        wow_exit_timer = _start_wow_lifecycle_timer(
            app,
            has_seen_wow=wow_watch_mode or is_wow_running(),
            quit_app=_quit_application,
        )
        setattr(app, "_applicant_scout_wow_exit_timer", wow_exit_timer)
    update_timer = QTimer(app)
    update_timer.setInterval(UPDATE_CHECK_INTERVAL_MS)
    update_timer.timeout.connect(_run_silent_update_check)
    update_timer.start()
    setattr(app, "_applicant_scout_update_timer", update_timer)
    QTimer.singleShot(UPDATE_CHECK_INITIAL_MS, _run_silent_update_check)
    if _should_show_settings_on_start(args, startup_settings_shown=startup_settings_shown):
        QTimer.singleShot(0, _show_settings)

    # Wire state-machine signals → overlay slots (unchanged from chatlog pipeline —
    # OverlayWindow API is transport-agnostic)
    machine.applicantAdded.connect(window.on_applicant_added)
    machine.applicantUpdated.connect(window.on_applicant_updated)
    machine.applicantRemoved.connect(window.on_applicant_removed)
    machine.listingChanged.connect(window.on_listing_changed)
    machine.cleared.connect(window.on_cleared)

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
