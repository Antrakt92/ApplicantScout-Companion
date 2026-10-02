"""M+ and raid provider failures recover independently without extending grace."""

import pytest
from threading import Event

import applicant_scout.raiderio_local as rio
from applicant_scout.raiderio_local import RaiderIOLocalReader
from test_raiderio_local import _write_mplus_generation, _write_raid_generation


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(rio.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(rio, "_NEGATIVE_CACHE_TTL_SECONDS", 5.0)
    monkeypatch.setattr(
        rio, "_EXPECTED_RAIDERIO_DUNGEON_ORDER", ("Skyreach", "Pit of Saron")
    )
    return now


def _write_both(root, *, score=3074, kills=(2, 0, 1)):
    _write_mplus_generation(root, score, 12)
    _write_raid_generation(root, kills)


def _lookup(reader, *, hot=False):
    return reader.lookup_profile("Chinie", "Ragnaros", "EU", allow_load=not hot)


def _failure(monkeypatch, half):
    blocked = [True]
    original = rio._read_provider_header

    def read(path):
        if blocked[0] and ("mythicplus" if half == "mplus" else "raiding") in path.name:
            raise PermissionError("Transient provider replacement")
        return original(path)

    monkeypatch.setattr(rio, "_read_provider_header", read)
    return blocked


def _assert_halves(profile, *, score, raid_killed):
    assert profile is not None
    assert profile.current_score == score
    assert profile.has_mplus_profile == (score > 0)
    assert profile.raid_progress.get("M", {}).get("killed", 0) == raid_killed


@pytest.mark.parametrize("half", ["mplus", "raid"])
def test_transient_single_half_failure_preserves_old_half_and_updates_other(
    tmp_path, monkeypatch, clock, half
):
    _write_both(tmp_path)
    reader = RaiderIOLocalReader(tmp_path)
    _assert_halves(_lookup(reader), score=3074, raid_killed=2)
    blocked = _failure(monkeypatch, half)
    _write_both(tmp_path, score=3444, kills=(1, 1, 1))
    clock[0] = 110.0
    _assert_halves(
        _lookup(reader),
        score=3074 if half == "mplus" else 3444,
        raid_killed=2 if half == "raid" else 3,
    )
    blocked[0] = False
    clock[0] = 116.0
    _assert_halves(_lookup(reader), score=3444, raid_killed=3)


@pytest.mark.parametrize("half", ["mplus", "raid"])
def test_initial_partial_load_retries_unchanged_provider_fingerprints(
    tmp_path, monkeypatch, clock, half
):
    _write_both(tmp_path)
    blocked = _failure(monkeypatch, half)
    reader = RaiderIOLocalReader(tmp_path)
    _assert_halves(
        _lookup(reader),
        score=0 if half == "mplus" else 3074,
        raid_killed=0 if half == "raid" else 2,
    )
    fingerprint = rio._region_db_fingerprint(tmp_path, "eu")
    blocked[0] = False
    clock[0] = 106.0
    assert rio._region_db_fingerprint(tmp_path, "eu") == fingerprint
    _assert_halves(_lookup(reader), score=3074, raid_killed=2)


@pytest.mark.parametrize("half", ["mplus", "raid"])
def test_hot_grace_expiry_drops_only_failed_half_without_filesystem_io(
    tmp_path, monkeypatch, clock, half
):
    _write_both(tmp_path)
    reader = RaiderIOLocalReader(tmp_path)
    assert _lookup(reader) is not None
    _failure(monkeypatch, half)
    _write_both(tmp_path, score=3444, kills=(1, 1, 1))
    clock[0] = 110.0
    _lookup(reader)
    clock[0] = 141.0
    for name in ("_region_db_fingerprint", "_region_db_stat", "_read_provider_header"):
        monkeypatch.setattr(
            rio,
            name,
            lambda *_a, **_k: pytest.fail("Hot lookup performed filesystem IO"),
        )
    _assert_halves(
        _lookup(reader, hot=True),
        score=0 if half == "mplus" else 3444,
        raid_killed=0 if half == "raid" else 3,
    )


@pytest.mark.parametrize("half", ["mplus", "raid"])
def test_provider_churn_does_not_renew_single_half_failure_grace(
    tmp_path, monkeypatch, clock, half
):
    _write_both(tmp_path)
    reader = RaiderIOLocalReader(tmp_path)
    assert _lookup(reader) is not None
    _failure(monkeypatch, half)
    for now in (110.0, 120.0, 130.0):
        _write_both(tmp_path, score=3444, kills=(1, 1, 1))
        clock[0] = now
        _assert_halves(
            _lookup(reader),
            score=3074 if half == "mplus" else 3444,
            raid_killed=2 if half == "raid" else 3,
        )
    clock[0] = 141.0
    _assert_halves(
        _lookup(reader, hot=True),
        score=0 if half == "mplus" else 3444,
        raid_killed=0 if half == "raid" else 3,
    )


@pytest.mark.parametrize("half", ["mplus", "raid"])
def test_failed_half_recovers_after_expired_grace_without_fingerprint_changes(
    tmp_path, monkeypatch, clock, half
):
    _write_both(tmp_path)
    reader = RaiderIOLocalReader(tmp_path)
    assert _lookup(reader) is not None
    blocked = _failure(monkeypatch, half)
    _write_both(tmp_path, score=3444, kills=(1, 1, 1))
    clock[0] = 110.0
    _lookup(reader)
    clock[0] = 141.0
    _assert_halves(
        _lookup(reader),
        score=0 if half == "mplus" else 3444,
        raid_killed=0 if half == "raid" else 3,
    )
    blocked[0] = False
    clock[0] = 147.0
    _assert_halves(_lookup(reader), score=3444, raid_killed=3)


@pytest.mark.parametrize("updated_half", ["mplus", "raid"])
def test_healthy_unchanged_provider_half_is_reused_without_loading(
    tmp_path, monkeypatch, clock, updated_half
):
    _write_both(tmp_path)
    calls = {"mplus": 0, "raid": 0}
    for half in calls:
        name = "_load_mplus_region" if half == "mplus" else "_load_raid_region"
        original = getattr(rio, name)

        def load(*args, _half=half, _original=original, **kwargs):
            calls[_half] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(rio, name, load)
    reader = RaiderIOLocalReader(tmp_path)
    assert _lookup(reader) is not None
    assert calls == {"mplus": 1, "raid": 1}
    if updated_half == "mplus":
        _write_mplus_generation(tmp_path, 3444, 14)
    else:
        _write_raid_generation(tmp_path, (1, 1, 1))
    clock[0] = 110.0
    _assert_halves(
        _lookup(reader),
        score=3444 if updated_half == "mplus" else 3074,
        raid_killed=3 if updated_half == "raid" else 2,
    )
    assert calls["raid" if updated_half == "mplus" else "mplus"] == 1


@pytest.mark.usefixtures("clock")
def test_healthy_hot_lookup_has_no_provider_io(tmp_path, monkeypatch):
    _write_both(tmp_path)
    reader = RaiderIOLocalReader(tmp_path)
    first = _lookup(reader)
    assert first is not None
    for name in ("_region_db_fingerprint", "_region_db_stat", "_read_provider_header"):
        monkeypatch.setattr(
            rio,
            name,
            lambda *_a, **_k: pytest.fail("Hot lookup performed filesystem IO"),
        )
    assert _lookup(reader, hot=True) == first


@pytest.mark.parametrize("half", ["mplus", "raid"])
@pytest.mark.parametrize("already_applied", [False, True])
def test_one_half_churn_during_every_load_does_not_expire_unchanged_other_half(
    tmp_path,
    monkeypatch,
    clock,
    half,
    already_applied,
):
    _write_both(tmp_path)
    reader = RaiderIOLocalReader(tmp_path)
    if already_applied:
        _assert_halves(_lookup(reader), score=3074, raid_killed=2)
    original_load = rio._RegionDB.load
    calls = []

    def load_then_replace_churning_half(root, token, **kwargs):
        loaded = original_load(root, token, **kwargs)
        calls.append(token)
        if half == "mplus":
            _write_mplus_generation(root, 3444 + len(calls), 14)
        else:
            _write_raid_generation(root, (len(calls), 1, 1))
        return loaded

    monkeypatch.setattr(rio._RegionDB, "load", load_then_replace_churning_half)
    if half == "mplus":
        _write_mplus_generation(tmp_path, 3444, 14)
    else:
        _write_raid_generation(tmp_path, (1, 1, 1))
    clock[0] = 110.0
    current = _lookup(reader)
    assert len(calls) == rio._REGION_LOAD_ATTEMPTS
    assert current is not None
    if half == "mplus":
        assert current.raid_progress["M"]["killed"] == 2
    else:
        assert current.current_score == 3074
    clock[0] = 141.0
    for name in ("_region_db_fingerprint", "_region_db_stat", "_read_provider_header"):
        monkeypatch.setattr(
            rio,
            name,
            lambda *_a, **_k: pytest.fail("Hot lookup performed filesystem IO"),
        )
    _assert_halves(
        _lookup(reader, hot=True),
        score=0 if half == "mplus" else 3074,
        raid_killed=0 if half == "raid" else 2,
    )


@pytest.mark.parametrize("half", ["mplus", "raid"])
def test_concurrent_preloads_share_failed_half_recovery_and_complete_both_callbacks(
    tmp_path,
    monkeypatch,
    clock,
    half,
):
    _write_both(tmp_path)
    blocked = _failure(monkeypatch, half)
    reader = RaiderIOLocalReader(tmp_path)
    assert _lookup(reader) is not None
    blocked[0] = False
    clock[0] = 106.0
    entered, release = Event(), Event()
    completed_first, completed_second = Event(), Event()
    original_header = rio._read_provider_header
    header_calls = []

    def pause_recovery(path):
        if ("mythicplus" if half == "mplus" else "raiding") in path.name:
            header_calls.append(path.name)
            entered.set()
            assert release.wait(5)
        return original_header(path)

    monkeypatch.setattr(rio, "_read_provider_header", pause_recovery)
    try:
        reader.preload_region_async("EU", on_loaded=completed_first.set)
        assert entered.wait(5)
        reader.preload_region_async("EU", on_loaded=completed_second.set)
        release.set()
        assert completed_first.wait(5)
        assert completed_second.wait(5)
        assert len(header_calls) == 2
        _assert_halves(_lookup(reader, hot=True), score=3074, raid_killed=2)
    finally:
        release.set()
        assert completed_first.wait(5)


def test_staggered_half_failures_have_independent_hot_lookup_grace_deadlines(
    tmp_path, monkeypatch, clock
):
    _write_both(tmp_path)
    reader = RaiderIOLocalReader(tmp_path)
    _assert_halves(_lookup(reader), score=3074, raid_killed=2)
    blocked = {"mplus"}
    original_header = rio._read_provider_header

    def read(path):
        half = "mplus" if "mythicplus" in path.name else "raid"
        if half in blocked:
            raise PermissionError("Provider replacement")
        return original_header(path)

    monkeypatch.setattr(rio, "_read_provider_header", read)
    _write_mplus_generation(tmp_path, 3444, 14)
    clock[0] = 110.0
    _assert_halves(_lookup(reader), score=3074, raid_killed=2)
    blocked.add("raid")
    _write_raid_generation(tmp_path, (1, 1, 1))
    clock[0] = 125.0
    _assert_halves(_lookup(reader), score=3074, raid_killed=2)
    for name in ("_region_db_fingerprint", "_region_db_stat", "_read_provider_header"):
        monkeypatch.setattr(
            rio,
            name,
            lambda *_a, **_k: pytest.fail("Hot lookup performed filesystem IO"),
        )
    clock[0] = 141.0
    _assert_halves(_lookup(reader, hot=True), score=0, raid_killed=2)
    clock[0] = 156.0
    assert _lookup(reader, hot=True) is None


@pytest.mark.parametrize("half", ["mplus", "raid"])
def test_content_audit_crossing_grace_deadline_cannot_return_expired_half(
    tmp_path, monkeypatch, clock, half
):
    _write_both(tmp_path)
    reader = RaiderIOLocalReader(tmp_path)
    assert _lookup(reader) is not None
    _failure(monkeypatch, half)
    _write_both(tmp_path, score=3444, kills=(1, 1, 1))
    clock[0] = 110.0
    _lookup(reader)
    # A recent failed retry is still within its retry cooldown, while the
    # original fallback deadline is about to expire during the content audit.
    clock[0] = 139.0
    _lookup(reader)
    clock[0] = 139.5
    monkeypatch.setattr(rio, "_CONTENT_AUDIT_INTERVAL_SECONDS", 0.0)
    original_fingerprint = rio._region_db_fingerprint

    def slow_fingerprint(root, token):
        fingerprint = original_fingerprint(root, token)
        clock[0] = 141.0
        return fingerprint

    monkeypatch.setattr(rio, "_region_db_fingerprint", slow_fingerprint)
    _assert_halves(
        _lookup(reader),
        score=0 if half == "mplus" else 3444,
        raid_killed=0 if half == "raid" else 3,
    )
