"""Short-lived restart bridge for the latest live ApplicantScout snapshot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_text
from .screenshot import (
    DecodedApplicant,
    DecodedLeaderKey,
    DecodedListing,
    DecodedRosterMember,
    DecodedVersion,
    Snapshot,
    validate_snapshot_for_application,
)


_log = logging.getLogger("applicant_scout.live_snapshot_cache")

LIVE_SNAPSHOT_CACHE_FILENAME = "last-live-snapshot.json"
LIVE_SNAPSHOT_CACHE_SCHEMA = 1
LIVE_SNAPSHOT_CACHE_TTL_SECONDS = 90.0
LIVE_SNAPSHOT_RESTORE_GRACE_SECONDS = 30.0
LIVE_SNAPSHOT_CLOSE_TIMEOUT_SECONDS = 2.0
LIVE_SNAPSHOT_CLOSE_RETRY_ATTEMPTS = 2


@dataclass(frozen=True)
class RestoredLiveSnapshot:
    snapshot: Snapshot
    saved_at: float


@dataclass(frozen=True)
class _PendingLiveSnapshotCacheOperation:
    kind: str
    snapshot: Snapshot | None = None
    saved_at: float | None = None


def live_snapshot_cache_path(cache_dir: Path) -> Path:
    return cache_dir / LIVE_SNAPSHOT_CACHE_FILENAME


def is_persistable_live_snapshot(snap: Snapshot) -> bool:
    return (
        snap.listing is not None
        and not snap.terminal_clear
        and not snap.lfg_unavailable
        and not snap.roster_unavailable
    )


def _should_clear_cache_for_snapshot(snap: Snapshot) -> bool:
    if snap.terminal_clear:
        return True
    return snap.listing is None and not snap.lfg_unavailable


def clear_live_snapshot(cache_dir: Path) -> bool:
    path = live_snapshot_cache_path(cache_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError as exc:
        _log.warning("Failed to remove live snapshot cache %s: %s", path, exc)
        return False
    return True


def save_live_snapshot(
    cache_dir: Path,
    snap: Snapshot,
    *,
    now: float | None = None,
) -> bool:
    if _should_clear_cache_for_snapshot(snap):
        return clear_live_snapshot(cache_dir)
    if not is_persistable_live_snapshot(snap):
        return True
    try:
        saved_at = _coerce_timestamp(time.time() if now is None else now)
    except ValueError as exc:
        _log.warning("Failed to save live snapshot cache: %s", exc)
        return False
    payload = {
        "schema": LIVE_SNAPSHOT_CACHE_SCHEMA,
        "saved_at": saved_at,
        "snapshot": _snapshot_to_dict(snap),
    }
    path = live_snapshot_cache_path(cache_dir)
    try:
        atomic_write_text(
            path,
            json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
            private=True,
        )
    except (OSError, TypeError, ValueError) as exc:
        _log.warning("Failed to save live snapshot cache %s: %s", path, exc)
        return False
    return True


class LiveSnapshotCacheWriter:
    def __init__(
        self,
        cache_dir: Path,
        *,
        defer_saves: bool = True,
        save_debounce_seconds: float = 0.25,
        close_timeout_seconds: float = LIVE_SNAPSHOT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._defer_saves = bool(defer_saves)
        self._save_debounce_seconds = max(0.0, float(save_debounce_seconds))
        self._close_timeout_seconds = max(0.0, float(close_timeout_seconds))
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: _PendingLiveSnapshotCacheOperation | None = None
        self._timer: threading.Timer | None = None
        self._closed = False
        self._generation = 0

    def submit(self, snap: Snapshot, *, now: float | None = None) -> None:
        operation = _operation_for_snapshot(snap, now=now)
        if operation is None:
            return
        flush_now = False
        with self._lock:
            if self._closed:
                return
            self._pending = operation
            if self._defer_saves:
                self._schedule_locked()
            else:
                flush_now = True
        if flush_now:
            self.flush()

    def flush(self) -> bool:
        # WHY: claim and requeue under the write barrier so close cannot observe
        # a false-empty queue between a failed I/O attempt and its retry state.
        with self._write_lock:
            with self._lock:
                self._cancel_timer_locked()
                if self._closed:
                    return self._pending is None
                operation = self._pending
                self._pending = None
                generation = self._generation
            if operation is None:
                return True
            succeeded = self._perform_operation(operation)
            if not succeeded:
                with self._lock:
                    if self._pending is None and generation == self._generation:
                        self._pending = operation
                        if not self._closed and self._defer_saves:
                            self._schedule_locked()
            return succeeded

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._cancel_timer_locked()
            self._pending = None
        with self._write_lock:
            pass

    def close(self) -> bool:
        # WHY: reject new submissions before boundedly joining any timer-owned
        # write; a failed in-flight operation is then available for final retry.
        with self._lock:
            self._closed = True
            self._cancel_timer_locked()

        if not self._write_lock.acquire(timeout=self._close_timeout_seconds):
            _log.warning(
                "Timed out after %.2fs draining the live snapshot cache writer.",
                self._close_timeout_seconds,
            )
            return False
        try:
            with self._lock:
                operation = self._pending
                self._pending = None
                generation = self._generation
            if operation is None:
                return True

            for _attempt in range(LIVE_SNAPSHOT_CLOSE_RETRY_ATTEMPTS):
                if self._perform_operation(operation):
                    return True

            with self._lock:
                if self._pending is None and generation == self._generation:
                    self._pending = operation
            _log.warning(
                "Failed to drain the live snapshot cache writer after %d attempts.",
                LIVE_SNAPSHOT_CLOSE_RETRY_ATTEMPTS,
            )
            return False
        finally:
            self._write_lock.release()

    def _schedule_locked(self) -> None:
        if self._timer is not None:
            return
        timer = threading.Timer(self._save_debounce_seconds, self.flush)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_timer_locked(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

    def _perform_operation(self, operation: _PendingLiveSnapshotCacheOperation) -> bool:
        if operation.kind == "clear":
            return clear_live_snapshot(self._cache_dir)
        if operation.kind == "save" and operation.snapshot is not None:
            return save_live_snapshot(
                self._cache_dir,
                operation.snapshot,
                now=operation.saved_at,
            )
        return True


def _operation_for_snapshot(
    snap: Snapshot,
    *,
    now: float | None,
) -> _PendingLiveSnapshotCacheOperation | None:
    if _should_clear_cache_for_snapshot(snap):
        return _PendingLiveSnapshotCacheOperation("clear")
    if not is_persistable_live_snapshot(snap):
        return None
    try:
        saved_at = _coerce_timestamp(time.time() if now is None else now)
    except ValueError as exc:
        _log.warning("Ignoring live snapshot cache save with invalid timestamp: %s", exc)
        return None
    return _PendingLiveSnapshotCacheOperation("save", snapshot=snap, saved_at=saved_at)


def load_live_snapshot(
    cache_dir: Path,
    *,
    now: float | None = None,
) -> RestoredLiveSnapshot | None:
    path = live_snapshot_cache_path(cache_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        _log.warning("Discarding invalid live snapshot cache %s: %s", path, exc)
        clear_live_snapshot(cache_dir)
        return None
    except OSError as exc:
        _log.warning("Failed to read live snapshot cache %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("cache root is not an object")
        if data.get("schema") != LIVE_SNAPSHOT_CACHE_SCHEMA:
            raise ValueError("unsupported cache schema")
        current_time = _coerce_timestamp(time.time() if now is None else now)
        saved_at = _strict_timestamp_field(data, "saved_at")
        if saved_at > current_time + 1.0:
            raise ValueError("saved_at is in the future")
        if current_time - saved_at > LIVE_SNAPSHOT_CACHE_TTL_SECONDS:
            clear_live_snapshot(cache_dir)
            return None
        snapshot_data = _required_field(data, "snapshot")
        if not isinstance(snapshot_data, dict):
            raise ValueError("snapshot is not an object")
        snap = _snapshot_from_dict(snapshot_data)
        if not is_persistable_live_snapshot(snap):
            raise ValueError("cached snapshot is not a live listing snapshot")
        return RestoredLiveSnapshot(snapshot=snap, saved_at=saved_at)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _log.warning("Discarding invalid live snapshot cache %s: %s", path, exc)
        clear_live_snapshot(cache_dir)
        return None


def clear_live_snapshot_if_saved_at(cache_dir: Path, expected_saved_at: float) -> bool:
    path = live_snapshot_cache_path(cache_dir)
    try:
        expected = _coerce_timestamp(expected_saved_at)
    except ValueError:
        return False
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("Failed to inspect live snapshot cache %s: %s", path, exc)
        return False
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("cache root is not an object")
        if data.get("schema") != LIVE_SNAPSHOT_CACHE_SCHEMA:
            raise ValueError("unsupported cache schema")
        saved_at = _strict_timestamp_field(data, "saved_at")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _log.warning("Discarding invalid live snapshot cache %s: %s", path, exc)
        return clear_live_snapshot(cache_dir)
    if saved_at != expected:
        return False
    return clear_live_snapshot(cache_dir)


def _snapshot_to_dict(snap: Snapshot) -> dict[str, Any]:
    return {
        "listing": asdict(snap.listing) if snap.listing is not None else None,
        "version": asdict(snap.version) if snap.version is not None else None,
        "leader_key": asdict(snap.leader_key) if snap.leader_key is not None else None,
        "applicants": [asdict(applicant) for applicant in snap.applicants],
        "roster": [asdict(member) for member in snap.roster],
        "terminal_clear": bool(snap.terminal_clear),
        "lfg_unavailable": bool(snap.lfg_unavailable),
        "roster_unavailable": bool(snap.roster_unavailable),
    }


def _snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    snap = Snapshot(
        listing=_optional_object(data, "listing", _decoded_listing_from_dict),
        version=_optional_object(data, "version", _decoded_version_from_dict),
        leader_key=_optional_object(
            data,
            "leader_key",
            _decoded_leader_key_from_dict,
        ),
        applicants=[
            _decoded_applicant_from_dict(item)
            for item in _list_of_dicts(_required_field(data, "applicants"))
        ],
        roster=[
            _decoded_roster_member_from_dict(item)
            for item in _list_of_dicts(_required_field(data, "roster"))
        ],
        terminal_clear=_strict_bool(_required_field(data, "terminal_clear")),
        lfg_unavailable=_strict_bool(_required_field(data, "lfg_unavailable")),
        roster_unavailable=_optional_bool_field(data, "roster_unavailable"),
        source=None,
    )
    return validate_snapshot_for_application(snap)


def _decoded_listing_from_dict(data: dict[str, Any]) -> DecodedListing:
    return DecodedListing(
        activity_id=_strict_int_field(data, "activity_id"),
        key_level=_strict_int_field(data, "key_level"),
        dungeon_name=_strict_str_field(data, "dungeon_name"),
        listing_name=_strict_str_field(data, "listing_name"),
        comment=_strict_str_field(data, "comment"),
        category_id=_strict_int_field(data, "category_id"),
        difficulty_id=_strict_int_field(data, "difficulty_id"),
    )


def _decoded_leader_key_from_dict(data: dict[str, Any]) -> DecodedLeaderKey:
    return DecodedLeaderKey(
        key_level=_strict_int_field(data, "key_level"),
        challenge_map_id=_strict_int_field(data, "challenge_map_id"),
        player_name=_strict_str_field(data, "player_name"),
    )


def _decoded_version_from_dict(data: dict[str, Any]) -> DecodedVersion:
    return DecodedVersion(
        addon_version=_strict_str_field(data, "addon_version"),
        game_version=_strict_str_field(data, "game_version"),
        region_id=_strict_int_field(data, "region_id"),
        player_name=_strict_str_field(data, "player_name"),
    )


def _decoded_applicant_from_dict(data: dict[str, Any]) -> DecodedApplicant:
    return DecodedApplicant(
        applicant_id=_strict_int_field(data, "applicant_id"),
        class_id=_strict_int_field(data, "class_id"),
        spec_id=_strict_int_field(data, "spec_id"),
        ilvl=_strict_int_field(data, "ilvl"),
        score=_strict_int_field(data, "score"),
        role=_strict_int_field(data, "role"),
        name=_strict_str_field(data, "name"),
        main_score=_strict_int_field(data, "main_score"),
        rio_profile=_strict_bool(_required_field(data, "rio_profile")),
        rio_best_key=_strict_int_field(data, "rio_best_key"),
        rio_best_dungeon_key=_strict_int_field(data, "rio_best_dungeon_key"),
        rio_timed_at_or_above=_strict_int_field(data, "rio_timed_at_or_above"),
        rio_timed_at_or_above_minus1=_strict_int_field(
            data,
            "rio_timed_at_or_above_minus1",
        ),
        rio_timed_at_or_above_minus2=_strict_int_field(
            data,
            "rio_timed_at_or_above_minus2",
        ),
        rio_completed_at_or_above_minus1=_strict_int_field(
            data,
            "rio_completed_at_or_above_minus1",
        ),
        rio_dungeon_count=_strict_int_field(data, "rio_dungeon_count"),
        rio_dungeons=_list_of_dicts(_required_field(data, "rio_dungeons")),
        member_idx=_strict_int_field(data, "member_idx"),
    )


def _decoded_roster_member_from_dict(data: dict[str, Any]) -> DecodedRosterMember:
    return DecodedRosterMember(
        unit_index=_strict_int_field(data, "unit_index"),
        flags=_strict_int_field(data, "flags"),
        subgroup=_strict_int_field(data, "subgroup"),
        class_id=_strict_int_field(data, "class_id"),
        spec_id=_strict_int_field(data, "spec_id"),
        ilvl=_strict_int_field(data, "ilvl"),
        score=_strict_int_field(data, "score"),
        main_score=_strict_int_field(data, "main_score"),
        rio_profile=_strict_bool(_required_field(data, "rio_profile")),
        rio_best_key=_strict_int_field(data, "rio_best_key"),
        rio_best_dungeon_key=_strict_int_field(data, "rio_best_dungeon_key"),
        rio_timed_at_or_above=_strict_int_field(data, "rio_timed_at_or_above"),
        rio_timed_at_or_above_minus1=_strict_int_field(
            data,
            "rio_timed_at_or_above_minus1",
        ),
        rio_timed_at_or_above_minus2=_strict_int_field(
            data,
            "rio_timed_at_or_above_minus2",
        ),
        rio_completed_at_or_above_minus1=_strict_int_field(
            data,
            "rio_completed_at_or_above_minus1",
        ),
        rio_dungeon_count=_strict_int_field(data, "rio_dungeon_count"),
        role=_strict_int_field(data, "role"),
        name=_strict_str_field(data, "name"),
        rio_dungeons=_list_of_dicts(_required_field(data, "rio_dungeons")),
    )


def _optional_object(
    data: dict[str, Any],
    key: str,
    loader,
):
    value = _required_field(data, key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} payload is not an object")
    return loader(value)


def _list_of_dicts(data: object) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ValueError("expected list")
    if not all(isinstance(item, dict) for item in data):
        raise ValueError("expected list of objects")
    return [dict(item) for item in data]


def _required_field(data: dict[str, Any], key: str) -> object:
    if key not in data:
        raise ValueError(f"{key} is missing")
    return data[key]


def _strict_int_field(data: dict[str, Any], key: str) -> int:
    value = _required_field(data, key)
    if type(value) is int:
        return value
    raise ValueError(f"{key} expected int")


def _strict_str_field(data: dict[str, Any], key: str) -> str:
    value = _required_field(data, key)
    if isinstance(value, str):
        return value
    raise ValueError(f"{key} expected string")


def _strict_bool(value: object) -> bool:
    if type(value) is bool:
        return value
    raise ValueError("expected bool")


def _optional_bool_field(
    data: dict[str, Any],
    key: str,
    *,
    default: bool = False,
) -> bool:
    if key not in data:
        return default
    return _strict_bool(data[key])


def _strict_timestamp_field(data: dict[str, Any], key: str) -> float:
    return _coerce_timestamp(_required_field(data, key))


def _coerce_timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timestamp is not numeric")
    timestamp = float(value)
    if not math.isfinite(timestamp):
        raise ValueError("timestamp is not finite")
    return timestamp
