"""WCL character-rank cache store: TTL freshness, scope covering, eviction.

Pure in-memory operations extracted from CharacterCache (wcl.py). Locking,
generation guards, TTL configuration, and disk persistence stay in
CharacterCache; quota/auth-retry paths in WCLClient are untouched.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences


@dataclass
class _CacheEntry:
    fetched_at: float
    ranks: dict | None = None  # asdict(CharacterRanks)
    raid_boss_details: dict | None = None


def _metric_preference_breadth(metric_preferences: MetricPreferences) -> int:
    return sum(
        (
            metric_preferences.mplus,
            metric_preferences.raid_normal,
            metric_preferences.raid_heroic,
            metric_preferences.raid_mythic,
        )
    )


_ALL_METRIC_PREFERENCE_SCOPES = tuple(
    MetricPreferences(
        mplus=mplus,
        raid_normal=raid_normal,
        raid_heroic=raid_heroic,
        raid_mythic=raid_mythic,
    )
    for mplus in (False, True)
    for raid_normal in (False, True)
    for raid_heroic in (False, True)
    for raid_mythic in (False, True)
)

# A requested scope has at most 15 broader Boolean supersets. Cache reads use
# these exact keys instead of scanning every cached character, which keeps GUI-
# thread lookup cost bounded as the persistent cache grows during a session.
# Read-only table shared by every lookup; do not mutate.
_COVERING_METRIC_SCOPE_KEYS = {
    requested.cache_key(): tuple(
        (stored.cache_key(), _metric_preference_breadth(stored))
        for stored in _ALL_METRIC_PREFERENCE_SCOPES
        if stored != requested and stored.covers(requested)
    )
    for requested in _ALL_METRIC_PREFERENCE_SCOPES
}


def _valid_fetched_at(value: object) -> float | None:
    """Return finite non-negative fetched_at seconds, None for corrupt values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        return None
    return result


def _ttl_for_key(key: str, *, ttl_seconds: float, not_found_ttl_seconds: float) -> float:
    return not_found_ttl_seconds if key.startswith("nf:") else ttl_seconds


def _entry_is_fresh_raw(
    key: str,
    fetched_at: float,
    *,
    now: float,
    ttl_seconds: float,
    not_found_ttl_seconds: float,
) -> bool:
    age = now - fetched_at
    if age < 0:
        return False
    return age <= _ttl_for_key(
        key, ttl_seconds=ttl_seconds, not_found_ttl_seconds=not_found_ttl_seconds
    )


def _entry_is_fresh(
    key: str,
    entry: _CacheEntry,
    *,
    now: float,
    ttl_seconds: float,
    not_found_ttl_seconds: float,
) -> bool:
    fetched_at = _valid_fetched_at(entry.fetched_at)
    if fetched_at is None:
        return False
    return _entry_is_fresh_raw(
        key,
        fetched_at,
        now=now,
        ttl_seconds=ttl_seconds,
        not_found_ttl_seconds=not_found_ttl_seconds,
    )


def _cap_entries_data(data: dict[str, _CacheEntry], max_entries: int) -> None:
    """Evict oldest-fetched_at entries beyond max_entries (H4).

    ``data`` is the live ``CharacterCache`` store; call only with
    ``CharacterCache._lock`` held.
    """
    overflow = len(data) - max_entries
    if overflow <= 0:
        return
    oldest = sorted(data.items(), key=lambda item: (item[1].fetched_at, item[0]))
    for key, _entry in oldest[:overflow]:
        data.pop(key, None)


def _prune_expired_data(
    data: dict[str, _CacheEntry],
    *,
    now: float,
    ttl_seconds: float,
    not_found_ttl_seconds: float,
) -> bool:
    """Drop stale entries. Returns True when anything was removed.

    ``data`` is the live ``CharacterCache`` store; call only with
    ``CharacterCache._lock`` held.
    """
    changed = False
    for key, entry in list(data.items()):
        if not _entry_is_fresh(
            key,
            entry,
            now=now,
            ttl_seconds=ttl_seconds,
            not_found_ttl_seconds=not_found_ttl_seconds,
        ):
            data.pop(key, None)
            changed = True
    return changed


def cache_get(
    entries: dict[str, _CacheEntry],
    *,
    prefix: str,
    not_found_key: str,
    metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
    ttl_seconds: float,
    not_found_ttl_seconds: float,
    now: float | None = None,
) -> tuple[_CacheEntry | None, tuple[_CacheEntry, ...]]:
    """Select the fresh negative + covering-scope candidates for a key prefix.

    Returns (negative_candidate, candidates) ordered exact-first, then newest
    covering scope, mirroring the former CharacterCache._lookup_snapshot core.
    Callers convert the winning entry and apply their own generation guards.
    ``entries`` is the live ``CharacterCache`` store; call only with
    ``CharacterCache._lock`` held.
    """
    current = time.time() if now is None else now
    requested_scope_key = metric_preferences.cache_key()
    exact_key = f"{prefix}:{requested_scope_key}"
    negative_candidate = entries.get(not_found_key)
    if negative_candidate is not None and not _entry_is_fresh(
        not_found_key,
        negative_candidate,
        now=current,
        ttl_seconds=ttl_seconds,
        not_found_ttl_seconds=not_found_ttl_seconds,
    ):
        negative_candidate = None

    candidates: list[tuple[float, int, str, _CacheEntry]] = []
    exact_candidate = entries.get(exact_key)
    if exact_candidate is not None and _entry_is_fresh(
        exact_key,
        exact_candidate,
        now=current,
        ttl_seconds=ttl_seconds,
        not_found_ttl_seconds=not_found_ttl_seconds,
    ):
        candidates.append(
            (
                -exact_candidate.fetched_at,
                0,
                requested_scope_key,
                exact_candidate,
            )
        )

    for scope_key, breadth in _COVERING_METRIC_SCOPE_KEYS[requested_scope_key]:
        stored_key = f"{prefix}:{scope_key}"
        entry = entries.get(stored_key)
        if entry is None or not _entry_is_fresh(
            stored_key,
            entry,
            now=current,
            ttl_seconds=ttl_seconds,
            not_found_ttl_seconds=not_found_ttl_seconds,
        ):
            continue
        candidates.append((-entry.fetched_at, breadth, scope_key, entry))

    candidates.sort()
    return negative_candidate, tuple(candidate[-1] for candidate in candidates)


def cache_put(
    entries: dict[str, _CacheEntry],
    *,
    key: str,
    not_found_key: str,
    entry: _CacheEntry,
    not_found: bool = False,
    sweep_prefixes: tuple[str, ...] = (),
    max_entries: int,
) -> None:
    """Insert one entry, then cap over max_entries.

    not_found inserts evict same-identity keys first (sweep_prefixes);
    positive inserts clear a prior not_found marker instead.
    ``entries`` is the live ``CharacterCache`` store; call only with
    ``CharacterCache._lock`` held.
    """
    if not_found:
        for stored_key in list(entries):
            if stored_key.startswith(sweep_prefixes):
                entries.pop(stored_key, None)
        entries[not_found_key] = entry
    else:
        entries.pop(not_found_key, None)
        entries[key] = entry
    _cap_entries_data(entries, max_entries)


def cache_evict(
    entries: dict[str, _CacheEntry],
    *,
    max_entries: int | None = None,
    now: float | None = None,
    ttl_seconds: float,
    not_found_ttl_seconds: float,
) -> bool:
    """Prune expired entries, then cap over max_entries when given.

    Returns True when anything was removed. max_entries=None prunes only,
    which is what snapshot saves need without touching put-path eviction.
    ``entries`` is the live ``CharacterCache`` store; call only with
    ``CharacterCache._lock`` held.
    """
    current = time.time() if now is None else now
    pruned = _prune_expired_data(
        entries,
        now=current,
        ttl_seconds=ttl_seconds,
        not_found_ttl_seconds=not_found_ttl_seconds,
    )
    if max_entries is None:
        return pruned
    before = len(entries)
    _cap_entries_data(entries, max_entries)
    return pruned or len(entries) < before
