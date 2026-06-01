"""Read installed RaiderIO addon DB files for local M+ and raid progress."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from .atomic_io import apply_private_directory_mode, apply_private_file_mode


_log = logging.getLogger("applicant_scout.raiderio_local")

_REGION_FILE_TOKENS = {
    "US": "us",
    "KR": "kr",
    "EU": "eu",
    "TW": "tw",
    "CN": "cn",
}

_DUNGEON_LEVELS_FIELD = 10
_NEGATIVE_CACHE_TTL_SECONDS = 30.0
_LOOKUP_PAYLOAD_CACHE_DIR = "raiderio-local"
_LOOKUP_PAYLOAD_CACHE_SUFFIX = ".payload.bin"
_LOOKUP_PAYLOAD_CACHE_LOCK = threading.Lock()
_LOOKUP_PAYLOAD_CACHE_GENERATIONS: dict[Path, int] = {}
_RegionDBFingerprint = tuple[tuple[str, bool, int, int], ...]


def _lookup_payload_cache_key(cache_dir: Path) -> Path:
    return Path(cache_dir).resolve(strict=False)


def _lookup_payload_cache_root(cache_dir: Path) -> Path:
    return Path(cache_dir) / _LOOKUP_PAYLOAD_CACHE_DIR


def _lookup_payload_cache_generation(cache_dir: Path | None) -> int | None:
    if cache_dir is None:
        return None
    key = _lookup_payload_cache_key(cache_dir)
    with _LOOKUP_PAYLOAD_CACHE_LOCK:
        return _LOOKUP_PAYLOAD_CACHE_GENERATIONS.get(key, 0)


def _lookup_payload_cache_generation_matches_locked(
    cache_dir: Path | None,
    expected_generation: int | None,
) -> bool:
    if cache_dir is None or expected_generation is None:
        return True
    key = _lookup_payload_cache_key(cache_dir)
    return _LOOKUP_PAYLOAD_CACHE_GENERATIONS.get(key, 0) == expected_generation


def clear_lookup_payload_cache(cache_dir: Path) -> None:
    payload_cache_dir = _lookup_payload_cache_root(cache_dir)
    key = _lookup_payload_cache_key(payload_cache_dir)
    with _LOOKUP_PAYLOAD_CACHE_LOCK:
        _LOOKUP_PAYLOAD_CACHE_GENERATIONS[key] = (
            _LOOKUP_PAYLOAD_CACHE_GENERATIONS.get(key, 0) + 1
        )
        try:
            shutil.rmtree(payload_cache_dir)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class RaiderIOLocalProfile:
    current_score: int
    dungeons: list[dict]
    raid_progress: dict[str, dict]
    has_mplus_profile: bool = True


@dataclass(frozen=True)
class _ProviderMeta:
    record_size: int
    encoding_order: tuple[int, ...]


@dataclass(frozen=True)
class _RaidInfo:
    name: str
    short_name: str
    boss_count: int


@dataclass(frozen=True)
class _RegionCacheEntry:
    db: _RegionDB | None
    fingerprint: _RegionDBFingerprint
    cached_at: float
    refresh_failed: bool = False


class RaiderIOLocalReader:
    """Lazy reader for RaiderIO's generated local profile databases."""

    def __init__(self, retail_root: Path, *, cache_dir: Path | None = None):
        self._retail_root = Path(retail_root)
        self._payload_cache_dir = (
            Path(cache_dir) / _LOOKUP_PAYLOAD_CACHE_DIR
            if cache_dir is not None
            else None
        )
        self._cache: dict[str, _RegionCacheEntry] = {}
        self._loading: set[str] = set()
        self._load_callbacks: dict[str, list[Callable[[], None]]] = {}
        self._lock = threading.Lock()

    def lookup_profile(
        self, name: str, realm: str, region: str | None, *, allow_load: bool = True
    ) -> RaiderIOLocalProfile | None:
        token = _REGION_FILE_TOKENS.get((region or "").upper())
        if not token:
            return None
        if allow_load:
            db = self._region_db(token)
        else:
            with self._lock:
                entry = self._cache.get(token)
                db = entry.db if entry is not None else None
        if db is None:
            return None
        return db.lookup_profile(name, realm)

    def preload_region_async(
        self, region: str | None, *, on_loaded: Callable[[], None] | None = None
    ) -> None:
        token = _REGION_FILE_TOKENS.get((region or "").upper())
        if not token:
            return
        with self._lock:
            if token in self._loading:
                if on_loaded is not None:
                    self._load_callbacks.setdefault(token, []).append(on_loaded)
                return
            else:
                if on_loaded is not None:
                    self._load_callbacks.setdefault(token, []).append(on_loaded)
                self._loading.add(token)

        def _worker() -> None:
            try:
                self._region_db(token)
            finally:
                with self._lock:
                    self._loading.discard(token)
                    callbacks = self._load_callbacks.pop(token, [])
                for callback in callbacks:
                    try:
                        callback()
                    except Exception:  # noqa: BLE001
                        _log.exception(
                            "RaiderIO local preload callback failed for %s", token
                        )

        threading.Thread(
            target=_worker,
            name=f"RaiderIOLocalLoad-{token}",
            daemon=True,
        ).start()

    def _region_db(self, token: str) -> _RegionDB | None:
        now = time.monotonic()
        fingerprint = _region_db_fingerprint(self._retail_root, token)
        with self._lock:
            entry = self._cache.get(token)
            if entry is not None and not _cache_entry_is_stale(
                entry, fingerprint, now
            ):
                return entry.db
            previous_entry = entry
        try:
            loaded = _RegionDB.load(
                self._retail_root,
                token,
                payload_cache_dir=self._payload_cache_dir,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("RaiderIO local DB unavailable for %s: %s", token, exc)
            loaded = None
        with self._lock:
            if loaded is None and previous_entry is not None and previous_entry.db is not None:
                current_entry = self._cache.get(token)
                if (
                    current_entry is not None
                    and current_entry is not previous_entry
                    and current_entry.db is not None
                    and not current_entry.refresh_failed
                ):
                    return current_entry.db
                self._cache[token] = _RegionCacheEntry(
                    db=previous_entry.db,
                    fingerprint=fingerprint,
                    cached_at=now,
                    refresh_failed=True,
                )
                return previous_entry.db
            self._cache[token] = _RegionCacheEntry(
                db=loaded,
                fingerprint=fingerprint,
                cached_at=now,
            )
        return loaded


class _RegionDB:
    def __init__(
        self,
        *,
        mplus_characters_path: Path | None,
        mplus_lookup_payload: bytes | None,
        mplus_meta: _ProviderMeta | None,
        dungeons: list[str],
        raid_characters_path: Path | None,
        raid_lookup_payload: bytes | None,
        raid_meta: _ProviderMeta | None,
        current_raids: list[_RaidInfo],
        previous_raids: list[_RaidInfo],
        mplus_realm_cache: dict[str, tuple[int, list[str]] | None] | None = None,
        raid_realm_cache: dict[str, tuple[int, list[str]] | None] | None = None,
    ):
        self._mplus_characters_path = mplus_characters_path
        self._mplus_lookup_payload = mplus_lookup_payload
        self._mplus_meta = mplus_meta
        self._dungeons = dungeons
        self._raid_characters_path = raid_characters_path
        self._raid_lookup_payload = raid_lookup_payload
        self._raid_meta = raid_meta
        self._current_raids = current_raids
        self._previous_raids = previous_raids
        self._mplus_realm_cache = mplus_realm_cache or {}
        self._raid_realm_cache = raid_realm_cache or {}

    @classmethod
    def load(
        cls,
        retail_root: Path,
        token: str,
        *,
        payload_cache_dir: Path | None = None,
    ) -> _RegionDB | None:
        payload_cache_generation = _lookup_payload_cache_generation(payload_cache_dir)
        db_root = retail_root / "Interface" / "AddOns" / "RaiderIO" / "db"
        mplus_characters_path = db_root / f"db_mythicplus_{token}_characters.lua"
        mplus_lookup_path = db_root / f"db_mythicplus_{token}_lookup.lua"
        raid_characters_path = db_root / f"db_raiding_{token}_characters.lua"
        raid_lookup_path = db_root / f"db_raiding_{token}_lookup.lua"
        dungeons_path = db_root / "db_dungeons.lua"
        has_mplus = (
            mplus_characters_path.is_file()
            and mplus_lookup_path.is_file()
            and dungeons_path.is_file()
        )
        has_raid = raid_characters_path.is_file() and raid_lookup_path.is_file()
        if not has_mplus and not has_raid:
            return None
        dungeons: list[str] = []
        mplus_meta: _ProviderMeta | None = None
        mplus_lookup_payload: bytes | None = None
        mplus_realm_cache: dict[str, tuple[int, list[str]] | None] = {}
        if has_mplus:
            try:
                mplus_characters_text = mplus_characters_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                lookup_text = mplus_lookup_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                mplus_meta = _parse_provider_meta(lookup_text)
                dungeons = _parse_dungeon_names(
                    dungeons_path.read_text(encoding="utf-8")
                )
                _validate_encoding_plan(mplus_meta, len(dungeons))
                mplus_lookup_payload = _parse_lookup_payload_for_character_layout(
                    lookup_text,
                    source_path=mplus_lookup_path,
                    payload_cache_dir=payload_cache_dir,
                    payload_cache_generation=payload_cache_generation,
                    characters_path=mplus_characters_path,
                    characters_text=mplus_characters_text,
                    meta=mplus_meta,
                )
                mplus_realm_cache = _parse_all_realm_data(mplus_characters_text)
            except (OSError, ValueError) as exc:
                _log.warning(
                    "could not load RaiderIO local M+ DB for %s: %s", token, exc
                )
                dungeons = []
                mplus_meta = None
                mplus_lookup_payload = None
                mplus_realm_cache = {}
                has_mplus = False
        raid_meta: _ProviderMeta | None = None
        raid_lookup_payload: bytes | None = None
        current_raids: list[_RaidInfo] = []
        previous_raids: list[_RaidInfo] = []
        raid_realm_cache: dict[str, tuple[int, list[str]] | None] = {}
        if has_raid:
            try:
                raid_characters_text = raid_characters_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                raid_lookup_text = raid_lookup_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                raid_meta = _parse_provider_meta(raid_lookup_text)
                current_raids = _parse_provider_raids(raid_lookup_text, "currentRaids")
                previous_raids = _parse_provider_raids(
                    raid_lookup_text, "previousRaids"
                )
                _validate_raid_encoding_plan(raid_meta, current_raids, previous_raids)
                raid_lookup_payload = _parse_lookup_payload_for_character_layout(
                    raid_lookup_text,
                    source_path=raid_lookup_path,
                    payload_cache_dir=payload_cache_dir,
                    payload_cache_generation=payload_cache_generation,
                    characters_path=raid_characters_path,
                    characters_text=raid_characters_text,
                    meta=raid_meta,
                )
                raid_realm_cache = _parse_all_realm_data(raid_characters_text)
            except (OSError, ValueError) as exc:
                _log.warning(
                    "could not load RaiderIO local raid DB for %s: %s", token, exc
                )
                raid_meta = None
                raid_lookup_payload = None
                current_raids = []
                previous_raids = []
                raid_realm_cache = {}
                has_raid = False
        if not has_mplus and not has_raid:
            return None
        return cls(
            mplus_characters_path=mplus_characters_path if has_mplus else None,
            mplus_lookup_payload=mplus_lookup_payload,
            mplus_meta=mplus_meta,
            dungeons=dungeons,
            raid_characters_path=raid_characters_path if has_raid else None,
            raid_lookup_payload=raid_lookup_payload,
            raid_meta=raid_meta,
            current_raids=current_raids,
            previous_raids=previous_raids,
            mplus_realm_cache=mplus_realm_cache,
            raid_realm_cache=raid_realm_cache,
        )

    def lookup_profile(self, name: str, realm: str) -> RaiderIOLocalProfile | None:
        name = name.strip()
        realm = realm.strip()
        if not name or not realm:
            return None
        mplus_profile: RaiderIOLocalProfile | None = None
        if (
            self._mplus_characters_path is not None
            and self._mplus_lookup_payload is not None
            and self._mplus_meta is not None
        ):
            record = self._record_for(
                self._mplus_characters_path,
                self._mplus_lookup_payload,
                self._mplus_meta,
                self._mplus_realm_cache,
                name,
                realm,
            )
            if record is not None:
                try:
                    mplus_profile = _decode_profile(
                        record, self._mplus_meta.encoding_order, self._dungeons
                    )
                except ValueError as exc:
                    _log.warning(
                        "could not decode RaiderIO local profile for %s-%s: %s",
                        name,
                        realm,
                        exc,
                    )
        raid_progress: dict[str, dict] = {}
        if (
            self._raid_characters_path is not None
            and self._raid_lookup_payload is not None
            and self._raid_meta is not None
        ):
            record = self._record_for(
                self._raid_characters_path,
                self._raid_lookup_payload,
                self._raid_meta,
                self._raid_realm_cache,
                name,
                realm,
            )
            if record is not None:
                try:
                    raid_progress = _decode_raid_progress(
                        record,
                        self._raid_meta.encoding_order,
                        self._current_raids,
                        self._previous_raids,
                    )
                except ValueError as exc:
                    _log.warning(
                        "could not decode RaiderIO local raid profile for %s-%s: %s",
                        name,
                        realm,
                        exc,
                    )
        if mplus_profile is None and not raid_progress:
            return None
        if mplus_profile is None:
            return RaiderIOLocalProfile(
                current_score=0,
                dungeons=[],
                raid_progress=raid_progress,
                has_mplus_profile=False,
            )
        return RaiderIOLocalProfile(
            current_score=mplus_profile.current_score,
            dungeons=mplus_profile.dungeons,
            raid_progress=raid_progress,
        )

    def _record_for(
        self,
        characters_path: Path,
        lookup_payload: bytes,
        meta: _ProviderMeta,
        realm_cache: dict[str, tuple[int, list[str]] | None],
        name: str,
        realm: str,
    ) -> bytes | None:
        realm_data = self._realm_data(characters_path, realm_cache, realm)
        if realm_data is None:
            return None
        base_offset, names = realm_data
        try:
            name_index = next(
                idx
                for idx, candidate in enumerate(names)
                if candidate.casefold() == name.casefold()
            )
        except StopIteration:
            return None
        record_offset = base_offset + name_index * meta.record_size
        record = lookup_payload[record_offset : record_offset + meta.record_size]
        if len(record) != meta.record_size:
            return None
        return record

    def _realm_data(
        self,
        characters_path: Path,
        realm_cache: dict[str, tuple[int, list[str]] | None],
        realm: str,
    ) -> tuple[int, list[str]] | None:
        cache_key = _realm_lookup_key(realm)
        return realm_cache.get(cache_key)


def _cache_entry_is_stale(
    entry: _RegionCacheEntry,
    fingerprint: _RegionDBFingerprint,
    now: float,
) -> bool:
    if entry.db is not None:
        return entry.fingerprint != fingerprint or (
            entry.refresh_failed
            and now - entry.cached_at >= _NEGATIVE_CACHE_TTL_SECONDS
        )
    return (
        entry.fingerprint != fingerprint
        or now - entry.cached_at >= _NEGATIVE_CACHE_TTL_SECONDS
    )


def _region_db_fingerprint(retail_root: Path, token: str) -> _RegionDBFingerprint:
    return tuple(
        _file_fingerprint(path)
        for path in _region_db_paths(retail_root, token)
    )


def _region_db_paths(retail_root: Path, token: str) -> tuple[Path, ...]:
    db_root = retail_root / "Interface" / "AddOns" / "RaiderIO" / "db"
    return (
        db_root / f"db_mythicplus_{token}_characters.lua",
        db_root / f"db_mythicplus_{token}_lookup.lua",
        db_root / "db_dungeons.lua",
        db_root / f"db_raiding_{token}_characters.lua",
        db_root / f"db_raiding_{token}_lookup.lua",
    )


def _file_fingerprint(path: Path) -> tuple[str, bool, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (path.name, False, 0, 0)
    return (path.name, True, stat.st_mtime_ns, stat.st_size)


def retail_root_from_screenshots_path(path: Path) -> Path | None:
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.name.lower() == "_retail_":
            return parent
    return None


def _realm_lookup_key(realm: str) -> str:
    return "".join(char for char in realm.casefold() if char.isalnum())


def _parse_provider_meta(text: str) -> _ProviderMeta:
    record_match = re.search(
        r'(?:\["recordSizeInBytes"\]|recordSizeInBytes)\s*=\s*(\d+)', text
    )
    order_match = re.search(
        r'(?:\["encodingOrder"\]|encodingOrder)\s*=\s*\{([^}]*)\}', text
    )
    if not record_match or not order_match:
        raise ValueError("RaiderIO lookup metadata missing record size or encoding order")
    order = tuple(int(value) for value in re.findall(r"\d+", order_match.group(1)))
    return _ProviderMeta(record_size=int(record_match.group(1)), encoding_order=order)


def _parse_provider_raids(text: str, key: str) -> list[_RaidInfo]:
    table = _extract_lua_table(text, key)
    if table is None:
        return []
    raids: list[_RaidInfo] = []
    for match in re.finditer(r"\{([^{}]*)\}", table):
        body = match.group(1)
        boss_count = _lua_number_field(body, "bossCount")
        if boss_count is None:
            continue
        name = _lua_string_field(body, "name") or "Raid"
        short_name = _lua_string_field(body, "shortName") or name
        raids.append(_RaidInfo(name=name, short_name=short_name, boss_count=boss_count))
    return raids


def _extract_lua_table(text: str, key: str) -> str | None:
    match = re.search(rf'(?:\["{re.escape(key)}"\]|{re.escape(key)})\s*=\s*\{{', text)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _lua_number_field(text: str, key: str) -> int | None:
    match = re.search(rf'(?:\["{re.escape(key)}"\]|{re.escape(key)})\s*=\s*(\d+)', text)
    return int(match.group(1)) if match else None


def _lua_string_field(text: str, key: str) -> str | None:
    match = re.search(
        rf'(?:\["{re.escape(key)}"\]|{re.escape(key)})\s*=\s*"((?:\\.|[^"])*)"',
        text,
    )
    if not match:
        return None
    return _decode_lua_string_bytes(match.group(1)).decode("utf-8", errors="replace")


def _encoding_field_width(field: int, dungeon_count: int) -> int:
    if field == 1:
        return 13
    if field in {2, 6, 15}:
        return 7
    if field == 3:
        return 14
    if field in {5, 12}:
        return 13
    if field in {7, 13}:
        return 12
    if field == 9:
        return 8 * 6
    if field in {_DUNGEON_LEVELS_FIELD, 14}:
        return dungeon_count * 8
    if field == 11:
        return 4
    raise ValueError(f"unsupported RaiderIO encoding field {field}")


def _validate_encoding_plan(meta: _ProviderMeta, dungeon_count: int) -> None:
    bit_budget = sum(
        _encoding_field_width(field, dungeon_count) for field in meta.encoding_order
    )
    max_bits = meta.record_size * 8
    if bit_budget > max_bits:
        raise ValueError(
            f"RaiderIO encoding plan uses {bit_budget} bits and exceeds record size "
            f"{meta.record_size} bytes ({max_bits} bits)"
        )


def _raid_encoding_field_width(
    field: int, current_raids: list[_RaidInfo], previous_raids: list[_RaidInfo]
) -> int:
    if field == 1:
        return sum(2 * (2 + raid.boss_count * 5) for raid in current_raids)
    if field == 2:
        return sum(2 + raid.boss_count * 5 for raid in previous_raids)
    if field == 3:
        return len(previous_raids) * 2 * 6
    if field == 4:
        return len(current_raids) * 2 * 6
    raise ValueError(f"unsupported RaiderIO raid encoding field {field}")


def _validate_raid_encoding_plan(
    meta: _ProviderMeta, current_raids: list[_RaidInfo], previous_raids: list[_RaidInfo]
) -> None:
    bit_budget = sum(
        _raid_encoding_field_width(field, current_raids, previous_raids)
        for field in meta.encoding_order
    )
    max_bits = meta.record_size * 8
    if bit_budget > max_bits:
        raise ValueError(
            f"RaiderIO raid encoding plan uses {bit_budget} bits and exceeds "
            f"record size {meta.record_size} bytes ({max_bits} bits)"
        )


def _parse_lookup_payload(
    text: str,
    *,
    source_path: Path | None = None,
    payload_cache_dir: Path | None = None,
    payload_cache_generation: int | None = None,
    use_cache: bool = True,
) -> bytes:
    match = re.search(r"provider\.lookup\[1\]\s*=\s*\"", text)
    if not match:
        raise ValueError("RaiderIO lookup payload missing")
    cache_path = (
        _lookup_payload_cache_path(payload_cache_dir, source_path)
        if payload_cache_dir is not None and source_path is not None
        else None
    )
    if cache_path is not None and use_cache:
        cached_payload: bytes | None = None
        with _LOOKUP_PAYLOAD_CACHE_LOCK:
            if _lookup_payload_cache_generation_matches_locked(
                payload_cache_dir,
                payload_cache_generation,
            ):
                try:
                    cached_payload = cache_path.read_bytes()
                except OSError:
                    pass
        if cached_payload is not None:
            _harden_existing_lookup_payload_cache(cache_path)
            return cached_payload
    start = match.end()
    end = _find_lua_string_end(text, start)
    payload = _decode_lua_string_bytes(text[start:end])
    if cache_path is not None:
        try:
            _write_lookup_payload_cache(
                cache_path,
                payload,
                payload_cache_dir=payload_cache_dir,
                payload_cache_generation=payload_cache_generation,
            )
        except OSError:
            pass
    return payload


def _parse_lookup_payload_for_character_layout(
    text: str,
    *,
    source_path: Path,
    payload_cache_dir: Path | None,
    characters_path: Path,
    characters_text: str | None = None,
    meta: _ProviderMeta,
    payload_cache_generation: int | None = None,
) -> bytes:
    payload = _parse_lookup_payload(
        text,
        source_path=source_path,
        payload_cache_dir=payload_cache_dir,
        payload_cache_generation=payload_cache_generation,
    )
    if payload_cache_dir is None:
        return payload
    if characters_text is None:
        characters_text = characters_path.read_text(encoding="utf-8", errors="replace")
    if _lookup_payload_covers_character_layout(
        payload, characters_text, meta.record_size
    ):
        return payload
    payload = _parse_lookup_payload(
        text,
        source_path=source_path,
        payload_cache_dir=payload_cache_dir,
        payload_cache_generation=payload_cache_generation,
        use_cache=False,
    )
    if _lookup_payload_covers_character_layout(
        payload, characters_text, meta.record_size
    ):
        return payload
    raise ValueError("RaiderIO lookup payload shorter than character layout")


def _lookup_payload_covers_character_layout(
    payload: bytes, characters_text: str, record_size: int
) -> bool:
    if record_size <= 0:
        return False
    required_size = 0
    pattern = re.compile(r'provider\.db\["([^"]+)"\]\s*=\s*\{')
    for match in pattern.finditer(characters_text):
        start = match.end()
        end = characters_text.find("}", start)
        if end < 0:
            return False
        body = characters_text[start:end]
        offset_match = re.match(r"\s*(\d+)", body)
        if not offset_match:
            return False
        names = re.findall(r'"([^"]*)"', body[offset_match.end() :])
        required_size = max(
            required_size, int(offset_match.group(1)) + len(names) * record_size
        )
    return len(payload) >= required_size


def _lookup_payload_cache_path(cache_dir: Path, source_path: Path) -> Path | None:
    _name, exists, mtime_ns, size = _file_fingerprint(source_path)
    if not exists:
        return None
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_path.stem)
    return cache_dir / f"{safe_stem}.{mtime_ns}.{size}{_LOOKUP_PAYLOAD_CACHE_SUFFIX}"


def _harden_existing_lookup_payload_cache(path: Path) -> None:
    try:
        apply_private_directory_mode(path.parent)
    except OSError:
        pass
    try:
        apply_private_file_mode(path)
    except OSError:
        pass


def _write_lookup_payload_cache(
    path: Path,
    payload: bytes,
    *,
    payload_cache_dir: Path | None = None,
    payload_cache_generation: int | None = None,
) -> None:
    if payload_cache_dir is not None and payload_cache_generation is not None:
        with _LOOKUP_PAYLOAD_CACHE_LOCK:
            if not _lookup_payload_cache_generation_matches_locked(
                payload_cache_dir,
                payload_cache_generation,
            ):
                return
            _write_lookup_payload_cache_unlocked(path, payload)
        return
    _write_lookup_payload_cache_unlocked(path, payload)


def _write_lookup_payload_cache_unlocked(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    apply_private_directory_mode(path.parent)
    fd = -1
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        apply_private_file_mode(temp_path)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        apply_private_file_mode(path)
        _prune_old_lookup_payload_caches(path)
    except BaseException:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _prune_old_lookup_payload_caches(current_path: Path) -> None:
    prefix = current_path.name.split(".", 1)[0]
    for candidate in current_path.parent.glob(
        f"{prefix}.*{_LOOKUP_PAYLOAD_CACHE_SUFFIX}"
    ):
        if candidate == current_path:
            continue
        try:
            candidate.unlink()
        except OSError:
            pass


def _find_lua_string_end(text: str, start: int) -> int:
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            return idx
    raise ValueError("unterminated RaiderIO Lua string")


def _decode_lua_string_bytes(value: str) -> bytes:
    out = bytearray()
    idx = 0
    size = len(value)
    while idx < len(value):
        char = value[idx]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            idx += 1
            continue
        idx += 1
        if idx < size and "0" <= value[idx] <= "9":
            end = idx + 1
            while end < size and end < idx + 3 and "0" <= value[end] <= "9":
                end += 1
            out.append(int(value[idx:end]) & 0xFF)
            idx = end
        elif idx < len(value):
            escaped = value[idx]
            out.extend(escaped.encode("utf-8"))
            idx += 1
    return bytes(out)


def _parse_dungeon_names(text: str) -> list[str]:
    text = text.split("-- Dungeon for this entire expansion", 1)[0]
    return re.findall(r'\["name"\]\s*=\s*"([^"]+)"', text)


def _parse_realm_data(text: str, realm: str) -> tuple[int, list[str]] | None:
    lookup_key = _realm_lookup_key(realm)
    return _parse_all_realm_data(text).get(lookup_key)


def _parse_all_realm_data(text: str) -> dict[str, tuple[int, list[str]] | None]:
    realms: dict[str, tuple[int, list[str]] | None] = {}
    pattern = re.compile(r'provider\.db\["([^"]+)"\]\s*=\s*\{')
    for match in pattern.finditer(text):
        lookup_key = _realm_lookup_key(match.group(1))
        if not lookup_key:
            continue
        start = match.end()
        end = text.find("}", start)
        if end < 0:
            continue
        body = text[start:end]
        offset_match = re.match(r"\s*(\d+)", body)
        if not offset_match:
            continue
        names = re.findall(r'"([^"]*)"', body[offset_match.end() :])
        realms[lookup_key] = (int(offset_match.group(1)), names)
    return realms


def _decode_profile(
    record: bytes, encoding_order: tuple[int, ...], dungeon_names: list[str]
) -> RaiderIOLocalProfile:
    bit_offset = 0
    current_score = 0
    dungeon_rows: list[dict] = []
    for field in encoding_order:
        if field == 1:
            current_score, bit_offset = _read_bits(record, bit_offset, 13)
        elif field in {2, 6, 15}:
            _, bit_offset = _read_bits(record, bit_offset, 7)
        elif field == 3:
            _, bit_offset = _read_bits(record, bit_offset, 12)
            _, bit_offset = _read_bits(record, bit_offset, 2)
        elif field in {5, 12}:
            _, bit_offset = _read_bits(record, bit_offset, 13)
        elif field in {7, 13}:
            _, bit_offset = _read_bits(record, bit_offset, 10)
            _, bit_offset = _read_bits(record, bit_offset, 2)
        elif field == 9:
            bit_offset += 8 * 6
        elif field == _DUNGEON_LEVELS_FIELD:
            rows, bit_offset = _read_dungeon_rows(record, bit_offset, dungeon_names)
            dungeon_rows = rows
        elif field == 11:
            _, bit_offset = _read_bits(record, bit_offset, 4)
        elif field == 14:
            _, bit_offset = _read_dungeon_rows(record, bit_offset, dungeon_names)
    return RaiderIOLocalProfile(
        current_score=current_score, dungeons=dungeon_rows, raid_progress={}
    )


_RAID_DIFFICULTY_KEYS = {
    1: "N",
    2: "H",
    3: "M",
}

_DECODE_BITS_5_TABLE = (
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    25,
    25,
    30,
    35,
    40,
    45,
    50,
)


def _decode_raid_progress(
    record: bytes,
    encoding_order: tuple[int, ...],
    current_raids: list[_RaidInfo],
    previous_raids: list[_RaidInfo],
) -> dict[str, dict]:
    bit_offset = 0
    progress: dict[str, dict] = {}
    for field in encoding_order:
        if field == 1:
            for raid in current_raids:
                for _idx in range(2):
                    row, bit_offset = _read_full_raid_progress(
                        record, bit_offset, raid
                    )
                    if row is None:
                        continue
                    key = row.pop("difficulty")
                    existing = progress.get(key)
                    if existing is None or row["killed"] > existing.get("killed", 0):
                        progress[key] = row
        elif field == 2:
            for raid in previous_raids:
                _, bit_offset = _read_full_raid_progress(record, bit_offset, raid)
        elif field == 3:
            bit_offset = _skip_summary_raid_progress(
                record, bit_offset, len(previous_raids) * 2
            )
        elif field == 4:
            bit_offset = _skip_summary_raid_progress(
                record, bit_offset, len(current_raids) * 2
            )
        else:
            raise ValueError(f"unsupported RaiderIO raid encoding field {field}")
    return progress


def _read_full_raid_progress(
    record: bytes, bit_offset: int, raid: _RaidInfo
) -> tuple[dict | None, int]:
    raw_difficulty, bit_offset = _read_bits(record, bit_offset, 2)
    difficulty = _RAID_DIFFICULTY_KEYS.get(raw_difficulty + 1)
    boss_kills: list[int] = []
    for _idx in range(raid.boss_count):
        raw_kills, bit_offset = _read_bits(record, bit_offset, 5)
        boss_kills.append(_decode_bits5(raw_kills))
    killed = sum(1 for kills in boss_kills if kills > 0)
    if not difficulty or killed == 0:
        return None, bit_offset
    return (
        {
            "difficulty": difficulty,
            "killed": killed,
            "total": raid.boss_count,
            "boss_kills": boss_kills,
            "raid_name": raid.name,
        },
        bit_offset,
    )


def _skip_summary_raid_progress(
    record: bytes, bit_offset: int, count: int
) -> int:
    for _idx in range(count):
        _, bit_offset = _read_bits(record, bit_offset, 2)
        _, bit_offset = _read_bits(record, bit_offset, 4)
    return bit_offset


def _decode_bits5(value: int) -> int:
    if 0 <= value < len(_DECODE_BITS_5_TABLE):
        return _DECODE_BITS_5_TABLE[value]
    return 0


def _read_dungeon_rows(
    record: bytes, bit_offset: int, dungeon_names: list[str]
) -> tuple[list[dict], int]:
    rows: list[dict] = []
    for name in dungeon_names:
        level, bit_offset = _read_bits(record, bit_offset, 6)
        upgrades, bit_offset = _read_bits(record, bit_offset, 2)
        if level > 0 and upgrades > 0:
            rows.append({"name": name, "key_level": level})
    return rows, bit_offset


def _read_bits(record: bytes, bit_offset: int, width: int) -> tuple[int, int]:
    if bit_offset + width > len(record) * 8:
        raise ValueError(
            f"bit read past record: offset={bit_offset} width={width} "
            f"record_bits={len(record) * 8}"
        )
    value = 0
    for idx in range(width):
        absolute = bit_offset + idx
        byte_index = absolute // 8
        if record[byte_index] & (1 << (absolute % 8)):
            value |= 1 << idx
    return value, bit_offset + width
