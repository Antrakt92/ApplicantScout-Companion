"""Application bootstrap: QApplication + runtime controllers + quit pipeline.

Pure moves from ``applicant_scout.__main__.main``. Main keeps the exact
initialization order (duplicate probe → QApplication → snapshot dispatcher →
icon → startup configurator → runtime controllers → quit pipeline → control
server → … → tray → watcher → event loop); this module only owns the branchy
blocks so they are unit-testable without a running GUI.

Import direction is intentionally one-way: this module never imports
``applicant_scout.__main__`` at module level (that would be a cycle because
``__main__`` imports this module). Callables defined in ``__main__`` are
resolved lazily inside the builders via injectable defaults, so tests can
pass fakes and production uses the real implementations.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("applicant_scout")


@dataclass
class AppRuntime:
    """Early runtime controllers created right after the QApplication."""

    update_quit_gate: Any
    watcher_signal_gate: Any
    update_signals: Any
    show_settings_action: Any
    wow_sync_startup_configurator: Any
    # Mutable holder for the async settings-download control; main swaps it
    # via _set_update_in_progress, the quit pipeline only reads it.
    active_update_control_holder: dict[str, Any] = field(default_factory=dict)


@dataclass
class QuitPipeline:
    """Quit-time ordering: flush once, then quit; gates stay injectable.

    The async settings drain is synced into ``settings_drain_holder`` (see
    ``make_quit_pipeline``) through main's settings-drain setter, which
    assigns and syncs together so rebinds cannot go stale; the flush reads
    the holder live at quit time.
    """

    flush: Callable[[], None]
    quit_app: Callable[[], None]
    can_quit: Callable[[], bool]
    can_control_quit: Callable[[], bool]
    prepare_quit: Callable[[], bool]
    prepare_control_quit: Callable[[], bool]
    request_quit: Callable[[], None]


def build_qapplication(args: list[str]):
    """Create the QApplication with app identity, dispatcher, and icon.

    Pure move of the ``main`` block starting at ``_set_windows_app_user_model_id``.
    The logging-setup warning dialog stays in ``main`` so startup ordering
    (warning surfaces after the app exists) is unchanged; the warning text is
    returned via the app attribute ``_applicant_scout_logging_warning``.
    """
    from . import __main__ as _main

    _main._set_windows_app_user_model_id()
    # Resolve via __main__ so tests monkeypatching main_mod.QApplication keep
    # working (duplicate-launch must never create a real QApplication).
    app = _main.QApplication([sys.argv[0], *args])
    app.setApplicationName("ApplicantScout")
    if isinstance(app, _main.QObject):
        _main._initialize_snapshot_apply_dispatcher(app)
    if isinstance(app, _main._QT_APPLICATION_CLASS):
        app.setWindowIcon(_main._app_icon())
    return app


def build_runtime_controllers(app) -> AppRuntime:
    """Create early controllers in ``main`` order (pure move, no behavior change)."""
    from . import __main__ as _main

    show_settings_action = _main._DeferredGuiAction()
    update_quit_gate = _main._UpdateQuitGate()
    watcher_signal_gate = _main._WatcherSignalGate()
    update_signals = _main.UpdateSignals(
        app if isinstance(app, _main.QObject) else None
    )
    wow_sync_startup_configurator = _main._WowSyncStartupConfigurator(
        app if isinstance(app, _main.QObject) else None
    )
    return AppRuntime(
        update_quit_gate=update_quit_gate,
        watcher_signal_gate=watcher_signal_gate,
        update_signals=update_signals,
        show_settings_action=show_settings_action,
        wow_sync_startup_configurator=wow_sync_startup_configurator,
    )


def run_duplicate_probe(
    duplicate_command: bytes | None,
    *,
    _send_command: Callable[..., Any] | None = None,
    _acknowledged: Callable[[Any], bool] | None = None,
    _timeout_ms: int | None = None,
    _server_names: tuple[str, ...] | None = None,
) -> int | None:
    """Run the pre-QApplication duplicate-instance probe (pure move from ``main``).

    Returns ``0``/``1`` when this process must exit, ``None`` to continue startup.
    ``duplicate_command`` is ``None`` when no probe is needed (e.g. WoW-watch mode).
    """
    if duplicate_command is None:
        return None
    from . import __main__ as _main

    send_command = _send_command or _main._send_control_command
    acknowledged = _acknowledged or _main._control_command_acknowledged
    timeout_ms = _main._DUPLICATE_PROBE_TIMEOUT_MS if _timeout_ms is None else _timeout_ms
    server_names = (_main.CONTROL_SERVER_NAME,) if _server_names is None else _server_names
    # H2 fast path: a single scoped connect with a short timeout, before
    # QApplication exists. The legacy endpoint is still probed after
    # startup by _create_control_server, so older instances keep working.
    result = send_command(
        duplicate_command,
        timeout_ms=timeout_ms,
        server_names=server_names,
    )
    if acknowledged(result):
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
    return None


def make_quit_pipeline(
    *,
    app,
    update_quit_gate,
    watcher_signal_gate,
    get_settings_dialog: Callable[[], Any],
    get_window: Callable[[], Any],
    get_watcher: Callable[[], Any],
    get_active_update_control: Callable[[], Any],
    get_tray_controller: Callable[[], Any],
    get_update_handoff_recovery: Callable[[], Any] | None = None,
) -> QuitPipeline:
    """Build the quit-time pipeline (pure move of ``main`` nested closures)."""
    from . import __main__ as _main

    drain_holder: dict[str, Callable[[], bool] | None] = {"drain": None}

    def _check_updates_with_handoff():
        return _main._check_updates(
            update_quit_gate=update_quit_gate,
            control=get_active_update_control(),
        )

    def _flush_before_quit_impl() -> None:
        _main._quiesce_screenshot_ingestion(
            get_watcher(),
            watcher_signal_gate,
        )
        dialog = get_settings_dialog()
        if dialog is not None:
            dialog.flush_pending_values()
        drain = drain_holder["drain"]
        if drain is not None and not drain():
            log.warning("Settings apply did not finish before quit; continuing.")
        window = get_window()
        if window is not None:
            window.flush_geometry()

    # The watcher gate is main's own gate (same object, same order). No lazy copy.
    _flush_before_quit = _main._make_one_shot_callback(_flush_before_quit_impl)

    def _quit_application() -> None:
        recovery = get_update_handoff_recovery() if get_update_handoff_recovery else None
        disarm = getattr(recovery, "disarm", None)
        if callable(disarm):
            disarm()
        _flush_before_quit()
        app.quit()

    def _can_quit_application() -> bool:
        return bool(update_quit_gate.can_user_quit())

    def _can_control_quit_application() -> bool:
        return bool(update_quit_gate.can_control_quit())

    def _show_update_quit_blocked() -> None:
        tray = get_tray_controller()
        if tray is not None:
            tray.show_update_quit_blocked()
            return
        dialog = get_settings_dialog()
        if dialog is not None:
            dialog.set_status(_main.UPDATE_QUIT_BLOCKED_MESSAGE, error=True)
            return
        log.info(_main.UPDATE_QUIT_BLOCKED_MESSAGE)

    def _show_settings_quit_blocked() -> None:
        dialog = get_settings_dialog()
        if dialog is not None:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return
        log.info(_main.SETTINGS_QUIT_BLOCKED_MESSAGE)

    def _prepare_quit_application() -> bool:
        if not _can_quit_application():
            _show_update_quit_blocked()
            return False
        if not _main._prepare_settings_before_quit(get_settings_dialog()):
            _show_settings_quit_blocked()
            return False
        return True

    def _prepare_control_quit_application() -> bool:
        if not _can_control_quit_application():
            _show_update_quit_blocked()
            return False
        return bool(update_quit_gate.prepare_control_quit(_prepare_quit_application))

    def _request_quit_application() -> None:
        if not _prepare_quit_application():
            return
        _quit_application()

    about_to_quit = getattr(app, "aboutToQuit", None)
    if about_to_quit is not None:
        about_to_quit.connect(_flush_before_quit)

    pipeline = QuitPipeline(
        flush=_flush_before_quit,
        quit_app=_quit_application,
        can_quit=_can_quit_application,
        can_control_quit=_can_control_quit_application,
        prepare_quit=_prepare_quit_application,
        prepare_control_quit=_prepare_control_quit_application,
        request_quit=_request_quit_application,
    )
    # Expose helpers main still needs inline (update handoff + drain holder).
    pipeline.__dict__["check_updates_with_handoff"] = _check_updates_with_handoff  # type: ignore[attr-defined]
    pipeline.__dict__["show_update_quit_blocked"] = _show_update_quit_blocked  # type: ignore[attr-defined]
    pipeline.__dict__["settings_drain_holder"] = drain_holder  # type: ignore[attr-defined]
    return pipeline
