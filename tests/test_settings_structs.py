"""P8 regression tests: SettingsApplyCtx / ControlServerOpts / ConfigValues.

Proves the struct paths behave identically to the legacy long-parameter
calls without renaming public APIs. Existing caller coverage stays intact;
these tests target only the new dataclass spellings.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import applicant_scout.__main__ as main_mod
from applicant_scout import runtime_control
from applicant_scout.config import Config, ConfigValues, save_config_values
from applicant_scout.metric_preferences import MetricPreferences


# ─── ConfigValues ─────────────────────────────────────────────────────────


def _config_kwargs(config_path: Path) -> dict:
    return {
        "wcl_client_id": "client-id",
        "wcl_client_secret": "client-secret",
        "region": "EU",
        "draft_wcl_client_id": "draft-id",
        "draft_wcl_client_secret": "draft-secret",
        "screenshots_path": r"C:\WoW\Screenshots",
        "cache_ttl_seconds": 3600,
        "metric_preferences": MetricPreferences(
            mplus=True,
            raid_normal=False,
            raid_heroic=True,
            raid_mythic=False,
        ),
        "sync_with_wow": False,
        "chatlog_path": r"C:\WoW\Logs\WoWChatLog.txt",
        "config_path": config_path,
    }


def test_config_values_struct_matches_kwargs(tmp_path: Path):
    kwargs_path = tmp_path / "kwargs.env"
    struct_path = tmp_path / "struct.env"
    kwargs = _config_kwargs(kwargs_path)
    save_config_values(**kwargs)
    struct_kwargs = dict(kwargs)
    struct_kwargs["config_path"] = struct_path
    save_config_values(ConfigValues(**struct_kwargs))
    assert struct_path.read_bytes() == kwargs_path.read_bytes()


def test_config_values_struct_wins_over_kwargs(tmp_path: Path):
    target = tmp_path / "config.env"
    save_config_values(
        ConfigValues(
            wcl_client_id="client-id",
            wcl_client_secret="client-secret",
            region="EU",
            config_path=target,
        ),
        region="US",
        wcl_client_id="other",
        wcl_client_secret="other",
    )
    text = target.read_text(encoding="utf-8")
    assert 'APSCOUT_REGION="EU"' in text
    assert '"US"' not in text


def test_save_config_values_requires_identity_without_struct():
    with pytest.raises(TypeError):
        save_config_values()
    with pytest.raises(TypeError):
        save_config_values(wcl_client_id="only-id")


# ─── ControlServerOpts ────────────────────────────────────────────────────


class _FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _FakeControlServer:
    def __init__(self, app) -> None:
        self.app = app
        self.listened: list[str] = []
        self.newConnection = _FakeSignal()

    def listen(self, name: str) -> bool:
        self.listened.append(name)
        return True

    def errorString(self) -> str:
        return ""


class _FakeServerFactory:
    def __init__(self, created: list) -> None:
        self._created = created

    def __call__(self, app):
        server = _FakeControlServer(app)
        self._created.append(server)
        return server

    @staticmethod
    def removeServer(_name: str) -> bool:
        return True


class _FakeOwner:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _drain_recorder(calls: list):
    def _drain(server, quit_app, show_settings, **kwargs) -> None:
        calls.append(
            {
                "server": server,
                "quit_app": quit_app,
                "show_settings": show_settings,
                **kwargs,
            }
        )

    return _drain


def test_control_server_opts_path_wires_commands():
    created: list[_FakeControlServer] = []
    calls: list[dict] = []
    quit_app = lambda: None  # noqa: E731
    show_settings = lambda: None  # noqa: E731
    opts = runtime_control.ControlServerOpts(
        app=object(),
        quit_app=quit_app,
        show_settings=show_settings,
        acquire_owner=_FakeOwner,
        local_server_type=_FakeServerFactory(created),
        server_name="test-server",
        drain_connections=_drain_recorder(calls),
    )
    server = runtime_control.create_control_server(opts=opts)
    assert created == [server]
    assert server.listened == ["test-server"]
    assert len(server.newConnection.callbacks) == 1
    server.newConnection.callbacks[0]()
    assert len(calls) == 1
    assert calls[0]["quit_app"] is quit_app
    assert calls[0]["show_settings"] is show_settings


def test_control_server_legacy_kwargs_path_still_works():
    created: list[_FakeControlServer] = []
    calls: list[dict] = []
    quit_app = lambda: None  # noqa: E731
    show_settings = lambda: None  # noqa: E731
    server = runtime_control.create_control_server(
        object(),
        quit_app=quit_app,
        show_settings=show_settings,
        acquire_owner=_FakeOwner,
        local_server_type=_FakeServerFactory(created),
        server_name="legacy-server",
        drain_connections=_drain_recorder(calls),
    )
    assert server.listened == ["legacy-server"]
    server.newConnection.callbacks[0]()
    assert calls[0]["quit_app"] is quit_app
    assert calls[0]["server"] is server


def test_control_server_requires_identity_without_opts():
    with pytest.raises(TypeError):
        runtime_control.create_control_server()


def test_control_server_opts_defaults():
    opts = runtime_control.ControlServerOpts(
        app=object(),
        quit_app=lambda: None,
        show_settings=lambda: None,
    )
    assert opts.server_name == runtime_control.CONTROL_SERVER_NAME
    assert opts.drain_connections is None
    assert opts.can_quit is None


# ─── SettingsApplyCtx ─────────────────────────────────────────────────────


def _commit_cfg(tmp_path: Path) -> tuple[Config, Path]:
    screenshots = tmp_path / "Screenshots"
    screenshots.mkdir()
    cfg = Config(
        wcl_client_id="client",
        wcl_client_secret="secret",
        chatlog_path=tmp_path / "WoWChatLog.txt",
        region="EU",
        cache_dir=tmp_path / "cache",
        config_dir=tmp_path / "config",
        screenshots_path=screenshots,
        config_path=tmp_path / "config.env",
    )
    return cfg, screenshots


def test_settings_apply_ctx_defaults():
    ctx = main_mod.SettingsApplyCtx(
        app=object(),
        prepared=None,  # type: ignore[arg-type]
        values=None,
        auth=None,
        wcl_client=None,
        region_runtime=None,  # type: ignore[arg-type]
        window=None,
        watcher=None,
        current_screenshots_dir=Path("."),
        machine=None,
        decode_failed_callback=lambda _a, _b: None,
        signal_gate=None,  # type: ignore[arg-type]
        wow_exit_timer=None,
        quit_app=lambda: None,
        can_quit=lambda: True,
    )
    assert ctx.prepare_quit is None
    assert ctx.live_snapshot_cache_writer is None


def test_commit_settings_apply_accepts_ctx(tmp_path: Path):
    cfg, screenshots = _commit_cfg(tmp_path)
    auth = object()
    watcher = object()
    prepared = main_mod._PreparedSettingsApply(
        old_cfg=cfg,
        new_cfg=cfg,
        new_screenshots_dir=screenshots,
        persisted_snapshot=main_mod._PersistedConfigSnapshot(
            path=cfg.config_path, contents=None
        ),
        apply_credentials=False,
    )
    window_calls: list[tuple] = []

    def _apply_prefs(prefs, **kwargs) -> None:
        window_calls.append((prefs, kwargs))

    ctx = main_mod.SettingsApplyCtx(
        app=object(),
        prepared=prepared,
        values=SimpleNamespace(
            wcl_client_id="client", wcl_client_secret="secret"
        ),
        auth=auth,
        wcl_client=SimpleNamespace(
            region="EU",
            reconfigure_auth=lambda _auth, **_kwargs: None,
            mark_active_auth_validated=lambda: None,
        ),
        region_runtime=main_mod._WCLRegionRuntime("EU"),
        window=SimpleNamespace(
            apply_metric_preferences=_apply_prefs,
            bump_wcl_runtime_generation=lambda: None,
        ),
        watcher=watcher,
        current_screenshots_dir=screenshots,
        machine=object(),
        decode_failed_callback=lambda _a, _b: None,
        signal_gate=main_mod._WatcherSignalGate(),
        wow_exit_timer=None,
        quit_app=lambda: None,
        can_quit=lambda: True,
    )
    result = main_mod._commit_settings_apply(ctx)
    assert result.cfg is cfg
    assert result.auth is auth
    assert result.watcher is watcher
    assert result.current_screenshots_dir == screenshots
    assert result.wow_exit_timer is None
    assert isinstance(result.overrides, list)
    assert len(window_calls) == 1


def test_save_config_values_rejects_string_values_positional_trap():
    # A legacy 3-positional-scalar call must fail loudly with TypeError
    # instead of misbinding the first scalar to `values`.
    with pytest.raises(TypeError, match="ConfigValues"):
        save_config_values("client-id")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ConfigValues"):
        save_config_values(values="client-id")  # type: ignore[arg-type]
