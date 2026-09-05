from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any, cast

import pytest

from applicant_scout import __main__ as main_mod
from applicant_scout import runtime_control


class _Signal:
    def __init__(self) -> None:
        self._callbacks: list[Any] = []

    def connect(self, callback: Any) -> None:
        self._callbacks.append(callback)

    def emit(self) -> None:
        for callback in tuple(self._callbacks):
            callback()


class _ServerSocket:
    def __init__(self) -> None:
        self.readyRead = _Signal()
        self.disconnected = _Signal()
        self._incoming = bytearray()
        self.calls: list[str] = []
        self.read_limits: list[int] = []

    def feed(self, chunk: bytes) -> None:
        self._incoming.extend(chunk)
        self.readyRead.emit()

    def prime(self, chunk: bytes) -> None:
        self._incoming.extend(chunk)

    def readAll(self) -> SimpleNamespace:
        chunk = bytes(self._incoming)
        self._incoming.clear()
        return SimpleNamespace(data=lambda: chunk)

    def read(self, max_bytes: int) -> SimpleNamespace:
        self.read_limits.append(max_bytes)
        chunk = bytes(self._incoming[:max_bytes])
        del self._incoming[:max_bytes]
        return SimpleNamespace(data=lambda: chunk)

    def bytesAvailable(self) -> int:
        return len(self._incoming)

    def write(self, value: bytes) -> None:
        self.calls.append(f"write:{value.decode().strip()}")

    def flush(self) -> None:
        self.calls.append("flush")

    def waitForBytesWritten(self, timeout_ms: int) -> bool:
        self.calls.append(f"wait-written:{timeout_ms}")
        return True

    def disconnectFromServer(self) -> None:
        self.calls.append("disconnect")
        self.disconnected.emit()

    def deleteLater(self) -> None:
        self.calls.append("delete")


class _Server:
    def __init__(self, *sockets: _ServerSocket) -> None:
        self._sockets = deque(sockets)

    def hasPendingConnections(self) -> bool:
        return bool(self._sockets)

    def nextPendingConnection(self) -> _ServerSocket | None:
        return self._sockets.popleft() if self._sockets else None


def _handle_control_command_with_immediate_schedule(
    socket: Any,
    quit_app: Any,
    show_settings: Any,
    **kwargs: Any,
) -> None:
    runtime_control.handle_control_command(
        socket,
        quit_app,
        show_settings,
        schedule=lambda callback: callback(),
        **kwargs,
    )


def test_control_server_waits_for_a_complete_fragmented_command_frame():
    socket = _ServerSocket()
    calls: list[str] = []
    runtime_control.drain_control_connections(
        _Server(socket),
        lambda: calls.append("quit"),
        lambda: calls.append("show"),
        handle_command=_handle_control_command_with_immediate_schedule,
    )

    socket.feed(b"qu")
    assert calls == []
    assert socket.calls == []

    socket.feed(b"it\n")
    socket.readyRead.emit()
    assert calls == ["quit"]
    assert max(socket.read_limits) <= runtime_control.CONTROL_FRAME_MAX_BYTES + 2
    assert socket.calls == [
        "write:ok",
        "flush",
        "wait-written:100",
        "disconnect",
        "delete",
    ]


def test_control_server_buffers_fragmented_show_settings_command():
    socket = _ServerSocket()
    calls: list[str] = []
    runtime_control.drain_control_connections(
        _Server(socket),
        lambda: calls.append("quit"),
        lambda: calls.append("show"),
        handle_command=_handle_control_command_with_immediate_schedule,
    )

    socket.feed(b"show-")
    socket.feed(b"settings\n")

    assert calls == ["show"]
    assert socket.calls[:3] == ["write:ok", "flush", "wait-written:100"]


def test_control_server_accepts_existing_crlf_command_bytes_on_connect():
    socket = _ServerSocket()
    socket.prime(b"  QuIt \r\n")
    calls: list[str] = []

    runtime_control.drain_control_connections(
        _Server(socket),
        lambda: calls.append("quit"),
        lambda: calls.append("show"),
        handle_command=_handle_control_command_with_immediate_schedule,
    )

    assert calls == ["quit"]
    assert socket.calls[0] == "write:ok"


def test_control_server_keeps_fragment_buffers_isolated_per_connection():
    quit_socket = _ServerSocket()
    settings_socket = _ServerSocket()
    calls: list[str] = []
    runtime_control.drain_control_connections(
        _Server(quit_socket, settings_socket),
        lambda: calls.append("quit"),
        lambda: calls.append("show"),
        handle_command=_handle_control_command_with_immediate_schedule,
    )

    quit_socket.feed(b"qu")
    settings_socket.feed(b"show-")
    quit_socket.feed(b"it\n")
    settings_socket.feed(b"settings\n")

    assert calls == ["quit", "show"]
    assert quit_socket.calls[0] == "write:ok"
    assert settings_socket.calls[0] == "write:ok"


def test_control_server_does_not_dispatch_an_incomplete_disconnected_frame():
    socket = _ServerSocket()
    calls: list[str] = []
    runtime_control.drain_control_connections(
        _Server(socket),
        lambda: calls.append("quit"),
        lambda: calls.append("show"),
        handle_command=_handle_control_command_with_immediate_schedule,
    )

    socket.feed(b"quit")
    socket.disconnected.emit()

    assert calls == []
    assert socket.calls == ["delete"]


@pytest.mark.parametrize(
    "payload",
    [
        b"x" * (runtime_control.CONTROL_FRAME_MAX_BYTES + 1),
        b" " * runtime_control.CONTROL_FRAME_MAX_BYTES + b"quit\n",
    ],
)
def test_control_server_rejects_an_oversized_frame_without_dispatching_it(
    payload: bytes,
):
    socket = _ServerSocket()
    calls: list[str] = []
    runtime_control.drain_control_connections(
        _Server(socket),
        lambda: calls.append("quit"),
        lambda: calls.append("show"),
        handle_command=_handle_control_command_with_immediate_schedule,
    )

    socket.feed(payload)

    assert calls == []
    assert socket.read_limits == [runtime_control.CONTROL_FRAME_MAX_BYTES + 2]
    assert socket.calls == ["write:unknown", "flush", "disconnect", "delete"]


class _ClientSocket:
    def __init__(self, *response_chunks: bytes) -> None:
        self._response_chunks = deque(response_chunks)
        self.calls: list[str] = []
        self.read_limits: list[int] = []

    def connectToServer(self, server_name: str) -> None:
        self.calls.append(f"connect:{server_name}")

    def waitForConnected(self, timeout_ms: int) -> bool:
        self.calls.append(f"wait-connected:{timeout_ms}")
        return True

    def write(self, value: bytes) -> None:
        self.calls.append(f"write:{value.decode().strip()}")

    def waitForBytesWritten(self, timeout_ms: int) -> bool:
        self.calls.append(f"wait-written:{timeout_ms}")
        return True

    def waitForReadyRead(self, timeout_ms: int) -> bool:
        self.calls.append(f"wait-ready:{timeout_ms}")
        return bool(self._response_chunks)

    def readAll(self) -> SimpleNamespace:
        chunk = self._response_chunks.popleft()
        self.calls.append(f"read:{chunk!r}")
        return SimpleNamespace(data=lambda: chunk)

    def read(self, max_bytes: int) -> SimpleNamespace:
        self.read_limits.append(max_bytes)
        chunk = self._response_chunks.popleft()
        value, remainder = chunk[:max_bytes], chunk[max_bytes:]
        if remainder:
            self._response_chunks.appendleft(remainder)
        self.calls.append(f"read:{value!r}")
        return SimpleNamespace(data=lambda: value)

    def disconnectFromServer(self) -> None:
        self.calls.append("disconnect")

    def errorString(self) -> str:
        return "socket error"


class _BufferedClientSocket(_ClientSocket):
    def __init__(self, buffered: bytes, *response_chunks: bytes) -> None:
        super().__init__(*response_chunks)
        self._incoming = bytearray(buffered)

    def bytesAvailable(self) -> int:
        return len(self._incoming)

    def waitForReadyRead(self, timeout_ms: int) -> bool:
        self.calls.append(f"wait-ready:{timeout_ms}")
        if not self._response_chunks:
            return False
        self._incoming.extend(self._response_chunks.popleft())
        return True

    def read(self, max_bytes: int) -> SimpleNamespace:
        self.read_limits.append(max_bytes)
        value = bytes(self._incoming[:max_bytes])
        del self._incoming[:max_bytes]
        return SimpleNamespace(data=lambda: value)


@pytest.mark.parametrize("response", [b"ok", b"blocked"])
def test_send_control_command_consumes_already_buffered_acknowledgment(response):
    socket = _BufferedClientSocket(response + b"\n")

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_QUIT_COMMAND,
        socket_factory=lambda: socket,
        server_names=("test",),
    )

    assert result.response == response
    assert result.error is None
    assert not any(call.startswith("wait-ready:") for call in socket.calls)
    assert socket.calls.count("disconnect") == 1


def test_send_control_command_completes_buffered_response_with_later_fragment():
    socket = _BufferedClientSocket(b"o", b"k\n")

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_SHOW_SETTINGS_COMMAND,
        socket_factory=lambda: socket,
        server_names=("test",),
    )

    assert result.response == b"ok"
    assert result.error is None
    assert len([call for call in socket.calls if call.startswith("wait-ready:")]) == 1


@pytest.mark.parametrize(
    "response,error",
    [
        (b"ok", "control response ended before a complete newline frame"),
        (b"ok\nblocked\n", "control response frame contained trailing bytes"),
        (b"x" * 65, "control response frame exceeded the size limit"),
    ],
)
def test_send_control_command_validates_already_buffered_response(response, error):
    socket = _BufferedClientSocket(response)

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_QUIT_COMMAND,
        socket_factory=lambda: socket,
        server_names=("test",),
    )

    assert result.response is None
    assert result.error == error
    assert max(socket.read_limits) <= runtime_control.CONTROL_FRAME_MAX_BYTES + 2


def test_send_control_command_stops_when_buffered_socket_returns_no_bytes():
    class UnreadableSocket(_BufferedClientSocket):
        def read(self, max_bytes: int) -> SimpleNamespace:
            self.read_limits.append(max_bytes)
            assert len(self.read_limits) == 1
            return SimpleNamespace(data=lambda: b"")

    socket = UnreadableSocket(b"ok\n")

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_QUIT_COMMAND,
        socket_factory=lambda: socket,
        server_names=("test",),
    )

    assert result.response is None
    assert result.error == "control response was not received"
    assert socket.calls.count("disconnect") == 1


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((b"o", b"k\n"), b"ok"),
        ((b"block", b"ed\n"), b"blocked"),
        ((b"unknown\n",), b"unknown"),
    ],
)
def test_send_control_command_buffers_a_complete_response_frame(
    chunks: tuple[bytes, ...],
    expected: bytes,
):
    socket = _ClientSocket(*chunks)

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_QUIT_COMMAND,
        socket_factory=lambda: socket,
        server_names=("test",),
    )

    assert result.response == expected
    assert result.error is None
    assert max(socket.read_limits) <= runtime_control.CONTROL_FRAME_MAX_BYTES + 2
    assert socket.calls.count("disconnect") == 1


def test_send_control_command_rejects_a_response_without_a_newline():
    socket = _ClientSocket(b"ok")

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_QUIT_COMMAND,
        socket_factory=lambda: socket,
        server_names=("test",),
    )

    assert result.response is None
    assert result.error == "control response ended before a complete newline frame"
    assert socket.calls.count("disconnect") == 1


def test_send_control_command_rejects_an_oversized_response_frame():
    socket = _ClientSocket(
        b"x" * (runtime_control.CONTROL_FRAME_MAX_BYTES + 1),
    )

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_QUIT_COMMAND,
        socket_factory=lambda: socket,
        server_names=("test",),
    )

    assert result.response is None
    assert result.error == "control response frame exceeded the size limit"
    assert socket.read_limits == [runtime_control.CONTROL_FRAME_MAX_BYTES + 2]
    assert socket.calls.count("disconnect") == 1


def test_send_control_command_rejects_trailing_bytes_after_the_response_frame():
    socket = _ClientSocket(b"ok\nunknown\n")

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_QUIT_COMMAND,
        socket_factory=lambda: socket,
        server_names=("test",),
    )

    assert result.response is None
    assert result.error == "control response frame contained trailing bytes"


def test_send_control_command_uses_one_total_response_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    socket = _ClientSocket(b"o", b"k")
    monotonic_values = iter((10.0, 10.3, 10.6))
    monkeypatch.setattr(
        runtime_control.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_QUIT_COMMAND,
        socket_factory=lambda: socket,
        server_names=("test",),
    )

    assert result.response is None
    assert result.error == "control response ended before a complete newline frame"
    assert [call for call in socket.calls if call.startswith("wait-ready:")] == [
        "wait-ready:500",
        "wait-ready:200",
    ]


def test_malformed_scoped_response_does_not_fall_through_to_legacy_endpoint():
    sockets = deque((_ClientSocket(b"ok"), _ClientSocket(b"ok\n")))

    result = runtime_control.send_control_command(
        runtime_control.CONTROL_QUIT_COMMAND,
        socket_factory=sockets.popleft,
        server_names=("scoped", "legacy"),
    )

    assert result.response is None
    assert result.error == "control response ended before a complete newline frame"
    assert len(sockets) == 1


def test_main_control_wrapper_forwards_an_already_parsed_frame(
    monkeypatch: pytest.MonkeyPatch,
):
    socket = _ServerSocket()
    calls: list[str] = []
    monkeypatch.setattr(
        main_mod.QTimer,
        "singleShot",
        lambda _interval, callback: callback(),
    )

    main_mod._handle_control_command(
        cast(Any, socket),
        lambda: calls.append("quit"),
        lambda: calls.append("show"),
        command=b"quit",
    )

    assert calls == ["quit"]
    assert socket.calls[0] == "write:ok"
