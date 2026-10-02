"""Coalesced settings preserve the last committed runtime/disk transaction."""
from types import SimpleNamespace

import pytest

import applicant_scout.__main__ as main_mod
from applicant_scout.config import Config, read_user_config_values, save_config_values


def _setup(monkeypatch, tmp_path, *, existing=True):
    for key in main_mod._settings_env_override_keys():
        monkeypatch.delenv(key, raising=False)
    screenshots = tmp_path / "World of Warcraft" / "_retail_" / "Screenshots"
    screenshots.mkdir(parents=True)
    (screenshots.parent / "Interface" / "AddOns").mkdir(parents=True)
    cfg = Config(
        wcl_client_id="example-client", wcl_client_secret="example-secret",
        chatlog_path=screenshots.parent / "Logs" / "WoWChatLog.txt",
        region="EU", cache_dir=tmp_path / "cache", config_dir=tmp_path / "config",
        config_path=tmp_path / "config" / "config.env", screenshots_path=screenshots,
    )
    if existing:
        save_config_values(
            wcl_client_id=cfg.wcl_client_id, wcl_client_secret=cfg.wcl_client_secret,
            region=cfg.region, screenshots_path=str(screenshots), config_path=cfg.config_path,
        )
        with cfg.config_path.open("ab") as stream:
            stream.write(b"# preserved owner comment\n")
    return cfg


def _values(cfg, **overrides):
    data = dict(
        wcl_client_id=cfg.wcl_client_id, wcl_client_secret=cfg.wcl_client_secret,
        region=cfg.region, screenshots_path=str(cfg.screenshots_path),
        metric_preferences=cfg.metric_preferences, sync_with_wow=cfg.sync_with_wow,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _commit(prepared, values):
    return main_mod._commit_settings_apply(main_mod.SettingsApplyCtx(
        app=object(), prepared=prepared, values=values, auth=object(),
        wcl_client=SimpleNamespace(
            region=prepared.old_cfg.region, reconfigure_auth=lambda _auth, **_kwargs: None,
        ),
        region_runtime=main_mod._WCLRegionRuntime(prepared.old_cfg.region),
        window=SimpleNamespace(
            apply_metric_preferences=lambda *_args, **_kwargs: None,
            bump_wcl_runtime_generation=lambda: None,
        ),
        watcher=object(), current_screenshots_dir=prepared.old_cfg.screenshots_path,
        machine=object(), decode_failed_callback=lambda *_args: None,
        signal_gate=main_mod._WatcherSignalGate(), wow_exit_timer=None,
        quit_app=lambda: None, can_quit=lambda: True,
    ))


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["commit", "prepare"])
def test_superseded_write_and_winning_failure_restore_committed_disk(
    monkeypatch, tmp_path, existing, failure,
):
    cfg = _setup(monkeypatch, tmp_path, existing=existing)
    before = cfg.config_path.read_bytes() if existing else None
    current = [cfg]
    workers = []
    outcomes = []
    applier = main_mod._CoalescedSettingsApplier(current_cfg=lambda: current[0], runner=workers.append)

    def fail_watch(*_args, **_kwargs):
        raise RuntimeError("watcher startup failed")

    monkeypatch.setattr(main_mod, "_replace_screenshots_runtime", fail_watch)

    def ready(outcome):
        outcomes.append(outcome)
        if outcome.ok:
            with pytest.raises(RuntimeError, match="watcher startup failed"):
                _commit(outcome.prepared, outcome.values)

    applier.configure(on_ready=ready)
    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    applier.submit(_values(cfg, region="KR", screenshots_path=str(tmp_path / "new")), apply_credentials=False)
    workers.pop(0)()
    assert outcomes == []
    assert read_user_config_values(cfg.config_path)["APSCOUT_REGION"] == "US"
    if failure == "prepare":
        real_persist = main_mod._persist_settings_values

        def persist_then_fail(*args, **kwargs):
            real_persist(*args, **kwargs)
            raise OSError("save failed after write")

        monkeypatch.setattr(main_mod, "_persist_settings_values", persist_then_fail)
    workers.pop(0)()
    assert len(outcomes) == 1
    assert current[0] is cfg
    assert cfg.region == "EU"
    if existing:
        assert cfg.config_path.read_bytes() == before
    else:
        assert not cfg.config_path.exists()
    assert applier.drain(timeout_s=0.01)


def test_successful_commit_advances_rollback_baseline(monkeypatch, tmp_path):
    cfg = _setup(monkeypatch, tmp_path)
    current = [cfg]
    workers = []
    applier = main_mod._CoalescedSettingsApplier(current_cfg=lambda: current[0], runner=workers.append)

    def ready(outcome):
        if outcome.values.region == "US":
            current[0] = _commit(outcome.prepared, outcome.values).cfg
        else:
            with pytest.raises(RuntimeError):
                _commit(outcome.prepared, outcome.values)

    applier.configure(on_ready=ready)
    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    workers.pop(0)()
    baseline = cfg.config_path.read_bytes()
    applier.submit(_values(current[0], region="KR"), apply_credentials=False)
    applier.submit(_values(current[0], region="TW", screenshots_path=str(tmp_path / "new")), apply_credentials=False)
    workers.pop(0)()
    monkeypatch.setattr(main_mod, "_replace_screenshots_runtime", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("failed")))
    workers.pop(0)()
    assert current[0].region == "US"
    assert cfg.config_path.read_bytes() == baseline


def test_reentrant_submit_does_not_start_before_commit_finishes(monkeypatch, tmp_path):
    cfg = _setup(monkeypatch, tmp_path)
    current = [cfg]
    workers = []
    applier = main_mod._CoalescedSettingsApplier(current_cfg=lambda: current[0], runner=workers.append)

    def ready(outcome):
        if outcome.values.region == "US":
            applier.submit(_values(outcome.prepared.new_cfg, region="KR"), apply_credentials=False)
            assert workers == []
        current[0] = _commit(outcome.prepared, outcome.values).cfg

    applier.configure(on_ready=ready)
    applier.submit(_values(cfg, region="US"), apply_credentials=False)
    workers.pop(0)()
    assert len(workers) == 1
    workers.pop(0)()
    assert current[0].region == "KR"
    assert read_user_config_values(cfg.config_path)["APSCOUT_REGION"] == "KR"


def test_promoted_credentials_survive_later_draft_commit_failure(monkeypatch, tmp_path):
    cfg = _setup(monkeypatch, tmp_path)
    current = [cfg]
    workers = []
    applier = main_mod._CoalescedSettingsApplier(current_cfg=lambda: current[0], runner=workers.append)
    monkeypatch.setattr(main_mod, "WCLAuth", lambda *_a, **_k: object())
    baseline = []

    def ready(outcome):
        if outcome.apply_credentials:
            current[0] = _commit(outcome.prepared, outcome.values).cfg
            baseline.append(cfg.config_path.read_bytes())
        else:
            with pytest.raises(RuntimeError):
                _commit(outcome.prepared, outcome.values)

    applier.configure(on_ready=ready)
    applier.submit(_values(cfg, region="US", wcl_client_id="new-client"), apply_credentials=True)
    applier.submit(_values(cfg, region="KR", screenshots_path=str(tmp_path / "new")), apply_credentials=False)
    workers.pop(0)()
    assert current[0].wcl_client_id == "new-client"
    monkeypatch.setattr(main_mod, "_replace_screenshots_runtime", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("failed")))
    workers.pop(0)()
    assert current[0].region == "US"
    assert cfg.config_path.read_bytes() == baseline[0]


def test_callback_error_after_runtime_promotion_advances_baseline(monkeypatch, tmp_path):
    cfg = _setup(monkeypatch, tmp_path)
    current = [cfg]
    workers = []
    applier = main_mod._CoalescedSettingsApplier(current_cfg=lambda: current[0], runner=workers.append)
    snapshot = main_mod._capture_persisted_config_snapshot(cfg)
    values = _values(cfg, region="US")
    prepared = main_mod._prepare_settings_apply(cfg=cfg, values=values, apply_credentials=False)

    def ready(outcome):
        current[0] = _commit(outcome.prepared, outcome.values).cfg
        raise RuntimeError("dialog status failed after commit")

    applier.configure(on_ready=ready)
    with pytest.raises(RuntimeError, match="dialog status"):
        applier._on_finished(main_mod._SettingsApplyOutcome(0, True, prepared, values, False, None, snapshot))
    baseline = cfg.config_path.read_bytes()
    applier.configure(on_ready=lambda _outcome: None)

    def fail_after_persist(*args, **kwargs):
        real_persist(*args, **kwargs)
        raise OSError("failed")

    real_persist = main_mod._persist_settings_values
    monkeypatch.setattr(main_mod, "_persist_settings_values", fail_after_persist)
    applier.submit(_values(current[0], region="KR"), apply_credentials=False)
    workers.pop(0)()
    assert current[0].region == "US"
    assert cfg.config_path.read_bytes() == baseline
