"""Visual rendering tears down actual window resources even on failure."""

import pytest
import sys
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

import applicant_scout.overlay as overlay
from scripts import render_overlay_fixture as renderer
from scripts.overlay_visual_fixture import (
    cleanup_overlay_visual_window,
    create_overlay_visual_window,
)


@pytest.mark.parametrize("failure", [None, "show", "grab", "null"])
def test_actual_renderer_shuts_down_before_close_and_disposal(
    qtbot, monkeypatch, failure
):
    events = []
    instances = []

    class Hook:
        def start(self, _callback):
            events.append("hook started")

        def stop(self):
            events.append("hook stopped")

    monkeypatch.setattr(overlay, "ForegroundHook", Hook)
    create = renderer.create_overlay_visual_window

    def capture(*args, **kwargs):
        state, window, client = create(*args, **kwargs)
        qtbot.addWidget(window)
        instances.append(window)
        shutdown = window.shutdown_fetches
        close = window.close
        dispose = client.close

        def shut():
            events.append("shutdown")
            return shutdown()

        def closing():
            events.append("window close")
            return close()

        def disposing():
            events.append("client disposed")
            return dispose()

        monkeypatch.setattr(window, "shutdown_fetches", shut)
        monkeypatch.setattr(window, "close", closing)
        monkeypatch.setattr(client, "close", disposing)
        return state, window, client

    def show(*_args, **_kwargs):
        if failure == "show":
            raise RuntimeError("show failed")

    def grab(_window):
        if failure == "grab":
            raise RuntimeError("grab failed")
        return QPixmap() if failure == "null" else QPixmap(10, 10)

    monkeypatch.setattr(renderer, "create_overlay_visual_window", capture)
    monkeypatch.setattr(renderer, "show_overlay_visual_window", show)
    monkeypatch.setattr(renderer, "grab_overlay_visual_image", grab)
    try:
        if failure:
            with pytest.raises(RuntimeError):
                renderer._render_fixture_pixmap(
                    QApplication.instance(), "applicants-default"
                )
        else:
            assert not renderer._render_fixture_pixmap(
                QApplication.instance(), "applicants-default"
            ).isNull()
        assert events.count("hook started") == events.count("hook stopped") == 1
        assert events.index("shutdown") < events.index("window close")
        assert events.index("shutdown") < events.index("client disposed")
        assert instances[0]._closed
        assert instances[0]._foreground_hook is None
        assert not instances[0]._foreground_timer.isActive()
    finally:
        # Keep a failed baseline from leaving a hook/window alive in the test process.
        for window in instances:
            window.shutdown_fetches()


@pytest.mark.real_display
@pytest.mark.skipif(
    sys.platform != "win32", reason="Native foreground hook requires Windows"
)
def test_renderer_terminates_native_hook_threads(qtbot, monkeypatch):
    hooks = []
    windows = []
    create = renderer.create_overlay_visual_window

    def capture(*args, **kwargs):
        result = create(*args, **kwargs)
        window = result[1]
        qtbot.addWidget(window)
        windows.append(window)
        hook = window._foreground_hook
        assert hook is not None and hook.running
        hooks.append(hook)
        return result

    monkeypatch.setattr(renderer, "create_overlay_visual_window", capture)
    monkeypatch.setattr(renderer, "show_overlay_visual_window", lambda *_a, **_k: None)
    monkeypatch.setattr(
        renderer, "grab_overlay_visual_image", lambda _w: QPixmap(10, 10)
    )
    try:
        for _ in range(2):
            renderer._render_fixture_pixmap(
                QApplication.instance(), "applicants-default"
            )
        assert len(hooks) == 2
        assert all(not hook.running for hook in hooks)
        assert all(
            window._closed and window._foreground_hook is None for window in windows
        )
    finally:
        for window in windows:
            window.shutdown_fetches()


@pytest.mark.parametrize("failure", ["shutdown", "window close"])
def test_cleanup_preserves_disposal_when_earlier_stage_raises(failure):
    events = []

    class Window:
        def shutdown_fetches(self):
            events.append("shutdown")
            if failure == "shutdown":
                raise RuntimeError(failure)

        def close(self):
            events.append("window close")
            if failure == "window close":
                raise RuntimeError(failure)

    class Client:
        def close(self):
            events.append("client close")

    with pytest.raises(RuntimeError, match=failure):
        cleanup_overlay_visual_window(Window(), Client())
    assert events == ["shutdown", "window close", "client close"]


def test_repeated_actual_fixture_cleanup_stops_hook_once(qtbot, tmp_path, monkeypatch):
    events = []

    class Hook:
        def start(self, _callback):
            events.append("started")

        def stop(self):
            events.append("stopped")

    monkeypatch.setattr(overlay, "ForegroundHook", Hook)
    _state, window, client = create_overlay_visual_window(tmp_path)
    qtbot.addWidget(window)
    try:
        cleanup_overlay_visual_window(window, client)
        cleanup_overlay_visual_window(window, client)
        assert events == ["started", "stopped"]
        assert window._closed and window._foreground_hook is None
    finally:
        window.shutdown_fetches()
