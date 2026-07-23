"""Short-lived restart bridge for the latest live ApplicantScout snapshot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_text
from .constants import REGION_ID_TO_WCL
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
LIVE_SNAPSHOT_CACHE_SCHEMA = 2
LIVE_SNAPSHOT_CACHE_TTL_SECONDS = 90.0
LIVE_SNAPSHOT_RESTORE_GRACE_SECONDS = 30.0
LIVE_SNAPSHOT_CLOSE_TIMEOUT_SECONDS = 2.0
LIVE_SNAPSHOT_CLOSE_RETRY_ATTEMPTS = 2
LIVE_SNAPSHOT_DUPLICATE_SUPPRESSION_SECONDS = 2.0
_UNSCOPED_SOURCE_ID = "unscoped"


@dataclass(frozen=True)
class RestoredLiveSnapshot:
    snapshot: Snapshot
    saved_at: float
    source_id: str


@dataclass(frozen=True)
class _PendingLiveSnapshotCacheOperation:
    kind: str
    source_id: str
    saved_at: float | None = None
    content: dict[str, Any] | None = None


def live_snapshot_cache_path(cache_dir: Path) -> Path:
    return cache_dir / LIVE_SNAPSHOT_CACHE_FILENAME


def live_snapshot_source_identity(screenshots_dir: Path) -> str:
    """Return a stable lexical identity without probing the source filesystem."""
    return os.path.normcase(os.path.abspath(os.fspath(screenshots_dir)))


def _coerce_source_id(source_id: object) -> str:
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("live snapshot source identity is empty")
    return source_id


def _cache_content(snap: Snapshot, source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "snapshot": _snapshot_to_dict(snap),
    }


def _snapshot_context(snap: Snapshot) -> dict[str, Any]:
    return {
        "listing": asdict(snap.listing) if snap.listing is not None else None,
        "player_name": (
            snap.version.player_name if snap.version is not None else None
        ),
    }


def _saved_content_context(content: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = content.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    version = snapshot.get("version")
    player_name = version.get("player_name") if isinstance(version, dict) else None
    return {
        "listing": snapshot.get("listing"),
        "player_name": player_name,
    }


def _snapshot_producer_context(snap: Snapshot) -> dict[str, Any] | None:
    if snap.version is None:
        return None
    return {
        "player_name": snap.version.player_name,
        "region_id": snap.version.region_id,
    }


def _saved_content_producer_context(
    content: dict[str, Any],
) -> dict[str, Any] | None:
    snapshot = content.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    version = snapshot.get("version")
    if not isinstance(version, dict):
        return None
    return {
        "player_name": version.get("player_name"),
        "region_id": version.get("region_id"),
    }


def _producer_contexts_conflict(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> bool:
    if left is None or right is None:
        return False
    left_identity = str(left.get("player_name") or "").strip().casefold()
    right_identity = str(right.get("player_name") or "").strip().casefold()
    left_name, _, left_realm = left_identity.partition("-")
    right_name, _, right_realm = right_identity.partition("-")
    left_region_id = left.get("region_id")
    right_region_id = right.get("region_id")
    left_region = (
        REGION_ID_TO_WCL.get(left_region_id)
        if isinstance(left_region_id, int)
        else None
    )
    right_region = (
        REGION_ID_TO_WCL.get(right_region_id)
        if isinstance(right_region_id, int)
        else None
    )
    return bool(
        left_name
        and right_name
        and (
            left_name != right_name
            or (left_realm and right_realm and left_realm != right_realm)
            or (left_region and right_region and left_region != right_region)
        )
    )


def is_persistable_live_snapshot(snap: Snapshot) -> bool:
    return (
        snap.listing is not None
        and not snap.terminal_clear
        and not snap.lfg_unavailable
        and not snap.roster_unavailable
        and not snap.applicants_unavailable
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
    source_id: str = _UNSCOPED_SOURCE_ID,
    now: float | None = None,
) -> bool:
    try:
        source_id = _coerce_source_id(source_id)
    except ValueError as exc:
        _log.warning("Failed to save live snapshot cache: %s", exc)
        return False
    if _should_clear_cache_for_snapshot(snap):
        return clear_live_snapshot(cache_dir)
    if (
        (snap.applicants_unavailable or snap.roster_unavailable)
        and not snap.lfg_unavailable
        and snap.listing is not None
    ):
        return _clear_live_snapshot_if_context_differs(
            cache_dir,
            _snapshot_context(snap),
            source_id=source_id,
            now=now,
        )
    if snap.lfg_unavailable and snap.version is not None:
        return _clear_live_snapshot_if_producer_differs(
            cache_dir,
            _snapshot_producer_context(snap),
            source_id=source_id,
            now=now,
        )
    if not is_persistable_live_snapshot(snap):
        return True
    try:
        saved_at = _coerce_timestamp(time.time() if now is None else now)
    except ValueError as exc:
        _log.warning("Failed to save live snapshot cache: %s", exc)
        return False
    return _save_live_snapshot_content(
        cache_dir,
        _cache_content(snap, source_id),
        saved_at=saved_at,
    )


def _clear_live_snapshot_if_context_differs(
    cache_dir: Path,
    expected_context: dict[str, Any],
    *,
    source_id: str,
    now: float | None,
) -> bool:
    restored = load_live_snapshot(
        cache_dir,
        expected_source_id=source_id,
        now=now,
    )
    if restored is None:
        return True
    if _snapshot_context(restored.snapshot) == expected_context:
        return True
    return clear_live_snapshot(cache_dir)


def _clear_live_snapshot_if_producer_differs(
    cache_dir: Path,
    incoming_producer: dict[str, Any] | None,
    *,
    source_id: str,
    now: float | None,
) -> bool:
    restored = load_live_snapshot(
        cache_dir,
        expected_source_id=source_id,
        now=now,
    )
    if restored is None:
        return True
    if not _producer_contexts_conflict(
        _snapshot_producer_context(restored.snapshot),
        incoming_producer,
    ):
        return True
    return clear_live_snapshot(cache_dir)


def _save_live_snapshot_content(
    cache_dir: Path,
    content: dict[str, Any],
    *,
    saved_at: float,
) -> bool:
    payload = {
        "schema": LIVE_SNAPSHOT_CACHE_SCHEMA,
        "saved_at": saved_at,
        **content,
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
        source_id: str = _UNSCOPED_SOURCE_ID,
        defer_saves: bool = True,
        save_debounce_seconds: float = 0.25,
        close_timeout_seconds: float = LIVE_SNAPSHOT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._source_id = _coerce_source_id(source_id)
        self._defer_saves = bool(defer_saves)
        self._save_debounce_seconds = max(0.0, float(save_debounce_seconds))
        self._close_timeout_seconds = max(0.0, float(close_timeout_seconds))
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: _PendingLiveSnapshotCacheOperation | None = None
        self._timer: threading.Timer | None = None
        self._closed = False
        self._generation = 0
        self._last_saved_content: dict[str, Any] | None = None
        self._last_saved_at: float | None = None

    @property
    def source_id(self) -> str:
        with self._lock:
            return self._source_id

    def rebind_source(self, source_id: str) -> bool:
        """Commit a new source after draining and retiring old-source writes."""
        next_source_id = _coerce_source_id(source_id)
        with self._write_lock:
            with self._lock:
                if self._closed or next_source_id == self._source_id:
                    return False
                self._source_id = next_source_id
                self._generation += 1
                self._cancel_timer_locked()
                self._pending = None
                self._last_saved_content = None
                self._last_saved_at = None
            if not clear_live_snapshot(self._cache_dir):
                _log.warning(
                    "Could not remove the previous source's live snapshot cache."
                )
        return True

    def submit(self, snap: Snapshot, *, now: float | None = None) -> None:
        with self._lock:
            source_id = self._source_id
        operation = _operation_for_snapshot(snap, source_id=source_id, now=now)
        if operation is None:
            return
        flush_now = False
        with self._lock:
            if self._closed:
                return
            if operation.source_id != self._source_id:
                return
            if self._preserves_pending_context_locked(operation):
                return
            if self._is_recent_duplicate_locked(operation):
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
            if succeeded:
                self._record_successful_operation(operation, generation)
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
            self._last_saved_content = None
            self._last_saved_at = None
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
                    self._record_successful_operation(operation, generation)
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
        if operation.kind == "clear_if_context_differs" and operation.content is not None:
            return _clear_live_snapshot_if_context_differs(
                self._cache_dir,
                operation.content,
                source_id=operation.source_id,
                now=operation.saved_at,
            )
        if operation.kind == "clear_if_producer_differs":
            return _clear_live_snapshot_if_producer_differs(
                self._cache_dir,
                operation.content,
                source_id=operation.source_id,
                now=operation.saved_at,
            )
        if (
            operation.kind == "save"
            and operation.content is not None
            and operation.saved_at is not None
        ):
            return _save_live_snapshot_content(
                self._cache_dir,
                operation.content,
                saved_at=operation.saved_at,
            )
        return True

    def _preserves_pending_context_locked(
        self,
        operation: _PendingLiveSnapshotCacheOperation,
    ) -> bool:
        if operation.kind not in {
            "clear_if_context_differs",
            "clear_if_producer_differs",
        }:
            return False
        if self._pending is not None and self._pending.kind == "clear":
            return True
        if operation.kind == "clear_if_producer_differs":
            for candidate in (self._pending,):
                if (
                    candidate is not None
                    and candidate.kind == "save"
                    and candidate.content is not None
                    and candidate.source_id == operation.source_id
                    and not _producer_contexts_conflict(
                        _saved_content_producer_context(candidate.content),
                        operation.content,
                    )
                ):
                    return True
            return bool(
                self._pending is None
                and self._last_saved_content is not None
                and not _producer_contexts_conflict(
                    _saved_content_producer_context(self._last_saved_content),
                    operation.content,
                )
            )
        if operation.content is None:
            return False
        for candidate in (self._pending,):
            if (
                candidate is not None
                and candidate.kind == "save"
                and candidate.content is not None
                and candidate.source_id == operation.source_id
                and _saved_content_context(candidate.content) == operation.content
            ):
                return True
        return (
            self._pending is None
            and self._last_saved_content is not None
            and _saved_content_context(self._last_saved_content) == operation.content
        )

    def _is_recent_duplicate_locked(
        self,
        operation: _PendingLiveSnapshotCacheOperation,
    ) -> bool:
        # Caller must hold self._lock. A pending operation is never skipped:
        # replacing it preserves the newest timestamp and lets clear/save order
        # continue to follow the latest accepted snapshot.
        if self._pending is not None or operation.kind != "save":
            return False
        if operation.content is None or operation.saved_at is None:
            return False
        if self._last_saved_content != operation.content:
            return False
        if self._last_saved_at is None:
            return False
        elapsed = operation.saved_at - self._last_saved_at
        return 0.0 <= elapsed <= LIVE_SNAPSHOT_DUPLICATE_SUPPRESSION_SECONDS

    def _record_successful_operation(
        self,
        operation: _PendingLiveSnapshotCacheOperation,
        generation: int,
    ) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if operation.kind == "save":
                self._last_saved_content = operation.content
                self._last_saved_at = operation.saved_at
                return
            self._last_saved_content = None
            self._last_saved_at = None


def _operation_for_snapshot(
    snap: Snapshot,
    *,
    source_id: str,
    now: float | None,
) -> _PendingLiveSnapshotCacheOperation | None:
    if _should_clear_cache_for_snapshot(snap):
        return _PendingLiveSnapshotCacheOperation("clear", source_id)
    if (
        (snap.applicants_unavailable or snap.roster_unavailable)
        and not snap.lfg_unavailable
        and snap.listing is not None
    ):
        try:
            observed_at = _coerce_timestamp(time.time() if now is None else now)
        except ValueError as exc:
            _log.warning(
                "Ignoring live snapshot cache context check with invalid timestamp: %s",
                exc,
            )
            return None
        return _PendingLiveSnapshotCacheOperation(
            "clear_if_context_differs",
            source_id,
            saved_at=observed_at,
            content=_snapshot_context(snap),
        )
    if snap.lfg_unavailable and snap.version is not None:
        try:
            observed_at = _coerce_timestamp(time.time() if now is None else now)
        except ValueError as exc:
            _log.warning(
                "Ignoring live snapshot cache producer check with invalid timestamp: %s",
                exc,
            )
            return None
        return _PendingLiveSnapshotCacheOperation(
            "clear_if_producer_differs",
            source_id,
            saved_at=observed_at,
            content=_snapshot_producer_context(snap),
        )
    if not is_persistable_live_snapshot(snap):
        return None
    try:
        saved_at = _coerce_timestamp(time.time() if now is None else now)
    except ValueError as exc:
        _log.warning("Ignoring live snapshot cache save with invalid timestamp: %s", exc)
        return None
    return _PendingLiveSnapshotCacheOperation(
        "save",
        source_id,
        saved_at=saved_at,
        content=_cache_content(snap, source_id),
    )


def load_live_snapshot(
    cache_dir: Path,
    *,
    expected_source_id: str | None = None,
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
        source_id = _coerce_source_id(_required_field(data, "source_id"))
        if (
            expected_source_id is not None
            and source_id != _coerce_source_id(expected_source_id)
        ):
            clear_live_snapshot(cache_dir)
            return None
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
        return RestoredLiveSnapshot(
            snapshot=snap,
            saved_at=saved_at,
            source_id=source_id,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _log.warning("Discarding invalid live snapshot cache %s: %s", path, exc)
        clear_live_snapshot(cache_dir)
        return None


def clear_live_snapshot_if_saved_at(
    cache_dir: Path,
    expected_saved_at: float,
    *,
    expected_source_id: str | None = None,
) -> bool:
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
        source_id = _coerce_source_id(_required_field(data, "source_id"))
        saved_at = _strict_timestamp_field(data, "saved_at")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _log.warning("Discarding invalid live snapshot cache %s: %s", path, exc)
        return clear_live_snapshot(cache_dir)
    if saved_at != expected:
        return False
    if (
        expected_source_id is not None
        and source_id != _coerce_source_id(expected_source_id)
    ):
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
        "applicants_unavailable": bool(snap.applicants_unavailable),
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
        applicants_unavailable=_optional_bool_field(
            data,
            "applicants_unavailable",
        ),
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
