"""Single-runtime ownership and local control-channel primitives."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from dataclasses import dataclass
import getpass
import hashlib
import logging
import os
import sys
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


log = logging.getLogger("applicant_scout")

CONTROL_SERVER_BASENAME = "Antrakt.ApplicantScout.Companion.Control"


def scoped_control_server_name(user_identity: str) -> str:
    normalized = user_identity.strip().casefold().encode("utf-8", errors="replace")
    suffix = hashlib.sha256(normalized).hexdigest()[:16]
    return f"{CONTROL_SERVER_BASENAME}.{suffix}"


def runtime_user_identity() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    username = getpass.getuser().strip()
    return f"{domain}\\{username}" if domain else username


CONTROL_SERVER_NAME = scoped_control_server_name(runtime_user_identity())
LEGACY_CONTROL_SERVER_NAME = CONTROL_SERVER_BASENAME
# WHY: config/cache are per-user rather than per-terminal-session. The user hash
# plus the process token's default ACL keeps the Global mutex scoped to that
# account while covering Fast User Switching and RDP sessions.
CONTROL_OWNER_NAME = f"Global\\{CONTROL_SERVER_NAME}.Owner"
CONTROL_QUIT_COMMAND = b"quit"
CONTROL_SHOW_SETTINGS_COMMAND = b"show-settings"


@dataclass(frozen=True)
class ControlCommandResult:
    connected: bool
    written: bool
    response: bytes | None = None
    error: str | None = None


class DuplicateInstanceFound(RuntimeError):
    pass


class ControlServerUnavailable(RuntimeError):
    pass


class RuntimeOwner:
    def __init__(self, handle: int | None = None) -> None:
        self._handle = handle

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None or sys.platform != "win32":
            return
        try:
            close_handle = ctypes.windll.kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(ctypes.c_void_p(handle))
        except (AttributeError, OSError, ValueError):
            log.warning("Could not release ApplicantScout runtime ownership.")

    def __del__(self) -> None:
        self.close()


def acquire_runtime_owner(owner_name: str = CONTROL_OWNER_NAME) -> RuntimeOwner | None:
    """Atomically claim one per-user runtime on Windows."""
    if sys.platform != "win32":
        return RuntimeOwner()
    try:
        kernel32 = ctypes.windll.kernel32
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        handle = create_mutex(None, False, owner_name)
        if not handle:
            raise OSError("CreateMutexW returned no handle")
        already_exists = int(kernel32.GetLastError()) == 183
    except (AttributeError, OSError, ValueError) as exc:
        raise ControlServerUnavailable(
            f"could not claim Windows runtime ownership: {exc}"
        ) from exc
    owner = RuntimeOwner(int(handle))
    if already_exists:
        owner.close()
        return None
    return owner


def send_control_command(
    command: bytes,
    *,
    timeout_ms: int = 2000,
    socket_factory: Callable[[], Any] = QLocalSocket,
    server_names: tuple[str, ...] = (
        CONTROL_SERVER_NAME,
        LEGACY_CONTROL_SERVER_NAME,
    ),
) -> ControlCommandResult:
    last_error: str | None = None
    for server_name in dict.fromkeys(server_names):
        socket = socket_factory()
        socket.connectToServer(server_name)
        if not socket.waitForConnected(timeout_ms):
            last_error = socket.errorString()
            continue
        payload = command.rstrip() + b"\n"
        socket.write(payload)
        if not socket.waitForBytesWritten(timeout_ms):
            error = socket.errorString()
            socket.disconnectFromServer()
            return ControlCommandResult(connected=True, written=False, error=error)
        response = None
        if socket.waitForReadyRead(500):
            response = socket.readAll().data().strip().lower()
        socket.disconnectFromServer()
        return ControlCommandResult(connected=True, written=True, response=response)
    return ControlCommandResult(
        connected=False,
        written=False,
        error=last_error,
    )


def shutdown_running_instance(
    *,
    timeout_ms: int = 2000,
    send_command: Callable[..., ControlCommandResult] = send_control_command,
) -> int:
    result = send_command(CONTROL_QUIT_COMMAND, timeout_ms=timeout_ms)
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


def control_command_acknowledged(result: ControlCommandResult) -> bool:
    return result.connected and result.written and result.response == b"ok"


def has_running_instance(
    *,
    timeout_ms: int = 200,
    socket_factory: Callable[[], Any] = QLocalSocket,
    server_names: tuple[str, ...] = (
        CONTROL_SERVER_NAME,
        LEGACY_CONTROL_SERVER_NAME,
    ),
) -> bool:
    for server_name in dict.fromkeys(server_names):
        socket = socket_factory()
        socket.connectToServer(server_name)
        if socket.waitForConnected(timeout_ms):
            socket.disconnectFromServer()
            return True
    return False


def create_control_server(
    app: Any,
    *,
    quit_app: Callable[[], None],
    show_settings: Callable[[], None],
    can_quit: Callable[[], bool] | None = None,
    prepare_quit: Callable[[], bool] | None = None,
    quit_blocked: Callable[[], None] | None = None,
    acquire_owner: Callable[[], RuntimeOwner | None] = acquire_runtime_owner,
    send_command: Callable[..., ControlCommandResult] = send_control_command,
    local_server_type: Any = QLocalServer,
    server_name: str = CONTROL_SERVER_NAME,
    show_settings_command: bytes = CONTROL_SHOW_SETTINGS_COMMAND,
    drain_connections: Callable[..., None] | None = None,
) -> Any:
    runtime_owner = acquire_owner()
    if runtime_owner is None:
        active_owner = send_command(show_settings_command, timeout_ms=200)
        if active_owner.connected and active_owner.written:
            if not control_command_acknowledged(active_owner):
                log.info(
                    "Concurrent runtime owns startup; settings request was queued "
                    "without an immediate acknowledgement."
                )
            raise DuplicateInstanceFound
        log.info("Concurrent ApplicantScout runtime owns startup.")
        raise DuplicateInstanceFound

    server = local_server_type(app)
    try:
        socket_option = getattr(
            getattr(local_server_type, "SocketOption", None),
            "UserAccessOption",
            None,
        )
        set_socket_options = getattr(server, "setSocketOptions", None)
        if socket_option is not None and callable(set_socket_options):
            set_socket_options(socket_option)
        if not server.listen(server_name):
            active_owner = send_command(show_settings_command, timeout_ms=200)
            if control_command_acknowledged(active_owner):
                raise DuplicateInstanceFound
            if active_owner.connected and active_owner.written:
                log.warning(
                    "Control server owner returned unexpected response while probing: %r",
                    active_owner.response,
                )
                raise DuplicateInstanceFound
            local_server_type.removeServer(server_name)
            if not server.listen(server_name):
                raise ControlServerUnavailable(server.errorString())
    except BaseException:
        runtime_owner.close()
        raise

    setattr(server, "_applicant_scout_runtime_owner", runtime_owner)
    drain = drain_connections or drain_control_connections
    server.newConnection.connect(
        lambda: drain(
            server,
            quit_app,
            show_settings,
            can_quit=can_quit,
            prepare_quit=prepare_quit,
            quit_blocked=quit_blocked,
        )
    )
    return server


def drain_control_connections(
    server: Any,
    quit_app: Callable[[], None],
    show_settings: Callable[[], None],
    *,
    can_quit: Callable[[], bool] | None = None,
    prepare_quit: Callable[[], bool] | None = None,
    quit_blocked: Callable[[], None] | None = None,
    handle_command: Callable[..., None] | None = None,
) -> None:
    if handle_command is None:
        handle_command = handle_control_command
    while server.hasPendingConnections():
        socket = server.nextPendingConnection()
        if socket is None:
            continue
        socket.readyRead.connect(
            lambda _socket=socket: handle_command(
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
            handle_command(
                socket,
                quit_app,
                show_settings,
                can_quit=can_quit,
                prepare_quit=prepare_quit,
                quit_blocked=quit_blocked,
            )


def handle_control_command(
    socket: Any,
    quit_app: Callable[[], None],
    show_settings: Callable[[], None] | None = None,
    *,
    can_quit: Callable[[], bool] | None = None,
    prepare_quit: Callable[[], bool] | None = None,
    quit_blocked: Callable[[], None] | None = None,
    schedule: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    schedule_callback = schedule
    if schedule_callback is None:
        def _schedule(callback: Callable[[], None]) -> None:
            QTimer.singleShot(0, callback)
        schedule_callback = _schedule
    command = socket.readAll().data().strip().lower()
    if command == CONTROL_QUIT_COMMAND:
        if can_quit is not None and not can_quit():
            socket.write(b"blocked\n")
            socket.flush()
            socket.waitForBytesWritten(100)
            socket.disconnectFromServer()
            if quit_blocked is not None:
                schedule_callback(quit_blocked)
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
        schedule_callback(quit_app)
        return
    if command == CONTROL_SHOW_SETTINGS_COMMAND and show_settings is not None:
        socket.write(b"ok\n")
        socket.flush()
        socket.waitForBytesWritten(100)
        socket.disconnectFromServer()
        schedule_callback(show_settings)
        return
    socket.write(b"unknown\n")
    socket.flush()
    socket.disconnectFromServer()
