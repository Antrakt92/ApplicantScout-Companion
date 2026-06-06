"""Short-lived restart bridge for the latest live ApplicantScout snapshot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
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
)


_log = logging.getLogger("applicant_scout.live_snapshot_cache")

LIVE_SNAPSHOT_CACHE_FILENAME = "last-live-snapshot.json"
LIVE_SNAPSHOT_CACHE_SCHEMA = 1
LIVE_SNAPSHOT_CACHE_TTL_SECONDS = 90.0
LIVE_SNAPSHOT_RESTORE_GRACE_SECONDS = 30.0


@dataclass(frozen=True)
class RestoredLiveSnapshot:
    snapshot: Snapshot
    saved_at: float


def live_snapshot_cache_path(cache_dir: Path) -> Path:
    return cache_dir / LIVE_SNAPSHOT_CACHE_FILENAME


def is_persistable_live_snapshot(snap: Snapshot) -> bool:
    return (
        snap.listing is not None
        and not snap.terminal_clear
        and not snap.lfg_unavailable
    )


def _should_clear_cache_for_snapshot(snap: Snapshot) -> bool:
    if snap.terminal_clear:
        return True
    return snap.listing is None and not snap.lfg_unavailable


def clear_live_snapshot(cache_dir: Path) -> None:
    path = live_snapshot_cache_path(cache_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log.warning("Failed to remove live snapshot cache %s: %s", path, exc)


def save_live_snapshot(
    cache_dir: Path,
    snap: Snapshot,
    *,
    now: float | None = None,
) -> None:
    if _should_clear_cache_for_snapshot(snap):
        clear_live_snapshot(cache_dir)
        return
    if not is_persistable_live_snapshot(snap):
        return
    saved_at = time.time() if now is None else float(now)
    payload = {
        "schema": LIVE_SNAPSHOT_CACHE_SCHEMA,
        "saved_at": saved_at,
        "snapshot": _snapshot_to_dict(snap),
    }
    path = live_snapshot_cache_path(cache_dir)
    try:
        atomic_write_text(
            path,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            private=True,
        )
    except OSError as exc:
        _log.warning("Failed to save live snapshot cache %s: %s", path, exc)


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
    except OSError as exc:
        _log.warning("Failed to read live snapshot cache %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("cache root is not an object")
        if data.get("schema") != LIVE_SNAPSHOT_CACHE_SCHEMA:
            raise ValueError("unsupported cache schema")
        saved_at = data.get("saved_at")
        if isinstance(saved_at, bool) or not isinstance(saved_at, (int, float)):
            raise ValueError("saved_at is not numeric")
        current_time = time.time() if now is None else float(now)
        if current_time - float(saved_at) > LIVE_SNAPSHOT_CACHE_TTL_SECONDS:
            clear_live_snapshot(cache_dir)
            return None
        snapshot_data = data.get("snapshot")
        if not isinstance(snapshot_data, dict):
            raise ValueError("snapshot is not an object")
        snap = _snapshot_from_dict(snapshot_data)
        if not is_persistable_live_snapshot(snap):
            raise ValueError("cached snapshot is not a live listing snapshot")
        return RestoredLiveSnapshot(snapshot=snap, saved_at=float(saved_at))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _log.warning("Discarding invalid live snapshot cache %s: %s", path, exc)
        clear_live_snapshot(cache_dir)
        return None


def _snapshot_to_dict(snap: Snapshot) -> dict[str, Any]:
    return {
        "listing": asdict(snap.listing) if snap.listing is not None else None,
        "version": asdict(snap.version) if snap.version is not None else None,
        "leader_key": asdict(snap.leader_key) if snap.leader_key is not None else None,
        "applicants": [asdict(applicant) for applicant in snap.applicants],
        "roster": [asdict(member) for member in snap.roster],
        "terminal_clear": bool(snap.terminal_clear),
        "lfg_unavailable": bool(snap.lfg_unavailable),
    }


def _snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    return Snapshot(
        listing=_optional_dataclass(DecodedListing, data.get("listing")),
        version=_optional_dataclass(DecodedVersion, data.get("version")),
        leader_key=_optional_dataclass(DecodedLeaderKey, data.get("leader_key")),
        applicants=[
            _required_dataclass(DecodedApplicant, item)
            for item in _list_of_dicts(data.get("applicants"))
        ],
        roster=[
            _required_dataclass(DecodedRosterMember, item)
            for item in _list_of_dicts(data.get("roster"))
        ],
        terminal_clear=_strict_bool(data.get("terminal_clear")),
        lfg_unavailable=_strict_bool(data.get("lfg_unavailable")),
        source=None,
    )


def _optional_dataclass(cls, data: object):
    if data is None:
        return None
    return _required_dataclass(cls, data)


def _required_dataclass(cls, data: object):
    if not isinstance(data, dict):
        raise ValueError(f"{cls.__name__} payload is not an object")
    return cls(**data)


def _list_of_dicts(data: object) -> list[dict[str, Any]]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("expected list")
    if not all(isinstance(item, dict) for item in data):
        raise ValueError("expected list of objects")
    return data


def _strict_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("expected bool")
