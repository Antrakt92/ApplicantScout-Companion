"""First-run configuration failures must not mutate the Windows startup shortcut."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import applicant_scout.__main__ as main_mod
from applicant_scout.config import Config, _read_env_file


@pytest.fixture
def first_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg = Config(
        wcl_client_id="example-client", wcl_client_secret="example-secret",
        chatlog_path=tmp_path / "Logs/WoWChatLog.txt", region="EU",
        cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
        config_path=tmp_path / "config/config.env", sync_with_wow=True,
    )
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id, wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region, screenshots_path=str(tmp_path / "Screenshots"),
        metric_preferences=cfg.metric_preferences, sync_with_wow=True,
    )
    shortcut = tmp_path / "startup/ApplicantScout.lnk"
    shortcut.parent.mkdir()
    calls: list[str] = []
    warnings: list[str] = []

    class Dialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def setWindowIcon(self, _icon):
            pass

        def exec(self):
            return main_mod.QDialog.DialogCode.Accepted

        def values(self):
            return values

    def configure(enabled):
        calls.append(f"shortcut:{enabled}")
        if enabled:
            shortcut.write_bytes(b"new shortcut")
        else:
            shortcut.unlink(missing_ok=True)

    # Every path and lifecycle action is isolated before the production entry point.
    monkeypatch.setattr(main_mod, "SettingsDialog", Dialog)
    monkeypatch.setattr(main_mod, "_app_icon", lambda: None)
    monkeypatch.setattr(main_mod, "configure_wow_sync_startup", configure)
    monkeypatch.setattr(main_mod, "start_wow_sync_watcher", lambda **_kwargs: calls.append("watcher"))
    monkeypatch.setattr(main_mod, "_stop_current_session_watcher_best_effort", lambda: calls.append("stop"))
    monkeypatch.setattr(main_mod.QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text))
    for key in main_mod._settings_env_override_keys():
        monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(cfg=cfg, values=values, shortcut=shortcut, calls=calls, warnings=warnings)


@pytest.mark.parametrize("old_shortcut", [None, b"\x00original shortcut\xff"])
@pytest.mark.parametrize("desired_sync", [True, False])
def test_first_run_failed_save_preserves_exact_prior_shortcut(
    first_run, monkeypatch: pytest.MonkeyPatch, old_shortcut: bytes | None, desired_sync: bool
):
    first_run.values.sync_with_wow = desired_sync
    if old_shortcut is not None:
        first_run.shortcut.write_bytes(old_shortcut)

    def fail_save(*_args, **_kwargs):
        raise OSError("config write denied")

    monkeypatch.setattr(main_mod, "_persist_settings_values", fail_save)
    assert main_mod._run_first_run_settings(first_run.cfg) is False
    assert first_run.calls == []
    assert (first_run.shortcut.read_bytes() if first_run.shortcut.exists() else None) == old_shortcut
    assert "config write denied" in first_run.warnings[0]
    assert not first_run.cfg.config_path.exists()


@pytest.mark.parametrize("original", [None, b'# exact previous bytes\r\nAPSCOUT_SYNC_WITH_WOW="0"\r\n'])
def test_enable_shortcut_failure_restores_exact_config(first_run, monkeypatch, original):
    path = first_run.cfg.config_path
    if original is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(original)
    first_run.shortcut.write_bytes(b"old shortcut")

    def fail_shortcut(_enabled):
        assert _read_env_file(path)["APSCOUT_SYNC_WITH_WOW"] == "1"
        raise RuntimeError("shortcut denied")

    monkeypatch.setattr(main_mod, "configure_wow_sync_startup", fail_shortcut)
    assert main_mod._run_first_run_settings(first_run.cfg) is False
    assert (path.read_bytes() if path.exists() else None) == original
    assert first_run.shortcut.read_bytes() == b"old shortcut"
    assert first_run.calls == []
    assert "shortcut denied" in first_run.warnings[0]


def test_disable_cleanup_failure_is_saved_before_success_message(first_run, monkeypatch):
    first_run.values.sync_with_wow = False
    first_run.shortcut.write_bytes(b"old shortcut")

    def fail_shortcut(_enabled):
        assert _read_env_file(first_run.cfg.config_path)["APSCOUT_SYNC_WITH_WOW"] == "0"
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(main_mod, "configure_wow_sync_startup", fail_shortcut)
    assert main_mod._run_first_run_settings(first_run.cfg) is True
    assert first_run.calls == ["stop"]
    assert "Settings were saved" in first_run.warnings[0]
    assert "cleanup denied" in first_run.warnings[0]


def test_disable_save_and_cleanup_failure_does_not_claim_saved(first_run, monkeypatch):
    first_run.values.sync_with_wow = False

    def fail_shortcut(_enabled):
        raise RuntimeError("cleanup denied")

    def fail_save(*_args, **_kwargs):
        raise OSError("config write denied")

    monkeypatch.setattr(main_mod, "configure_wow_sync_startup", fail_shortcut)
    monkeypatch.setattr(main_mod, "_persist_settings_values", fail_save)
    assert main_mod._run_first_run_settings(first_run.cfg) is False
    assert first_run.calls == []
    assert not any("Settings were saved" in text for text in first_run.warnings)


def test_enable_failure_reports_config_rollback_failure(first_run, monkeypatch):
    def fail_shortcut(_enabled):
        raise RuntimeError("shortcut denied")

    def fail_restore(_snapshot):
        raise OSError("rollback denied")

    monkeypatch.setattr(main_mod, "configure_wow_sync_startup", fail_shortcut)
    monkeypatch.setattr(main_mod, "_restore_persisted_config_snapshot", fail_restore)
    assert main_mod._run_first_run_settings(first_run.cfg) is False
    assert first_run.calls == []
    assert "shortcut denied" in first_run.warnings[0]
    assert "rollback denied" in first_run.warnings[0]


def test_failed_post_write_save_restores_config_without_touching_shortcut(first_run, monkeypatch):
    path = first_run.cfg.config_path
    path.parent.mkdir(parents=True)
    old = b"# old config\r\n"
    path.write_bytes(old)

    def fail_after_write(*_args, **_kwargs):
        path.write_bytes(b"partially completed save")
        raise OSError("post-write error")

    monkeypatch.setattr(main_mod, "_persist_settings_values", fail_after_write)
    assert main_mod._run_first_run_settings(first_run.cfg) is False
    assert path.read_bytes() == old
    assert first_run.calls == []
    assert not first_run.shortcut.exists()


def test_env_only_sync_override_preserves_new_default_when_saving(first_run, monkeypatch):
    monkeypatch.setenv("APSCOUT_SYNC_WITH_WOW", "0")
    first_run.cfg.sync_with_wow = first_run.values.sync_with_wow = False
    main_mod._persist_settings_values(first_run.cfg, first_run.values)
    assert _read_env_file(first_run.cfg.config_path)["APSCOUT_SYNC_WITH_WOW"] == "1"
