"""Visual baselines must not inherit a hosted runner's small desktop bounds."""
from PySide6.QtCore import QRect
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication
import pytest

from scripts.overlay_visual_fixture import (
    create_overlay_visual_window, cleanup_overlay_visual_window, show_overlay_visual_window,
)
from scripts.settings_dialog_visual_fixture import create_settings_visual_dialog, show_settings_visual_dialog


@pytest.mark.parametrize("scenario", ["applicants-default", "raid-listing"])
def test_overlay_fixture_size_ignores_small_desktop(qtbot, tmp_path, monkeypatch, scenario):
    def size(directory):
        directory.mkdir()
        _, window, client = create_overlay_visual_window(directory, scenario)
        qtbot.addWidget(window)
        try:
            show_overlay_visual_window(window, scenario, process_events=QApplication.processEvents)
            return window.size()
        finally:
            cleanup_overlay_visual_window(window, client)

    expected = size(tmp_path / "normal")
    monkeypatch.setattr(QScreen, "availableGeometry", lambda _self: QRect(0, 0, 819, 582))
    small_screen_method = QScreen.availableGeometry
    assert size(tmp_path / "small") == expected
    assert QScreen.availableGeometry is small_screen_method


def test_settings_fixture_size_ignores_small_desktop(qtbot, monkeypatch):
    def size():
        dialog = create_settings_visual_dialog("first-run")
        qtbot.addWidget(dialog)
        show_settings_visual_dialog(dialog, process_events=QApplication.processEvents)
        result = dialog.size()
        dialog.hide()
        return result

    expected = size()
    monkeypatch.setattr(QScreen, "availableGeometry", lambda _self: QRect(0, 0, 819, 400))
    small_screen_method = QScreen.availableGeometry
    assert size() == expected
    assert QScreen.availableGeometry is small_screen_method


def test_settings_fixture_uses_preferred_size_when_native_autosize_caps(qtbot, monkeypatch):
    from applicant_scout.settings_dialog import SettingsDialog

    # Model Qt's C++ adjustSize cap on a 768px hosted desktop. That code reads
    # native screen metrics, bypassing Python QScreen.availableGeometry mocks.
    def constrained_adjust_size(dialog):
        preferred = dialog.sizeHint()
        dialog.resize(preferred.width(), min(preferred.height(), 409))

    monkeypatch.setattr(SettingsDialog, "adjustSize", constrained_adjust_size)
    dialog = create_settings_visual_dialog("first-run")
    qtbot.addWidget(dialog)
    preferred = dialog.sizeHint()
    assert preferred.height() > 409
    show_settings_visual_dialog(dialog, process_events=QApplication.processEvents)
    assert dialog.height() == preferred.height()
    assert dialog.width() >= preferred.width()


@pytest.mark.real_display
def test_fixture_theme_replaces_host_light_accent(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette
    from scripts.visual_fixture_checks import configure_visual_fixture_theme

    original_palette = qapp.palette()
    original_scheme = qapp.styleHints().colorScheme()
    try:
        qapp.styleHints().setColorScheme(Qt.ColorScheme.Light)
        qapp.processEvents()
        host_palette = qapp.palette()
        host_palette.setColor(QPalette.ColorRole.Accent, QColor("#0067c0"))
        host_palette.setColor(QPalette.ColorRole.Link, QColor("#0078d4"))
        qapp.setPalette(host_palette)

        configure_visual_fixture_theme(qapp)

        assert qapp.styleHints().colorScheme() == Qt.ColorScheme.Dark
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            assert qapp.palette().color(group, QPalette.ColorRole.Accent).name() == "#f38064"
            assert qapp.palette().color(group, QPalette.ColorRole.Link).name() == "#faa683"
    finally:
        qapp.styleHints().setColorScheme(original_scheme)
        qapp.processEvents()
        qapp.setPalette(original_palette)


@pytest.mark.parametrize("renderer_name", ["render_overlay_fixture", "render_settings_dialog_fixture"])
@pytest.mark.parametrize("fails", [False, True])
def test_renderer_restores_shared_application_theme(qapp, monkeypatch, renderer_name, fails):
    import importlib
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette

    renderer = importlib.import_module(f"scripts.{renderer_name}")
    original_palette = qapp.palette()
    original_scheme = qapp.styleHints().colorScheme()
    qapp.styleHints().setColorScheme(Qt.ColorScheme.Light)
    qapp.processEvents()
    host_scheme = qapp.styleHints().colorScheme()
    host_palette = qapp.palette()
    host_palette.setColor(QPalette.ColorRole.Accent, QColor("#123456"))
    qapp.setPalette(host_palette)

    def render(*_args, **_kwargs):
        assert qapp.palette().color(QPalette.ColorRole.Accent).name() == "#f38064"
        if fails:
            raise RuntimeError("synthetic render failure")
        return 7

    monkeypatch.setattr(renderer, "run_visual_fixture_scenarios", render)
    try:
        if fails:
            with pytest.raises(RuntimeError, match="synthetic render failure"):
                renderer.main(["--check"])
        else:
            assert renderer.main(["--check"]) == 7
        assert qapp.palette() == host_palette
        assert qapp.styleHints().colorScheme() == host_scheme
    finally:
        qapp.styleHints().setColorScheme(original_scheme)
        qapp.processEvents()
        qapp.setPalette(original_palette)
