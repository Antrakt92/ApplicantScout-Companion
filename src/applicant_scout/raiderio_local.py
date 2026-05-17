"""Read installed RaiderIO addon DB files for local M+ dungeon completions."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import threading
from pathlib import Path


_log = logging.getLogger("applicant_scout.raiderio_local")

_REGION_FILE_TOKENS = {
    "US": "us",
    "KR": "kr",
    "EU": "eu",
    "TW": "tw",
    "CN": "cn",
}

_DUNGEON_LEVELS_FIELD = 10


@dataclass(frozen=True)
class RaiderIOLocalProfile:
    current_score: int
    dungeons: list[dict]


@dataclass(frozen=True)
class _ProviderMeta:
    record_size: int
    encoding_order: tuple[int, ...]


class RaiderIOLocalReader:
    """Lazy reader for RaiderIO's generated local Mythic+ database."""

    def __init__(self, retail_root: Path):
        self._retail_root = Path(retail_root)
        self._cache: dict[str, _RegionDB | None] = {}
        self._loading: set[str] = set()
        self._lock = threading.Lock()

    def lookup_profile(
        self, name: str, realm: str, region: str | None, *, allow_load: bool = True
    ) -> RaiderIOLocalProfile | None:
        token = _REGION_FILE_TOKENS.get((region or "").upper())
        if not token:
            return None
        db = self._region_db(token) if allow_load else self._cache.get(token)
        if db is None:
            return None
        return db.lookup_profile(name, realm)

    def preload_region_async(self, region: str | None) -> None:
        token = _REGION_FILE_TOKENS.get((region or "").upper())
        if not token:
            return
        with self._lock:
            if token in self._cache or token in self._loading:
                return
            self._loading.add(token)

        def _worker() -> None:
            try:
                self._region_db(token)
            finally:
                with self._lock:
                    self._loading.discard(token)

        threading.Thread(
            target=_worker,
            name=f"RaiderIOLocalLoad-{token}",
            daemon=True,
        ).start()

    def _region_db(self, token: str) -> _RegionDB | None:
        with self._lock:
            if token in self._cache:
                return self._cache[token]
        try:
            loaded = _RegionDB.load(self._retail_root, token)
        except Exception as exc:  # noqa: BLE001
            _log.warning("RaiderIO local DB unavailable for %s: %s", token, exc)
            loaded = None
        with self._lock:
            self._cache[token] = loaded
        return loaded


class _RegionDB:
    def __init__(
        self,
        *,
        characters_path: Path,
        lookup_payload: bytes,
        meta: _ProviderMeta,
        dungeons: list[str],
    ):
        self._characters_path = characters_path
        self._lookup_payload = lookup_payload
        self._meta = meta
        self._dungeons = dungeons
        self._realm_cache: dict[str, tuple[int, list[str]] | None] = {}

    @classmethod
    def load(cls, retail_root: Path, token: str) -> _RegionDB | None:
        db_root = retail_root / "Interface" / "AddOns" / "RaiderIO" / "db"
        characters_path = db_root / f"db_mythicplus_{token}_characters.lua"
        lookup_path = db_root / f"db_mythicplus_{token}_lookup.lua"
        dungeons_path = db_root / "db_dungeons.lua"
        if not (
            characters_path.is_file()
            and lookup_path.is_file()
            and dungeons_path.is_file()
        ):
            return None
        lookup_text = lookup_path.read_text(encoding="utf-8", errors="replace")
        return cls(
            characters_path=characters_path,
            lookup_payload=_parse_lookup_payload(lookup_text),
            meta=_parse_provider_meta(lookup_text),
            dungeons=_parse_dungeon_names(dungeons_path.read_text(encoding="utf-8")),
        )

    def lookup_profile(self, name: str, realm: str) -> RaiderIOLocalProfile | None:
        name = name.strip()
        realm = realm.strip()
        if not name or not realm:
            return None
        realm_data = self._realm_data(realm)
        if realm_data is None:
            return None
        base_offset, names = realm_data
        try:
            name_index = next(
                idx for idx, candidate in enumerate(names) if candidate.casefold() == name.casefold()
            )
        except StopIteration:
            return None
        record_offset = base_offset + name_index * self._meta.record_size
        record = self._lookup_payload[record_offset : record_offset + self._meta.record_size]
        if len(record) != self._meta.record_size:
            return None
        return _decode_profile(record, self._meta.encoding_order, self._dungeons)

    def _realm_data(self, realm: str) -> tuple[int, list[str]] | None:
        cache_key = _realm_lookup_key(realm)
        if cache_key not in self._realm_cache:
            text = self._characters_path.read_text(encoding="utf-8", errors="replace")
            self._realm_cache[cache_key] = _parse_realm_data(text, realm)
        return self._realm_cache[cache_key]


def retail_root_from_screenshots_path(path: Path) -> Path | None:
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.name.lower() == "_retail_":
            return parent
    return None


def _realm_lookup_key(realm: str) -> str:
    return "".join(char for char in realm.casefold() if char.isalnum())


def _parse_provider_meta(text: str) -> _ProviderMeta:
    record_match = re.search(r"recordSizeInBytes\s*=\s*(\d+)", text)
    order_match = re.search(r"encodingOrder\s*=\s*\{([^}]*)\}", text)
    if not record_match or not order_match:
        raise ValueError("RaiderIO lookup metadata missing record size or encoding order")
    order = tuple(int(value) for value in re.findall(r"\d+", order_match.group(1)))
    return _ProviderMeta(record_size=int(record_match.group(1)), encoding_order=order)


def _parse_lookup_payload(text: str) -> bytes:
    match = re.search(r"provider\.lookup\[1\]\s*=\s*\"", text)
    if not match:
        raise ValueError("RaiderIO lookup payload missing")
    start = match.end()
    end = _find_lua_string_end(text, start)
    return _decode_lua_string_bytes(text[start:end])


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
    pattern = re.compile(r'provider\.db\["([^"]+)"\]\s*=\s*\{')
    for match in pattern.finditer(text):
        if _realm_lookup_key(match.group(1)) != lookup_key:
            continue
        start = match.end()
        end = text.find("}", start)
        if end < 0:
            return None
        body = text[start:end]
        offset_match = re.match(r"\s*(\d+)", body)
        if not offset_match:
            return None
        names = re.findall(r'"([^"]*)"', body[offset_match.end() :])
        return int(offset_match.group(1)), names
    return None


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
    return RaiderIOLocalProfile(current_score=current_score, dungeons=dungeon_rows)


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
    value = 0
    for idx in range(width):
        absolute = bit_offset + idx
        byte_index = absolute // 8
        if byte_index >= len(record):
            return value, bit_offset + width
        if record[byte_index] & (1 << (absolute % 8)):
            value |= 1 << idx
    return value, bit_offset + width
