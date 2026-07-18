from __future__ import annotations

from dataclasses import replace
import json
import threading

import pytest

import applicant_scout.live_snapshot_cache as cache_mod
from applicant_scout.live_snapshot_cache import (
    LIVE_SNAPSHOT_CACHE_FILENAME,
    LIVE_SNAPSHOT_CACHE_TTL_SECONDS,
    LiveSnapshotCacheWriter,
    clear_live_snapshot_if_saved_at,
    load_live_snapshot,
    save_live_snapshot,
)
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
    SnapshotSource,
)


def _live_snapshot() -> Snapshot:
    return Snapshot(
        listing=DecodedListing(
            activity_id=401,
            key_level=14,
            dungeon_name="Theater of Pain",
            listing_name="+14 weekly",
            comment="chill",
            category_id=2,
            difficulty_id=8,
        ),
        version=DecodedVersion(
            addon_version="0.4.3",
            game_version="12.0.5",
            region_id=3,
            player_name="Host-Realm",
        ),
        applicants=[
            DecodedApplicant(
                applicant_id=42,
                member_idx=1,
                class_id=10,
                spec_id=270,
                ilvl=685,
                score=3100,
                role=1,
                name="Healer-Realm",
                main_score=3200,
                rio_profile=True,
                rio_best_key=14,
                rio_best_dungeon_key=13,
                rio_timed_at_or_above=2,
                rio_timed_at_or_above_minus1=4,
                rio_timed_at_or_above_minus2=6,
                rio_completed_at_or_above_minus1=5,
                rio_dungeon_count=8,
                rio_dungeons=[{"name": "Theater of Pain", "key_level": 14}],
            )
        ],
        roster=[
            DecodedRosterMember(
                unit_index=1,
                flags=1,
                subgroup=1,
                class_id=1,
                spec_id=73,
                ilvl=690,
                score=3000,
                main_score=3000,
                role=0,
                name="Tank-Realm",
            )
        ],
        source=SnapshotSource(mtime_ns=123, file_id="WoWScrnShot.jpg", size=456),
    )


def _cache_path(tmp_path):
    return tmp_path / LIVE_SNAPSHOT_CACHE_FILENAME


def _cache_payload(tmp_path, *, now: float = 100.0) -> dict:
    save_live_snapshot(tmp_path, _live_snapshot(), now=now)
    return json.loads(_cache_path(tmp_path).read_text(encoding="utf-8"))


def _write_cache_payload(tmp_path, payload: dict) -> None:
    _cache_path(tmp_path).write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )


def test_save_and_load_live_snapshot_round_trips_without_source(tmp_path):
    snap = _live_snapshot()

    save_live_snapshot(tmp_path, snap, now=100.0)
    restored = load_live_snapshot(tmp_path, now=120.0)

    assert restored is not None
    assert restored.saved_at == 100.0
    assert restored.snapshot.source is None
    assert restored.snapshot.listing == snap.listing
    assert restored.snapshot.version == snap.version
    assert restored.snapshot.applicants == snap.applicants
    assert restored.snapshot.roster == snap.roster
    assert not restored.snapshot.terminal_clear
    assert not restored.snapshot.lfg_unavailable
    assert not restored.snapshot.roster_unavailable


def test_load_live_snapshot_rejects_and_removes_expired_snapshot(tmp_path):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)

    restored = load_live_snapshot(
        tmp_path,
        now=100.0 + LIVE_SNAPSHOT_CACHE_TTL_SECONDS + 0.1,
    )

    assert restored is None
    assert not (tmp_path / LIVE_SNAPSHOT_CACHE_FILENAME).exists()


def test_save_live_snapshot_ignores_partial_snapshot_without_clearing_previous(tmp_path):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    partial = Snapshot(
        listing=None,
        version=DecodedVersion("0.4.3", "12.0.5", 3, "Host-Realm"),
        lfg_unavailable=True,
    )

    save_live_snapshot(tmp_path, partial, now=110.0)
    restored = load_live_snapshot(tmp_path, now=120.0)

    assert restored is not None
    assert restored.saved_at == 100.0


def test_save_live_snapshot_ignores_roster_unavailable_without_clearing_previous(
    tmp_path,
):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    partial = Snapshot(
        listing=_live_snapshot().listing,
        version=DecodedVersion("0.4.3", "12.0.5", 3, "Host-Realm"),
        applicants=[
            DecodedApplicant(
                applicant_id=99,
                member_idx=1,
                class_id=8,
                spec_id=64,
                ilvl=281,
                score=2444,
                role=2,
                name="Fresh-Realm",
            )
        ],
        roster=[],
        roster_unavailable=True,
    )

    save_live_snapshot(tmp_path, partial, now=110.0)
    restored = load_live_snapshot(tmp_path, now=120.0)

    assert restored is not None
    assert restored.saved_at == 100.0
    assert restored.snapshot.applicants[0].name == "Healer-Realm"


def test_save_live_snapshot_clears_on_terminal_clear_or_no_listing(tmp_path):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    save_live_snapshot(
        tmp_path,
        Snapshot(listing=None, version=None, terminal_clear=True),
        now=110.0,
    )

    assert load_live_snapshot(tmp_path, now=111.0) is None

    save_live_snapshot(tmp_path, _live_snapshot(), now=120.0)
    save_live_snapshot(tmp_path, Snapshot(listing=None, version=None), now=121.0)

    assert load_live_snapshot(tmp_path, now=122.0) is None


def test_load_live_snapshot_rejects_malformed_or_non_live_payload(tmp_path):
    path = tmp_path / LIVE_SNAPSHOT_CACHE_FILENAME
    path.write_text("{not json", encoding="utf-8")

    assert load_live_snapshot(tmp_path, now=100.0) is None
    assert not path.exists()

    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "saved_at": 100.0,
                "snapshot": {
                    "listing": None,
                    "version": None,
                    "leader_key": None,
                    "applicants": [],
                    "roster": [],
                    "terminal_clear": False,
                    "lfg_unavailable": False,
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_live_snapshot(tmp_path, now=101.0) is None
    assert not path.exists()


def test_load_live_snapshot_rejects_wrong_scalar_types_from_disk(tmp_path):
    payload = _cache_payload(tmp_path)
    payload["snapshot"]["applicants"][0]["spec_id"] = "270"
    _write_cache_payload(tmp_path, payload)

    assert load_live_snapshot(tmp_path, now=101.0) is None
    assert not _cache_path(tmp_path).exists()

    payload = _cache_payload(tmp_path)
    payload["snapshot"]["applicants"][0]["rio_profile"] = "false"
    _write_cache_payload(tmp_path, payload)

    assert load_live_snapshot(tmp_path, now=101.0) is None
    assert not _cache_path(tmp_path).exists()


@pytest.mark.parametrize("saved_at", [float("nan"), float("inf"), 130.0])
def test_load_live_snapshot_rejects_nonfinite_or_future_saved_at(tmp_path, saved_at):
    payload = _cache_payload(tmp_path)
    payload["saved_at"] = saved_at
    _write_cache_payload(tmp_path, payload)

    assert load_live_snapshot(tmp_path, now=100.0) is None
    assert not _cache_path(tmp_path).exists()


def test_load_live_snapshot_rejects_invalid_utf8_cache_file(tmp_path):
    _cache_path(tmp_path).write_bytes(b"\xff\xfe\x00")

    assert load_live_snapshot(tmp_path, now=100.0) is None
    assert not _cache_path(tmp_path).exists()


def test_load_live_snapshot_requires_explicit_applicant_and_roster_arrays(tmp_path):
    payload = _cache_payload(tmp_path)
    del payload["snapshot"]["applicants"]
    _write_cache_payload(tmp_path, payload)

    assert load_live_snapshot(tmp_path, now=101.0) is None
    assert not _cache_path(tmp_path).exists()

    payload = _cache_payload(tmp_path)
    del payload["snapshot"]["roster"]
    _write_cache_payload(tmp_path, payload)

    assert load_live_snapshot(tmp_path, now=101.0) is None
    assert not _cache_path(tmp_path).exists()


def test_load_live_snapshot_rejects_malformed_rio_dungeons_from_disk(tmp_path):
    payload = _cache_payload(tmp_path)
    payload["snapshot"]["applicants"][0]["rio_dungeons"] = ["Theater of Pain"]
    _write_cache_payload(tmp_path, payload)

    assert load_live_snapshot(tmp_path, now=101.0) is None
    assert not _cache_path(tmp_path).exists()


def test_load_live_snapshot_filters_placeholder_identities_before_restore(tmp_path):
    snap = _live_snapshot()
    placeholder_applicant = replace(
        snap.applicants[0],
        applicant_id=43,
        member_idx=1,
        name="UNKNOWNOBJECT",
    )
    placeholder_roster = replace(
        snap.roster[0],
        unit_index=2,
        name="Unknown-Realm",
    )
    snap = replace(
        snap,
        applicants=[snap.applicants[0], placeholder_applicant],
        roster=[snap.roster[0], placeholder_roster],
    )

    save_live_snapshot(tmp_path, snap, now=100.0)
    restored = load_live_snapshot(tmp_path, now=101.0)

    assert restored is not None
    assert restored.snapshot.applicants == [snap.applicants[0]]
    assert restored.snapshot.roster == [snap.roster[0]]


def test_load_live_snapshot_rejects_duplicate_real_identities(tmp_path):
    snap = _live_snapshot()
    duplicate = replace(snap.applicants[0])
    snap = replace(snap, applicants=[snap.applicants[0], duplicate])
    save_live_snapshot(tmp_path, snap, now=100.0)

    assert load_live_snapshot(tmp_path, now=101.0) is None
    assert not _cache_path(tmp_path).exists()


def test_clear_live_snapshot_if_saved_at_preserves_newer_snapshot(tmp_path):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    save_live_snapshot(tmp_path, _live_snapshot(), now=120.0)

    assert not clear_live_snapshot_if_saved_at(tmp_path, 100.0)
    restored = load_live_snapshot(tmp_path, now=121.0)

    assert restored is not None
    assert restored.saved_at == 120.0


def test_clear_live_snapshot_if_saved_at_removes_matching_snapshot(tmp_path):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)

    assert clear_live_snapshot_if_saved_at(tmp_path, 100.0)
    assert not _cache_path(tmp_path).exists()


def test_live_snapshot_writer_suppresses_bounded_source_only_resend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    original_save = cache_mod._save_live_snapshot_content
    save_times: list[float | None] = []

    def counting_save(cache_dir, content, *, saved_at):
        save_times.append(saved_at)
        return original_save(cache_dir, content, saved_at=saved_at)

    monkeypatch.setattr(cache_mod, "_save_live_snapshot_content", counting_save)
    writer = LiveSnapshotCacheWriter(tmp_path, defer_saves=False)
    first = _live_snapshot()
    resend = replace(
        first,
        source=SnapshotSource(
            mtime_ns=456,
            file_id="WoWScrnShot-resend.jpg",
            size=456,
        ),
    )

    writer.submit(first, now=100.0)
    writer.submit(resend, now=100.5)

    assert save_times == [100.0]
    restored = load_live_snapshot(tmp_path, now=101.0)
    assert restored is not None
    assert restored.saved_at == 100.0

    writer.submit(resend, now=103.0)

    assert save_times == [100.0, 103.0]
    refreshed = load_live_snapshot(tmp_path, now=104.0)
    assert refreshed is not None
    assert refreshed.saved_at == 103.0


def test_live_snapshot_writer_duplicate_suppression_resets_after_clear_and_invalidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    original_save = cache_mod._save_live_snapshot_content
    save_times: list[float | None] = []

    def counting_save(cache_dir, content, *, saved_at):
        save_times.append(saved_at)
        return original_save(cache_dir, content, saved_at=saved_at)

    monkeypatch.setattr(cache_mod, "_save_live_snapshot_content", counting_save)
    writer = LiveSnapshotCacheWriter(tmp_path, defer_saves=False)
    snapshot = _live_snapshot()

    writer.submit(snapshot, now=100.0)
    writer.submit(Snapshot(listing=None, version=None), now=100.1)
    writer.submit(snapshot, now=100.2)
    writer.invalidate()
    writer.submit(snapshot, now=100.3)

    assert save_times == [100.0, 100.2, 100.3]


def test_live_snapshot_writer_partial_snapshot_does_not_cancel_pending_full_save(tmp_path):
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )
    writer.submit(_live_snapshot(), now=100.0)
    writer.submit(
        Snapshot(
            listing=None,
            version=DecodedVersion("0.4.3", "12.0.5", 3, "Host-Realm"),
            lfg_unavailable=True,
        ),
        now=110.0,
    )

    writer.flush()
    restored = load_live_snapshot(tmp_path, now=111.0)

    assert restored is not None
    assert restored.saved_at == 100.0


def test_live_snapshot_writer_clear_wins_over_pending_save(tmp_path):
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )
    writer.submit(_live_snapshot(), now=100.0)
    writer.submit(Snapshot(listing=None, version=None), now=101.0)

    writer.flush()

    assert load_live_snapshot(tmp_path, now=102.0) is None
    assert not _cache_path(tmp_path).exists()


def test_live_snapshot_writer_save_after_clear_persists_newer_snapshot(tmp_path):
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )
    writer.submit(Snapshot(listing=None, version=None), now=100.0)
    writer.submit(_live_snapshot(), now=120.0)

    writer.flush()
    restored = load_live_snapshot(tmp_path, now=121.0)

    assert restored is not None
    assert restored.saved_at == 120.0


def test_live_snapshot_writer_retries_failed_save_on_next_flush(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    original_write = cache_mod.atomic_write_text
    calls = 0

    def flaky_write(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("locked")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(cache_mod, "atomic_write_text", flaky_write)
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )
    writer.submit(_live_snapshot(), now=100.0)

    writer.flush()
    assert not _cache_path(tmp_path).exists()

    writer.flush()
    restored = load_live_snapshot(tmp_path, now=101.0)

    assert calls == 2
    assert restored is not None
    assert restored.saved_at == 100.0


def test_live_snapshot_writer_materializes_content_once_across_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    original_snapshot_to_dict = cache_mod._snapshot_to_dict
    original_write = cache_mod.atomic_write_text
    conversions = 0
    writes = 0

    def counting_snapshot_to_dict(snapshot):
        nonlocal conversions
        conversions += 1
        return original_snapshot_to_dict(snapshot)

    def flaky_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("locked")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(cache_mod, "_snapshot_to_dict", counting_snapshot_to_dict)
    monkeypatch.setattr(cache_mod, "atomic_write_text", flaky_write)
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )

    writer.submit(_live_snapshot(), now=100.0)
    assert not writer.flush()
    assert writer.flush()

    assert conversions == 1
    assert writes == 2
    restored = load_live_snapshot(tmp_path, now=101.0)
    assert restored is not None
    assert restored.saved_at == 100.0


def test_live_snapshot_writer_invalidate_waits_for_in_flight_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    started = threading.Event()
    release = threading.Event()
    invalidate_returned = threading.Event()
    original_save = cache_mod._save_live_snapshot_content

    def slow_save(cache_dir, content, *, saved_at):
        started.set()
        assert release.wait(timeout=2.0)
        return original_save(cache_dir, content, saved_at=saved_at)

    monkeypatch.setattr(cache_mod, "_save_live_snapshot_content", slow_save)
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )
    writer.submit(_live_snapshot(), now=100.0)

    flush_thread = threading.Thread(target=writer.flush)
    flush_thread.start()
    assert started.wait(timeout=2.0)

    invalidate_thread = threading.Thread(
        target=lambda: (writer.invalidate(), invalidate_returned.set())
    )
    invalidate_thread.start()
    assert not invalidate_returned.wait(timeout=0.05)

    release.set()
    flush_thread.join(timeout=2.0)
    invalidate_thread.join(timeout=2.0)

    assert not flush_thread.is_alive()
    assert not invalidate_thread.is_alive()
    assert invalidate_returned.is_set()


def test_live_snapshot_writer_invalidate_drops_failed_in_flight_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    started = threading.Event()
    release = threading.Event()
    invalidate_returned = threading.Event()
    calls = 0
    original_save = cache_mod._save_live_snapshot_content

    def failing_save(_cache_dir, _content, *, saved_at):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2.0)
        return False

    monkeypatch.setattr(cache_mod, "_save_live_snapshot_content", failing_save)
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )
    writer.submit(_live_snapshot(), now=100.0)

    flush_thread = threading.Thread(target=writer.flush)
    flush_thread.start()
    assert started.wait(timeout=2.0)

    invalidate_thread = threading.Thread(
        target=lambda: (writer.invalidate(), invalidate_returned.set())
    )
    invalidate_thread.start()
    assert not invalidate_returned.wait(timeout=0.05)

    release.set()
    flush_thread.join(timeout=2.0)
    invalidate_thread.join(timeout=2.0)

    assert not flush_thread.is_alive()
    assert not invalidate_thread.is_alive()
    assert invalidate_returned.is_set()
    assert writer.flush()
    assert calls == 1

    monkeypatch.setattr(cache_mod, "_save_live_snapshot_content", original_save)
    writer.submit(_live_snapshot(), now=120.0)
    assert writer.flush()
    restored = load_live_snapshot(tmp_path, now=121.0)
    assert restored is not None
    assert restored.saved_at == 120.0


def test_live_snapshot_writer_retries_failed_clear_on_next_flush(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    original_clear = cache_mod.clear_live_snapshot
    calls = 0

    def flaky_clear(cache_dir):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        return original_clear(cache_dir)

    monkeypatch.setattr(cache_mod, "clear_live_snapshot", flaky_clear)
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )
    writer.submit(Snapshot(listing=None, version=None), now=101.0)

    writer.flush()
    assert _cache_path(tmp_path).exists()

    writer.flush()

    assert calls == 2
    assert not _cache_path(tmp_path).exists()


def test_live_snapshot_writer_close_waits_for_dequeued_failed_clear_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    started = threading.Event()
    release = threading.Event()
    close_returned = threading.Event()
    original_clear = cache_mod.clear_live_snapshot
    close_results: list[bool] = []
    calls = 0

    def blocked_flaky_clear(cache_dir):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(timeout=2.0)
            return False
        return original_clear(cache_dir)

    monkeypatch.setattr(cache_mod, "clear_live_snapshot", blocked_flaky_clear)
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )
    writer.submit(Snapshot(listing=None, version=None), now=101.0)

    flush_thread = threading.Thread(target=writer.flush)
    flush_thread.start()
    assert started.wait(timeout=2.0)

    close_thread = threading.Thread(
        target=lambda: (close_results.append(writer.close()), close_returned.set())
    )
    close_thread.start()
    try:
        assert not close_returned.wait(timeout=0.05)
    finally:
        release.set()
    flush_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not flush_thread.is_alive()
    assert not close_thread.is_alive()
    assert close_results == [True]
    assert calls == 2
    assert load_live_snapshot(tmp_path, now=102.0) is None


def test_live_snapshot_writer_close_retries_failed_final_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    original_clear = cache_mod.clear_live_snapshot
    calls = 0

    def flaky_clear(cache_dir):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        return original_clear(cache_dir)

    monkeypatch.setattr(cache_mod, "clear_live_snapshot", flaky_clear)
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
    )
    writer.submit(Snapshot(listing=None, version=None), now=101.0)

    assert writer.close()
    assert calls == 2
    assert load_live_snapshot(tmp_path, now=102.0) is None


def test_live_snapshot_writer_close_times_out_but_can_finish_later(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    save_live_snapshot(tmp_path, _live_snapshot(), now=100.0)
    started = threading.Event()
    release = threading.Event()
    original_clear = cache_mod.clear_live_snapshot
    calls = 0

    def blocked_flaky_clear(cache_dir):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(timeout=2.0)
            return False
        return original_clear(cache_dir)

    monkeypatch.setattr(cache_mod, "clear_live_snapshot", blocked_flaky_clear)
    writer = LiveSnapshotCacheWriter(
        tmp_path,
        defer_saves=True,
        save_debounce_seconds=60.0,
        close_timeout_seconds=0.05,
    )
    writer.submit(Snapshot(listing=None, version=None), now=101.0)
    flush_thread = threading.Thread(target=writer.flush)
    flush_thread.start()
    assert started.wait(timeout=2.0)

    try:
        assert not writer.close()
        assert flush_thread.is_alive()
    finally:
        release.set()
    flush_thread.join(timeout=2.0)

    assert not flush_thread.is_alive()
    assert writer.close()
    assert calls == 2
    assert load_live_snapshot(tmp_path, now=102.0) is None
