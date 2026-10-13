"""WCL character-rank cache store: TTL freshness, scope covering, eviction.

Pure in-memory operations extracted from CharacterCache (wcl.py). Locking,
generation guards, TTL configuration, and disk persistence stay in
CharacterCache; quota/auth-retry paths in WCLClient are untouched.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace

from .constants import CURRENT_RAID_ENCOUNTERS
from .metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences
from .wcl_availability import METRIC_SCOPES, RAID_DETAIL_SCOPES, normalize_availability


@dataclass
class _CacheEntry:
    fetched_at: float
    ranks: dict | None = None  # asdict(CharacterRanks)
    raid_boss_details: dict | None = None
    # Session-only ordering; loaded entries predate every current request.
    publication_epoch: int = 0
    raid_boss_availability: dict[str, str] | None = None
    raid_boss_publication_epochs: dict[str, int] | None = None


def raid_detail_scope_epoch(entry: _CacheEntry, difficulty: str) -> int:
    epochs = entry.raid_boss_publication_epochs or {}
    return epochs.get(difficulty, entry.publication_epoch)


def record_publication_proofs(
    proofs: dict[tuple[str, str], int], *, identity: str, scopes: set[str],
    publication_epoch: int, max_entries: int, retired_floor: int,
) -> int:
    """Retain bounded session order independently of cache row eviction and TTL."""
    for scope in scopes:
        proofs[(identity, scope)] = publication_epoch
    while len(proofs) > max_entries:
        oldest = min(proofs, key=lambda key: proofs[key])
        retired_floor = max(retired_floor, proofs.pop(oldest))
    return retired_floor


def sanitize_raid_evidence(value: object) -> dict[str, dict]:
    """Keep observed coverage and complete repeat counts; invalid fields stay unknown."""
    if not isinstance(value, dict):
        return {}
    total_bosses = len(CURRENT_RAID_ENCOUNTERS)
    result: dict[str, dict] = {}
    for difficulty in ("N", "H", "M"):
        raw = value.get(difficulty)
        if not isinstance(raw, dict):
            continue
        boss_count = raw.get("boss_count")
        raw_total = raw.get("total_bosses")
        if (
            isinstance(boss_count, bool)
            or not isinstance(boss_count, int)
            or not 0 <= boss_count <= total_bosses
            or isinstance(raw_total, bool)
            or not isinstance(raw_total, int)
            or raw_total != total_bosses
        ):
            continue
        row: dict = {
            "boss_count": boss_count,
            "total_bosses": total_bosses,
            "sample_known": False,
        }
        kill_count = raw.get("kill_count")
        if (
            raw.get("sample_known") is True
            and boss_count > 0
            and not isinstance(kill_count, bool)
            and isinstance(kill_count, int)
            and kill_count.bit_length() <= 63
            and kill_count >= boss_count
        ):
            row["kill_count"] = kill_count
            row["sample_known"] = True
        result[difficulty] = row
    return result


def retract_private_raid_details(
    data: dict[str, _CacheEntry],
    *,
    identity_prefix: str,
    difficulties: set[str],
    publication_epoch: int,
    expected_publication_epoch: int | None,
) -> None:
    """Mark older manual scopes private while preserving other evidence and its TTL."""
    prefix = f"rb:{identity_prefix}:"
    for key, entry in list(data.items()):
        if (
            not key.startswith(prefix)
            or not isinstance(entry.raid_boss_details, dict)
        ):
            continue
        availability = normalize_availability(entry.raid_boss_availability, scopes=RAID_DETAIL_SCOPES)
        affected = difficulties.intersection(set(entry.raid_boss_details) | set(availability))
        if expected_publication_epoch is not None:
            affected = {
                difficulty for difficulty in affected
                if raid_detail_scope_epoch(entry, difficulty) <= expected_publication_epoch
            }
        if not affected:
            continue
        rows = dict(entry.raid_boss_details)
        epochs = {difficulty: raid_detail_scope_epoch(entry, difficulty) for difficulty in availability}
        for difficulty in affected:
            rows.pop(difficulty, None)
            availability[difficulty] = "restricted"
            epochs[difficulty] = publication_epoch
        data[key] = replace(
            entry, raid_boss_details=rows, raid_boss_availability=availability,
            publication_epoch=publication_epoch,
            raid_boss_publication_epochs=epochs,
        )


def retract_private_rank_summaries(
    data: dict[str, _CacheEntry],
    *,
    identity_prefix: str,
    scopes: set[str],
    expected_publication_epoch: int | None,
) -> None:
    """Retract older scalar scopes without renewing other averages or their TTL."""
    difficulties = {"raid_normal": "N", "raid_heroic": "H", "raid_mythic": "M"}
    for stored in _ALL_METRIC_PREFERENCE_SCOPES:
        key = f"{identity_prefix}:{stored.cache_key()}"
        entry = data.get(key)
        if (
            entry is None
            or not isinstance(entry.ranks, dict)
            or expected_publication_epoch is not None
            and entry.publication_epoch > expected_publication_epoch
        ):
            continue
        affected = {scope for scope in scopes if getattr(stored, scope)}
        if not affected:
            continue
        ranks = dict(entry.ranks)
        availability = normalize_availability(ranks.get("availability"))
        evidence = sanitize_raid_evidence(ranks.get("raid_evidence"))
        for scope in affected:
            availability[scope] = "restricted"
            if scope == "mplus":
                for metric in ("dps", "hps"):
                    field_name = f"mplus_{metric}"
                    ranks[field_name] = ranks[f"{field_name}_median"] = None
                    ranks[f"{field_name}_breakdown"] = []
            else:
                ranks[scope] = ranks[f"{scope}_median"] = None
                evidence.pop(difficulties[scope], None)
        ranks["availability"] = availability
        ranks["raid_evidence"] = evidence
        # Keep the original source clock too: hiding H does not make M newer.
        data[key] = replace(entry, ranks=ranks)


def newer_public_raid_detail_evidence(
    data: dict[str, _CacheEntry],
    *,
    identity_prefix: str,
    difficulties: set[str],
    expected_publication_epoch: int,
) -> bool:
    """An obsolete private response cannot hide newer overlapping public detail."""
    prefix = f"rb:{identity_prefix}:"
    for key, entry in data.items():
        if not key.startswith(prefix):
            continue
        availability = normalize_availability(entry.raid_boss_availability, scopes=RAID_DETAIL_SCOPES)
        if any(
            availability.get(difficulty) in {"available", "partial"}
            and raid_detail_scope_epoch(entry, difficulty) > expected_publication_epoch
            for difficulty in difficulties
        ):
            return True
    return False


def newer_raid_detail_evidence(
    data: dict[str, _CacheEntry],
    *,
    identity_prefix: str,
    difficulty: str,
    expected_publication_epoch: int,
) -> bool:
    prefix = f"rb:{identity_prefix}:"
    return any(
        key.startswith(prefix)
        and difficulty in normalize_availability(entry.raid_boss_availability, scopes=RAID_DETAIL_SCOPES)
        and raid_detail_scope_epoch(entry, difficulty) > expected_publication_epoch
        for key, entry in data.items()
    )


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


def newer_rank_scope_evidence(
    data: dict[str, _CacheEntry],
    *,
    identity_prefix: str,
    metric_preferences: MetricPreferences,
    expected_publication_epoch: int,
) -> bool:
    """Check at most 16 query shapes for a newer overlapping summary response."""
    for stored in _ALL_METRIC_PREFERENCE_SCOPES:
        overlaps = any(getattr(stored, scope) and getattr(metric_preferences, scope) for scope in METRIC_SCOPES)
        if not overlaps:
            continue
        entry = data.get(f"{identity_prefix}:{stored.cache_key()}")
        if entry is not None and entry.publication_epoch > expected_publication_epoch:
            return True
    return False

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


def raid_detail_candidates(
    entries: dict[str, _CacheEntry], *, prefix: str, ttl_seconds: float,
    not_found_ttl_seconds: float, now: float, metric_preferences: MetricPreferences,
) -> tuple[_CacheEntry, ...]:
    """Read the bounded preference keys; each may supply independent difficulties."""
    candidates: list[_CacheEntry] = []
    for preferences in _ALL_METRIC_PREFERENCE_SCOPES:
        key = f"{prefix}:{preferences.cache_key()}"
        entry = entries.get(key)
        if entry is not None and _entry_is_fresh(
            key, entry, now=now, ttl_seconds=ttl_seconds,
            not_found_ttl_seconds=not_found_ttl_seconds,
        ):
            if preferences == metric_preferences:
                candidates.insert(0, entry)
            else:
                candidates.append(entry)
    return tuple(candidates)


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
