from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import applicant_scout.__main__ as main_mod
from applicant_scout import atomic_io
import applicant_scout.config as config_mod
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


def _without_root_logging_handlers() -> tuple[logging.Logger, list[logging.Handler]]:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    for handler in old_handlers:
        root.removeHandler(handler)
    return root, old_handlers


def _restore_root_logging_handlers(
    root: logging.Logger,
    old_handlers: list[logging.Handler],
) -> None:
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in old_handlers:
        root.addHandler(handler)


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


def test_release_notes_loader_prefers_frozen_app_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    app_dir = tmp_path / "ApplicantScout"
    app_dir.mkdir()
    notes = app_dir / "RELEASE_NOTES.md"
    notes.write_text("# Packaged notes\n", encoding="utf-8")
    monkeypatch.setattr(main_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main_mod.sys, "executable", str(app_dir / "ApplicantScout.exe"))
    monkeypatch.delattr(main_mod.sys, "_MEIPASS", raising=False)

    assert main_mod._load_release_notes_text() == "# Packaged notes\n"


def test_release_notes_loader_falls_back_to_source_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "repo"
    module_dir = root / "src" / "applicant_scout"
    module_dir.mkdir(parents=True)
    notes = root / "RELEASE_NOTES.md"
    notes.write_text("# Source notes\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "__file__", str(module_dir / "__main__.py"))
    monkeypatch.delattr(main_mod.sys, "frozen", raising=False)
    monkeypatch.delattr(main_mod.sys, "_MEIPASS", raising=False)

    assert main_mod._load_release_notes_text() == "# Source notes\n"


def test_release_notes_loader_skips_non_utf8_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    app_dir = tmp_path / "ApplicantScout"
    app_dir.mkdir()
    (app_dir / "RELEASE_NOTES.md").write_bytes(b"\xff\xfe\x00")
    root = tmp_path / "repo"
    module_dir = root / "src" / "applicant_scout"
    module_dir.mkdir(parents=True)
    (root / "RELEASE_NOTES.md").write_text("# Source notes\n", encoding="utf-8")
    monkeypatch.setattr(main_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main_mod.sys, "executable", str(app_dir / "ApplicantScout.exe"))
    monkeypatch.setattr(main_mod, "__file__", str(module_dir / "__main__.py"))
    monkeypatch.delattr(main_mod.sys, "_MEIPASS", raising=False)

    assert main_mod._load_release_notes_text() == "# Source notes\n"


def test_setup_logging_applies_private_mode_to_log_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls: list[Path] = []
    monkeypatch.setattr(
        main_mod,
        "apply_private_file_mode",
        lambda path: calls.append(Path(path)),
    )
    root, old_handlers = _without_root_logging_handlers()
    try:
        main_mod._setup_logging(tmp_path)
    finally:
        _restore_root_logging_handlers(root, old_handlers)

    assert tmp_path / "applicant-scout.log" in calls


def test_private_rotating_file_handler_applies_private_mode_to_rollover_backups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls: list[Path] = []
    monkeypatch.setattr(
        main_mod,
        "apply_private_file_mode",
        lambda path: calls.append(Path(path)),
    )
    log_path = tmp_path / "applicant-scout.log"
    handler = main_mod._PrivateRotatingFileHandler(
        log_path,
        maxBytes=1,
        backupCount=1,
        encoding="utf-8",
    )
    try:
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "abcd", (), None)
        handler.emit(record)
        handler.emit(record)
    finally:
        handler.close()

    assert log_path in calls
    assert tmp_path / "applicant-scout.log.1" in calls


def test_setup_logging_closes_replaced_handlers(tmp_path: Path):
    class DummyHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.closed_by_setup = False

        def close(self) -> None:
            self.closed_by_setup = True
            super().close()

    root, old_handlers = _without_root_logging_handlers()
    dummy = DummyHandler()
    root.addHandler(dummy)
    try:
        main_mod._setup_logging(tmp_path)
    finally:
        _restore_root_logging_handlers(root, old_handlers)

    assert dummy.closed_by_setup is True


def test_show_release_notes_dialog_uses_loaded_notes(
    monkeypatch: pytest.MonkeyPatch,
):
    created: list[tuple[str, object]] = []
    exec_calls: list[bool] = []

    class FakeDialog:
        def __init__(self, text: str, parent=None) -> None:
            created.append((text, parent))

        def exec(self) -> None:
            exec_calls.append(True)

    parent = object()
    monkeypatch.setattr(main_mod, "_load_release_notes_text", lambda: "# Notes")
    monkeypatch.setattr(main_mod, "ReleaseNotesDialog", FakeDialog)

    main_mod._show_release_notes_dialog(parent)

    assert created == [("# Notes", parent)]
    assert exec_calls == [True]


def test_show_release_notes_dialog_warns_when_notes_are_not_utf8(
    monkeypatch: pytest.MonkeyPatch,
):
    warnings: list[tuple[object, str, str]] = []

    def fake_warning(parent, title: str, text: str) -> None:
        warnings.append((parent, title, text))

    parent = object()
    monkeypatch.setattr(
        main_mod,
        "_load_release_notes_text",
        lambda: (_ for _ in ()).throw(
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        ),
    )
    monkeypatch.setattr(main_mod.QMessageBox, "warning", fake_warning)

    main_mod._show_release_notes_dialog(parent)

    assert warnings
    assert warnings[0][0] is parent
    assert warnings[0][1] == "ApplicantScout changelog"
    assert "Could not open changelog" in warnings[0][2]


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


def test_load_config_rejects_unknown_region_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _clean_load_config_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APSCOUT_REGION", "moon")

    with pytest.raises(ConfigError, match="APSCOUT_REGION"):
        load_config()


def test_save_config_values_rejects_unknown_region(tmp_path: Path):
    with pytest.raises(ConfigError, match="APSCOUT_REGION"):
        save_config_values(
            wcl_client_id="client",
            wcl_client_secret="secret",
            region="moon",
            config_path=tmp_path / "config.env",
        )


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


def test_load_config_rejects_all_wcl_metric_flags_disabled_from_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _clean_load_config_env(monkeypatch, tmp_path)
    save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=False,
        ),
    )

    with pytest.raises(ConfigError, match="at least one WCL data type"):
        load_config()


def test_apply_process_env_overrides_rejects_all_wcl_metric_flags_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    cfg.metric_preferences = MetricPreferences(
        mplus=True,
        raid_normal=True,
        raid_heroic=True,
        raid_mythic=True,
    )
    monkeypatch.setenv("APSCOUT_FETCH_MPLUS", "0")
    monkeypatch.setenv("APSCOUT_FETCH_RAID_NORMAL", "0")
    monkeypatch.setenv("APSCOUT_FETCH_RAID_HEROIC", "0")
    monkeypatch.setenv("APSCOUT_FETCH_RAID_MYTHIC", "0")

    with pytest.raises(ConfigError, match="at least one WCL data type"):
        main_mod._apply_process_env_overrides_to_config(cfg)


def test_load_config_rejects_invalid_metric_bool_from_process_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _clean_load_config_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APSCOUT_FETCH_MPLUS", "definitely")

    with pytest.raises(ConfigError, match="APSCOUT_FETCH_MPLUS"):
        load_config()


def test_load_config_rejects_invalid_metric_bool_from_user_config_before_legacy_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _clean_load_config_env(monkeypatch, tmp_path)
    save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        metric_preferences=MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=False,
        ),
    )
    config_path = user_config_path()
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'APSCOUT_FETCH_RAID_NORMAL="0"',
            'APSCOUT_FETCH_RAID_NORMAL="maybe"',
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        'APSCOUT_FETCH_RAID_NORMAL="1"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="APSCOUT_FETCH_RAID_NORMAL"):
        load_config()


def test_load_config_rejects_malformed_user_config_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _clean_load_config_env(monkeypatch, tmp_path)
    config_path = user_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('WCL_CLIENT_ID="unterminated\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="Could not parse ApplicantScout config"):
        load_config()


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


def test_load_config_rejects_invalid_sync_with_wow_from_process_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _clean_load_config_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APSCOUT_SYNC_WITH_WOW", "definitely")

    with pytest.raises(ConfigError, match="APSCOUT_SYNC_WITH_WOW"):
        load_config()


def test_load_config_rejects_invalid_sync_with_wow_from_user_config_before_legacy_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _clean_load_config_env(monkeypatch, tmp_path)
    save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        sync_with_wow=True,
    )
    config_path = user_config_path()
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'APSCOUT_SYNC_WITH_WOW="1"',
            'APSCOUT_SYNC_WITH_WOW="maybe"',
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        'APSCOUT_SYNC_WITH_WOW="0"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="APSCOUT_SYNC_WITH_WOW"):
        load_config()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_load_config_accepts_sync_with_wow_bool_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw: str,
    expected: bool,
):
    _clean_load_config_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APSCOUT_SYNC_WITH_WOW", raw)

    cfg = load_config()

    assert cfg.sync_with_wow is expected


@pytest.mark.parametrize(
    ("saved", "override", "expected"),
    [
        (True, "0", False),
        (False, "1", True),
    ],
)
def test_load_config_process_env_sync_with_wow_overrides_saved_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    saved: bool,
    override: str,
    expected: bool,
):
    _clean_load_config_env(monkeypatch, tmp_path)
    save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        sync_with_wow=saved,
    )
    monkeypatch.setenv("APSCOUT_SYNC_WITH_WOW", override)

    cfg = load_config()

    assert cfg.sync_with_wow is expected


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


def test_persist_settings_values_keeps_validated_wcl_draft_when_env_blocks_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    config_path = save_config_values(
        wcl_client_id="saved-client",
        wcl_client_secret="saved-secret",
        draft_wcl_client_id="draft-client",
        draft_wcl_client_secret="draft-secret",
        region="EU",
    )
    monkeypatch.setenv("WCL_CLIENT_ID", "env-client")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "env-secret")
    cfg = load_config()
    values = SimpleNamespace(
        wcl_client_id="draft-client",
        wcl_client_secret="draft-secret",
        region=cfg.region,
        screenshots_path="",
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=cfg.sync_with_wow,
    )

    main_mod._persist_settings_values(cfg, values, apply_credentials=True)
    saved = config_mod._read_env_file(config_path)

    assert saved["WCL_CLIENT_ID"] == "saved-client"
    assert saved["WCL_CLIENT_SECRET"] == "saved-secret"
    assert saved["APSCOUT_DRAFT_WCL_CLIENT_ID"] == "draft-client"
    assert saved["APSCOUT_DRAFT_WCL_CLIENT_SECRET"] == "draft-secret"


def test_persist_settings_values_repairs_invalid_saved_bool_masked_by_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    config_path = save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        metric_preferences=MetricPreferences(
            mplus=False,
            raid_normal=True,
            raid_heroic=False,
            raid_mythic=False,
        ),
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'APSCOUT_FETCH_MPLUS="0"',
            'APSCOUT_FETCH_MPLUS="definitely"',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APSCOUT_FETCH_MPLUS", "1")
    cfg = load_config()
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region="US",
        screenshots_path="",
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=cfg.sync_with_wow,
    )

    main_mod._persist_settings_values(cfg, values, apply_credentials=False)
    saved = config_mod._read_env_file(config_path)

    assert saved["APSCOUT_REGION"] == "US"
    assert saved["APSCOUT_FETCH_MPLUS"] == "1"


def test_has_pending_wcl_credentials_detects_partial_or_complete_drafts(
    tmp_path: Path,
):
    cfg = _cfg(tmp_path)

    assert not main_mod._has_pending_wcl_credentials(cfg)

    cfg.draft_wcl_client_id = "draft-client"
    assert main_mod._has_pending_wcl_credentials(cfg)

    cfg.draft_wcl_client_id = ""
    cfg.draft_wcl_client_secret = "draft-secret"
    assert main_mod._has_pending_wcl_credentials(cfg)

    cfg.draft_wcl_client_id = "draft-client"
    assert main_mod._has_pending_wcl_credentials(cfg)


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


def test_settings_change_rolls_back_config_when_screenshot_runtime_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    cfg = _cfg(tmp_path, screenshots_path=root / "old" / "Screenshots")
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region,
        screenshots_path=str(root / "new" / "Screenshots"),
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=cfg.sync_with_wow,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        main_mod,
        "_replace_screenshots_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("watch failed")),
    )
    monkeypatch.setattr(
        main_mod,
        "_persist_settings_values",
        lambda *_args, **_kwargs: calls.append("persist-new"),
    )
    monkeypatch.setattr(
        main_mod,
        "_persist_config_snapshot",
        lambda _cfg: calls.append("rollback-old"),
    )

    with pytest.raises(RuntimeError, match="watch failed"):
        main_mod._apply_settings_change(
            app=object(),
            cfg=cfg,
            values=values,
            apply_credentials=False,
            auth=object(),
            wcl_client=SimpleNamespace(region=cfg.region, reconfigure_auth=lambda _auth: None),
            region_runtime=main_mod._WCLRegionRuntime(cfg.region),
            window=SimpleNamespace(
                apply_metric_preferences=lambda *_args, **_kwargs: None,
                bump_wcl_runtime_generation=lambda: None,
            ),
            watcher=object(),
            current_screenshots_dir=cfg.screenshots_path,
            machine=object(),
            decode_failed_callback=lambda *_args: None,
            signal_gate=main_mod._WatcherSignalGate(),
            wow_exit_timer=None,
            quit_app=lambda: None,
            can_quit=lambda: True,
        )

    assert calls == ["persist-new", "rollback-old"]


def test_settings_change_rolls_back_config_when_wow_sync_runtime_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    cfg = _cfg(tmp_path, screenshots_path=root / "Screenshots")
    cfg.sync_with_wow = False
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region,
        screenshots_path=str(cfg.screenshots_path),
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=True,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        main_mod,
        "_apply_wow_sync_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("shortcut failed")),
    )
    monkeypatch.setattr(
        main_mod,
        "_persist_settings_values",
        lambda *_args, **_kwargs: calls.append("persist-new"),
    )
    monkeypatch.setattr(
        main_mod,
        "_persist_config_snapshot",
        lambda _cfg: calls.append("rollback-old"),
    )

    with pytest.raises(RuntimeError, match="shortcut failed"):
        main_mod._apply_settings_change(
            app=object(),
            cfg=cfg,
            values=values,
            apply_credentials=False,
            auth=object(),
            wcl_client=SimpleNamespace(region=cfg.region, reconfigure_auth=lambda _auth: None),
            region_runtime=main_mod._WCLRegionRuntime(cfg.region),
            window=SimpleNamespace(
                apply_metric_preferences=lambda *_args, **_kwargs: None,
                bump_wcl_runtime_generation=lambda: None,
            ),
            watcher=object(),
            current_screenshots_dir=cfg.screenshots_path,
            machine=object(),
            decode_failed_callback=lambda *_args: None,
            signal_gate=main_mod._WatcherSignalGate(),
            wow_exit_timer=None,
            quit_app=lambda: None,
            can_quit=lambda: True,
        )

    assert calls == ["persist-new", "rollback-old"]


def test_settings_change_validates_screenshots_before_wow_sync_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path, screenshots_path=None)
    cfg.sync_with_wow = False
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region,
        screenshots_path="",
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=True,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        main_mod,
        "resolve_screenshots_path",
        lambda _cfg: (_ for _ in ()).throw(ConfigError("screenshots invalid")),
    )
    monkeypatch.setattr(
        main_mod,
        "_apply_wow_sync_runtime",
        lambda *_args, **_kwargs: calls.append("wow-sync"),
    )
    monkeypatch.setattr(
        main_mod,
        "_persist_settings_values",
        lambda *_args, **_kwargs: calls.append("persist"),
    )
    monkeypatch.setattr(
        main_mod,
        "_persist_config_snapshot",
        lambda _cfg: calls.append("rollback"),
    )

    with pytest.raises(ConfigError, match="screenshots invalid"):
        main_mod._apply_settings_change(
            app=object(),
            cfg=cfg,
            values=values,
            apply_credentials=False,
            auth=object(),
            wcl_client=SimpleNamespace(region=cfg.region, reconfigure_auth=lambda _auth: None),
            region_runtime=main_mod._WCLRegionRuntime(cfg.region),
            window=SimpleNamespace(
                apply_metric_preferences=lambda *_args, **_kwargs: None,
                bump_wcl_runtime_generation=lambda: None,
            ),
            watcher=object(),
            current_screenshots_dir=tmp_path / "old" / "Screenshots",
            machine=object(),
            decode_failed_callback=lambda *_args: None,
            signal_gate=main_mod._WatcherSignalGate(),
            wow_exit_timer=None,
            quit_app=lambda: None,
            can_quit=lambda: True,
        )

    assert calls == []


def test_settings_apply_respects_process_env_overrides_for_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    env_root = tmp_path / "env" / "World of Warcraft" / "_retail_"
    ui_root = tmp_path / "ui" / "World of Warcraft" / "_retail_"
    for root in (env_root, ui_root):
        (root / "Interface" / "AddOns").mkdir(parents=True)
        (root / "Screenshots").mkdir()
    env_screenshots = env_root / "Screenshots"
    ui_screenshots = ui_root / "Screenshots"
    env_prefs = MetricPreferences(
        mplus=False,
        raid_normal=False,
        raid_heroic=True,
        raid_mythic=False,
    )
    cfg = _cfg(tmp_path, screenshots_path=env_screenshots)
    cfg.metric_preferences = env_prefs
    cfg.sync_with_wow = True
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region,
        screenshots_path=str(ui_screenshots),
        metric_preferences=MetricPreferences(),
        sync_with_wow=False,
    )
    applied_preferences: list[MetricPreferences] = []
    calls: list[str] = []

    monkeypatch.setenv("APSCOUT_SCREENSHOTS_PATH", str(env_screenshots))
    monkeypatch.setenv("APSCOUT_FETCH_MPLUS", "0")
    monkeypatch.setenv("APSCOUT_FETCH_RAID_NORMAL", "0")
    monkeypatch.setenv("APSCOUT_FETCH_RAID_HEROIC", "1")
    monkeypatch.setenv("APSCOUT_FETCH_RAID_MYTHIC", "0")
    monkeypatch.setenv("APSCOUT_SYNC_WITH_WOW", "1")
    monkeypatch.setattr(main_mod, "_persist_settings_values", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main_mod,
        "_replace_screenshots_runtime",
        lambda *_args, **_kwargs: calls.append("replace-screenshots"),
    )
    monkeypatch.setattr(
        main_mod,
        "_apply_wow_sync_runtime",
        lambda *_args, **_kwargs: calls.append("wow-sync"),
    )

    result = main_mod._apply_settings_change(
        app=object(),
        cfg=cfg,
        values=values,
        apply_credentials=False,
        auth=object(),
        wcl_client=SimpleNamespace(region=cfg.region, reconfigure_auth=lambda _auth: None),
        region_runtime=main_mod._WCLRegionRuntime(cfg.region),
        window=SimpleNamespace(
            apply_metric_preferences=lambda prefs, **_kwargs: applied_preferences.append(
                prefs
            ),
            bump_wcl_runtime_generation=lambda: None,
        ),
        watcher=object(),
        current_screenshots_dir=env_screenshots,
        machine=object(),
        decode_failed_callback=lambda *_args: None,
        signal_gate=main_mod._WatcherSignalGate(),
        wow_exit_timer=object(),
        quit_app=lambda: None,
        can_quit=lambda: True,
    )

    assert result.cfg.screenshots_path == env_screenshots
    assert result.cfg.metric_preferences == env_prefs
    assert result.cfg.sync_with_wow is True
    assert result.current_screenshots_dir == env_screenshots
    assert applied_preferences == [env_prefs]
    assert calls == []


def test_settings_change_rejects_explicit_suspicious_screenshots_before_persist_or_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path, screenshots_path=tmp_path / "old" / "Screenshots")
    suspicious_path = tmp_path / "NotWow"
    suspicious_path.mkdir()
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region,
        screenshots_path=str(suspicious_path),
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=cfg.sync_with_wow,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        main_mod,
        "_persist_settings_values",
        lambda *_args, **_kwargs: calls.append("persist"),
    )
    monkeypatch.setattr(
        main_mod,
        "_apply_wow_sync_runtime",
        lambda *_args, **_kwargs: calls.append("wow-sync"),
    )
    monkeypatch.setattr(
        main_mod,
        "_replace_screenshots_runtime",
        lambda *_args, **_kwargs: calls.append("replace") or object(),
    )

    with pytest.raises(ConfigError, match="Screenshots folder warning"):
        main_mod._apply_settings_change(
            app=object(),
            cfg=cfg,
            values=values,
            apply_credentials=False,
            auth=object(),
            wcl_client=SimpleNamespace(region=cfg.region, reconfigure_auth=lambda _auth: None),
            region_runtime=main_mod._WCLRegionRuntime(cfg.region),
            window=SimpleNamespace(
                apply_metric_preferences=lambda *_args, **_kwargs: None,
                bump_wcl_runtime_generation=lambda: None,
            ),
            watcher=object(),
            current_screenshots_dir=cfg.screenshots_path,
            machine=object(),
            decode_failed_callback=lambda *_args: None,
            signal_gate=main_mod._WatcherSignalGate(),
            wow_exit_timer=None,
            quit_app=lambda: None,
            can_quit=lambda: True,
        )

    assert calls == []


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


def test_load_config_wraps_unreadable_user_config_as_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _clean_load_config_env(monkeypatch, tmp_path)
    user_config_path().parent.mkdir(parents=True)
    user_config_path().write_text("WCL_CLIENT_ID=client\n", encoding="utf-8")
    monkeypatch.setattr(
        config_mod,
        "_read_env_file",
        lambda _path: (_ for _ in ()).throw(OSError("locked")),
    )

    with pytest.raises(ConfigError, match="Could not read ApplicantScout config"):
        load_config()


def test_load_config_wraps_non_utf8_user_config_as_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _clean_load_config_env(monkeypatch, tmp_path)
    monkeypatch.delenv("WCL_CLIENT_ID")
    monkeypatch.delenv("WCL_CLIENT_SECRET")
    user_config_path().parent.mkdir(parents=True)
    user_config_path().write_bytes(b"\xff\xfe\xfa")

    with pytest.raises(ConfigError, match="Could not read ApplicantScout config"):
        load_config()


def test_load_config_wraps_unwritable_user_data_dir_as_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _clean_load_config_env(monkeypatch, tmp_path)
    cache_path = tmp_path / "localappdata" / "applicant-scout" / "cache"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigError, match="Could not create ApplicantScout data directories"):
        load_config()


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
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            connected=False,
            written=False,
            response=None,
        ),
    )
    monkeypatch.setattr(main_mod, "_create_control_server", lambda *_args, **_kwargs: object())
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
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            connected=False,
            written=False,
            response=None,
        ),
    )
    monkeypatch.setattr(main_mod, "_create_control_server", lambda *_args, **_kwargs: object())

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


def test_control_quit_command_reports_blocked_without_quitting(
    monkeypatch: pytest.MonkeyPatch,
):
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

    main_mod._handle_control_command(
        FakeSocket(),
        lambda: calls.append("quit"),
        can_quit=lambda: False,
        quit_blocked=lambda: calls.append("blocked"),
    )

    assert calls == [
        "write:blocked",
        "flush",
        "wait",
        "disconnect",
        "singleShot",
        "blocked",
    ]


def test_control_show_settings_command_uses_show_settings_callback(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class FakeSocket:
        def readAll(self):
            return SimpleNamespace(data=lambda: b"show-settings\n")

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

    main_mod._handle_control_command(
        FakeSocket(),
        lambda: calls.append("quit"),
        lambda: calls.append("show"),
    )

    assert calls == ["write:ok", "flush", "wait", "disconnect", "singleShot", "show"]


def test_control_unknown_command_does_not_call_callbacks():
    calls: list[str] = []

    class FakeSocket:
        def readAll(self):
            return SimpleNamespace(data=lambda: b"bogus\n")

        def write(self, value: bytes) -> None:
            calls.append(f"write:{value.decode().strip()}")

        def flush(self) -> None:
            calls.append("flush")

        def disconnectFromServer(self) -> None:
            calls.append("disconnect")

    main_mod._handle_control_command(
        FakeSocket(),
        lambda: calls.append("quit"),
        lambda: calls.append("show"),
    )

    assert calls == ["write:unknown", "flush", "disconnect"]


def test_send_control_command_reports_ok_response(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    class FakeSocket:
        def connectToServer(self, server_name: str) -> None:
            calls.append(f"connect:{server_name}")

        def waitForConnected(self, timeout_ms: int) -> bool:
            calls.append(f"wait-connected:{timeout_ms}")
            return True

        def write(self, value: bytes) -> None:
            calls.append(f"write:{value.decode().strip()}")

        def waitForBytesWritten(self, timeout_ms: int) -> bool:
            calls.append(f"wait-written:{timeout_ms}")
            return True

        def waitForReadyRead(self, timeout_ms: int) -> bool:
            calls.append(f"wait-ready:{timeout_ms}")
            return True

        def readAll(self):
            calls.append("read")
            return SimpleNamespace(data=lambda: b"ok\n")

        def disconnectFromServer(self) -> None:
            calls.append("disconnect")

        def errorString(self) -> str:
            return "socket error"

    monkeypatch.setattr(main_mod, "QLocalSocket", FakeSocket)

    result = main_mod._send_control_command(main_mod.CONTROL_SHOW_SETTINGS_COMMAND)

    assert result.connected
    assert result.written
    assert result.response == b"ok"
    assert calls == [
        f"connect:{main_mod.CONTROL_SERVER_NAME}",
        "wait-connected:2000",
        "write:show-settings",
        "wait-written:2000",
        "wait-ready:500",
        "read",
        "disconnect",
    ]


def test_shutdown_running_instance_reports_blocked_quit(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda *_args, **_kwargs: main_mod._ControlCommandResult(
            connected=True,
            written=True,
            response=b"blocked",
        ),
    )

    assert main_mod._shutdown_running_instance() == 1


@pytest.mark.parametrize("response", [None, b"unknown"])
def test_shutdown_running_instance_requires_ok_response(
    monkeypatch: pytest.MonkeyPatch,
    response: bytes | None,
):
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda *_args, **_kwargs: main_mod._ControlCommandResult(
            connected=True,
            written=True,
            response=response,
        ),
    )

    assert main_mod._shutdown_running_instance() == 1


def test_deferred_gui_action_coalesces_requests_until_callback_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class FakeTimer:
        @staticmethod
        def singleShot(_interval: int, callback) -> None:
            calls.append("singleShot")
            callback()

    monkeypatch.setattr(main_mod, "QTimer", FakeTimer)
    action = main_mod._DeferredGuiAction()

    action.request()
    action.request()

    assert calls == []

    action.set_callback(lambda: calls.append("show"))

    assert calls == ["singleShot", "show"]

    action.request()

    assert calls == ["singleShot", "show", "singleShot", "show"]


def test_create_control_server_detects_active_owner_before_stale_cleanup(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class FakeServer:
        def __init__(self, _app) -> None:
            pass

        def listen(self, server_name: str) -> bool:
            calls.append(f"listen:{server_name}")
            return False

        def errorString(self) -> str:
            return "already listening"

    class FakeLocalServer:
        def __call__(self, app):
            return FakeServer(app)

        @staticmethod
        def removeServer(server_name: str) -> None:
            calls.append(f"remove:{server_name}")

    monkeypatch.setattr(main_mod, "QLocalServer", FakeLocalServer())
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda command, **_kwargs: calls.append(command.decode())
        or SimpleNamespace(connected=True, written=True, response=b"ok"),
    )

    with pytest.raises(main_mod._DuplicateInstanceFound):
        main_mod._create_control_server(
            object(),
            quit_app=lambda: None,
            show_settings=lambda: None,
        )

    assert calls == [f"listen:{main_mod.CONTROL_SERVER_NAME}", "show-settings"]


def test_main_duplicate_launch_exits_before_startup_when_instance_is_running(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("duplicate launch should exit before startup side effects")

    monkeypatch.setattr(main_mod, "_setup_logging", lambda: None)
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            connected=True,
            written=True,
            response=b"unknown",
        ),
    )
    monkeypatch.setattr(main_mod, "QApplication", fail_if_called)
    monkeypatch.setattr(main_mod, "_load_startup_config", fail_if_called)
    monkeypatch.setattr(main_mod, "WCLAuth", fail_if_called)
    monkeypatch.setattr(main_mod, "WCLClient", fail_if_called)
    monkeypatch.setattr(main_mod, "OverlayWindow", fail_if_called)
    monkeypatch.setattr(main_mod, "ScreenshotWatcher", fail_if_called)

    assert main_mod.main([]) == 0


def test_main_duplicate_manual_launch_requests_settings_before_qapplication(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    monkeypatch.setattr(main_mod, "_setup_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda command, **_kwargs: calls.append(command.decode())
        or SimpleNamespace(connected=True, written=True, response=b"ok"),
    )

    def fail_qapplication(*_args, **_kwargs):
        raise AssertionError("duplicate launch should not create QApplication")

    monkeypatch.setattr(main_mod, "QApplication", fail_qapplication)

    assert main_mod.main([]) == 0
    assert calls == ["logging", "show-settings"]


def test_main_duplicate_show_settings_arg_requests_settings_before_qapplication(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    monkeypatch.setattr(main_mod, "_setup_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda command, **_kwargs: calls.append(command.decode())
        or SimpleNamespace(connected=True, written=True, response=b"ok"),
    )

    def fail_qapplication(*_args, **_kwargs):
        raise AssertionError("duplicate launch should not create QApplication")

    monkeypatch.setattr(main_mod, "QApplication", fail_qapplication)

    assert main_mod.main(["--show-settings"]) == 0
    assert calls == ["logging", "show-settings"]


def test_main_manual_launch_continues_when_no_control_server_exists(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class FakeApp:
        aboutToQuit = None

        def __init__(self, *_args, **_kwargs):
            calls.append("app")

        def setApplicationName(self, _name: str) -> None:
            pass

        def quit(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_setup_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(main_mod, "_set_windows_app_user_model_id", lambda: None)
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda *_args, **_kwargs: calls.append("control")
        or SimpleNamespace(connected=False, written=False, response=None),
    )
    monkeypatch.setattr(main_mod, "QApplication", FakeApp)
    monkeypatch.setattr(main_mod, "_create_control_server", lambda *_args, **_kwargs: calls.append("server") or object())
    monkeypatch.setattr(main_mod, "_load_startup_config", lambda: calls.append("config") or None)

    assert main_mod.main([]) == 1
    assert calls == ["logging", "control", "app", "server", "config"]


def test_main_claims_control_server_before_loading_startup_config(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class FakeApp:
        aboutToQuit = None

        def __init__(self, *_args, **_kwargs):
            pass

        def setApplicationName(self, _name: str) -> None:
            pass

        def quit(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_setup_logging", lambda: None)
    monkeypatch.setattr(main_mod, "_set_windows_app_user_model_id", lambda: None)
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            connected=False,
            written=False,
            response=None,
        ),
    )
    monkeypatch.setattr(main_mod, "QApplication", FakeApp)
    monkeypatch.setattr(
        main_mod,
        "_create_control_server",
        lambda *_args, **_kwargs: calls.append("server") or object(),
    )
    monkeypatch.setattr(
        main_mod,
        "_load_startup_config",
        lambda: calls.append("config") or None,
    )

    assert main_mod.main([]) == 1
    assert calls == ["server", "config"]


def test_main_control_server_uses_guarded_quit_callback(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    class FakeApp:
        aboutToQuit = None

        def __init__(self, *_args, **_kwargs):
            pass

        def setApplicationName(self, _name: str) -> None:
            pass

        def quit(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_setup_logging", lambda: None)
    monkeypatch.setattr(main_mod, "_set_windows_app_user_model_id", lambda: None)
    monkeypatch.setattr(
        main_mod,
        "_send_control_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            connected=False,
            written=False,
            response=None,
        ),
    )
    monkeypatch.setattr(main_mod, "QApplication", FakeApp)

    def fake_create_control_server(*_args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(main_mod, "_create_control_server", fake_create_control_server)
    monkeypatch.setattr(main_mod, "_load_startup_config", lambda: None)

    assert main_mod.main([]) == 1
    assert captured["quit_app"].__name__ == "_request_quit_application"
    assert captured["can_quit"].__name__ == "_can_quit_application"
    assert captured["quit_blocked"].__name__ == "_show_update_quit_blocked"


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


def test_show_settings_on_start_opens_for_manual_launch_or_explicit_arg():
    assert main_mod._should_show_settings_on_start(
        ["--show-settings"], startup_settings_shown=False, wow_watch_mode=True
    )
    assert main_mod._should_show_settings_on_start(
        [], startup_settings_shown=False, wow_watch_mode=False
    )
    assert not main_mod._should_show_settings_on_start(
        [], startup_settings_shown=False, wow_watch_mode=True
    )
    assert not main_mod._should_show_settings_on_start(
        ["--show-settings"], startup_settings_shown=True, wow_watch_mode=False
    )


def test_duplicate_launch_command_only_notifies_manual_owner():
    assert main_mod._duplicate_launch_command([], wow_watch_mode=False) == (
        main_mod.CONTROL_SHOW_SETTINGS_COMMAND
    )
    assert (
        main_mod._duplicate_launch_command(
            [main_mod.SHOW_SETTINGS_ARG], wow_watch_mode=False
        )
        == main_mod.CONTROL_SHOW_SETTINGS_COMMAND
    )
    assert main_mod._duplicate_launch_command([], wow_watch_mode=True) is None
    assert (
        main_mod._duplicate_launch_command(
            [main_mod.SHOW_SETTINGS_ARG], wow_watch_mode=True
        )
        is None
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
    assert calls == ["shortcut", "save", "watcher"]


def test_persist_settings_values_preserves_process_env_overridden_saved_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    saved_shots = tmp_path / "saved" / "World of Warcraft" / "_retail_" / "Screenshots"
    env_shots = tmp_path / "env" / "World of Warcraft" / "_retail_" / "Screenshots"
    for path in (saved_shots, env_shots):
        (path.parent / "Interface" / "AddOns").mkdir(parents=True)
        path.mkdir(parents=True)
    config_path = save_config_values(
        wcl_client_id="saved-client",
        wcl_client_secret="saved-secret",
        region="EU",
        screenshots_path=str(saved_shots),
        metric_preferences=MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=False,
        ),
        sync_with_wow=False,
    )
    monkeypatch.setenv("WCL_CLIENT_ID", "env-client")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "env-secret")
    monkeypatch.setenv("APSCOUT_SCREENSHOTS_PATH", str(env_shots))
    monkeypatch.setenv("APSCOUT_FETCH_MPLUS", "0")
    monkeypatch.setenv("APSCOUT_FETCH_RAID_NORMAL", "1")
    monkeypatch.setenv("APSCOUT_SYNC_WITH_WOW", "1")
    cfg = load_config()
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region="US",
        screenshots_path=str(cfg.screenshots_path),
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=cfg.sync_with_wow,
    )

    main_mod._persist_settings_values(cfg, values, apply_credentials=False)
    saved = config_mod._read_env_file(config_path)

    assert saved["WCL_CLIENT_ID"] == "saved-client"
    assert saved["WCL_CLIENT_SECRET"] == "saved-secret"
    assert saved["APSCOUT_SCREENSHOTS_PATH"] == str(saved_shots)
    assert saved["APSCOUT_FETCH_MPLUS"] == "1"
    assert saved["APSCOUT_FETCH_RAID_NORMAL"] == "0"
    assert saved["APSCOUT_SYNC_WITH_WOW"] == "0"
    assert saved["APSCOUT_REGION"] == "US"


def test_settings_env_override_keys_includes_cache_ttl_seconds(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("APSCOUT_CACHE_TTL_SECONDS", "60")

    assert "APSCOUT_CACHE_TTL_SECONDS" in main_mod._settings_env_override_keys()


def test_persist_settings_values_preserves_saved_cache_ttl_when_env_override_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    config_path = save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        cache_ttl_seconds=43200,
        metric_preferences=MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=False,
        ),
    )
    monkeypatch.setenv("APSCOUT_CACHE_TTL_SECONDS", "60")
    cfg = load_config()
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region,
        screenshots_path="",
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=cfg.sync_with_wow,
    )

    main_mod._persist_settings_values(cfg, values, apply_credentials=False)
    saved = config_mod._read_env_file(config_path)

    assert saved["APSCOUT_CACHE_TTL_SECONDS"] == "43200"


def test_persist_settings_values_does_not_persist_env_only_cache_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    config_path = save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        metric_preferences=MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=False,
        ),
    )
    monkeypatch.setenv("APSCOUT_CACHE_TTL_SECONDS", "60")
    cfg = load_config()
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region,
        screenshots_path="",
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=cfg.sync_with_wow,
    )

    main_mod._persist_settings_values(cfg, values, apply_credentials=False)
    saved = config_mod._read_env_file(config_path)

    assert "APSCOUT_CACHE_TTL_SECONDS" not in saved


def test_persist_config_snapshot_preserves_saved_cache_ttl_when_env_override_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    config_path = save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        cache_ttl_seconds=43200,
        metric_preferences=MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=False,
        ),
    )
    monkeypatch.setenv("APSCOUT_CACHE_TTL_SECONDS", "60")
    cfg = load_config()

    main_mod._persist_config_snapshot(cfg)
    saved = config_mod._read_env_file(config_path)

    assert saved["APSCOUT_CACHE_TTL_SECONDS"] == "43200"


def test_persist_settings_values_repairs_invalid_saved_cache_ttl_masked_by_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    config_path = save_config_values(
        wcl_client_id="client",
        wcl_client_secret="secret",
        region="EU",
        metric_preferences=MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=False,
            raid_mythic=False,
        ),
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "APSCOUT_CACHE_TTL_SECONDS=not-an-int\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APSCOUT_CACHE_TTL_SECONDS", "60")
    cfg = load_config()
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id,
        wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region,
        screenshots_path="",
        metric_preferences=cfg.metric_preferences,
        sync_with_wow=cfg.sync_with_wow,
    )

    main_mod._persist_settings_values(cfg, values, apply_credentials=False)
    saved = config_mod._read_env_file(config_path)

    assert "APSCOUT_CACHE_TTL_SECONDS" not in saved


def test_first_run_wow_sync_failure_does_not_persist_enabled_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    calls: list[str] = []
    warnings: list[str] = []

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
        lambda _enabled: (_ for _ in ()).throw(RuntimeError("shortcut failed")),
    )
    monkeypatch.setattr(
        main_mod.QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    assert not main_mod._run_first_run_settings(cfg)
    assert calls == []
    assert len(warnings) == 1
    assert "shortcut failed" in warnings[0]


def test_first_run_wow_sync_disable_cleanup_failure_still_saves_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    calls: list[str] = []
    warnings: list[str] = []

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
                sync_with_wow = False

            return Values()

    monkeypatch.setattr(main_mod, "SettingsDialog", FakeDialog)
    monkeypatch.setattr(main_mod, "_app_icon", lambda: object())
    monkeypatch.setattr(main_mod, "save_config_values", lambda **_kwargs: calls.append("save"))
    monkeypatch.setattr(
        main_mod,
        "configure_wow_sync_startup",
        lambda _enabled: (_ for _ in ()).throw(RuntimeError("shortcut cleanup failed")),
    )
    monkeypatch.setattr(
        main_mod.QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    assert main_mod._run_first_run_settings(cfg)
    assert calls == ["save"]
    assert len(warnings) == 1
    assert "Settings were saved" in warnings[0]
    assert "shortcut cleanup failed" in warnings[0]


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


def test_wow_sync_runtime_apply_rolls_back_startup_when_watcher_start_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    monkeypatch.setattr(
        main_mod,
        "configure_wow_sync_startup",
        lambda enabled: calls.append(f"shortcut:{enabled}"),
    )
    monkeypatch.setattr(
        main_mod,
        "start_wow_sync_watcher",
        lambda: calls.append("watcher")
        or (_ for _ in ()).throw(RuntimeError("watcher failed")),
    )

    with pytest.raises(RuntimeError, match="watcher failed"):
        main_mod._apply_wow_sync_runtime(object(), True, None)

    assert calls == ["shortcut:True", "watcher", "shortcut:False"]


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

    main_mod._start_wow_lifecycle_timer(
        FakeApp(),
        has_seen_wow=False,
        async_runner=lambda worker: worker(),
    )

    callbacks[0]()
    callbacks[0]()
    callbacks[0]()

    assert quit_calls == ["quit"]


def test_wow_lifecycle_timer_does_not_run_process_scan_on_gui_tick(
    monkeypatch: pytest.MonkeyPatch,
):
    callbacks = []
    workers = []
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

    def running_checker() -> bool:
        calls.append("scan")
        return False

    monkeypatch.setattr(main_mod, "QTimer", FakeTimer)

    main_mod._start_wow_lifecycle_timer(
        FakeApp(),
        has_seen_wow=True,
        running_checker=running_checker,
        async_runner=workers.append,
    )

    callbacks[0]()

    assert calls == []
    assert len(workers) == 1

    workers[0]()

    assert calls == ["scan", "quit"]


def test_wow_lifecycle_timer_retries_after_process_scan_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    callbacks = []
    workers = []
    calls: list[str] = []
    states = iter([RuntimeError("tasklist failed"), False])

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

    def running_checker() -> bool:
        state = next(states)
        if isinstance(state, Exception):
            raise state
        return state

    monkeypatch.setattr(main_mod, "QTimer", FakeTimer)

    main_mod._start_wow_lifecycle_timer(
        FakeApp(),
        has_seen_wow=True,
        running_checker=running_checker,
        async_runner=workers.append,
    )

    callbacks[0]()
    workers[0]()

    assert calls == []

    callbacks[0]()
    workers[1]()

    assert calls == ["quit"]


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

    main_mod._start_wow_lifecycle_timer(
        FakeApp(),
        has_seen_wow=True,
        async_runner=lambda worker: worker(),
    )

    callbacks[0]()

    assert calls == ["watcher", "quit"]


def test_wow_lifecycle_timer_defers_rearm_and_quit_when_quit_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
):
    callbacks = []
    calls: list[str] = []
    can_quit_values = iter([False, True])

    class FakeTimer:
        def __init__(self, _parent) -> None:
            self.timeout = SimpleNamespace(connect=lambda callback: callbacks.append(callback))

        def setInterval(self, _interval: int) -> None:
            pass

        def start(self) -> None:
            pass

    class FakeApp:
        def quit(self) -> None:
            calls.append("app-quit")

    monkeypatch.setattr(main_mod, "QTimer", FakeTimer)
    monkeypatch.setattr(main_mod, "is_wow_running", lambda: False)
    monkeypatch.setattr(
        main_mod,
        "start_wow_sync_watcher",
        lambda: calls.append("watcher"),
    )

    main_mod._start_wow_lifecycle_timer(
        FakeApp(),
        has_seen_wow=True,
        quit_app=lambda: calls.append("quit"),
        can_quit=lambda: next(can_quit_values),
        async_runner=lambda worker: worker(),
    )

    callbacks[0]()
    assert calls == []

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
            signal_gate=main_mod._WatcherSignalGate(),
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
        signal_gate=main_mod._WatcherSignalGate(),
    )

    assert new.path == tmp_path / "new"
    assert calls == ["start:new", "stop:old"]


def test_replace_screenshot_watcher_ignores_old_queued_signals_after_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    class FakeSignal:
        def __init__(self) -> None:
            self._callbacks = []

        def connect(self, callback) -> None:
            self._callbacks.append(callback)

        def emit(self, *args) -> None:
            for callback in list(self._callbacks):
                callback(*args)

    class FakeWatcher:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.snapshotReceived = FakeSignal()
            self.decodeFailed = FakeSignal()

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeMachine:
        def __init__(self) -> None:
            self.snapshots: list[object] = []

        def apply_snapshot(self, snap: object) -> None:
            self.snapshots.append(snap)

    class FakeWindow:
        def __init__(self) -> None:
            self.decoded: list[object] = []
            self.failures: list[tuple[str, str]] = []

        def note_decode(self, snap: object) -> None:
            self.decoded.append(snap)

        def note_decode_failed(self, path: str, reason: str) -> None:
            self.failures.append((path, reason))

    created: list[FakeWatcher] = []

    def create_watcher(path: Path) -> FakeWatcher:
        watcher = FakeWatcher(path)
        created.append(watcher)
        return watcher

    machine = FakeMachine()
    window = FakeWindow()
    failures: list[tuple[str, str]] = []
    gate = main_mod._WatcherSignalGate()
    monkeypatch.setattr(main_mod, "ScreenshotWatcher", create_watcher)

    old = main_mod._replace_screenshot_watcher(
        None,
        tmp_path / "old",
        machine,
        window,
        lambda path, reason: failures.append((path, reason)),
        signal_gate=gate,
    )
    new = main_mod._replace_screenshot_watcher(
        old,
        tmp_path / "new",
        machine,
        window,
        lambda path, reason: failures.append((path, reason)),
        signal_gate=gate,
    )

    old.snapshotReceived.emit("old-snap")
    old.decodeFailed.emit("old.jpg", "old failed")
    new.snapshotReceived.emit("new-snap")
    new.decodeFailed.emit("new.jpg", "new failed")

    assert created == [old, new]
    assert machine.snapshots == ["new-snap"]
    assert window.decoded == ["new-snap"]
    assert failures == [("new.jpg", "new failed")]
    assert window.failures == [("new.jpg", "new failed")]


def test_replace_screenshot_watcher_restores_old_generation_when_new_start_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    class FakeSignal:
        def __init__(self) -> None:
            self._callbacks = []

        def connect(self, callback) -> None:
            self._callbacks.append(callback)

        def emit(self, *args) -> None:
            for callback in list(self._callbacks):
                callback(*args)

    class FakeWatcher:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.snapshotReceived = FakeSignal()
            self.decodeFailed = FakeSignal()

        def start(self) -> None:
            if self.path.name == "new":
                raise RuntimeError("cannot watch")

        def stop(self) -> None:
            pass

    class FakeMachine:
        def __init__(self) -> None:
            self.snapshots: list[object] = []

        def apply_snapshot(self, snap: object) -> None:
            self.snapshots.append(snap)

    created: list[FakeWatcher] = []

    def create_watcher(path: Path) -> FakeWatcher:
        watcher = FakeWatcher(path)
        created.append(watcher)
        return watcher

    machine = FakeMachine()
    gate = main_mod._WatcherSignalGate()
    monkeypatch.setattr(main_mod, "ScreenshotWatcher", create_watcher)

    old = main_mod._replace_screenshot_watcher(
        None,
        tmp_path / "old",
        machine,
        object(),
        lambda *_args: None,
        signal_gate=gate,
    )
    with pytest.raises(RuntimeError, match="cannot watch"):
        main_mod._replace_screenshot_watcher(
            old,
            tmp_path / "new",
            machine,
            object(),
            lambda *_args: None,
            signal_gate=gate,
        )

    old.snapshotReceived.emit("old-after-failed-replace")

    assert machine.snapshots == ["old-after-failed-replace"]


def test_replace_screenshot_watcher_keeps_old_signals_current_until_new_start_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    class FakeSignal:
        def __init__(self) -> None:
            self._callbacks = []

        def connect(self, callback) -> None:
            self._callbacks.append(callback)

        def emit(self, *args) -> None:
            for callback in list(self._callbacks):
                callback(*args)

    class FakeWatcher:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.snapshotReceived = FakeSignal()
            self.decodeFailed = FakeSignal()

        def start(self) -> None:
            if self.path.name == "new":
                old.snapshotReceived.emit("old-during-new-start")
                raise RuntimeError("cannot watch")

        def stop(self) -> None:
            pass

    class FakeMachine:
        def __init__(self) -> None:
            self.snapshots: list[object] = []

        def apply_snapshot(self, snap: object) -> None:
            self.snapshots.append(snap)

    created: list[FakeWatcher] = []

    def create_watcher(path: Path) -> FakeWatcher:
        watcher = FakeWatcher(path)
        created.append(watcher)
        return watcher

    machine = FakeMachine()
    gate = main_mod._WatcherSignalGate()
    monkeypatch.setattr(main_mod, "ScreenshotWatcher", create_watcher)

    old = main_mod._replace_screenshot_watcher(
        None,
        tmp_path / "old",
        machine,
        object(),
        lambda *_args: None,
        signal_gate=gate,
    )
    with pytest.raises(RuntimeError, match="cannot watch"):
        main_mod._replace_screenshot_watcher(
            old,
            tmp_path / "new",
            machine,
            object(),
            lambda *_args: None,
            signal_gate=gate,
        )

    assert machine.snapshots == ["old-during-new-start"]


def test_screenshot_runtime_sets_rio_reader_before_watcher_backlog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    class FakeSignal:
        def __init__(self) -> None:
            self._callbacks = []

        def connect(self, callback) -> None:
            self._callbacks.append(callback)

        def emit(self, *args) -> None:
            for callback in list(self._callbacks):
                callback(*args)

    class FakeWatcher:
        def __init__(self, _path: Path) -> None:
            self.snapshotReceived = FakeSignal()
            self.decodeFailed = FakeSignal()

        def start(self) -> None:
            self.snapshotReceived.emit("backlog-snapshot")

        def stop(self) -> None:
            pass

    class FakeMachine:
        def __init__(self) -> None:
            self.reader = "old-reader"
            self.reader_seen_by_snapshots: list[object] = []

        def set_rio_reader(self, reader: object) -> None:
            self.reader = reader

        def apply_snapshot(self, _snap: object) -> None:
            self.reader_seen_by_snapshots.append(self.reader)

    class FakeWindow:
        def note_decode(self, _snap: object) -> None:
            pass

        def note_decode_failed(self, _path: str, _reason: str) -> None:
            pass

    monkeypatch.setattr(main_mod, "ScreenshotWatcher", FakeWatcher)
    monkeypatch.setattr(
        main_mod,
        "_raiderio_reader_for_screenshots_path",
        lambda _path: "new-reader",
    )
    machine = FakeMachine()

    main_mod._replace_screenshots_runtime(
        None,
        tmp_path / "new" / "Screenshots",
        machine,
        FakeWindow(),
        lambda *_args: None,
        signal_gate=main_mod._WatcherSignalGate(),
    )

    assert machine.reader_seen_by_snapshots == ["new-reader"]


def test_screenshot_runtime_keeps_old_reader_for_old_pending_signals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    class FakeSignal:
        def __init__(self) -> None:
            self._callbacks = []

        def connect(self, callback) -> None:
            self._callbacks.append(callback)

        def emit(self, *args) -> None:
            for callback in list(self._callbacks):
                callback(*args)

    class FakeWatcher:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.snapshotReceived = FakeSignal()
            self.decodeFailed = FakeSignal()

        def start(self) -> None:
            if self.path.parent.name == "new":
                old_watcher.snapshotReceived.emit("old-during-new-start")
                self.snapshotReceived.emit("new-during-new-start")

        def stop(self) -> None:
            pass

    class FakeMachine:
        def __init__(self) -> None:
            self._rio_reader = "initial-reader"
            self.reader_seen_by_snapshots: list[tuple[object, object]] = []

        def set_rio_reader(self, reader: object) -> None:
            self._rio_reader = reader

        def apply_snapshot(self, snap: object) -> None:
            self.reader_seen_by_snapshots.append((snap, self._rio_reader))

    class FakeWindow:
        def note_decode(self, _snap: object) -> None:
            pass

        def note_decode_failed(self, _path: str, _reason: str) -> None:
            pass

    created: list[FakeWatcher] = []

    def create_watcher(path: Path) -> FakeWatcher:
        watcher = FakeWatcher(path)
        created.append(watcher)
        return watcher

    def reader_for_path(path: Path) -> str:
        return f"{path.parent.name}-reader"

    monkeypatch.setattr(main_mod, "ScreenshotWatcher", create_watcher)
    monkeypatch.setattr(main_mod, "_raiderio_reader_for_screenshots_path", reader_for_path)
    machine = FakeMachine()
    gate = main_mod._WatcherSignalGate()
    old_watcher = main_mod._replace_screenshots_runtime(
        None,
        tmp_path / "old" / "Screenshots",
        machine,
        FakeWindow(),
        lambda *_args: None,
        signal_gate=gate,
    )

    main_mod._replace_screenshots_runtime(
        old_watcher,
        tmp_path / "new" / "Screenshots",
        machine,
        FakeWindow(),
        lambda *_args: None,
        signal_gate=gate,
    )

    assert machine.reader_seen_by_snapshots == [
        ("old-during-new-start", "old-reader"),
        ("new-during-new-start", "new-reader"),
    ]
    assert machine._rio_reader == "new-reader"


def test_screenshot_runtime_restores_rio_reader_when_watcher_start_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    class FakeSignal:
        def connect(self, _callback) -> None:
            pass

    class FakeWatcher:
        def __init__(self, _path: Path) -> None:
            self.snapshotReceived = FakeSignal()
            self.decodeFailed = FakeSignal()

        def start(self) -> None:
            raise RuntimeError("cannot watch")

    class FakeMachine:
        def __init__(self) -> None:
            self._rio_reader = "old-reader"
            self.reader = "old-reader"

        def set_rio_reader(self, reader: object) -> None:
            self._rio_reader = reader
            self.reader = reader

    monkeypatch.setattr(main_mod, "ScreenshotWatcher", FakeWatcher)
    monkeypatch.setattr(
        main_mod,
        "_raiderio_reader_for_screenshots_path",
        lambda _path: "new-reader",
    )
    machine = FakeMachine()

    with pytest.raises(RuntimeError, match="cannot watch"):
        main_mod._replace_screenshots_runtime(
            None,
            tmp_path / "new" / "Screenshots",
            machine,
            object(),
            lambda *_args: None,
            signal_gate=main_mod._WatcherSignalGate(),
        )

    assert machine.reader == "old-reader"


def test_settings_saved_status_preserves_screenshots_path_warning(tmp_path: Path):
    values = SimpleNamespace(screenshots_path=str(tmp_path / "not-wow"))

    text, is_error = main_mod._settings_saved_status(values, [])

    assert is_error
    assert "Screenshots folder warning" in text


def test_settings_autosave_status_reports_pending_wcl_validation(tmp_path: Path):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    values = SimpleNamespace(screenshots_path=str(root / "Screenshots"))
    cfg = _cfg(tmp_path)
    cfg.draft_wcl_client_id = "draft-client"
    cfg.draft_wcl_client_secret = "draft-secret"

    text, is_error, is_warning = main_mod._settings_autosave_status(values, [], cfg)

    assert not is_error
    assert is_warning
    assert text.startswith("Saved.")
    assert "pending validation" in text
    assert "Test WCL" in text


def test_settings_autosave_status_keeps_plain_saved_without_pending_draft(
    tmp_path: Path,
):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    values = SimpleNamespace(screenshots_path=str(root / "Screenshots"))
    cfg = _cfg(tmp_path)

    text, is_error, is_warning = main_mod._settings_autosave_status(values, [], cfg)

    assert (text, is_error, is_warning) == ("Saved.", False, False)


def test_settings_autosave_status_combines_pending_validation_with_env_override(
    tmp_path: Path,
):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    values = SimpleNamespace(screenshots_path=str(root / "Screenshots"))
    cfg = _cfg(tmp_path)
    cfg.draft_wcl_client_id = "draft-client"

    text, is_error, is_warning = main_mod._settings_autosave_status(
        values,
        ["APSCOUT_DRAFT_WCL_CLIENT_ID"],
        cfg,
    )

    assert is_error
    assert not is_warning
    assert "environment overrides" in text
    assert "APSCOUT_DRAFT_WCL_CLIENT_ID" in text
    assert "pending validation" in text


def test_settings_autosave_status_combines_pending_validation_with_screenshots_warning(
    tmp_path: Path,
):
    values = SimpleNamespace(screenshots_path=str(tmp_path / "not-wow"))
    cfg = _cfg(tmp_path)
    cfg.draft_wcl_client_secret = "draft-secret"

    text, is_error, is_warning = main_mod._settings_autosave_status(values, [], cfg)

    assert is_error
    assert not is_warning
    assert "Screenshots folder warning" in text
    assert "pending validation" in text


def test_settings_wcl_test_success_status_does_not_look_like_plain_autosave(
    tmp_path: Path,
):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    values = SimpleNamespace(screenshots_path=str(root / "Screenshots"))

    text, is_error = main_mod._settings_wcl_test_success_status(values, [])

    assert not is_error
    assert text == "WCL credentials are valid."


def test_settings_wcl_test_success_status_keeps_override_warning(tmp_path: Path):
    root = _retail_root(tmp_path)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    values = SimpleNamespace(screenshots_path=str(root / "Screenshots"))

    text, is_error = main_mod._settings_wcl_test_success_status(
        values,
        ["APSCOUT_WCL_CLIENT_ID"],
    )

    assert is_error
    assert text.startswith("WCL credentials are valid, but ")
    assert "environment overrides" in text
    assert "APSCOUT_WCL_CLIENT_ID" in text


def test_settings_wcl_test_success_status_keeps_screenshots_warning(tmp_path: Path):
    values = SimpleNamespace(screenshots_path=str(tmp_path / "not-wow"))

    text, is_error = main_mod._settings_wcl_test_success_status(values, [])

    assert is_error
    assert text.startswith("WCL credentials are valid.")
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


def test_update_result_has_installable_asset_rejects_blank_metadata():
    class Result:
        asset_name = "ApplicantScoutCompanionSetup-0.2.0.exe"
        asset_url = ""
        checksum_name = "ApplicantScoutCompanionSetup-0.2.0.exe.sha256"
        checksum_url = "   "

    assert not main_mod._update_result_has_installable_asset(Result())


def test_update_result_has_installable_asset_rejects_portable_zip():
    class Result:
        asset_name = "ApplicantScoutCompanion-0.2.0-portable.zip"
        asset_url = "https://example.test/portable.zip"
        checksum_name = "ApplicantScoutCompanion-0.2.0-portable.zip.sha256"
        checksum_url = "https://example.test/portable.zip.sha256"

    assert not main_mod._update_result_has_installable_asset(Result())


def test_update_result_has_installable_asset_rejects_path_separator():
    class Result:
        asset_name = r"ApplicantScoutCompanionSetup-0.2.0.exe\evil.exe"
        asset_url = "https://example.test/setup.exe"
        checksum_name = "ApplicantScoutCompanionSetup-0.2.0.exe.sha256"
        checksum_url = "https://example.test/setup.exe.sha256"

    assert not main_mod._update_result_has_installable_asset(Result())


def test_update_checks_run_hourly_after_initial_startup():
    assert main_mod.UPDATE_CHECK_INITIAL_MS == 1_000
    assert main_mod.UPDATE_CHECK_INTERVAL_MS == 60 * 60 * 1000


def test_update_check_coordinator_rejects_stale_out_of_order_results():
    coordinator = main_mod._UpdateCheckCoordinator()

    slow_old_generation = coordinator.next_generation()
    fast_new_generation = coordinator.next_generation()

    assert not coordinator.is_current(slow_old_generation)
    assert coordinator.is_current(fast_new_generation)


def test_update_check_result_resolver_ignores_stale_available_result():
    coordinator = main_mod._UpdateCheckCoordinator()

    slow_old_generation = coordinator.next_generation()
    fast_new_generation = coordinator.next_generation()
    stale_available = SimpleNamespace(
        status="available",
        latest_version="0.2.0",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        asset_url="https://example.test/setup.exe",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
        checksum_url="https://example.test/setup.exe.sha256",
    )

    stale_decision = main_mod._resolve_update_check_result(
        coordinator,
        slow_old_generation,
        stale_available,
    )
    current_decision = main_mod._resolve_update_check_result(
        coordinator,
        fast_new_generation,
        stale_available,
    )

    assert not stale_decision.is_current
    assert stale_decision.pending_update_version is None
    assert current_decision.is_current
    assert current_decision.pending_update_version == "0.2.0"


def test_wow_start_update_prompt_only_shows_for_initial_wow_launch_update():
    assert main_mod._should_show_wow_start_update_prompt(
        wow_watch_mode=True,
        startup_update_prompt_pending=True,
        pending_update_version="v0.2.2",
    )
    assert not main_mod._should_show_wow_start_update_prompt(
        wow_watch_mode=False,
        startup_update_prompt_pending=True,
        pending_update_version="v0.2.2",
    )
    assert not main_mod._should_show_wow_start_update_prompt(
        wow_watch_mode=True,
        startup_update_prompt_pending=False,
        pending_update_version="v0.2.2",
    )
    assert not main_mod._should_show_wow_start_update_prompt(
        wow_watch_mode=True,
        startup_update_prompt_pending=True,
        pending_update_version=None,
    )


def test_wow_start_update_prompt_message_points_at_titlebar_icon():
    assert main_mod._wow_start_update_prompt_message("v0.2.2") == (
        "Update v0.2.2 is available. Click the blue download icon in the "
        "title bar to install it."
    )


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


def test_safe_update_check_reports_unavailable_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        main_mod,
        "check_for_update",
        lambda _version: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = main_mod._safe_check_for_update("0.1.0")

    assert result.status == "unavailable"
    assert result.current_version == "0.1.0"
    assert "boom" in result.message


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


def test_check_updates_rejects_parallel_installer_runs():
    assert main_mod._UPDATE_INSTALL_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="already in progress"):
            main_mod._check_updates()
    finally:
        main_mod._UPDATE_INSTALL_LOCK.release()


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

    update_result = main_mod._check_updates()

    assert calls == [result, installer]
    assert isinstance(update_result, main_mod.SettingsUpdateResult)
    assert update_result.open_url is None
    assert update_result.installer_handoff is True
    assert "Installing ApplicantScout Companion v0.2.0" in update_result.message


def test_clear_cache_dir_preserves_update_downloads_and_clears_character_cache(
    tmp_path: Path,
):
    cache_dir = tmp_path / "cache"
    updates_dir = cache_dir / "updates"
    updates_dir.mkdir(parents=True)
    installer = updates_dir / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_bytes(b"installer")
    (cache_dir / "token.json").write_text("old-token", encoding="utf-8")
    stale_dir = cache_dir / "stale"
    stale_dir.mkdir()
    (stale_dir / "old.txt").write_text("old", encoding="utf-8")
    character_cache = main_mod.CharacterCache(cache_dir)
    (cache_dir / "character-cache.json").write_text("{}", encoding="utf-8")
    generation = character_cache.generation

    assert main_mod._clear_cache_dir(cache_dir, character_cache) == "Cache cleared."

    assert installer.read_bytes() == b"installer"
    assert not (cache_dir / "token.json").exists()
    assert not (cache_dir / "character-cache.json").exists()
    assert not stale_dir.exists()
    assert character_cache.generation > generation


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

        def collapse_to_launcher(self) -> None:
            self.hide_called = True

        def restore_from_launcher(self) -> None:
            self.show_called = True
            self.raise_called = True
            self.activate_called = True

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


def test_tray_controller_disables_update_action_while_installing(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeSignal:
        def __init__(self) -> None:
            self._callbacks = []

        def connect(self, callback) -> None:
            self._callbacks.append(callback)

        def emit(self) -> None:
            for callback in self._callbacks:
                callback()

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
        def addAction(self, text: str) -> FakeAction:
            return FakeAction(text)

        def addSeparator(self) -> None:
            pass

    class FakeTray:
        class ActivationReason:
            DoubleClick = "double-click"

        MessageIcon = main_mod.QSystemTrayIcon.MessageIcon

        def __init__(self, *_args) -> None:
            self.activated = FakeSignal()
            self.tooltip = ""
            self.messages: list[tuple[object, ...]] = []

        @staticmethod
        def isSystemTrayAvailable() -> bool:
            return True

        def setToolTip(self, value: str) -> None:
            self.tooltip = value

        def setContextMenu(self, _menu) -> None:
            pass

        def show(self) -> None:
            pass

        def showMessage(self, *args) -> None:
            self.messages.append(args)

    class FakeApp:
        def setQuitOnLastWindowClosed(self, _value: bool) -> None:
            pass

    monkeypatch.setattr(main_mod, "QMenu", FakeMenu)
    monkeypatch.setattr(main_mod, "QSystemTrayIcon", FakeTray)
    quit_calls: list[str] = []
    controller = main_mod._create_tray_controller(
        FakeApp(),
        icon=object(),
        window=object(),
        show_settings=lambda: None,
        open_logs=lambda: "",
        run_update=lambda: None,
        quit_app=lambda: quit_calls.append("quit"),
    )

    assert controller is not None
    controller.set_update_available("v0.2.0")
    controller.set_update_in_progress(True)

    assert controller.update_action.text == "Installing update..."
    assert not controller.update_action.enabled
    assert not controller.quit_action.enabled
    assert "update is installing" in controller.tray.tooltip
    controller._request_quit(lambda: quit_calls.append("quit"))
    assert quit_calls == []
    assert controller.tray.messages
    assert main_mod.UPDATE_QUIT_BLOCKED_MESSAGE in controller.tray.messages[-1]

    controller.set_update_available("v0.2.1")

    assert controller.update_action.text == "Installing update..."
    assert not controller.update_action.enabled
    assert not controller.quit_action.enabled
    assert "update is installing" in controller.tray.tooltip

    controller.set_update_in_progress(False)

    assert controller.update_action.text == "Update to v0.2.1"
    assert controller.update_action.enabled
    assert controller.quit_action.enabled
    controller.quit_action.trigger()
    assert quit_calls == ["quit"]


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
    assert app.quit_on_last_window_closed


def test_tray_open_logs_failure_surfaces_notification_without_raising(
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
            self.triggered = FakeSignal()
            self.enabled = True

        def trigger(self) -> None:
            self.triggered.emit()

        def setEnabled(self, value: bool) -> None:
            self.enabled = value

        def setText(self, value: str) -> None:
            self.text = value

    class FakeMenu:
        def addAction(self, text: str) -> FakeAction:
            return FakeAction(text)

        def addSeparator(self) -> None:
            pass

    class FakeTray:
        class ActivationReason:
            DoubleClick = "double-click"

        MessageIcon = main_mod.QSystemTrayIcon.MessageIcon

        def __init__(self, *_args) -> None:
            self.activated = FakeSignal()
            self.messages: list[tuple[str, str, object, int]] = []

        @staticmethod
        def isSystemTrayAvailable() -> bool:
            return True

        def setToolTip(self, _value: str) -> None:
            pass

        def setContextMenu(self, _menu) -> None:
            pass

        def show(self) -> None:
            pass

        def showMessage(self, title: str, body: str, icon, timeout_ms: int) -> None:
            self.messages.append((title, body, icon, timeout_ms))

    class FakeApp:
        def setQuitOnLastWindowClosed(self, _value: bool) -> None:
            pass

    monkeypatch.setattr(main_mod, "QMenu", FakeMenu)
    monkeypatch.setattr(main_mod, "QSystemTrayIcon", FakeTray)

    controller = main_mod._create_tray_controller(
        FakeApp(),
        icon=object(),
        window=object(),
        show_settings=lambda: None,
        open_logs=lambda: (_ for _ in ()).throw(RuntimeError("folder missing")),
        run_update=lambda: None,
        quit_app=lambda: None,
    )

    assert controller is not None
    controller.tray.messages.clear()

    controller.open_logs_action.trigger()

    assert controller.tray.messages == [
        (
            "ApplicantScout logs",
            "Could not open logs: folder missing",
            FakeTray.MessageIcon.Warning,
            7000,
        )
    ]


def test_main_update_flushes_pending_settings_before_starting_worker():
    calls: list[str] = []

    class FakeSettingsDialog:
        def flush_pending_values(self) -> bool:
            calls.append("flush")
            return True

    assert main_mod._flush_settings_before_update(FakeSettingsDialog()) is True
    assert calls == ["flush"]


def test_main_update_flush_failure_blocks_worker_start():
    calls: list[str] = []

    class FakeSettingsDialog:
        def flush_pending_values(self) -> bool:
            calls.append("flush")
            return False

    assert main_mod._flush_settings_before_update(FakeSettingsDialog()) is False
    assert calls == ["flush"]


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
