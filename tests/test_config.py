from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import applicant_scout.__main__ as main_mod
from applicant_scout import atomic_io
from applicant_scout.config import (
    Config,
    ConfigError,
    is_config_ready,
    load_config,
    resolve_screenshots_path,
    save_config_values,
    screenshots_path_health_warning,
    user_config_path,
)
from applicant_scout.metric_preferences import MetricPreferences


def _cfg(
    tmp_path: Path,
    *,
    chatlog_path: Path | None = None,
    screenshots_path: Path | None = None,
) -> Config:
    return Config(
        wcl_client_id="client",
        wcl_client_secret="secret",
        chatlog_path=chatlog_path
        or tmp_path / "World of Warcraft" / "_retail_" / "Logs" / "WoWChatLog.txt",
        region="EU",
        cache_dir=tmp_path / "cache",
        config_dir=tmp_path / "config",
        screenshots_path=screenshots_path,
    )


def _clean_load_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("WCL_CLIENT_ID", "client")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "secret")
    monkeypatch.delenv("APSCOUT_CHATLOG_PATH", raising=False)
    monkeypatch.delenv("APSCOUT_SCREENSHOTS_PATH", raising=False)
    monkeypatch.delenv("APSCOUT_REGION", raising=False)
    monkeypatch.delenv("APSCOUT_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.delenv("APSCOUT_FETCH_MPLUS", raising=False)
    monkeypatch.delenv("APSCOUT_FETCH_RAID_NORMAL", raising=False)
    monkeypatch.delenv("APSCOUT_FETCH_RAID_HEROIC", raising=False)
    monkeypatch.delenv("APSCOUT_FETCH_RAID_MYTHIC", raising=False)
    monkeypatch.delenv("APSCOUT_SYNC_WITH_WOW", raising=False)


def _retail_root(tmp_path: Path) -> Path:
    return tmp_path / "World of Warcraft" / "_retail_"


def test_wcl_region_runtime_uses_fallback_until_live_region_known():
    runtime = main_mod._WCLRegionRuntime("EU")

    assert runtime.effective_region == "EU"
    assert runtime.set_fallback("US") is True
    assert runtime.effective_region == "US"


def test_wcl_region_runtime_live_region_overrides_fallback_changes():
    runtime = main_mod._WCLRegionRuntime("EU")

    assert runtime.set_live_region_id(1) is True
    assert runtime.effective_region == "US"
    assert runtime.set_fallback("KR") is False
    assert runtime.effective_region == "US"


def test_wcl_region_runtime_unknown_live_region_does_not_override():
    runtime = main_mod._WCLRegionRuntime("EU")

    assert runtime.set_live_region_id(99) is False
    assert runtime.effective_region == "EU"
    assert runtime.set_fallback("TW") is True
    assert runtime.effective_region == "TW"


def test_explicit_nonexistent_screenshots_override_returns_path(tmp_path: Path):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    explicit = root / "Screenshots"

    result = resolve_screenshots_path(_cfg(tmp_path, screenshots_path=explicit))

    assert result == explicit
    assert not explicit.exists()


def test_explicit_suspicious_screenshots_override_raises_without_creating_path(
    tmp_path: Path,
):
    explicit = tmp_path / "not-wow" / "Shots"

    with pytest.raises(ConfigError, match="Screenshots folder warning"):
        resolve_screenshots_path(_cfg(tmp_path, screenshots_path=explicit))

    assert not explicit.exists()


def test_explicit_existing_file_screenshots_override_raises(tmp_path: Path):
    explicit_file = tmp_path / "not-a-folder"
    explicit_file.write_text("x", encoding="utf-8")

    with pytest.raises(ConfigError, match="points to a file"):
        resolve_screenshots_path(_cfg(tmp_path, screenshots_path=explicit_file))


def test_screenshots_path_warning_accepts_valid_retail_screenshots(tmp_path: Path):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)

    warning = screenshots_path_health_warning(root / "Screenshots")

    assert warning is None


def test_screenshots_path_warning_flags_non_screenshots_folder(tmp_path: Path):
    warning = screenshots_path_health_warning(tmp_path / "World of Warcraft" / "_retail_" / "Shots")

    assert warning is not None
    assert "Screenshots" in warning


def test_screenshots_path_warning_flags_path_outside_retail_root(tmp_path: Path):
    warning = screenshots_path_health_warning(tmp_path / "Screenshots")

    assert warning is not None
    assert "_retail_" in warning


def test_screenshots_path_warning_flags_retail_root_without_wow_markers(tmp_path: Path):
    path = tmp_path / "World of Warcraft" / "_retail_" / "Screenshots"
    path.parent.mkdir(parents=True)

    warning = screenshots_path_health_warning(path)

    assert warning is not None
    assert "WoW install" in warning


def test_valid_legacy_chatlog_file_infers_screenshots(tmp_path: Path):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    chatlog = root / "Logs" / "WoWChatLog.txt"

    result = resolve_screenshots_path(_cfg(tmp_path, chatlog_path=chatlog))

    assert result == root / "Screenshots"
    assert not result.exists()


def test_valid_legacy_logs_dir_infers_screenshots(tmp_path: Path):
    root = _retail_root(tmp_path)
    (root / "WTF").mkdir(parents=True)
    logs_dir = root / "Logs"

    result = resolve_screenshots_path(_cfg(tmp_path, chatlog_path=logs_dir))

    assert result == root / "Screenshots"
    assert not result.exists()


def test_missing_inferred_root_raises_without_creating_directories(tmp_path: Path):
    root = _retail_root(tmp_path)
    chatlog = root / "Logs" / "WoWChatLog.txt"

    with pytest.raises(ConfigError, match="does not exist"):
        resolve_screenshots_path(_cfg(tmp_path, chatlog_path=chatlog))

    assert not root.exists()
    assert not (root / "Screenshots").exists()


@pytest.mark.parametrize("folder", ["Logs", "Screenshots"])
def test_weak_folder_only_retail_root_raises(tmp_path: Path, folder: str):
    root = _retail_root(tmp_path)
    (root / folder).mkdir(parents=True)
    chatlog = root / "Logs" / "WoWChatLog.txt"

    with pytest.raises(ConfigError, match="does not look like a WoW install"):
        resolve_screenshots_path(_cfg(tmp_path, chatlog_path=chatlog))


@pytest.mark.parametrize(
    "marker_parts",
    [
        ("Wow.exe",),
        ("Interface",),
        ("Interface", "AddOns"),
        ("WTF",),
    ],
)
def test_strong_wow_root_markers_are_accepted(
    tmp_path: Path, marker_parts: tuple[str, ...]
):
    root = _retail_root(tmp_path)
    marker = root.joinpath(*marker_parts)
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.suffix:
        marker.write_text("", encoding="utf-8")
    else:
        marker.mkdir(parents=True, exist_ok=True)

    result = resolve_screenshots_path(
        _cfg(tmp_path, chatlog_path=root / "Logs" / "WoWChatLog.txt")
    )

    assert result == root / "Screenshots"


@pytest.mark.parametrize(
    ("marker_parts", "as_file"),
    [
        (("Wow.exe",), False),
        (("Interface",), True),
        (("WTF",), True),
    ],
)
def test_wrong_type_wow_root_markers_are_rejected(
    tmp_path: Path, marker_parts: tuple[str, ...], as_file: bool
):
    root = _retail_root(tmp_path)
    marker = root.joinpath(*marker_parts)
    marker.parent.mkdir(parents=True, exist_ok=True)
    if as_file:
        marker.write_text("", encoding="utf-8")
    else:
        marker.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ConfigError, match="does not look like a WoW install"):
        resolve_screenshots_path(
            _cfg(tmp_path, chatlog_path=root / "Logs" / "WoWChatLog.txt")
        )


def test_load_config_parses_cache_ttl_seconds_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _clean_load_config_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APSCOUT_CACHE_TTL_SECONDS", "60")

    cfg = load_config()

    assert cfg.cache_ttl_seconds == 60
    assert cfg.cache_dir == tmp_path / "localappdata" / "applicant-scout" / "cache"
    assert cfg.config_path == tmp_path / "localappdata" / "applicant-scout" / "config" / "config.env"


def test_load_config_uses_default_for_blank_cache_ttl_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _clean_load_config_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APSCOUT_CACHE_TTL_SECONDS", "   ")

    cfg = load_config()

    assert cfg.cache_ttl_seconds is None


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "abc", "1.5", "999999999"],
)
def test_load_config_rejects_invalid_cache_ttl_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
):
    _clean_load_config_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APSCOUT_CACHE_TTL_SECONDS", value)

    with pytest.raises(ConfigError, match="APSCOUT_CACHE_TTL_SECONDS"):
        load_config()


def test_load_config_reads_user_config_file_without_cwd_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("WCL_CLIENT_ID", raising=False)
    monkeypatch.delenv("WCL_CLIENT_SECRET", raising=False)
    config_path = user_config_path()
    save_config_values(
        wcl_client_id="stored-client",
        wcl_client_secret="stored-secret",
        region="US",
        screenshots_path=str(tmp_path / "Shots"),
        config_path=config_path,
    )

    cfg = load_config()

    assert cfg.wcl_client_id == "stored-client"
    assert cfg.wcl_client_secret == "stored-secret"
    assert cfg.region == "US"
    assert cfg.screenshots_path == tmp_path / "Shots"
    assert is_config_ready(cfg)


def test_save_config_values_quotes_comment_like_path_segments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    weird_path = tmp_path / "WoW #2" / "_retail_" / "Screenshots"

    save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        screenshots_path=str(weird_path),
    )
    cfg = load_config()

    assert cfg.screenshots_path == weird_path


def test_save_config_values_failed_replace_preserves_existing_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "config.env"
    target.write_text("WCL_CLIENT_ID=\"old\"\n", encoding="utf-8")

    def fail_replace(_src: object, _dst: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="locked"):
        save_config_values(
            wcl_client_id="new",
            wcl_client_secret="secret",
            region="EU",
            config_path=target,
        )

    assert target.read_text(encoding="utf-8") == "WCL_CLIENT_ID=\"old\"\n"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_load_config_round_trips_metric_preferences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))

    save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=True,
            raid_heroic=False,
            raid_mythic=True,
        ),
    )

    cfg = load_config()

    assert cfg.metric_preferences == MetricPreferences(
        mplus=False,
        raid_normal=True,
        raid_heroic=False,
        raid_mythic=True,
    )


def test_load_config_defaults_to_mplus_only_for_first_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _clean_load_config_env(monkeypatch, tmp_path)

    cfg = load_config()

    assert cfg.metric_preferences == MetricPreferences(
        mplus=True,
        raid_normal=False,
        raid_heroic=False,
        raid_mythic=False,
    )


def test_save_config_defaults_write_mplus_only_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))

    config_path = save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
    )

    saved = config_path.read_text(encoding="utf-8")
    assert 'APSCOUT_FETCH_MPLUS="1"' in saved
    assert 'APSCOUT_FETCH_RAID_NORMAL="0"' in saved
    assert 'APSCOUT_FETCH_RAID_HEROIC="0"' in saved
    assert 'APSCOUT_FETCH_RAID_MYTHIC="0"' in saved


def test_load_config_round_trips_sync_with_wow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))

    save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        sync_with_wow=True,
    )

    cfg = load_config()

    assert cfg.sync_with_wow is True


def test_load_config_round_trips_draft_wcl_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _clean_load_config_env(monkeypatch, tmp_path)
    monkeypatch.delenv("WCL_CLIENT_ID", raising=False)
    monkeypatch.delenv("WCL_CLIENT_SECRET", raising=False)
    config_path = user_config_path()

    save_config_values(
        wcl_client_id="active-client",
        wcl_client_secret="active-secret",
        draft_wcl_client_id="draft-client",
        draft_wcl_client_secret="draft-secret",
        region="EU",
        config_path=config_path,
    )

    cfg = load_config()

    assert cfg.wcl_client_id == "active-client"
    assert cfg.wcl_client_secret == "active-secret"
    assert cfg.draft_wcl_client_id == "draft-client"
    assert cfg.draft_wcl_client_secret == "draft-secret"


def test_persist_settings_values_stages_changed_wcl_credentials_as_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    values = SimpleNamespace(
        wcl_client_id="new-client",
        wcl_client_secret="new-secret",
        region="EU",
        screenshots_path=str(tmp_path / "Screenshots"),
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=False,
    )
    saved: dict[str, object] = {}

    monkeypatch.setattr(main_mod, "save_config_values", lambda **kwargs: saved.update(kwargs))

    main_mod._persist_settings_values(cfg, values, apply_credentials=False)

    assert saved["wcl_client_id"] == "client"
    assert saved["wcl_client_secret"] == "secret"
    assert saved["draft_wcl_client_id"] == "new-client"
    assert saved["draft_wcl_client_secret"] == "new-secret"


def test_persist_settings_values_promotes_validated_wcl_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    cfg.draft_wcl_client_id = "new-client"
    cfg.draft_wcl_client_secret = "new-secret"
    values = SimpleNamespace(
        wcl_client_id="new-client",
        wcl_client_secret="new-secret",
        region="EU",
        screenshots_path=str(tmp_path / "Screenshots"),
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=False,
    )
    saved: dict[str, object] = {}

    monkeypatch.setattr(main_mod, "save_config_values", lambda **kwargs: saved.update(kwargs))

    main_mod._persist_settings_values(cfg, values, apply_credentials=True)

    assert saved["wcl_client_id"] == "new-client"
    assert saved["wcl_client_secret"] == "new-secret"
    assert saved["draft_wcl_client_id"] == ""
    assert saved["draft_wcl_client_secret"] == ""


def test_persist_settings_values_clearing_screenshots_override_preserves_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    chatlog = tmp_path / "World of Warcraft" / "_retail_" / "Logs" / "WoWChatLog.txt"
    cfg = _cfg(tmp_path, chatlog_path=chatlog, screenshots_path=tmp_path / "old")
    values = SimpleNamespace(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        screenshots_path="",
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=False,
    )
    saved: dict[str, object] = {}

    monkeypatch.setattr(main_mod, "save_config_values", lambda **kwargs: saved.update(kwargs))

    main_mod._persist_settings_values(cfg, values, apply_credentials=False)

    assert saved["screenshots_path"] == ""
    assert saved["chatlog_path"] == str(chatlog)


def test_load_config_uses_legacy_env_only_when_user_config_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("WCL_CLIENT_ID", raising=False)
    monkeypatch.delenv("WCL_CLIENT_SECRET", raising=False)
    legacy_env = tmp_path / ".env"
    legacy_env.write_text(
        "WCL_CLIENT_ID=legacy-client\nWCL_CLIENT_SECRET=legacy-secret\n",
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.wcl_client_id == "legacy-client"
    assert cfg.wcl_client_secret == "legacy-secret"
    assert not user_config_path().exists()


def test_load_config_missing_credentials_returns_incomplete_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("WCL_CLIENT_ID", raising=False)
    monkeypatch.delenv("WCL_CLIENT_SECRET", raising=False)

    cfg = load_config()

    assert cfg.wcl_client_id == ""
    assert cfg.wcl_client_secret == ""
    assert not is_config_ready(cfg)
    assert not (tmp_path / ".env").exists()


def test_malformed_legacy_path_outside_retail_logs_raises(tmp_path: Path):
    chatlog = tmp_path / "Logs" / "WoWChatLog.txt"

    with pytest.raises(ConfigError, match="not under a _retail_ folder"):
        resolve_screenshots_path(_cfg(tmp_path, chatlog_path=chatlog))


def test_main_returns_before_startup_when_inferred_screenshots_path_is_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = _retail_root(tmp_path)
    cfg = _cfg(tmp_path, chatlog_path=root / "Logs" / "WoWChatLog.txt")
    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "_has_running_instance", lambda *_args: False)
    monkeypatch.setattr(main_mod, "_run_settings_dialog", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(main_mod.QMessageBox, "warning", lambda *_args, **_kwargs: None)

    class FakeApp:
        def __init__(self, *_args, **_kwargs):
            pass

        def setApplicationName(self, _name: str) -> None:
            pass

        def setWindowIcon(self, _icon) -> None:
            pass

    monkeypatch.setattr(main_mod, "QApplication", FakeApp)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("startup continued after ConfigError")

    monkeypatch.setattr(main_mod, "WCLAuth", fail_if_called)
    monkeypatch.setattr(main_mod, "WCLClient", fail_if_called)
    monkeypatch.setattr(main_mod, "ScreenshotWatcher", fail_if_called)

    assert main_mod.main() == 1
    assert not (root / "Screenshots").exists()


def test_main_returns_before_startup_when_cache_ttl_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
):
    def raise_config_error():
        raise ConfigError("APSCOUT_CACHE_TTL_SECONDS must be a positive integer")

    monkeypatch.setattr(main_mod, "load_config", raise_config_error)
    monkeypatch.setattr(main_mod, "_has_running_instance", lambda *_args: False)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("startup continued after ConfigError")

    class FakeApp:
        def __init__(self, *_args, **_kwargs):
            pass

        def setApplicationName(self, _name: str) -> None:
            pass

        def setWindowIcon(self, _icon) -> None:
            pass

    monkeypatch.setattr(main_mod, "QApplication", FakeApp)
    monkeypatch.setattr(main_mod.QMessageBox, "critical", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_mod, "WCLAuth", fail_if_called)
    monkeypatch.setattr(main_mod, "WCLClient", fail_if_called)
    monkeypatch.setattr(main_mod, "ScreenshotWatcher", fail_if_called)

    assert main_mod.main() == 1


def test_main_shutdown_arg_exits_before_qapplication(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    monkeypatch.setattr(main_mod, "_setup_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(
        main_mod,
        "_shutdown_running_instance",
        lambda: calls.append("shutdown") or 0,
    )

    def fail_qapplication(*_args, **_kwargs):
        raise AssertionError("shutdown command should not start the GUI")

    monkeypatch.setattr(main_mod, "QApplication", fail_qapplication)

    assert main_mod.main(["--shutdown-running-instance"]) == 0
    assert calls == ["logging", "shutdown"]


def test_control_quit_command_uses_quit_callback(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    class FakeSocket:
        def readAll(self):
            return SimpleNamespace(data=lambda: b"quit\n")

        def write(self, value: bytes) -> None:
            calls.append(f"write:{value.decode().strip()}")

        def flush(self) -> None:
            calls.append("flush")

        def waitForBytesWritten(self, _timeout: int) -> None:
            calls.append("wait")

        def disconnectFromServer(self) -> None:
            calls.append("disconnect")

    class FakeTimer:
        @staticmethod
        def singleShot(_interval: int, callback) -> None:
            calls.append("singleShot")
            callback()

    monkeypatch.setattr(main_mod, "QTimer", FakeTimer)

    main_mod._handle_control_command(FakeSocket(), lambda: calls.append("quit"))

    assert calls == ["write:ok", "flush", "wait", "disconnect", "singleShot", "quit"]


def test_main_normal_launch_exits_before_startup_when_instance_is_running(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeApp:
        def __init__(self, *_args, **_kwargs):
            pass

        def setApplicationName(self, _name: str) -> None:
            pass

        def setWindowIcon(self, _icon) -> None:
            pass

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("duplicate launch should exit before startup side effects")

    monkeypatch.setattr(main_mod, "_setup_logging", lambda: None)
    monkeypatch.setattr(main_mod, "_set_windows_app_user_model_id", lambda: None)
    monkeypatch.setattr(main_mod, "QApplication", FakeApp)
    monkeypatch.setattr(main_mod, "_has_running_instance", lambda: True)
    monkeypatch.setattr(main_mod, "_load_startup_config", fail_if_called)
    monkeypatch.setattr(main_mod, "WCLAuth", fail_if_called)
    monkeypatch.setattr(main_mod, "WCLClient", fail_if_called)
    monkeypatch.setattr(main_mod, "OverlayWindow", fail_if_called)
    monkeypatch.setattr(main_mod, "ScreenshotWatcher", fail_if_called)

    assert main_mod.main([]) == 0


def test_load_startup_config_marks_whether_first_run_setup_was_shown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    incomplete_cfg = _cfg(tmp_path)
    incomplete_cfg.wcl_client_id = ""
    ready_cfg = _cfg(tmp_path, screenshots_path=tmp_path / "Screenshots")
    calls: list[str] = []

    configs = iter([incomplete_cfg, ready_cfg])
    monkeypatch.setattr(main_mod, "load_config", lambda: next(configs))
    monkeypatch.setattr(
        main_mod,
        "_run_settings_dialog",
        lambda *_args, **_kwargs: calls.append("settings") or True,
    )
    monkeypatch.setattr(
        main_mod,
        "resolve_screenshots_path",
        lambda _cfg: tmp_path / "Screenshots",
    )

    loaded = main_mod._load_startup_config()

    assert loaded == (ready_cfg, tmp_path / "Screenshots", True)
    assert calls == ["settings"]


def test_load_startup_config_prompts_for_saved_suspicious_screenshots_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    bad_cfg = _cfg(tmp_path, screenshots_path=tmp_path / "not-wow" / "Shots")
    root = _retail_root(tmp_path)
    (root / "WTF").mkdir(parents=True)
    ready_cfg = _cfg(tmp_path, screenshots_path=root / "Screenshots")
    configs = iter([bad_cfg, ready_cfg])
    calls: list[str] = []

    monkeypatch.setattr(main_mod, "load_config", lambda: next(configs))
    monkeypatch.setattr(
        main_mod,
        "_run_settings_dialog",
        lambda *_args, **_kwargs: calls.append("settings") or True,
    )
    monkeypatch.setattr(
        main_mod.QMessageBox,
        "warning",
        lambda *_args, **_kwargs: calls.append("warning"),
    )

    loaded = main_mod._load_startup_config()

    assert loaded == (ready_cfg, root / "Screenshots", True)
    assert calls == ["warning", "settings"]


def test_load_startup_config_rejects_suspicious_process_env_override_without_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    bad_path = tmp_path / "not-wow" / "Shots"
    cfg = _cfg(tmp_path, screenshots_path=bad_path)
    calls: list[str] = []

    monkeypatch.setenv("APSCOUT_SCREENSHOTS_PATH", str(bad_path))
    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        main_mod,
        "_run_settings_dialog",
        lambda *_args, **_kwargs: calls.append("settings") or True,
    )
    monkeypatch.setattr(
        main_mod,
        "_show_config_error",
        lambda message: calls.append(message),
    )

    loaded = main_mod._load_startup_config()

    assert loaded is None
    assert calls
    assert "APSCOUT_SCREENSHOTS_PATH" in calls[0]
    assert calls == [calls[0]]


def test_show_settings_start_arg_only_opens_when_startup_dialog_did_not_show():
    assert main_mod._should_show_settings_on_start(
        ["--show-settings"], startup_settings_shown=False
    )
    assert not main_mod._should_show_settings_on_start(
        ["--show-settings"], startup_settings_shown=True
    )
    assert not main_mod._should_show_settings_on_start(
        [], startup_settings_shown=False
    )


def test_wow_watch_mode_exits_if_sync_is_disabled_while_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    enabled_cfg = _cfg(tmp_path)
    enabled_cfg.sync_with_wow = True
    disabled_cfg = _cfg(tmp_path)
    disabled_cfg.sync_with_wow = False
    configs = iter([enabled_cfg, disabled_cfg])
    checks: list[str] = []

    monkeypatch.setattr(main_mod, "load_config", lambda: next(configs))
    monkeypatch.setattr(main_mod, "is_wow_running", lambda: checks.append("wow") or False)

    args, watch_mode, early_exit = main_mod._prepare_wow_watch_mode(["--watch-wow"])

    assert args == []
    assert not watch_mode
    assert early_exit == 0
    assert checks == ["wow"]


def test_wow_watch_mode_exits_if_companion_is_already_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    cfg.sync_with_wow = True
    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "is_wow_running", lambda: True)
    monkeypatch.setattr(main_mod, "_has_running_instance", lambda: True)

    _args, watch_mode, early_exit = main_mod._prepare_wow_watch_mode(["--watch-wow"])

    assert not watch_mode
    assert early_exit == 0


def test_wow_watch_mode_keeps_waiting_when_companion_is_running_before_wow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    cfg.sync_with_wow = True
    wow_checks = iter([False, True])
    checks: list[str] = []

    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        main_mod,
        "is_wow_running",
        lambda: checks.append("wow") or next(wow_checks),
    )
    monkeypatch.setattr(main_mod, "_has_running_instance", lambda: True)
    monkeypatch.setattr(main_mod.time, "sleep", lambda _seconds: None)

    _args, watch_mode, early_exit = main_mod._prepare_wow_watch_mode(["--watch-wow"])

    assert checks == ["wow", "wow"]
    assert not watch_mode
    assert early_exit == 0


def test_first_run_settings_with_wow_sync_enabled_starts_current_session_watcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    cfg.sync_with_wow = True
    calls: list[str] = []

    class FakeDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def setWindowIcon(self, _icon) -> None:
            pass

        def exec(self):
            return main_mod.QDialog.DialogCode.Accepted

        def values(self):
            class Values:
                wcl_client_id = "client"
                wcl_client_secret = "secret"
                region = "EU"
                screenshots_path = str(tmp_path / "Screenshots")
                metric_preferences = cfg.metric_preferences
                sync_with_wow = True

            return Values()

    monkeypatch.setattr(main_mod, "SettingsDialog", FakeDialog)
    monkeypatch.setattr(main_mod, "_app_icon", lambda: object())
    monkeypatch.setattr(main_mod, "save_config_values", lambda **_kwargs: calls.append("save"))
    monkeypatch.setattr(
        main_mod,
        "configure_wow_sync_startup",
        lambda _enabled: calls.append("shortcut"),
    )
    monkeypatch.setattr(
        main_mod,
        "start_wow_sync_watcher",
        lambda: calls.append("watcher"),
        raising=False,
    )

    assert main_mod._run_first_run_settings(cfg)
    assert calls == ["save", "shortcut", "watcher"]


def test_wow_sync_runtime_apply_starts_and_stops_lifecycle_timer(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class FakeTimer:
        def __init__(self) -> None:
            self.stopped = False
            self.deleted = False

        def stop(self) -> None:
            self.stopped = True
            calls.append("timer-stop")

        def deleteLater(self) -> None:
            self.deleted = True
            calls.append("timer-delete")

    timer = FakeTimer()
    app = object()
    monkeypatch.setattr(
        main_mod,
        "configure_wow_sync_startup",
        lambda enabled: calls.append(f"shortcut:{enabled}"),
    )
    monkeypatch.setattr(
        main_mod,
        "start_wow_sync_watcher",
        lambda: calls.append("watcher"),
    )
    monkeypatch.setattr(main_mod, "is_wow_running", lambda: True)
    monkeypatch.setattr(
        main_mod,
        "_start_wow_lifecycle_timer",
        lambda _app, *, has_seen_wow, quit_app=None: calls.append(
            f"timer-start:{has_seen_wow}:{quit_app is None}"
        )
        or timer,
    )

    started = main_mod._apply_wow_sync_runtime(app, True, None)
    stopped = main_mod._apply_wow_sync_runtime(app, False, started)

    assert started is timer
    assert stopped is None
    assert calls == [
        "shortcut:True",
        "watcher",
        "timer-start:True:True",
        "shortcut:False",
        "timer-stop",
        "timer-delete",
    ]


def test_wow_lifecycle_timer_waits_until_wow_seen_before_quitting(
    monkeypatch: pytest.MonkeyPatch,
):
    callbacks = []
    states = iter([False, True, False])
    quit_calls: list[str] = []

    class FakeTimer:
        def __init__(self, _parent) -> None:
            self.timeout = SimpleNamespace(connect=lambda callback: callbacks.append(callback))

        def setInterval(self, _interval: int) -> None:
            pass

        def start(self) -> None:
            pass

    class FakeApp:
        def quit(self) -> None:
            quit_calls.append("quit")

    monkeypatch.setattr(main_mod, "QTimer", FakeTimer)
    monkeypatch.setattr(main_mod, "is_wow_running", lambda: next(states))

    main_mod._start_wow_lifecycle_timer(FakeApp(), has_seen_wow=False)

    callbacks[0]()
    callbacks[0]()
    callbacks[0]()

    assert quit_calls == ["quit"]


def test_wow_lifecycle_timer_rearms_watcher_before_quitting(
    monkeypatch: pytest.MonkeyPatch,
):
    callbacks = []
    calls: list[str] = []

    class FakeTimer:
        def __init__(self, _parent) -> None:
            self.timeout = SimpleNamespace(connect=lambda callback: callbacks.append(callback))

        def setInterval(self, _interval: int) -> None:
            pass

        def start(self) -> None:
            pass

    class FakeApp:
        def quit(self) -> None:
            calls.append("quit")

    monkeypatch.setattr(main_mod, "QTimer", FakeTimer)
    monkeypatch.setattr(main_mod, "is_wow_running", lambda: False)
    monkeypatch.setattr(
        main_mod,
        "start_wow_sync_watcher",
        lambda: calls.append("watcher"),
    )

    main_mod._start_wow_lifecycle_timer(FakeApp(), has_seen_wow=True)

    callbacks[0]()

    assert calls == ["watcher", "quit"]


def test_wow_sync_runtime_apply_starts_lifecycle_timer_even_when_wow_is_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []
    timer = object()

    monkeypatch.setattr(
        main_mod,
        "configure_wow_sync_startup",
        lambda enabled: calls.append(f"shortcut:{enabled}"),
    )
    monkeypatch.setattr(
        main_mod,
        "start_wow_sync_watcher",
        lambda: calls.append("watcher"),
    )
    monkeypatch.setattr(main_mod, "is_wow_running", lambda: False)
    monkeypatch.setattr(
        main_mod,
        "_start_wow_lifecycle_timer",
        lambda _app, *, has_seen_wow, quit_app=None: calls.append(
            f"timer-start:{has_seen_wow}:{quit_app is None}"
        )
        or timer,
    )

    started = main_mod._apply_wow_sync_runtime(object(), True, None)

    assert started is timer
    assert calls == ["shortcut:True", "watcher", "timer-start:False:True"]


def test_replace_screenshot_watcher_keeps_old_watcher_when_new_start_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[str] = []

    class FakeWatcher:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.snapshotReceived = type("Signal", (), {"connect": lambda *_args: None})()
            self.decodeFailed = type("Signal", (), {"connect": lambda *_args: None})()

        def start(self) -> None:
            calls.append(f"start:{self.path.name}")
            if self.path.name == "new":
                raise RuntimeError("cannot watch")

        def stop(self) -> None:
            calls.append(f"stop:{self.path.name}")

    old = FakeWatcher(tmp_path / "old")
    monkeypatch.setattr(main_mod, "ScreenshotWatcher", FakeWatcher)

    with pytest.raises(RuntimeError, match="cannot watch"):
        main_mod._replace_screenshot_watcher(
            old,
            tmp_path / "new",
            object(),
            object(),
            lambda *_args: None,
        )

    assert calls == ["start:new"]


def test_replace_screenshot_watcher_starts_new_before_stopping_old(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[str] = []

    class FakeSignal:
        def connect(self, _callback) -> None:
            pass

    class FakeWatcher:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.snapshotReceived = FakeSignal()
            self.decodeFailed = FakeSignal()

        def start(self) -> None:
            calls.append(f"start:{self.path.name}")

        def stop(self) -> None:
            calls.append(f"stop:{self.path.name}")

    old = FakeWatcher(tmp_path / "old")
    monkeypatch.setattr(main_mod, "ScreenshotWatcher", FakeWatcher)

    new = main_mod._replace_screenshot_watcher(
        old,
        tmp_path / "new",
        object(),
        object(),
        lambda *_args: None,
    )

    assert new.path == tmp_path / "new"
    assert calls == ["start:new", "stop:old"]


def test_settings_saved_status_preserves_screenshots_path_warning(tmp_path: Path):
    values = SimpleNamespace(screenshots_path=str(tmp_path / "not-wow"))

    text, is_error = main_mod._settings_saved_status(values, [])

    assert is_error
    assert "Screenshots folder warning" in text


def test_update_result_has_installable_asset_accepts_case_insensitive_setup_name():
    class Result:
        asset_name = "applicantscoutcompanionsetup-0.2.0.EXE"
        asset_url = "https://example.test/setup.exe"
        checksum_name = "applicantscoutcompanionsetup-0.2.0.EXE.sha256"
        checksum_url = "https://example.test/setup.exe.sha256"

    assert main_mod._update_result_has_installable_asset(Result())


def test_update_result_has_installable_asset_rejects_missing_checksum():
    class Result:
        asset_name = "ApplicantScoutCompanionSetup-0.2.0.exe"
        asset_url = "https://example.test/setup.exe"
        checksum_name = None
        checksum_url = None

    assert not main_mod._update_result_has_installable_asset(Result())


def test_update_result_has_installable_asset_rejects_portable_zip():
    class Result:
        asset_name = "ApplicantScoutCompanion-0.2.0-portable.zip"
        asset_url = "https://example.test/portable.zip"
        checksum_name = "ApplicantScoutCompanion-0.2.0-portable.zip.sha256"
        checksum_url = "https://example.test/portable.zip.sha256"

    assert not main_mod._update_result_has_installable_asset(Result())


def test_update_checks_run_hourly_after_initial_startup():
    assert main_mod.UPDATE_CHECK_INITIAL_MS == 30_000
    assert main_mod.UPDATE_CHECK_INTERVAL_MS == 60 * 60 * 1000


def test_check_updates_treats_unavailable_update_check_as_error(
    monkeypatch: pytest.MonkeyPatch,
):
    result = SimpleNamespace(
        status="unavailable",
        message="GitHub update check failed: offline",
        asset_name=None,
    )
    monkeypatch.setattr(main_mod, "check_for_update", lambda _version: result)
    monkeypatch.setattr(
        "applicant_scout.updater.check_for_update",
        lambda _version: result,
    )

    with pytest.raises(RuntimeError, match="offline"):
        main_mod._check_updates()


def test_check_updates_treats_uninstallable_available_release_as_error(
    monkeypatch: pytest.MonkeyPatch,
):
    result = SimpleNamespace(
        status="available",
        message="Version v0.2.0 is available, but no installer asset was published.",
        latest_version="v0.2.0",
        asset_name=None,
    )
    monkeypatch.setattr(main_mod, "check_for_update", lambda _version: result)
    monkeypatch.setattr(
        "applicant_scout.updater.check_for_update",
        lambda _version: result,
    )

    with pytest.raises(RuntimeError, match="no installer asset"):
        main_mod._check_updates()


def test_check_updates_downloads_and_launches_installable_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    result = SimpleNamespace(
        status="available",
        message="Version v0.2.0 is available.",
        latest_version="v0.2.0",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        asset_url="https://example.test/setup.exe",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
        checksum_url="https://example.test/setup.exe.sha256",
    )
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    calls: list[object] = []
    monkeypatch.setattr(main_mod, "check_for_update", lambda _version: result)
    monkeypatch.setattr(
        main_mod,
        "download_update_installer",
        lambda update_result: calls.append(update_result) or installer,
    )
    monkeypatch.setattr(
        main_mod,
        "launch_update_installer",
        lambda path: calls.append(path),
    )

    message, open_url = main_mod._check_updates()

    assert calls == [result, installer]
    assert open_url is None
    assert "Installing ApplicantScout Companion v0.2.0" in message


def test_settings_dialog_gets_explicit_app_icon_before_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    icon = object()
    seen_dialogs: list[FakeDialog] = []

    class FakeDialog:
        def __init__(self, *_args, **_kwargs):
            self.icon = None
            seen_dialogs.append(self)

        def setWindowIcon(self, value) -> None:
            self.icon = value

        def exec(self):
            return main_mod.QDialog.DialogCode.Rejected

    monkeypatch.setattr(main_mod, "_app_icon", lambda: icon)
    monkeypatch.setattr(main_mod, "SettingsDialog", FakeDialog)

    assert not main_mod._run_settings_dialog(cfg, first_run=True)
    assert seen_dialogs
    assert seen_dialogs[0].icon is icon


def test_tray_controller_exposes_running_status_and_controls(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeSignal:
        def __init__(self) -> None:
            self._callbacks = []

        def connect(self, callback) -> None:
            self._callbacks.append(callback)

        def emit(self, *args) -> None:
            for callback in self._callbacks:
                callback(*args)

    class FakeAction:
        def __init__(self, text: str) -> None:
            self.text = text
            self.enabled = True
            self.triggered = FakeSignal()

        def trigger(self) -> None:
            self.triggered.emit()

        def setEnabled(self, value: bool) -> None:
            self.enabled = value

        def setText(self, value: str) -> None:
            self.text = value

    class FakeMenu:
        def __init__(self) -> None:
            self.actions: list[FakeAction | None] = []

        def addAction(self, text: str) -> FakeAction:
            action = FakeAction(text)
            self.actions.append(action)
            return action

        def addSeparator(self) -> None:
            self.actions.append(None)

    class FakeTray:
        class ActivationReason:
            DoubleClick = "double-click"

        MessageIcon = main_mod.QSystemTrayIcon.MessageIcon

        def __init__(self, icon, parent) -> None:
            self.icon = icon
            self.parent = parent
            self.activated = FakeSignal()
            self.tooltip = ""
            self.menu = None
            self.shown = False
            self.messages: list[tuple[str, str, object, int]] = []

        @staticmethod
        def isSystemTrayAvailable() -> bool:
            return True

        def setToolTip(self, value: str) -> None:
            self.tooltip = value

        def setContextMenu(self, menu) -> None:
            self.menu = menu

        def show(self) -> None:
            self.shown = True

        def showMessage(self, title: str, body: str, icon, timeout_ms: int) -> None:
            self.messages.append((title, body, icon, timeout_ms))

    class FakeApp:
        def __init__(self) -> None:
            self.quit_on_last_window_closed = True
            self.quit_called = False

        def setQuitOnLastWindowClosed(self, value: bool) -> None:
            self.quit_on_last_window_closed = value

        def quit(self) -> None:
            self.quit_called = True

    class FakeWindow:
        def __init__(self) -> None:
            self.show_called = False
            self.hide_called = False
            self.raise_called = False
            self.activate_called = False

        def show(self) -> None:
            self.show_called = True

        def hide(self) -> None:
            self.hide_called = True

        def raise_(self) -> None:
            self.raise_called = True

        def activateWindow(self) -> None:
            self.activate_called = True

    monkeypatch.setattr(main_mod, "QMenu", FakeMenu)
    monkeypatch.setattr(main_mod, "QSystemTrayIcon", FakeTray)
    app = FakeApp()
    window = FakeWindow()
    calls: list[str] = []

    controller = main_mod._create_tray_controller(
        app,
        icon=object(),
        window=window,
        show_settings=lambda: calls.append("settings"),
        open_logs=lambda: calls.append("logs") or "logs opened",
        run_update=lambda: calls.append("update"),
        quit_app=lambda: calls.append("quit"),
    )

    assert controller is not None
    assert not app.quit_on_last_window_closed
    assert controller.tray.tooltip == "ApplicantScout Companion is running"
    assert controller.tray.shown
    assert controller.tray.messages == [
        (
            "ApplicantScout Companion",
            "Running in the system tray. Right-click for settings.",
            FakeTray.MessageIcon.Information,
            5000,
        )
    ]
    assert [action.text if action else None for action in controller.menu.actions] == [
        "Open settings",
        "Show overlay",
        "Hide overlay",
        "Update",
        "Open logs",
        None,
        "Quit ApplicantScout",
    ]

    controller.settings_action.trigger()
    controller.show_overlay_action.trigger()
    controller.hide_overlay_action.trigger()
    controller.update_action.trigger()
    controller.open_logs_action.trigger()
    controller.tray.activated.emit(FakeTray.ActivationReason.DoubleClick)
    controller.quit_action.trigger()

    assert calls == ["settings", "update", "logs", "settings", "quit"]
    assert window.show_called
    assert window.hide_called
    assert window.raise_called
    assert window.activate_called
    assert not app.quit_called


def test_tray_controller_skips_unavailable_system_tray(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeTray:
        @staticmethod
        def isSystemTrayAvailable() -> bool:
            return False

    class FakeApp:
        def __init__(self) -> None:
            self.quit_on_last_window_closed = True

        def setQuitOnLastWindowClosed(self, value: bool) -> None:
            self.quit_on_last_window_closed = value

    monkeypatch.setattr(main_mod, "QSystemTrayIcon", FakeTray)
    app = FakeApp()

    assert (
        main_mod._create_tray_controller(
            app,
            icon=object(),
            window=object(),
            show_settings=lambda: None,
            open_logs=lambda: "",
            run_update=lambda: None,
            quit_app=lambda: None,
        )
        is None
    )
    assert not app.quit_on_last_window_closed


def test_wcl_credential_test_ignores_shared_cached_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "token.json").write_text("old-token", encoding="utf-8")
    seen_cache_dirs: list[Path] = []

    class FakeAuth:
        def __init__(self, _client_id: str, _client_secret: str, cache_path: Path):
            seen_cache_dirs.append(cache_path)
            assert cache_path != cache_dir
            assert not (cache_path / "token.json").exists()

        def get_token(self) -> str:
            return "fresh-token"

    monkeypatch.setattr(main_mod, "WCLAuth", FakeAuth)

    assert (
        main_mod._test_wcl_credentials(cache_dir, "new-client", "new-secret", "EU")
        == "WCL credentials are valid."
    )
    assert seen_cache_dirs
    assert (cache_dir / "token.json").read_text(encoding="utf-8") == "old-token"
