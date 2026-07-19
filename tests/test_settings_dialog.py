from __future__ import annotations

import json
from pathlib import Path
import sys
import threading

import pytest
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QWidget,
)

from applicant_scout import __version__
from applicant_scout.config import Config
from applicant_scout.metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences
import applicant_scout.settings_dialog as settings_mod
from applicant_scout.settings_dialog import (
    ReleaseNotesDialog,
    SettingsDialog,
    SettingsUpdateResult,
)


ASYNC_TEST_BLOCK_TIMEOUT = 10.0
ASYNC_TEST_FALLBACK_RELEASE_DELAY = 5.0


def _cfg(tmp_path: Path, *, client_id: str = "client", secret: str = "secret") -> Config:
    retail_root = tmp_path / "World of Warcraft" / "_retail_"
    (retail_root / "Interface" / "AddOns").mkdir(parents=True, exist_ok=True)
    return Config(
        wcl_client_id=client_id,
        wcl_client_secret=secret,
        chatlog_path=retail_root / "Logs" / "WoWChatLog.txt",
        region="EU",
        cache_dir=tmp_path / "cache",
        config_dir=tmp_path / "config",
        screenshots_path=retail_root / "Screenshots",
        log_dir=tmp_path / "logs",
    )


def _wait_for_screenshots_warning(qtbot, dialog: SettingsDialog) -> None:
    qtbot.waitUntil(
        lambda: "Screenshots folder warning" in dialog.status_label.text(),
        timeout=1000,
    )


def _fallback_release(
    event: threading.Event,
    *,
    delay: float = ASYNC_TEST_FALLBACK_RELEASE_DELAY,
) -> threading.Timer:
    timer = threading.Timer(delay, event.set)
    timer.start()
    return timer


def test_settings_dialog_exposes_config_values(qtbot, tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.sync_with_wow = True
    cfg.metric_preferences = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=True,
    )
    dialog = SettingsDialog(cfg)
    qtbot.addWidget(dialog)

    values = dialog.values()

    assert values.wcl_client_id == "client"
    assert values.wcl_client_secret == "secret"
    assert values.region == "EU"
    assert values.screenshots_path.endswith(r"_retail_\Screenshots") or values.screenshots_path.endswith("_retail_/Screenshots")
    assert values.metric_preferences == MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=True,
    )
    assert values.sync_with_wow is True


def test_settings_dialog_title_shows_companion_version(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == f"ApplicantScout Companion · v{__version__}"


def test_first_run_dialog_title_keeps_setup_context_and_version(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path), first_run=True)
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == f"ApplicantScout Companion · First-run setup · v{__version__}"


def test_settings_dialog_first_run_defaults_to_mplus_only(qtbot, tmp_path: Path):
    cfg = _cfg(tmp_path)
    dialog = SettingsDialog(cfg, first_run=True)
    qtbot.addWidget(dialog)

    values = dialog.values()

    assert cfg.metric_preferences == DEFAULT_METRIC_PREFERENCES
    assert values.metric_preferences == MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )


def test_settings_dialog_prefers_draft_wcl_credentials(qtbot, tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.draft_wcl_client_id = "draft-client"
    cfg.draft_wcl_client_secret = "draft-secret"

    dialog = SettingsDialog(cfg)
    qtbot.addWidget(dialog)

    values = dialog.values()

    assert values.wcl_client_id == "draft-client"
    assert values.wcl_client_secret == "draft-secret"


def test_settings_dialog_has_wow_lifecycle_checkbox_near_bottom(qtbot, tmp_path: Path):
    cfg = _cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    qtbot.addWidget(dialog)

    checkbox = dialog.findChild(QCheckBox, "syncWithWow")

    assert checkbox is not None
    assert checkbox.text() == "Start and stop with WoW"
    assert not checkbox.isChecked()
    checkbox.setChecked(True)
    assert dialog.values().sync_with_wow is True


def test_settings_dialog_orders_wcl_data_with_mplus_last(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)

    assert [
        dialog.raid_normal_check,
        dialog.raid_heroic_check,
        dialog.raid_mythic_check,
        dialog.mplus_check,
    ] == [
        dialog.raid_normal_check.parent().layout().itemAt(i).widget()
        for i in range(4)
    ]


def test_settings_dialog_rejects_all_wcl_data_disabled(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)

    for checkbox in (
        dialog.mplus_check,
        dialog.raid_normal_check,
        dialog.raid_heroic_check,
        dialog.raid_mythic_check,
    ):
        checkbox.blockSignals(True)
        checkbox.setChecked(False)
        checkbox.blockSignals(False)
    dialog.accept()

    assert not dialog.result()
    assert "at least one" in dialog.status_label.text()


def test_settings_dialog_rejects_missing_credentials(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path, client_id="", secret=""))
    qtbot.addWidget(dialog)

    dialog.accept()

    assert not dialog.result()
    assert "required" in dialog.status_label.text()


def test_first_run_dialog_explains_wcl_client_creation(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path, client_id="", secret=""), first_run=True)
    qtbot.addWidget(dialog)

    visible_text = "\n".join(
        label.text() for label in dialog.findChildren(type(dialog.status_label))
    )

    assert "http://localhost" in visible_text
    assert "Public Client" in visible_text
    assert "unchecked" in visible_text
    assert "Client ID" in visible_text
    assert "Client Secret" in visible_text


def test_wcl_setup_example_button_opens_local_screenshot_popup(
    qtbot, tmp_path: Path, monkeypatch
):
    shown: list[settings_mod.QDialog] = []

    def fake_exec(popup: settings_mod.QDialog) -> int:
        shown.append(popup)
        return 0

    monkeypatch.setattr(settings_mod.QDialog, "exec", fake_exec)
    dialog = SettingsDialog(_cfg(tmp_path, client_id="", secret=""), first_run=True)
    qtbot.addWidget(dialog)

    dialog.wcl_example_button.click()

    assert shown
    popup = shown[0]
    visible_text = "\n".join(
        label.text() for label in popup.findChildren(type(dialog.status_label))
    )
    image_labels = [
        label
        for label in popup.findChildren(type(dialog.status_label))
        if label.pixmap() is not None and not label.pixmap().isNull()
    ]

    assert popup.windowTitle() == "Warcraft Logs API client example"
    assert "http://localhost" in visible_text
    assert "Public Client" in visible_text
    assert "unchecked" in visible_text
    assert image_labels


def test_wcl_setup_example_button_sits_next_to_clients_link(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path, client_id="", secret=""), first_run=True)
    qtbot.addWidget(dialog)

    assert dialog.wcl_example_button.text() == "Show example"
    assert dialog.wcl_example_button.parent() is dialog.wcl_clients_link.parent()


def test_wcl_clients_link_visually_points_to_setup_example_button(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(_cfg(tmp_path, client_id="", secret=""), first_run=True)
    qtbot.addWidget(dialog)
    row = dialog.wcl_clients_link.parent()
    layout = row.layout()

    assert dialog.wcl_example_arrow.text() == "→"
    assert dialog.wcl_example_arrow.parent() is row
    assert [
        layout.itemAt(i).widget()
        for i in range(3)
    ] == [
        dialog.wcl_clients_link,
        dialog.wcl_example_arrow,
        dialog.wcl_example_button,
    ]


def test_wcl_setup_example_exposes_copyable_create_client_values(
    qtbot, tmp_path: Path, monkeypatch
):
    clipboard_text = ""

    class FakeClipboard:
        def setText(self, value: str) -> None:
            nonlocal clipboard_text
            clipboard_text = value

    monkeypatch.setattr(settings_mod.QApplication, "clipboard", lambda: FakeClipboard())
    dialog = SettingsDialog(_cfg(tmp_path, client_id="", secret=""), first_run=True)
    qtbot.addWidget(dialog)
    popup = dialog._build_wcl_setup_example_dialog()
    qtbot.addWidget(popup)

    app_name = popup.findChild(QLineEdit, "wclExampleApplicationName")
    redirect_url = popup.findChild(QLineEdit, "wclExampleRedirectUrl")
    public_client = popup.findChild(QCheckBox, "wclExamplePublicClientUnchecked")
    copy_app_name = popup.findChild(QPushButton, "copyWclExampleApplicationName")
    copy_redirect = popup.findChild(QPushButton, "copyWclExampleRedirectUrl")

    assert app_name is not None
    assert app_name.text() == "ApplicantScout"
    assert app_name.isReadOnly()
    assert redirect_url is not None
    assert redirect_url.text() == "http://localhost"
    assert redirect_url.isReadOnly()
    assert public_client is not None
    assert not public_client.isChecked()
    assert not public_client.isEnabled()
    assert copy_app_name is not None
    assert copy_redirect is not None
    assert app_name.accessibleName() == "WCL application name"
    assert "Application name" in app_name.accessibleDescription()
    assert redirect_url.accessibleName() == "WCL redirect URL"
    assert "Redirect URL" in redirect_url.accessibleDescription()
    assert public_client.accessibleName() == "WCL Public Client checkbox"
    assert "unchecked" in public_client.accessibleDescription().lower()
    assert copy_app_name.accessibleName() == "Copy WCL application name"
    assert "Application name" in copy_app_name.accessibleDescription()
    assert copy_redirect.accessibleName() == "Copy WCL redirect URL"
    assert "Redirect URL" in copy_redirect.accessibleDescription()

    copy_app_name.click()
    assert clipboard_text == "ApplicantScout"
    copy_redirect.click()
    assert clipboard_text == "http://localhost"


def test_settings_dialog_rejects_suspicious_screenshots_path(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)

    dialog.screenshots_edit.setText(str(tmp_path / "not-wow" / "Shots"))

    _wait_for_screenshots_warning(qtbot, dialog)
    assert "Screenshots folder warning" in dialog.status_label.text()
    assert "#ff6666" in dialog.status_label.styleSheet()

    dialog.accept()

    assert not dialog.result()
    assert "Screenshots folder warning" in dialog.status_label.text()


def test_settings_dialog_rejects_nested_screenshots_path(qtbot, tmp_path: Path):
    cfg = _cfg(tmp_path)
    nested = (
        cfg.screenshots_path.parent
        / "Interface"
        / "AddOns"
        / "SomeAddon"
        / "Screenshots"
    )
    dialog = SettingsDialog(cfg)
    qtbot.addWidget(dialog)

    dialog.screenshots_edit.setText(str(nested))

    _wait_for_screenshots_warning(qtbot, dialog)
    assert "Screenshots folder warning" in dialog.status_label.text()
    assert "#ff6666" in dialog.status_label.styleSheet()

    dialog.accept()

    assert not dialog.result()
    assert "Screenshots folder warning" in dialog.status_label.text()


def test_settings_dialog_defers_screenshots_health_check_on_text_change(
    qtbot, tmp_path: Path, monkeypatch
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    calls: list[Path] = []

    def fake_warning(path: Path) -> str:
        calls.append(path)
        return "Screenshots folder warning: slow path."

    monkeypatch.setattr(settings_mod, "screenshots_path_health_warning", fake_warning)

    dialog.screenshots_edit.setText(str(tmp_path / "sleeping-drive" / "Screenshots"))

    assert calls == []
    qtbot.waitUntil(
        lambda: dialog.status_label.text()
        == "Screenshots folder warning: slow path.",
        timeout=1000,
    )
    assert calls == [Path(dialog.screenshots_edit.text())]


def test_settings_dialog_defers_initial_screenshots_health_check(
    qtbot, tmp_path: Path, monkeypatch
):
    cfg = _cfg(tmp_path)
    cfg.screenshots_path = tmp_path / "sleeping-drive" / "Screenshots"
    calls: list[Path] = []

    def fake_warning(path: Path) -> str:
        calls.append(path)
        return "Screenshots folder warning: slow path."

    monkeypatch.setattr(settings_mod, "screenshots_path_health_warning", fake_warning)

    dialog = SettingsDialog(cfg)
    qtbot.addWidget(dialog)

    assert calls == []
    qtbot.waitUntil(
        lambda: dialog.status_label.text()
        == "Screenshots folder warning: slow path.",
        timeout=1000,
    )
    assert calls == [cfg.screenshots_path]


def test_settings_dialog_reuses_one_path_probe_for_warning_and_autosave(
    qtbot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(
        lambda: dialog._screenshots_validation_ready_generation
        == dialog._screenshots_validation_generation,
        timeout=1000,
    )
    path = tmp_path / "other" / "_retail_" / "Screenshots"
    calls: list[Path] = []
    saved: list[str] = []
    monkeypatch.setattr(
        settings_mod,
        "screenshots_path_health_warning",
        lambda candidate: calls.append(candidate) or None,
    )
    dialog.valuesChanged.connect(lambda values: saved.append(values.screenshots_path))

    dialog.screenshots_edit.setText(str(path))
    assert not dialog.flush_pending_values()
    qtbot.waitUntil(lambda: bool(saved), timeout=1000)

    assert calls == [path]
    assert saved == [str(path)]


def test_settings_dialog_ignores_stale_path_probe_result(
    qtbot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(
        lambda: dialog._screenshots_validation_ready_generation
        == dialog._screenshots_validation_generation,
        timeout=1000,
    )
    first = tmp_path / "first" / "_retail_" / "Screenshots"
    second = tmp_path / "second" / "_retail_" / "Screenshots"
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[Path] = []
    saved: list[str] = []

    def probe(path: Path) -> str | None:
        calls.append(path)
        if path == first:
            first_started.set()
            if not release_first.wait(ASYNC_TEST_BLOCK_TIMEOUT):
                raise RuntimeError("stale path probe timed out")
            return "Screenshots folder warning: stale result."
        return None

    monkeypatch.setattr(settings_mod, "screenshots_path_health_warning", probe)
    dialog.valuesChanged.connect(lambda values: saved.append(values.screenshots_path))
    fallback = _fallback_release(release_first)
    try:
        dialog.screenshots_edit.setText(str(first))
        assert not dialog.flush_pending_values()
        assert first_started.wait(1)

        dialog.screenshots_edit.setText(str(second))
        assert dialog._autosave_timer.isActive()
        qtbot.waitUntil(lambda: saved == [str(second)], timeout=1000)
        assert not dialog._autosave_timer.isActive()
        qtbot.wait(750)
        assert saved == [str(second)]

        release_first.set()
        qtbot.wait(100)
        assert calls.count(first) == 1
        assert calls.count(second) == 1
        assert dialog.current_screenshots_warning() is None
        assert "stale result" not in dialog.status_label.text()
    finally:
        release_first.set()
        fallback.cancel()


def test_settings_dialog_isolated_path_probes_supersede_stuck_workers(
    qtbot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(
        lambda: dialog._screenshots_validation_ready_generation
        == dialog._screenshots_validation_generation,
        timeout=1000,
    )
    helper = tmp_path / "path_probe_helper.py"
    markers = tmp_path / "probe-markers"
    markers.mkdir()
    helper.write_text(
        "\n".join(
            (
                "import json",
                "from pathlib import Path",
                "import sys",
                "import tempfile",
                "import time",
                "path = Path(sys.argv[1])",
                "markers = Path(sys.argv[2])",
                "token = sys.argv[3]",
                "label = path.parents[1].name",
                "(markers / f'{label}.started').write_text('started')",
                "if label.startswith('stuck-'):",
                "    time.sleep(30)",
                "result = Path(tempfile.gettempdir()) / f'applicant-scout-path-probe-{token}.json'",
                "result.write_text(json.dumps({'warning': None}), encoding='utf-8')",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        settings_mod,
        "_requires_isolated_screenshots_probe",
        lambda _path: True,
    )
    monkeypatch.setattr(
        settings_mod,
        "_screenshots_path_probe_program_args",
        lambda path, token: (
            sys.executable,
            [str(helper), path, str(markers), token],
        ),
    )
    saved: list[str] = []

    def apply(values) -> None:
        saved.append(values.screenshots_path)
        dialog.report_values_apply_result(True)

    dialog.valuesChanged.connect(apply)
    first = tmp_path / "stuck-a" / "_retail_" / "Screenshots"
    second = tmp_path / "stuck-b" / "_retail_" / "Screenshots"
    valid = tmp_path / "valid" / "_retail_" / "Screenshots"

    try:
        dialog.screenshots_edit.setText(str(first))
        assert not dialog.flush_pending_values()
        qtbot.waitUntil(lambda: (markers / "stuck-a.started").exists(), timeout=1000)

        dialog.screenshots_edit.setText(str(second))
        assert not dialog.flush_pending_values()
        qtbot.waitUntil(lambda: (markers / "stuck-b.started").exists(), timeout=1000)

        assert saved == []
        assert not dialog.prepare_quit()

        dialog.screenshots_edit.setText(str(valid))
        assert not dialog.flush_pending_values()
        qtbot.waitUntil(lambda: saved == [str(valid)], timeout=2000)

        assert dialog._screenshots_validation_process is None
        assert dialog.current_screenshots_warning() is None
        assert dialog.prepare_quit()
        qtbot.wait(100)
        assert saved == [str(valid)]
    finally:
        dialog._cancel_isolated_screenshots_validation()


def test_settings_dialog_isolated_path_probe_timeout_is_determinate(
    qtbot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(
        lambda: dialog._screenshots_validation_ready_generation
        == dialog._screenshots_validation_generation,
        timeout=1000,
    )
    helper = tmp_path / "slow_path_probe.py"
    helper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(
        settings_mod,
        "_requires_isolated_screenshots_probe",
        lambda _path: True,
    )
    monkeypatch.setattr(
        settings_mod,
        "_screenshots_path_probe_program_args",
        lambda path, token: (sys.executable, [str(helper), path, token]),
    )
    dialog._screenshots_validation_process_timeout.setInterval(50)
    seen: list[str] = []
    dialog.valuesChanged.connect(lambda values: seen.append(values.screenshots_path))

    try:
        pending = tmp_path / "offline" / "_retail_" / "Screenshots"
        dialog.screenshots_edit.setText(str(pending))
        assert not dialog.flush_pending_values()

        qtbot.waitUntil(
            lambda: dialog.status_label.text()
            == settings_mod.SCREENSHOTS_PATH_PROBE_TIMEOUT_WARNING,
            timeout=1000,
        )
        assert dialog._screenshots_validation_process is None
        assert seen == []
        assert not dialog.prepare_quit()
    finally:
        dialog._cancel_isolated_screenshots_validation()


def test_screenshots_path_probe_command_emits_strict_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    warning = "Screenshots folder warning: test."
    monkeypatch.setattr(
        settings_mod,
        "_screenshots_path_warning",
        lambda _path: warning,
    )
    token = "a" * 32
    result_path = settings_mod._screenshots_path_probe_result_path(token)

    try:
        assert (
            settings_mod.run_screenshots_path_probe_command(str(tmp_path), token) == 0
        )

        assert json.loads(result_path.read_text(encoding="utf-8")) == {
            "warning": warning
        }
        assert capsys.readouterr().out == ""
    finally:
        result_path.unlink(missing_ok=True)


def test_screenshots_path_probe_command_rejects_invalid_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        settings_mod,
        "_screenshots_path_warning",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("invalid token must fail before filesystem access")
        ),
    )

    assert settings_mod.run_screenshots_path_probe_command(str(tmp_path), "../x") == 2


def test_screenshots_path_probe_program_args_cover_source_and_frozen(
    monkeypatch: pytest.MonkeyPatch,
):
    executable = r"C:\ApplicantScout\ApplicantScout.exe"
    monkeypatch.setattr(settings_mod.sys, "executable", executable)
    monkeypatch.delattr(settings_mod.sys, "frozen", raising=False)

    assert settings_mod._screenshots_path_probe_program_args("D:\\Shots", "a" * 32) == (
        executable,
        [
            "-m",
            "applicant_scout",
            settings_mod.SCREENSHOTS_PATH_PROBE_ARG,
            "D:\\Shots",
            "a" * 32,
        ],
    )

    monkeypatch.setattr(settings_mod.sys, "frozen", True, raising=False)
    assert settings_mod._screenshots_path_probe_program_args("D:\\Shots", "b" * 32) == (
        executable,
        [settings_mod.SCREENSHOTS_PATH_PROBE_ARG, "D:\\Shots", "b" * 32],
    )


def test_settings_dialog_slow_path_probe_keeps_gui_responsive(
    qtbot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(
        lambda: dialog._screenshots_validation_ready_generation
        == dialog._screenshots_validation_generation,
        timeout=1000,
    )
    path = tmp_path / "sleeping-drive" / "_retail_" / "Screenshots"
    probe_started = threading.Event()
    release_probe = threading.Event()
    gui_ticks: list[bool] = []

    def slow_probe(_path: Path) -> None:
        probe_started.set()
        if not release_probe.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("slow path probe timed out")
        return None

    monkeypatch.setattr(settings_mod, "screenshots_path_health_warning", slow_probe)
    fallback = _fallback_release(release_probe)
    try:
        dialog.screenshots_edit.setText(str(path))
        assert not dialog.flush_pending_values()
        assert probe_started.wait(1)

        settings_mod.QTimer.singleShot(0, lambda: gui_ticks.append(True))
        qtbot.waitUntil(lambda: gui_ticks == [True], timeout=500)
    finally:
        release_probe.set()
        fallback.cancel()


def test_settings_dialog_does_not_emit_values_changed_for_suspicious_screenshots_path(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    seen = []
    dialog.valuesChanged.connect(seen.append)

    dialog.screenshots_edit.setText(str(tmp_path / "not-wow" / "Shots"))

    assert not dialog.flush_pending_values()
    assert seen == []
    _wait_for_screenshots_warning(qtbot, dialog)
    assert "Screenshots folder warning" in dialog.status_label.text()


def test_settings_dialog_does_not_emit_values_changed_for_nested_screenshots_path(
    qtbot, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    nested = (
        cfg.screenshots_path.parent
        / "Interface"
        / "AddOns"
        / "SomeAddon"
        / "Screenshots"
    )
    dialog = SettingsDialog(cfg)
    qtbot.addWidget(dialog)
    seen = []
    dialog.valuesChanged.connect(seen.append)

    dialog.screenshots_edit.setText(str(nested))

    assert not dialog.flush_pending_values()
    assert seen == []
    _wait_for_screenshots_warning(qtbot, dialog)
    assert "Screenshots folder warning" in dialog.status_label.text()


def test_settings_dialog_suggests_wow_screenshots_folder_from_chatlog_path(
    qtbot, tmp_path: Path
):
    root = tmp_path / "World of Warcraft" / "_retail_"
    (root / "Interface" / "AddOns").mkdir(parents=True)
    cfg = _cfg(tmp_path)
    cfg.screenshots_path = None
    cfg.chatlog_path = root / "Logs" / "WoWChatLog.txt"

    dialog = SettingsDialog(cfg)
    qtbot.addWidget(dialog)

    assert dialog.values().screenshots_path == str(root / "Screenshots")
    assert dialog.screenshots_edit.placeholderText().startswith("Example:")
    assert dialog.screenshots_edit.toolTip()
    assert any(
        label.text() == "WoW Screenshots folder"
        for label in dialog.findChildren(type(dialog.status_label))
    )


def test_settings_dialog_finds_common_wow_screenshots_folder(
    qtbot, tmp_path: Path, monkeypatch
):
    root = tmp_path / "Common" / "World of Warcraft" / "_retail_"
    (root / "WTF").mkdir(parents=True)
    cfg = _cfg(tmp_path)
    cfg.screenshots_path = None
    cfg.chatlog_path = tmp_path / "missing" / "Logs" / "WoWChatLog.txt"
    monkeypatch.setattr(settings_mod, "COMMON_WOW_RETAIL_ROOTS", (root,))

    dialog = SettingsDialog(cfg)
    qtbot.addWidget(dialog)

    assert dialog.values().screenshots_path == str(root / "Screenshots")


def test_settings_dialog_runs_action_callbacks(qtbot, tmp_path: Path):
    calls: list[str] = []
    dialog = SettingsDialog(
        _cfg(tmp_path),
        credential_tester=lambda *_args: calls.append("test") or "credentials ok",
        open_logs=lambda: calls.append("logs") or "logs opened",
        clear_cache=lambda: calls.append("cache") or "cache cleared",
        check_updates=lambda: calls.append("updates") or "up to date",
    )
    qtbot.addWidget(dialog)

    dialog.test_button.click()
    qtbot.waitUntil(lambda: dialog.status_label.text() == "credentials ok")
    assert dialog.status_label.text() == "credentials ok"
    dialog.logs_action.trigger()
    dialog.cache_action.trigger()
    qtbot.waitUntil(lambda: dialog.status_label.text() == "cache cleared")
    dialog.set_update_available("v0.2.0")
    dialog.update_button.click()
    qtbot.waitUntil(lambda: dialog.status_label.text() == "up to date")

    assert dialog.update_button.text() == ""
    assert calls == ["test", "logs", "cache", "updates"]
    assert dialog.status_label.text() == "up to date"


def test_normal_settings_uses_actions_menu_and_tray_close(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)

    title_bar = dialog.findChild(QWidget, "settingsTitleBar")
    title_label = dialog.findChild(QLabel, "settingsTitle")
    close_button = dialog.findChild(QToolButton, "settingsClose")
    footer = dialog.findChild(QWidget, "settingsFooter")
    test_button = dialog.findChild(QPushButton, "testWcl")
    update_button = dialog.findChild(QToolButton, "installUpdate")
    support_button = dialog.findChild(QToolButton, "supportApplicantScout")
    more_button = dialog.findChild(QToolButton, "settingsMoreActions")
    assert title_bar is not None
    assert title_label is not None
    assert close_button is not None
    assert footer is not None
    assert test_button is not None
    assert update_button is not None
    assert support_button is not None
    assert more_button is not None
    assert title_label.text() == dialog.windowTitle()
    assert close_button.text() == "×"
    assert close_button.accessibleName() == "Hide settings to tray"
    assert close_button.accessibleDescription() == (
        "Hide ApplicantScout settings to tray."
    )
    assert test_button.text() == "Test WCL"
    assert update_button.text() == ""
    assert not update_button.icon().isNull()
    assert update_button.accessibleName() == "Install ApplicantScout update"
    assert update_button.accessibleDescription() == (
        "Install available ApplicantScout update."
    )
    assert update_button.isHidden()
    assert update_button.width() == 30
    assert update_button.height() == 26
    assert "background: transparent" in update_button.styleSheet()
    assert "#4da3ff" in update_button.styleSheet()
    assert "#74baff" in update_button.styleSheet()
    assert support_button.text() == "♡"
    assert support_button.accessibleName() == "Support ApplicantScout"
    assert "Ko-fi" in support_button.accessibleDescription()
    assert "ko-fi" in support_button.toolTip().lower()
    assert support_button.width() == 26
    assert support_button.height() == 24
    assert "background: transparent" in support_button.styleSheet()
    assert "#ff6b7a" in support_button.styleSheet()
    assert "#ff8a95" in support_button.styleSheet()
    assert more_button.text() == "More"
    assert more_button.accessibleName() == "More settings actions"
    assert "Open logs" in more_button.accessibleDescription()
    assert update_button.parent() is title_bar
    assert footer.layout().itemAt(0).widget() is support_button
    assert test_button.parent() is footer
    assert support_button.parent() is footer
    assert more_button.parent() is footer
    assert dialog.status_label.parent() is footer
    assert more_button.menu() is not None
    assert [
        action.objectName()
        for action in more_button.menu().actions()
        if isinstance(action, QAction) and not action.isSeparator()
    ] == ["openLogs", "viewChangelog", "clearCache", "quitApplicantScout"]
    assert [
        action.text()
        for action in more_button.menu().actions()
        if isinstance(action, QAction) and not action.isSeparator()
    ] == ["Open logs", "View changelog", "Reset cached data", "Quit ApplicantScout"]
    action_help = {
        action.objectName(): (action.toolTip(), action.statusTip(), action.whatsThis())
        for action in more_button.menu().actions()
        if isinstance(action, QAction) and not action.isSeparator()
    }
    assert "log" in action_help["openLogs"][0].lower()
    assert "changelog" in action_help["viewChangelog"][1].lower()
    assert "cache" in action_help["clearCache"][2].lower()
    assert "quit" in action_help["quitApplicantScout"][0].lower()
    assert any(action.isSeparator() for action in more_button.menu().actions())
    assert dialog.findChild(QPushButton, "hideToTray") is None
    assert dialog.findChild(QPushButton, "quitApplicantScout") is None
    assert dialog.findChild(QDialogButtonBox) is None


def test_settings_dialog_form_controls_have_accessibility_metadata(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path), first_run=True)
    qtbot.addWidget(dialog)

    assert dialog.screenshots_edit.accessibleName() == "WoW Screenshots folder"
    assert "_retail_" in dialog.screenshots_edit.accessibleDescription()
    assert dialog.browse_button.accessibleName() == "Browse WoW Screenshots folder"
    assert "Screenshots" in dialog.browse_button.accessibleDescription()
    assert dialog.test_button.accessibleName() == "Test Warcraft Logs credentials"
    assert "Validate" in dialog.test_button.accessibleDescription()
    assert dialog.more_actions_button.accessibleName() == "More settings actions"
    assert "reset cached data" in dialog.more_actions_button.accessibleDescription().lower()
    assert dialog.wcl_example_button.accessibleName() == "Show WCL setup example"
    assert "Create Client" in dialog.wcl_example_button.accessibleDescription()


def test_settings_dialog_changelog_action_emits_request(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    received: list[bool] = []
    dialog.changelogRequested.connect(lambda: received.append(True))

    dialog.changelog_action.trigger()

    assert received == [True]


def test_settings_dialog_more_quit_action_requests_full_quit(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    quit_requested: list[bool] = []
    dialog.quitRequested.connect(lambda: quit_requested.append(True))

    dialog.quit_action.trigger()

    assert quit_requested == [True]


def test_settings_dialog_more_quit_action_flushes_pending_values_before_quit(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    seen: list[tuple[str, str]] = []
    dialog.valuesChanged.connect(lambda values: seen.append(("saved", values.wcl_client_id)))
    dialog.quitRequested.connect(lambda: seen.append(("quit", "")))

    dialog.client_id_edit.setText("new-client")
    assert dialog._autosave_timer.isActive()
    dialog.quit_action.trigger()

    assert seen == [("saved", "new-client"), ("quit", "")]


def test_settings_dialog_more_quit_action_blocks_when_pending_values_are_invalid(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    quit_requested: list[bool] = []
    dialog.quitRequested.connect(lambda: quit_requested.append(True))

    dialog.client_id_edit.setText("")
    assert dialog._autosave_timer.isActive()
    dialog.quit_action.trigger()

    assert quit_requested == []
    assert "client id" in dialog.status_label.text().lower()
    assert "#ff6666" in dialog.status_label.styleSheet()


def test_settings_dialog_more_quit_action_blocks_during_update(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    quit_requested: list[bool] = []
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    dialog.set_update_in_progress(True)

    dialog.quit_action.trigger()

    assert quit_requested == []
    assert "update is installing" in dialog.status_label.text().lower()
    assert "#ff6666" in dialog.status_label.styleSheet()


def test_settings_dialog_more_quit_action_blocks_during_wcl_test(
    qtbot, tmp_path: Path
):
    tester_entered = threading.Event()
    release_tester = threading.Event()

    def tester(*_args) -> str:
        tester_entered.set()
        if not release_tester.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("credential test timed out")
        return "credentials ok"

    dialog = SettingsDialog(_cfg(tmp_path), credential_tester=tester)
    qtbot.addWidget(dialog)
    quit_requested: list[bool] = []
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    fallback = _fallback_release(release_tester)

    try:
        dialog.test_button.click()
        assert tester_entered.wait(1)

        dialog.quit_action.trigger()

        assert quit_requested == []
        assert "credential test" in dialog.status_label.text().lower()
        assert "#ff6666" in dialog.status_label.styleSheet()

        release_tester.set()
        qtbot.waitUntil(lambda: dialog.status_label.text() == "credentials ok")
    finally:
        release_tester.set()
        fallback.cancel()


def test_settings_dialog_status_supports_warning_style(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)

    dialog.set_status("Pending validation.", warning=True)

    assert "#e5cc80" in dialog.status_label.styleSheet()

    dialog.set_status("Hard failure.", error=True, warning=True)

    assert "#ff6666" in dialog.status_label.styleSheet()


def test_release_notes_dialog_renders_markdown_changelog(qtbot):
    dialog = ReleaseNotesDialog(
        "# ApplicantScout Companion 0.5.0\n\n- Fixed updater shutdown guard."
    )
    qtbot.addWidget(dialog)

    browser = dialog.findChild(QTextBrowser, "releaseNotesText")

    assert dialog.windowTitle() == "ApplicantScout Changelog"
    assert browser is not None
    assert "Fixed updater shutdown guard." in browser.toPlainText()


def test_settings_dialog_support_button_opens_kofi_link(
    monkeypatch: pytest.MonkeyPatch, qtbot, tmp_path: Path
):
    opened: list[str] = []
    monkeypatch.setattr(
        settings_mod.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)

    dialog.support_button.click()

    assert opened == ["https://ko-fi.com/antrakt92"]


def test_settings_dialog_support_button_surfaces_open_failure(
    monkeypatch: pytest.MonkeyPatch, qtbot, tmp_path: Path
):
    monkeypatch.setattr(
        settings_mod.QDesktopServices,
        "openUrl",
        lambda _url: False,
    )
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)

    dialog.support_button.click()

    assert "support link" in dialog.status_label.text()
    assert "#ff6666" in dialog.status_label.styleSheet()


def test_settings_dialog_shows_blue_update_icon_only_when_update_available(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    update_button = dialog.findChild(QToolButton, "installUpdate")

    assert update_button is not None
    assert update_button.isHidden()

    dialog.set_update_available("v0.2.0")

    assert not update_button.isHidden()
    assert "v0.2.0" in update_button.toolTip()
    assert "v0.2.0" in update_button.accessibleDescription()
    assert "#4da3ff" in update_button.styleSheet()

    dialog.set_update_available(None)

    assert update_button.isHidden()
    assert update_button.accessibleDescription() == (
        "Install available ApplicantScout update."
    )


def test_settings_dialog_keeps_update_icon_visible_after_update_failure(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    qtbot.addWidget(dialog)
    dialog.set_update_available("v0.2.0")

    dialog.update_button.click()

    qtbot.waitUntil(lambda: "offline" in dialog.status_label.text(), timeout=1000)
    assert not dialog.update_button.isHidden()
    assert "v0.2.0" in dialog.update_button.toolTip()
    assert "#ff6666" in dialog.status_label.styleSheet()


def test_settings_dialog_disables_editable_settings_during_update(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)

    dialog.set_update_in_progress(True)

    assert not dialog.client_id_edit.isEnabled()
    assert not dialog.client_secret_edit.isEnabled()
    assert not dialog.region_combo.isEnabled()
    assert not dialog.screenshots_edit.isEnabled()
    assert not dialog.test_button.isEnabled()
    assert not dialog.more_actions_button.isEnabled()
    assert not dialog.cache_action.isEnabled()
    assert dialog.update_button.accessibleDescription() == (
        "Installing ApplicantScout update..."
    )


def test_settings_dialog_cache_reset_flushes_pending_values_before_starting(
    qtbot, tmp_path: Path
):
    calls: list[str] = []
    dialog = SettingsDialog(
        _cfg(tmp_path),
        clear_cache=lambda: calls.append("cache") or "Cache cleared.",
    )
    qtbot.addWidget(dialog)

    dialog.client_id_edit.setText("")
    assert dialog._autosave_timer.isActive()
    dialog.cache_action.trigger()

    assert calls == []
    assert "client id" in dialog.status_label.text().lower()
    assert dialog.cache_action.isEnabled()


def test_settings_dialog_cache_reset_waits_for_wcl_test_to_finish(
    qtbot, tmp_path: Path
):
    tester_entered = threading.Event()
    release_tester = threading.Event()
    cache_calls: list[str] = []

    def tester(*_args) -> str:
        tester_entered.set()
        if not release_tester.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("credential test timed out")
        return "credentials ok"

    dialog = SettingsDialog(
        _cfg(tmp_path),
        credential_tester=tester,
        clear_cache=lambda: cache_calls.append("cache") or "Cache cleared.",
    )
    qtbot.addWidget(dialog)
    fallback = _fallback_release(release_tester)

    try:
        dialog.test_button.click()
        assert tester_entered.wait(1)

        dialog.cache_action.trigger()

        assert cache_calls == []
        assert "settings action" in dialog.status_label.text().lower()

        release_tester.set()
        qtbot.waitUntil(lambda: dialog.status_label.text() == "credentials ok")
    finally:
        release_tester.set()
        fallback.cancel()


def test_settings_dialog_update_waits_for_wcl_test_to_finish(qtbot, tmp_path: Path):
    tester_entered = threading.Event()
    release_tester = threading.Event()
    update_calls: list[str] = []
    started: list[bool] = []

    def tester(*_args) -> str:
        tester_entered.set()
        if not release_tester.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("credential test timed out")
        return "credentials ok"

    dialog = SettingsDialog(
        _cfg(tmp_path),
        credential_tester=tester,
        check_updates=lambda: update_calls.append("update") or "up to date",
    )
    qtbot.addWidget(dialog)
    dialog.updateStarted.connect(lambda: started.append(True))
    dialog.set_update_available("v0.2.0")
    fallback = _fallback_release(release_tester)

    try:
        dialog.test_button.click()
        assert tester_entered.wait(1)

        dialog.update_button.click()

        assert update_calls == []
        assert started == []
        assert "credential test" in dialog.status_label.text().lower()

        release_tester.set()
        qtbot.waitUntil(lambda: dialog.status_label.text() == "credentials ok")
    finally:
        release_tester.set()
        fallback.cancel()


def test_settings_dialog_cache_reset_runs_async_and_blocks_settings_until_finished(
    qtbot, tmp_path: Path
):
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def clear_cache() -> str:
        calls.append("cache")
        entered.set()
        if not release.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("cache test timed out")
        return "Cache cleared."

    dialog = SettingsDialog(_cfg(tmp_path), clear_cache=clear_cache)
    qtbot.addWidget(dialog)
    fallback = _fallback_release(release)

    try:
        dialog.cache_action.trigger()
        assert entered.wait(1)
        assert calls == ["cache"]
        assert dialog.status_label.text() == "Resetting cached data..."
        assert not dialog.cache_action.isEnabled()
        assert not dialog.client_id_edit.isEnabled()
        assert not dialog.more_actions_button.isEnabled()
        assert not dialog.update_button.isEnabled()

        dialog.cache_action.trigger()
        qtbot.wait(50)
        assert calls == ["cache"]

        release.set()
        qtbot.waitUntil(lambda: dialog.status_label.text() == "Cache cleared.")
        assert dialog.cache_action.isEnabled()
        assert dialog.client_id_edit.isEnabled()
        assert dialog.more_actions_button.isEnabled()
        assert dialog.update_button.isEnabled()
    finally:
        release.set()
        fallback.cancel()


def test_settings_dialog_cache_reset_surfaces_async_error_and_restores_controls(
    qtbot, tmp_path: Path
):
    entered = threading.Event()
    release = threading.Event()

    def clear_cache() -> str:
        entered.set()
        if not release.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("cache test timed out")
        raise RuntimeError("locked")

    dialog = SettingsDialog(_cfg(tmp_path), clear_cache=clear_cache)
    qtbot.addWidget(dialog)
    fallback = _fallback_release(release)

    try:
        dialog.cache_action.trigger()
        assert entered.wait(1)
        assert dialog.status_label.text() == "Resetting cached data..."
        assert not dialog.cache_action.isEnabled()

        release.set()
        qtbot.waitUntil(lambda: "locked" in dialog.status_label.text())
        assert dialog.status_label.text() == "Could not clear cache: locked"
        assert "#ff6666" in dialog.status_label.styleSheet()
        assert dialog.cache_action.isEnabled()
        assert dialog.client_id_edit.isEnabled()
    finally:
        release.set()
        fallback.cancel()


def test_settings_dialog_cache_reset_update_overlap_keeps_action_state_consistent(
    qtbot, tmp_path: Path
):
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    def clear_cache() -> str:
        entered.set()
        if not release.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("cache test timed out")
        returned.set()
        return "Cache cleared."

    dialog = SettingsDialog(_cfg(tmp_path), clear_cache=clear_cache)
    qtbot.addWidget(dialog)
    fallback = _fallback_release(release)

    try:
        dialog.cache_action.trigger()
        assert entered.wait(1)
        dialog.set_update_in_progress(True)
        assert not dialog.cache_action.isEnabled()
        assert not dialog.more_actions_button.isEnabled()

        release.set()
        assert returned.wait(1)
        qtbot.wait(100)
        assert not dialog.cache_action.isEnabled()
        assert "Cache cleared" not in dialog.status_label.text()

        dialog.set_update_in_progress(False)
        qtbot.waitUntil(lambda: dialog.cache_action.isEnabled())
        assert dialog.more_actions_button.isEnabled()
    finally:
        release.set()
        fallback.cancel()


def test_settings_dialog_suppresses_pending_values_while_update_in_progress(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    dialog._autosave_timer.setInterval(5000)
    seen: list[str] = []
    dialog.valuesChanged.connect(lambda values: seen.append(values.wcl_client_id))

    dialog.client_id_edit.setText("new-client")
    assert dialog._autosave_timer.isActive()
    dialog.set_update_in_progress(True)

    assert not dialog.flush_pending_values()
    assert seen == []


def test_settings_dialog_emits_update_lifecycle_for_failure(qtbot, tmp_path: Path):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    qtbot.addWidget(dialog)
    seen: list[tuple[str, bool | None]] = []
    dialog.updateStarted.connect(lambda: seen.append(("started", None)))
    dialog.updateFinished.connect(lambda error: seen.append(("finished", error)))
    dialog.set_update_available("v0.2.0")

    dialog.update_button.click()

    qtbot.waitUntil(lambda: len(seen) == 2, timeout=1000)
    assert seen == [("started", None), ("finished", True)]


def test_settings_update_flushes_pending_values_before_update_start(
    qtbot,
    tmp_path: Path,
):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: SettingsUpdateResult(
            "Installing update.",
            installer_handoff=True,
        ),
    )
    qtbot.addWidget(dialog)
    dialog._autosave_timer.setInterval(5000)
    seen: list[tuple[str, str]] = []
    dialog.valuesChanged.connect(
        lambda values: seen.append(("saved", values.wcl_client_id))
    )
    dialog.updateStarted.connect(lambda: seen.append(("started", "")))
    dialog.set_update_available("v0.2.0")

    dialog.client_id_edit.setText("new-client")
    assert dialog._autosave_timer.isActive()
    dialog.update_button.click()

    qtbot.waitUntil(
        lambda: any(kind == "started" for kind, _value in seen),
        timeout=1000,
    )
    assert seen[:2] == [("saved", "new-client"), ("started", "")]
    assert not dialog._autosave_timer.isActive()


def test_settings_update_started_signal_fires_before_update_action(qtbot, tmp_path: Path):
    seen: list[str] = []
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: seen.append("action") or "Installing update.",
    )
    qtbot.addWidget(dialog)
    dialog.updateStarted.connect(lambda: seen.append("started"))
    dialog.set_update_available("v0.2.0")

    dialog.update_button.click()

    qtbot.waitUntil(lambda: "action" in seen, timeout=1000)
    assert seen[:2] == ["started", "action"]


def test_settings_update_aborts_when_pending_values_apply_fails(
    qtbot,
    tmp_path: Path,
):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: SettingsUpdateResult(
            "Installing update.",
            installer_handoff=True,
        ),
    )
    qtbot.addWidget(dialog)
    dialog._autosave_timer.setInterval(5000)
    seen: list[str] = []

    def reject_apply(_values) -> None:
        seen.append("saved")
        dialog.report_values_apply_result(False)

    dialog.valuesChanged.connect(reject_apply)
    dialog.updateStarted.connect(lambda: seen.append("started"))
    dialog.set_update_available("v0.2.0")

    dialog.client_id_edit.setText("new-client")
    assert dialog._autosave_timer.isActive()
    dialog.update_button.click()

    assert seen == ["saved"]
    assert not dialog._autosave_timer.isActive()


def test_settings_update_aborts_after_immediate_values_apply_failure(
    qtbot,
    tmp_path: Path,
):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: "Installing update.",
    )
    qtbot.addWidget(dialog)
    seen: list[str] = []
    dialog.updateStarted.connect(lambda: seen.append("started"))
    dialog.set_update_available("v0.2.0")
    dialog.report_values_apply_result(False)

    dialog.update_button.click()

    assert seen == []


def test_settings_update_aborts_when_pending_values_are_invalid(qtbot, tmp_path: Path):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: "Installing update.",
    )
    qtbot.addWidget(dialog)
    dialog._autosave_timer.setInterval(5000)
    seen: list[str] = []
    dialog.valuesChanged.connect(lambda _values: seen.append("saved"))
    dialog.updateStarted.connect(lambda: seen.append("started"))
    dialog.set_update_available("v0.2.0")

    dialog.client_id_edit.setText("")
    assert dialog._autosave_timer.isActive()
    dialog.update_button.click()

    assert seen == []
    assert dialog._autosave_timer.isActive() is False
    assert "required" in dialog.status_label.text()


def test_settings_dialog_keeps_update_blocked_after_installer_handoff(
    qtbot, tmp_path: Path
):
    launch = object()
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: SettingsUpdateResult(
            "Installing update.",
            installer_handoff=True,
            installer_launch=launch,
        ),
    )
    qtbot.addWidget(dialog)
    finished: list[bool] = []
    completed: list[None] = []
    handoffs: list[tuple[str, object]] = []
    dialog.updateFinished.connect(lambda error: finished.append(error))
    dialog.updateCompleted.connect(lambda: completed.append(None))
    dialog.updateHandoffStarted.connect(
        lambda message, installer_launch: handoffs.append((message, installer_launch))
    )
    dialog.set_update_available("v0.2.0")

    dialog.update_button.click()

    qtbot.waitUntil(
        lambda: dialog.status_label.text() == "Installing update.",
        timeout=1000,
    )
    assert finished == []
    assert completed == []
    assert handoffs == [("Installing update.", launch)]
    assert not dialog.update_button.isHidden()
    assert not dialog.update_button.isEnabled()
    assert dialog.update_button.accessibleDescription() == (
        "Installing ApplicantScout update..."
    )


def test_settings_dialog_does_not_emit_handoff_signal_without_installer_handoff(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: "ApplicantScout Companion is up to date.",
    )
    qtbot.addWidget(dialog)
    handoffs: list[tuple[str, object]] = []
    completed: list[None] = []
    dialog.updateHandoffStarted.connect(
        lambda message, installer_launch: handoffs.append((message, installer_launch))
    )
    dialog.updateCompleted.connect(lambda: completed.append(None))
    dialog.set_update_available("v0.2.0")

    dialog.update_button.click()

    qtbot.waitUntil(lambda: bool(completed), timeout=1000)
    assert handoffs == []


def test_settings_dialog_opens_manual_update_url_without_handoff(
    monkeypatch: pytest.MonkeyPatch, qtbot, tmp_path: Path
):
    opened: list[str] = []
    monkeypatch.setattr(
        settings_mod.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: SettingsUpdateResult(
            "Manual install required.",
            open_url="https://github.com/example/release",
        ),
    )
    qtbot.addWidget(dialog)
    finished: list[bool] = []
    handoffs: list[tuple[str, object]] = []
    dialog.updateFinished.connect(lambda error: finished.append(error))
    dialog.updateHandoffStarted.connect(
        lambda message, installer_launch: handoffs.append((message, installer_launch))
    )
    dialog.set_update_available("v0.2.0")

    dialog.update_button.click()

    qtbot.waitUntil(lambda: bool(finished), timeout=1000)
    assert finished == [False]
    assert handoffs == []
    assert opened == ["https://github.com/example/release"]


def test_settings_dialog_treats_untrusted_installer_as_update_failure(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: (_ for _ in ()).throw(
            RuntimeError("Update installer is not trusted")
        ),
    )
    qtbot.addWidget(dialog)
    finished: list[bool] = []
    handoffs: list[tuple[str, object]] = []
    dialog.updateFinished.connect(lambda error: finished.append(error))
    dialog.updateHandoffStarted.connect(
        lambda message, installer_launch: handoffs.append((message, installer_launch))
    )
    dialog.set_update_available("v0.2.0")

    dialog.update_button.click()

    qtbot.waitUntil(lambda: bool(finished), timeout=1000)
    assert finished == [True]
    assert handoffs == []
    assert dialog.update_button.isEnabled()


def test_settings_dialog_finishes_update_when_no_installer_handoff(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        check_updates=lambda: "ApplicantScout Companion is up to date.",
    )
    qtbot.addWidget(dialog)
    finished: list[bool] = []
    completed: list[None] = []
    dialog.updateFinished.connect(lambda error: finished.append(error))
    dialog.updateCompleted.connect(lambda: completed.append(None))
    dialog.set_update_available("v0.2.0")

    dialog.update_button.click()

    qtbot.waitUntil(lambda: bool(completed), timeout=1000)
    assert finished == [False]
    assert completed == [None]
    assert dialog.update_button.isHidden()


def test_normal_settings_close_button_hides_to_tray(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    hidden: list[bool] = []
    quit_requested: list[bool] = []
    dialog.hideRequested.connect(lambda: hidden.append(True))
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    dialog.show()

    closed = dialog.close()

    assert not closed
    assert not dialog.isVisible()
    assert hidden == [True]
    assert quit_requested == []


def test_custom_titlebar_close_button_matches_dialog_close_behavior(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    hidden: list[bool] = []
    dialog.hideRequested.connect(lambda: hidden.append(True))
    dialog.show()

    dialog.close_button.click()

    assert not dialog.isVisible()
    assert hidden == [True]


def test_normal_settings_close_requests_quit_when_tray_hide_disabled(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path), hide_to_tray_on_close=False)
    qtbot.addWidget(dialog)
    hidden: list[bool] = []
    quit_requested: list[bool] = []
    dialog.hideRequested.connect(lambda: hidden.append(True))
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    dialog.show()

    closed = dialog.close()

    assert closed
    assert not dialog.isVisible()
    assert hidden == []
    assert quit_requested == [True]


def test_no_tray_close_blocks_quit_during_wcl_test(qtbot, tmp_path: Path):
    tester_entered = threading.Event()
    release_tester = threading.Event()

    def tester(*_args) -> str:
        tester_entered.set()
        if not release_tester.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("credential test timed out")
        return "credentials ok"

    dialog = SettingsDialog(
        _cfg(tmp_path),
        credential_tester=tester,
        hide_to_tray_on_close=False,
    )
    qtbot.addWidget(dialog)
    quit_requested: list[bool] = []
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    dialog.show()
    fallback = _fallback_release(release_tester)

    try:
        dialog.test_button.click()
        assert tester_entered.wait(1)

        closed = dialog.close()

        assert not closed
        assert dialog.isVisible()
        assert quit_requested == []
        assert "credential test" in dialog.status_label.text().lower()

        release_tester.set()
        qtbot.waitUntil(lambda: dialog.status_label.text() == "credentials ok")
    finally:
        release_tester.set()
        fallback.cancel()


def test_custom_titlebar_close_respects_no_tray_quit_policy(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path), hide_to_tray_on_close=False)
    qtbot.addWidget(dialog)
    hidden: list[bool] = []
    quit_requested: list[bool] = []
    dialog.hideRequested.connect(lambda: hidden.append(True))
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    dialog.show()

    dialog.close_button.click()

    assert not dialog.isVisible()
    assert hidden == []
    assert quit_requested == [True]
    assert dialog.close_button.accessibleName() == "Quit ApplicantScout"
    assert dialog.close_button.accessibleDescription() == "Quit ApplicantScout."


def test_no_tray_close_flushes_pending_text_values_before_quit(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path), hide_to_tray_on_close=False)
    qtbot.addWidget(dialog)
    seen: list[tuple[str, str]] = []
    dialog.valuesChanged.connect(lambda values: seen.append(("saved", values.wcl_client_id)))
    dialog.quitRequested.connect(lambda: seen.append(("quit", "")))
    dialog.show()

    dialog.client_id_edit.setText("new-client")
    assert dialog._autosave_timer.isActive()

    closed = dialog.close()

    assert closed
    assert seen == [("saved", "new-client"), ("quit", "")]


def test_no_tray_close_blocks_quit_when_pending_values_are_invalid(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(_cfg(tmp_path), hide_to_tray_on_close=False)
    qtbot.addWidget(dialog)
    quit_requested: list[bool] = []
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    dialog.show()

    dialog.client_id_edit.setText("")
    assert dialog._autosave_timer.isActive()
    closed = dialog.close()

    assert not closed
    assert dialog.isVisible()
    assert quit_requested == []
    assert "client id" in dialog.status_label.text().lower()
    assert "#ff6666" in dialog.status_label.styleSheet()


def test_no_tray_close_blocks_quit_when_pending_apply_fails(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path), hide_to_tray_on_close=False)
    qtbot.addWidget(dialog)
    seen: list[str] = []

    def reject_apply(_values) -> None:
        seen.append("saved")
        dialog.report_values_apply_result(False)
        dialog.set_status("Could not save/apply settings: boom", error=True)

    dialog.valuesChanged.connect(reject_apply)
    dialog.quitRequested.connect(lambda: seen.append("quit"))
    dialog.show()

    dialog.client_id_edit.setText("new-client")
    assert dialog._autosave_timer.isActive()
    closed = dialog.close()

    assert not closed
    assert dialog.isVisible()
    assert seen == ["saved"]
    assert "boom" in dialog.status_label.text()
    assert "#ff6666" in dialog.status_label.styleSheet()


def test_settings_close_ignored_while_update_in_progress(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path), hide_to_tray_on_close=False)
    qtbot.addWidget(dialog)
    hidden: list[bool] = []
    quit_requested: list[bool] = []
    dialog.hideRequested.connect(lambda: hidden.append(True))
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    dialog.show()
    dialog.set_update_in_progress(True)

    closed = dialog.close()

    assert not closed
    assert dialog.isVisible()
    assert hidden == []
    assert quit_requested == []
    assert "update" in dialog.status_label.text().lower()


def test_settings_close_ignored_while_cache_reset_in_progress(qtbot, tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()

    def clear_cache() -> str:
        entered.set()
        if not release.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("cache test timed out")
        return "Cache cleared."

    dialog = SettingsDialog(
        _cfg(tmp_path),
        clear_cache=clear_cache,
        hide_to_tray_on_close=False,
    )
    qtbot.addWidget(dialog)
    hidden: list[bool] = []
    quit_requested: list[bool] = []
    dialog.hideRequested.connect(lambda: hidden.append(True))
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    dialog.show()
    fallback = _fallback_release(release)

    try:
        dialog.cache_action.trigger()
        assert entered.wait(1)

        closed = dialog.close()

        assert not closed
        assert dialog.isVisible()
        assert hidden == []
        assert quit_requested == []
        assert "cache reset" in dialog.status_label.text().lower()

        release.set()
        qtbot.waitUntil(lambda: dialog.status_label.text() == "Cache cleared.")
    finally:
        release.set()
        fallback.cancel()


def test_settings_dialog_more_quit_action_blocks_during_cache_reset(
    qtbot, tmp_path: Path
):
    entered = threading.Event()
    release = threading.Event()

    def clear_cache() -> str:
        entered.set()
        if not release.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("cache test timed out")
        return "Cache cleared."

    dialog = SettingsDialog(_cfg(tmp_path), clear_cache=clear_cache)
    qtbot.addWidget(dialog)
    quit_requested: list[bool] = []
    dialog.quitRequested.connect(lambda: quit_requested.append(True))
    fallback = _fallback_release(release)

    try:
        dialog.cache_action.trigger()
        assert entered.wait(1)

        dialog.quit_action.trigger()

        assert quit_requested == []
        assert "cache reset" in dialog.status_label.text().lower()
        assert "#ff6666" in dialog.status_label.styleSheet()

        release.set()
        qtbot.waitUntil(lambda: dialog.status_label.text() == "Cache cleared.")
    finally:
        release.set()
        fallback.cancel()


def test_first_run_setup_actions_blocked_while_cache_reset_in_progress(
    qtbot, tmp_path: Path
):
    entered = threading.Event()
    release = threading.Event()

    def clear_cache() -> str:
        entered.set()
        if not release.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("cache test timed out")
        return "Cache cleared."

    dialog = SettingsDialog(
        _cfg(tmp_path),
        first_run=True,
        clear_cache=clear_cache,
    )
    qtbot.addWidget(dialog)
    start_button = dialog.findChild(QPushButton, "startCompanion")
    setup_quit_button = dialog.findChild(QPushButton, "quitApplicantScout")
    assert start_button is not None
    assert setup_quit_button is not None
    dialog.show()
    fallback = _fallback_release(release)

    try:
        dialog.cache_action.trigger()
        assert entered.wait(1)

        assert not start_button.isEnabled()
        assert not setup_quit_button.isEnabled()

        dialog.accept()
        assert not dialog.result()
        assert dialog.isVisible()
        assert "cache reset" in dialog.status_label.text().lower()

        dialog.reject()
        assert not dialog.result()
        assert dialog.isVisible()
        assert "cache reset" in dialog.status_label.text().lower()

        release.set()
        qtbot.waitUntil(lambda: dialog.status_label.text() == "Cache cleared.")
        assert start_button.isEnabled()
        assert setup_quit_button.isEnabled()
    finally:
        release.set()
        fallback.cancel()


def test_first_run_setup_actions_blocked_while_update_in_progress(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(_cfg(tmp_path), first_run=True)
    qtbot.addWidget(dialog)
    start_button = dialog.findChild(QPushButton, "startCompanion")
    setup_quit_button = dialog.findChild(QPushButton, "quitApplicantScout")
    assert start_button is not None
    assert setup_quit_button is not None
    dialog.show()

    dialog.set_update_in_progress(True)

    assert not start_button.isEnabled()
    assert not setup_quit_button.isEnabled()

    dialog.accept()
    assert not dialog.result()
    assert dialog.isVisible()
    assert "update" in dialog.status_label.text().lower()

    dialog.reject()
    assert not dialog.result()
    assert dialog.isVisible()
    assert "update" in dialog.status_label.text().lower()

    closed = dialog.close()
    assert not closed
    assert dialog.isVisible()
    assert "update" in dialog.status_label.text().lower()


def test_first_run_setup_actions_blocked_while_wcl_test_in_progress(
    qtbot, tmp_path: Path
):
    tester_entered = threading.Event()
    release_tester = threading.Event()

    def tester(*_args) -> str:
        tester_entered.set()
        if not release_tester.wait(ASYNC_TEST_BLOCK_TIMEOUT):
            raise RuntimeError("credential test timed out")
        return "credentials ok"

    dialog = SettingsDialog(
        _cfg(tmp_path),
        first_run=True,
        credential_tester=tester,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    fallback = _fallback_release(release_tester)

    try:
        dialog.test_button.click()
        assert tester_entered.wait(1)

        dialog.accept()
        assert not dialog.result()
        assert dialog.isVisible()
        assert "credential test" in dialog.status_label.text().lower()

        dialog.reject()
        assert not dialog.result()
        assert dialog.isVisible()
        assert "credential test" in dialog.status_label.text().lower()

        closed = dialog.close()
        assert not closed
        assert dialog.isVisible()
        assert "credential test" in dialog.status_label.text().lower()

        release_tester.set()
        qtbot.waitUntil(lambda: dialog.status_label.text() == "credentials ok")
    finally:
        release_tester.set()
        fallback.cancel()


def test_first_run_titlebar_close_button_uses_setup_copy(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path), first_run=True)
    qtbot.addWidget(dialog)

    assert dialog.close_button.toolTip() == "Close ApplicantScout setup."
    assert dialog.close_button.accessibleName() == "Close setup"
    assert dialog.close_button.accessibleDescription() == "Close ApplicantScout setup."


def test_settings_dialog_emits_values_changed_after_text_debounce(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    seen = []
    dialog.valuesChanged.connect(seen.append)

    dialog.client_id_edit.setText("new-client")

    qtbot.waitUntil(lambda: bool(seen), timeout=1500)
    assert seen[-1].wcl_client_id == "new-client"


def test_settings_dialog_flush_pending_values_emits_debounced_text(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    seen = []
    dialog.valuesChanged.connect(seen.append)

    dialog.client_id_edit.setText("new-client")

    assert not seen
    assert dialog.flush_pending_values()

    assert seen[-1].wcl_client_id == "new-client"


def test_settings_dialog_emits_values_changed_for_immediate_controls(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path))
    qtbot.addWidget(dialog)
    seen = []
    dialog.valuesChanged.connect(seen.append)

    dialog.sync_with_wow_check.setChecked(True)

    qtbot.waitUntil(lambda: bool(seen), timeout=1000)
    assert seen[-1].sync_with_wow is True


def test_settings_dialog_emits_validated_credentials_after_successful_test(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        credential_tester=lambda *_args: "credentials ok",
    )
    qtbot.addWidget(dialog)
    seen = []
    dialog.credentialsValidated.connect(seen.append)

    dialog.client_id_edit.setText("new-client")
    dialog.client_secret_edit.setText("new-secret")
    dialog.test_button.click()

    qtbot.waitUntil(lambda: bool(seen), timeout=1500)
    assert seen[-1].wcl_client_id == "new-client"
    assert seen[-1].wcl_client_secret == "new-secret"


def test_settings_dialog_successful_wcl_test_stops_pending_autosave(
    qtbot, tmp_path: Path
):
    dialog = SettingsDialog(
        _cfg(tmp_path),
        credential_tester=lambda *_args: "credentials ok",
    )
    qtbot.addWidget(dialog)
    dialog._autosave_timer.setInterval(5000)
    validated = []
    autosaved = []
    dialog.credentialsValidated.connect(validated.append)
    dialog.valuesChanged.connect(autosaved.append)

    dialog.client_id_edit.setText("new-client")
    dialog.client_secret_edit.setText("new-secret")
    assert dialog._autosave_timer.isActive()
    dialog.test_button.click()

    qtbot.waitUntil(lambda: bool(validated), timeout=1500)
    assert not dialog._autosave_timer.isActive()
    assert autosaved == []


def test_settings_dialog_ignores_stale_credentials_test_result(
    qtbot, tmp_path: Path
):
    tester_entered = threading.Event()
    release_tester = threading.Event()

    def tester(*_args) -> str:
        tester_entered.set()
        assert release_tester.wait(2)
        return "credentials ok"

    dialog = SettingsDialog(_cfg(tmp_path), credential_tester=tester)
    qtbot.addWidget(dialog)
    seen = []
    dialog.credentialsValidated.connect(seen.append)

    dialog.client_id_edit.setText("new-client")
    dialog.client_secret_edit.setText("new-secret")
    dialog.test_button.click()
    assert tester_entered.wait(2)
    dialog.client_secret_edit.setText("changed-during-test")
    release_tester.set()

    qtbot.waitUntil(
        lambda: "changed" in dialog.status_label.text().lower(),
        timeout=1500,
    )
    assert seen == []


def test_settings_dialog_ignores_stale_region_test_result(
    qtbot, tmp_path: Path
):
    tester_entered = threading.Event()
    release_tester = threading.Event()

    def tester(*_args) -> str:
        tester_entered.set()
        assert release_tester.wait(2)
        return "credentials ok"

    dialog = SettingsDialog(_cfg(tmp_path), credential_tester=tester)
    qtbot.addWidget(dialog)
    seen = []
    dialog.credentialsValidated.connect(seen.append)

    dialog.client_id_edit.setText("new-client")
    dialog.client_secret_edit.setText("new-secret")
    dialog.test_button.click()
    assert tester_entered.wait(2)
    dialog.region_combo.setCurrentText("US")
    release_tester.set()

    qtbot.waitUntil(
        lambda: bool(seen) or "changed" in dialog.status_label.text().lower(),
        timeout=1500,
    )
    assert seen == []
    assert "changed" in dialog.status_label.text().lower()


def test_settings_dialog_ignores_credential_result_during_update(
    qtbot, tmp_path: Path
):
    tester_entered = threading.Event()
    release_tester = threading.Event()
    tester_returned = threading.Event()

    def tester(*_args) -> str:
        tester_entered.set()
        assert release_tester.wait(2)
        tester_returned.set()
        return "credentials ok"

    dialog = SettingsDialog(_cfg(tmp_path), credential_tester=tester)
    qtbot.addWidget(dialog)
    seen = []
    dialog.credentialsValidated.connect(seen.append)

    dialog.client_id_edit.setText("new-client")
    dialog.client_secret_edit.setText("new-secret")
    dialog.test_button.click()
    assert tester_entered.wait(2)
    dialog.set_update_in_progress(True)
    release_tester.set()

    assert tester_returned.wait(2)
    qtbot.wait(100)
    assert seen == []
    assert not dialog.test_button.isEnabled()
    assert "credentials ok" not in dialog.status_label.text().lower()


def test_settings_dialog_clears_wcl_test_busy_when_result_ignored_during_update(
    qtbot, tmp_path: Path
):
    tester_entered = threading.Event()
    release_tester = threading.Event()
    tester_returned = threading.Event()

    def tester(*_args) -> str:
        tester_entered.set()
        assert release_tester.wait(2)
        tester_returned.set()
        return "credentials ok"

    dialog = SettingsDialog(_cfg(tmp_path), credential_tester=tester)
    qtbot.addWidget(dialog)
    quit_requested: list[bool] = []
    dialog.quitRequested.connect(lambda: quit_requested.append(True))

    dialog.test_button.click()
    assert tester_entered.wait(2)
    dialog.set_update_in_progress(True)
    release_tester.set()

    assert tester_returned.wait(2)
    qtbot.wait(100)
    assert not dialog.test_button.isEnabled()

    dialog.set_update_in_progress(False)
    qtbot.waitUntil(lambda: dialog.test_button.isEnabled())
    dialog.quit_action.trigger()

    assert quit_requested == [True]


def test_settings_dialog_rejects_validated_credentials_when_current_values_invalid(
    qtbot, tmp_path: Path
):
    tester_entered = threading.Event()
    release_tester = threading.Event()
    invalid_file = tmp_path / "not-a-folder"
    invalid_file.write_text("x", encoding="utf-8")

    def tester(*_args) -> str:
        tester_entered.set()
        assert release_tester.wait(2)
        return "credentials ok"

    dialog = SettingsDialog(_cfg(tmp_path), credential_tester=tester)
    qtbot.addWidget(dialog)
    seen = []
    dialog.credentialsValidated.connect(seen.append)

    dialog.test_button.click()
    assert tester_entered.wait(2)
    dialog.screenshots_edit.setText(str(invalid_file))
    release_tester.set()

    qtbot.waitUntil(
        lambda: "points to a file" in dialog.status_label.text(),
        timeout=1500,
    )
    assert seen == []


def test_settings_dialog_prevents_unchecking_last_wcl_data_type(qtbot, tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.metric_preferences = MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=False,
    )
    dialog = SettingsDialog(cfg)
    qtbot.addWidget(dialog)
    seen = []
    dialog.valuesChanged.connect(seen.append)

    dialog.raid_normal_check.setChecked(False)

    assert dialog.raid_normal_check.isChecked()
    assert dialog.values().metric_preferences.any_enabled
    assert "at least one" in dialog.status_label.text()
    assert seen == []


def test_first_run_dialog_uses_start_companion_button(qtbot, tmp_path: Path):
    dialog = SettingsDialog(_cfg(tmp_path), first_run=True)
    qtbot.addWidget(dialog)

    start_button = dialog.findChild(QPushButton, "startCompanion")

    assert start_button is not None
    assert start_button.text() == "Start companion"
    assert dialog.findChild(QDialogButtonBox) is None
