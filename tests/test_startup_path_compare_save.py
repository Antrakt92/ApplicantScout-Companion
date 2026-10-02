"""Moved-install discovery cannot replace a newer explicit settings binding."""
from __future__ import annotations

from io import StringIO
import threading

import pytest
import shiboken6

import applicant_scout.__main__ as main_mod
from applicant_scout import atomic_io, config


@pytest.fixture
def paths(monkeypatch, tmp_path):
    monkeypatch.delenv("APSCOUT_SCREENSHOTS_PATH", raising=False)
    monkeypatch.setattr(atomic_io, "_is_windows", lambda: False)
    old = tmp_path / "old" / "World of Warcraft" / "_retail_" / "Screenshots"
    discovered = tmp_path / "found" / "World of Warcraft" / "_retail_" / "Screenshots"
    chosen = tmp_path / "chosen" / "World of Warcraft" / "_ptr_" / "Screenshots"
    cfg = config.Config(
        wcl_client_id="fixture-id", wcl_client_secret="fixture-secret",
        chatlog_path=old.parent / "Logs" / "WoWChatLog.txt", region="EU",
        cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
        config_path=tmp_path / "config" / "config.env", screenshots_path=old,
    )
    _save(cfg, str(old))
    monkeypatch.setattr(
        main_mod, "run_bounded_screenshots_path_probe",
        lambda path: None if path == discovered else "folder does not exist",
    )
    return cfg, old, discovered, chosen


def _save(cfg, binding, *, region="EU"):
    config.save_config_values(
        wcl_client_id=cfg.wcl_client_id, wcl_client_secret=cfg.wcl_client_secret,
        region=region, screenshots_path=binding, config_path=cfg.config_path,
    )


def _load_fixture_config(monkeypatch, cfg):
    monkeypatch.setattr(config, "user_config_path", lambda: cfg.config_path)
    monkeypatch.setattr(
        config, "_prepare_user_storage",
        lambda: (cfg.config_dir, cfg.cache_dir, cfg.config_dir / "logs", cfg.config_path),
    )
    return config.load_config()


def test_loaded_generation_rejects_new_choice_then_original_path_before_worker(monkeypatch, paths):
    cfg, old, discovered, chosen = paths
    loaded = _load_fixture_config(monkeypatch, cfg)
    _save(cfg, str(chosen))
    _save(cfg, str(old))
    before = cfg.config_path.read_bytes()
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: discovered)
    outcome = main_mod._verify_startup_screenshots_dir(loaded, old)
    assert outcome.kind == "ok"
    assert cfg.config_path.read_bytes() == before


def test_loaded_source_rejects_deleted_config_before_worker_with_matching_legacy(monkeypatch, paths, tmp_path):
    cfg, old, discovered, _chosen = paths
    loaded = _load_fixture_config(monkeypatch, cfg)
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(cfg.config_path.read_bytes())
    monkeypatch.setattr(config, "_legacy_env_path", lambda: legacy)
    cfg.config_path.unlink()
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: discovered)
    outcome = main_mod._verify_startup_screenshots_dir(loaded, old)
    assert outcome.kind == "ok"
    assert not cfg.config_path.exists()


@pytest.mark.parametrize("binding_kind", ["path", "empty", "removed"])
def test_newer_choice_during_discovery_is_preserved(monkeypatch, paths, binding_kind):
    cfg, old, discovered, chosen = paths
    binding = {"path": str(chosen), "empty": "", "removed": None}[binding_kind]
    saved = []

    def discover(_path):
        _save(cfg, binding, region="KR")
        saved.append(cfg.config_path.read_bytes())
        return discovered

    monkeypatch.setattr(main_mod, "run_bounded_discovery", discover)
    outcome = main_mod._verify_startup_screenshots_dir(cfg, old)
    assert outcome.kind == "ok"
    assert outcome.cfg is cfg
    assert cfg.config_path.read_bytes() == saved[0]


def test_newer_binding_before_worker_starts_is_preserved(monkeypatch, paths):
    cfg, old, discovered, chosen = paths
    _save(cfg, str(chosen))
    before = cfg.config_path.read_bytes()
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: discovered)
    outcome = main_mod._verify_startup_screenshots_dir(cfg, old)
    assert outcome.kind == "ok"
    assert cfg.config_path.read_bytes() == before


def test_environment_override_introduced_during_discovery_suppresses_repair(monkeypatch, paths):
    cfg, old, discovered, chosen = paths
    before = cfg.config_path.read_bytes()

    def discover(_path):
        monkeypatch.setenv("APSCOUT_SCREENSHOTS_PATH", str(chosen))
        return discovered

    monkeypatch.setattr(main_mod, "run_bounded_discovery", discover)
    outcome = main_mod._verify_startup_screenshots_dir(cfg, old)
    assert outcome.kind == "ok"
    assert cfg.config_path.read_bytes() == before


def test_unchanged_binding_repairs_and_preserves_unmanaged_bytes(monkeypatch, paths):
    cfg, old, discovered, _chosen = paths
    unmanaged = b'# owner comment\r\nCUSTOM="line one\r\nline two"\r\nUNTOUCHED = " exact "\r\n'
    with cfg.config_path.open("ab") as output:
        output.write(unmanaged)
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: discovered)
    outcome = main_mod._verify_startup_screenshots_dir(cfg, old)
    assert outcome.kind == "repaired"
    assert outcome.cfg.screenshots_path == discovered
    assert config.read_user_config_values(cfg.config_path)["APSCOUT_SCREENSHOTS_PATH"] == str(discovered)
    assert cfg.config_path.read_bytes().endswith(unmanaged)


def test_guarded_legacy_binding_repairs_into_current_config(monkeypatch, paths, tmp_path):
    cfg, old, discovered, _chosen = paths
    legacy = tmp_path / "legacy.env"
    original = cfg.config_path.read_bytes()
    legacy.write_bytes(original)
    cfg.config_path.unlink()
    monkeypatch.setattr(config, "_legacy_env_path", lambda: legacy)
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: discovered)
    outcome = main_mod._verify_startup_screenshots_dir(cfg, old)
    assert outcome.kind == "repaired"
    assert config.read_user_config_values(cfg.config_path)["APSCOUT_SCREENSHOTS_PATH"] == str(discovered)
    assert legacy.read_bytes() == original


def test_completed_repair_does_not_surface_after_newer_explicit_save(monkeypatch, paths):
    cfg, old, discovered, chosen = paths
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: discovered)
    displayed = []
    monkeypatch.setattr(
        main_mod.QMessageBox, "information", lambda *args: displayed.append(args),
    )
    run = main_mod._StartupScreenshotsVerificationRun(cfg, old)
    run._run()
    assert config.read_user_config_values(cfg.config_path)["APSCOUT_SCREENSHOTS_PATH"] == str(discovered)
    _save(cfg, str(chosen))
    run.surface()
    assert displayed == []


def test_current_config_removed_during_discovery_is_not_restored_from_legacy(monkeypatch, paths, tmp_path):
    cfg, old, discovered, _chosen = paths
    legacy = tmp_path / "legacy.env"
    legacy.write_bytes(cfg.config_path.read_bytes())
    monkeypatch.setattr(config, "_legacy_env_path", lambda: legacy)

    def discover(_path):
        cfg.config_path.unlink()
        return discovered

    monkeypatch.setattr(main_mod, "run_bounded_discovery", discover)
    outcome = main_mod._verify_startup_screenshots_dir(cfg, old)
    assert outcome.kind == "ok"
    assert not cfg.config_path.exists()


@pytest.mark.parametrize("last_binding_is_original", [False, True])
def test_discovery_uses_last_duplicate_binding_like_config_load(monkeypatch, paths, last_binding_is_original):
    cfg, old, discovered, chosen = paths
    first, last = (chosen, old) if last_binding_is_original else (old, chosen)
    cfg.config_path.write_text(
        config._env_line("APSCOUT_SCREENSHOTS_PATH", str(first))
        + config._env_line("APSCOUT_SCREENSHOTS_PATH", str(last)),
        encoding="utf-8",
    )
    before = cfg.config_path.read_bytes()
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: discovered)
    outcome = main_mod._verify_startup_screenshots_dir(cfg, old)
    if last_binding_is_original:
        assert outcome.kind == "repaired"
        assert config.read_user_config_values(cfg.config_path)["APSCOUT_SCREENSHOTS_PATH"] == str(discovered)
    else:
        assert outcome.kind == "ok"
        assert cfg.config_path.read_bytes() == before


def test_malformed_saved_binding_is_preserved_without_discovery_write(monkeypatch, paths):
    cfg, old, discovered, _chosen = paths
    with cfg.config_path.open("a", encoding="utf-8") as output:
        output.write("APSCOUT_SCREENSHOTS_PATH\n")
    before = cfg.config_path.read_bytes()
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: discovered)
    with pytest.raises(config.ConfigError, match="invalid line"):
        main_mod._verify_startup_screenshots_dir(cfg, old)
    assert cfg.config_path.read_bytes() == before


def test_cancel_before_worker_skips_all_probes(monkeypatch, paths):
    cfg, old, _discovered, _chosen = paths
    probes = []
    monkeypatch.setattr(main_mod, "run_bounded_screenshots_path_probe", lambda path: probes.append(path))
    run = main_mod._StartupScreenshotsVerificationRun(cfg, old)
    before = cfg.config_path.read_bytes()
    run.cancel()
    run._run()
    assert probes == []
    assert cfg.config_path.read_bytes() == before
    assert not run.surface()


def test_cancel_during_discovery_preserves_persisted_path(monkeypatch, paths):
    cfg, old, discovered, _chosen = paths
    run = main_mod._StartupScreenshotsVerificationRun(cfg, old)
    before = cfg.config_path.read_bytes()

    def discover(_path):
        run.cancel()
        return discovered

    monkeypatch.setattr(main_mod, "run_bounded_discovery", discover)
    run._run()
    assert cfg.config_path.read_bytes() == before
    assert not run.surface()


def test_queued_warning_after_cancel_does_not_show_dialog(monkeypatch, paths):
    cfg, old, _discovered, _chosen = paths
    displayed = []
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: None)
    monkeypatch.setattr(main_mod.QMessageBox, "warning", lambda *args: displayed.append(args))
    run = main_mod._StartupScreenshotsVerificationRun(cfg, old)
    run._run()
    run.cancel()
    run._on_verified(None)
    assert not run.surface()
    assert displayed == []


def test_cancel_during_discovery_does_not_emit_to_destroyed_qt_signals(monkeypatch, paths):
    cfg, old, discovered, _chosen = paths
    signals = main_mod._StartupVerificationSignals()
    run = main_mod._StartupScreenshotsVerificationRun(cfg, old)
    run._signals = signals

    def discover(_path):
        run.cancel()
        shiboken6.delete(signals)
        return discovered

    monkeypatch.setattr(main_mod, "run_bounded_discovery", discover)
    run._run()
    assert not run.surface()


def test_discovery_compare_write_serializes_with_ordinary_settings_save(monkeypatch, paths):
    cfg, old, discovered, chosen = paths
    monkeypatch.setattr(main_mod, "run_bounded_discovery", lambda _path: discovered)
    inspected = threading.Event()
    release_discovery = threading.Event()
    explicit_finished = threading.Event()
    failures = []
    real_write = config.atomic_write_text

    def paused_write(path, text, **kwargs):
        bindings = {item.key: item.value for item in config.parse_stream(StringIO(text)) if item.key}
        if bindings.get("APSCOUT_SCREENSHOTS_PATH") == str(discovered):
            inspected.set()
            assert release_discovery.wait(timeout=2)
        real_write(path, text, **kwargs)

    monkeypatch.setattr(config, "atomic_write_text", paused_write)

    def run_discovery():
        try:
            main_mod._verify_startup_screenshots_dir(cfg, old)
        except Exception as exc:
            failures.append(exc)

    def save_explicit():
        try:
            _save(cfg, str(chosen), region="KR")
        except Exception as exc:
            failures.append(exc)
        finally:
            explicit_finished.set()

    discovery_worker = threading.Thread(target=run_discovery, daemon=True)
    explicit_worker = threading.Thread(target=save_explicit, daemon=True)
    discovery_worker.start()
    try:
        assert inspected.wait(timeout=1)
        explicit_worker.start()
        assert not explicit_finished.wait(timeout=0.1)
    finally:
        release_discovery.set()
        discovery_worker.join(timeout=2)
        if explicit_worker.ident is not None:
            explicit_worker.join(timeout=2)
        assert not discovery_worker.is_alive()
        assert not explicit_worker.is_alive()
    assert failures == []
    saved = config.read_user_config_values(cfg.config_path)
    assert saved["APSCOUT_SCREENSHOTS_PATH"] == str(chosen)
    assert saved["APSCOUT_REGION"] == "KR"
