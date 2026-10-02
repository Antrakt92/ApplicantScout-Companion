"""Execute production dialog callbacks independently of a live tray/application."""
from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

import applicant_scout.__main__ as main_mod
from applicant_scout.config import Config, save_config_values


class DeletedDialog:
    def __getattr__(self, name):
        raise AssertionError(f"Inactive dialog was accessed: {name}")


def _callbacks(cfg, *, active=None, commit=None):
    """Compile unchanged callback bodies inside their required lexical scope."""
    tree = ast.parse(Path(main_mod.__file__).read_text(encoding="utf-8"))
    names = {
        "_forget_dialog", "_request_startup_shortcut", "_mark_apply_busy",
        "_commit_apply_outcome",
    }
    callbacks = {
        node.name: copy.deepcopy(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    template = ast.parse("""
def build(cfg, dialog, settings_dialog):
    auth = object()
    watcher = object()
    current_screenshots_dir = cfg.screenshots_path
    wow_exit_timer = None
    def state():
        return cfg, auth, watcher, current_screenshots_dir, wow_exit_timer, settings_dialog
    return (_commit_apply_outcome, _forget_dialog, _mark_apply_busy, state)
""")
    factory = template.body[0]
    factory.body[-1:-1] = [callbacks[name] for name in sorted(names)]
    namespace = dict(vars(main_mod))
    shortcuts = []
    namespace.update(
        app=object(), wcl_client=SimpleNamespace(region=cfg.region),
        region_runtime=main_mod._WCLRegionRuntime(cfg.region),
        window=SimpleNamespace(
            apply_metric_preferences=lambda *_a, **_k: None,
            bump_wcl_runtime_generation=lambda: None,
        ),
        machine=object(), _log_decode_failed=lambda *_a: None,
        watcher_signal_gate=main_mod._WatcherSignalGate(),
        _request_quit_application=lambda: None, _can_quit_application=lambda: True,
        _prepare_quit_application=lambda: None, live_snapshot_writer=None,
        wow_sync_startup_configurator=SimpleNamespace(
            request=lambda enabled, **kwargs: shortcuts.append((enabled, kwargs)),
        ),
    )
    if commit is not None:
        namespace["_commit_settings_apply"] = commit
    ast.fix_missing_locations(template)
    exec(compile(template, "<settings-dialog-production-callbacks>", "exec"), namespace)
    return (*namespace["build"](cfg, DeletedDialog(), active), shortcuts)


def _config(tmp_path):
    return Config(
        wcl_client_id="fixture-client", wcl_client_secret="fixture-secret",
        chatlog_path=tmp_path / "Logs" / "WoWChatLog.txt", region="EU",
        cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
        config_path=tmp_path / "config" / "config.env",
        screenshots_path=tmp_path / "Screenshots", sync_with_wow=False,
    )


@pytest.mark.parametrize("active", [None, "replacement-dialog"])
@pytest.mark.parametrize("sync_changed", [False, True])
def test_completed_prepare_promotes_runtime_without_inactive_dialog_ui(
    tmp_path, active, sync_changed,
):
    cfg = _config(tmp_path)
    new_cfg = main_mod.replace(cfg, region="US", sync_with_wow=sync_changed)
    new_auth, new_watcher, new_timer = object(), object(), object()
    committed = []

    def commit(ctx):
        committed.append(ctx)
        return SimpleNamespace(
            cfg=new_cfg, auth=new_auth, watcher=new_watcher,
            current_screenshots_dir=new_cfg.screenshots_path,
            wow_exit_timer=new_timer, overrides=[],
        )

    ready, forget, busy, state, shortcuts = _callbacks(
        cfg, active=active, commit=commit,
    )
    prepared = SimpleNamespace(new_cfg=new_cfg)
    ready(main_mod._SettingsApplyOutcome(1, True, prepared, object(), False, None))
    assert len(committed) == 1
    assert state() == (
        new_cfg, new_auth, new_watcher, new_cfg.screenshots_path, new_timer, active,
    )
    busy()
    forget()
    assert state()[-1] == active
    assert [enabled for enabled, _kwargs in shortcuts] == ([True] if sync_changed else [])


def test_inactive_dialog_commit_failure_restores_persisted_baseline(
    monkeypatch, tmp_path,
):
    for key in main_mod._settings_env_override_keys():
        monkeypatch.delenv(key, raising=False)
    cfg = _config(tmp_path)
    save_config_values(
        wcl_client_id=cfg.wcl_client_id, wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region, screenshots_path=str(cfg.screenshots_path),
        config_path=cfg.config_path,
    )
    baseline = cfg.config_path.read_bytes()
    values = SimpleNamespace(
        wcl_client_id=cfg.wcl_client_id, wcl_client_secret=cfg.wcl_client_secret,
        region="US", screenshots_path=str(tmp_path / "new"),
        metric_preferences=cfg.metric_preferences, sync_with_wow=False,
    )
    prepared = main_mod._prepare_settings_apply(
        cfg=cfg, values=values, apply_credentials=False,
    )
    assert cfg.config_path.read_bytes() != baseline

    def fail_watch(*_args, **_kwargs):
        raise RuntimeError("watcher startup failed")

    monkeypatch.setattr(main_mod, "_replace_screenshots_runtime", fail_watch)
    ready, _forget, _busy, state, shortcuts = _callbacks(cfg)
    ready(main_mod._SettingsApplyOutcome(1, True, prepared, values, False, None))
    assert state()[0] is cfg
    assert cfg.config_path.read_bytes() == baseline
    assert shortcuts == []


def test_inactive_dialog_prepare_failure_never_enters_runtime_commit(tmp_path):
    def forbidden_commit(_ctx):
        pytest.fail("failed preparation was committed")

    cfg = _config(tmp_path)
    ready, _forget, _busy, state, shortcuts = _callbacks(
        cfg, commit=forbidden_commit,
    )
    ready(main_mod._SettingsApplyOutcome(
        1, False, None, object(), False, OSError("disk save failed"),
    ))
    assert state()[0] is cfg
    assert shortcuts == []
