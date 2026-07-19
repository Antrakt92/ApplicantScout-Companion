from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import applicant_scout.raiderio_local as raiderio_local_mod
from applicant_scout.raiderio_local import RaiderIOLocalProfile, RaiderIOLocalReader


def _write_test_db(
    root: Path,
    lookup_payload: bytes,
    *,
    record_size: int = 5,
    encoding_order: tuple[int, ...] = (1, 2, 10),
    dungeons: tuple[str, ...] = ("Skyreach", "Pit of Saron"),
) -> None:
    db = root / "Interface" / "AddOns" / "RaiderIO" / "db"
    db.mkdir(parents=True, exist_ok=True)
    (db / "db_dungeons.lua").write_text(
        (
            """
local _, ns = ...
ns.dungeons = {
"""
            + "\n".join(
                f'    [{idx}] = {{ ["name"] = "{name}", ["shortName"] = "D{idx}" }},'
                for idx, name in enumerate(dungeons, start=1)
            )
            + """
}
"""
        ),
        encoding="utf-8",
    )
    (db / "db_mythicplus_eu_characters.lua").write_text(
        'local provider={name=...,data=1,region="eu",date="test-mplus",'
        f'numCharacters={len(lookup_payload) // record_size},db={{}}}}\n'
        'provider.db["Ragnaros"]={0,"Alphapack","Chinie"}\n',
        encoding="utf-8",
    )
    encoded = "".join(f"\\{byte}" for byte in lookup_payload)
    order = ",".join(str(value) for value in encoding_order)
    (db / "db_mythicplus_eu_lookup.lua").write_text(
        'local provider={name=...,data=1,region="eu",date="test-mplus",'
        f'numCharacters={len(lookup_payload) // record_size},lookup={{}},'
        f"recordSizeInBytes={record_size},encodingOrder={{{order}}}}}\n"
        f'provider.lookup[1] = "{encoded}"\n',
        encoding="utf-8",
    )


def _write_mplus_character_names(
    root: Path,
    names: list[str],
    *,
    realm: str = "Ragnaros",
    base_offset: int = 0,
) -> None:
    characters_path = (
        root
        / "Interface"
        / "AddOns"
        / "RaiderIO"
        / "db"
        / "db_mythicplus_eu_characters.lua"
    )
    encoded_names = ",".join(f'"{name}"' for name in names)
    characters_path.write_text(
        'local provider={name=...,data=1,region="eu",date="test-mplus",'
        f'numCharacters={len(names)},db={{}}}}\n'
        f'provider.db["{realm}"]={{{base_offset},{encoded_names}}}\n',
        encoding="utf-8",
    )


def _write_multirealm_test_db(root: Path, lookup_payload: bytes) -> None:
    db = root / "Interface" / "AddOns" / "RaiderIO" / "db"
    db.mkdir(parents=True, exist_ok=True)
    (db / "db_dungeons.lua").write_text(
        """
local _, ns = ...
ns.dungeons = {
    [1] = { ["name"] = "Skyreach", ["shortName"] = "D1" },
    [2] = { ["name"] = "Pit of Saron", ["shortName"] = "D2" },
}
""",
        encoding="utf-8",
    )
    (db / "db_mythicplus_eu_characters.lua").write_text(
        'local provider={name=...,data=1,region="eu",date="test-mplus",'
        f'numCharacters={len(lookup_payload) // 5},db={{}}}}\n'
        'provider.db["Ragnaros"]={0,"Alphapack"}\n'
        'provider.db["Silvermoon"]={5,"Moonie"}\n',
        encoding="utf-8",
    )
    encoded = "".join(f"\\{byte}" for byte in lookup_payload)
    (db / "db_mythicplus_eu_lookup.lua").write_text(
        'local provider={name=...,data=1,region="eu",date="test-mplus",'
        f'numCharacters={len(lookup_payload) // 5},lookup={{}},'
        "recordSizeInBytes=5,encodingOrder={1,2,10}}\n"
        f'provider.lookup[1] = "{encoded}"\n',
        encoding="utf-8",
    )


def _write_test_raid_db(
    root: Path,
    lookup_payload: bytes,
    *,
    record_size: int = 6,
    encoding_order: tuple[int, ...] = (1, 4),
    boss_count: int = 3,
) -> None:
    db = root / "Interface" / "AddOns" / "RaiderIO" / "db"
    db.mkdir(parents=True, exist_ok=True)
    (db / "db_raiding_eu_characters.lua").write_text(
        'local provider={name=...,data=2,region="eu",date="test-raid",'
        f'numCharacters={len(lookup_payload) // record_size},db={{}}}}\n'
        'provider.db["Ragnaros"]={0,"Alphapack","Chinie"}\n',
        encoding="utf-8",
    )
    encoded = "".join(f"\\{byte}" for byte in lookup_payload)
    order = ",".join(str(value) for value in encoding_order)
    (db / "db_raiding_eu_lookup.lua").write_text(
        'local provider={name=...,data=2,region="eu",date="test-raid",'
        f'numCharacters={len(lookup_payload) // record_size},lookup={{}},'
        f"recordSizeInBytes={record_size},encodingOrder={{{order}}},"
        f'currentRaids={{{{["id"]=1,["name"]="Test Raid",["shortName"]="TR",'
        f'["bossCount"]={boss_count},["ordinal"]=1}}}},previousRaids={{}}}}\n'
        f'provider.lookup[1] = "{encoded}"\n',
        encoding="utf-8",
    )


def _write_invalid_test_raid_db(root: Path) -> None:
    db = root / "Interface" / "AddOns" / "RaiderIO" / "db"
    db.mkdir(parents=True, exist_ok=True)
    (db / "db_raiding_eu_characters.lua").write_text(
        'local provider={name=...,data=2,region="eu",date="test-raid",'
        'numCharacters=1,db={}}\n'
        'provider.db["Ragnaros"]={0,"Chinie"}\n',
        encoding="utf-8",
    )
    (db / "db_raiding_eu_lookup.lua").write_text(
        'local provider={name=...,data=2,region="eu",date="test-raid",'
        'numCharacters=1,lookup={},'
        "recordSizeInBytes=1,encodingOrder={99},currentRaids={},previousRaids={}}\n"
        'provider.lookup[1] = "\\0"\n',
        encoding="utf-8",
    )


def _mark_test_db_changed(root: Path) -> None:
    path = (
        root
        / "Interface"
        / "AddOns"
        / "RaiderIO"
        / "db"
        / "db_mythicplus_eu_lookup.lua"
    )
    path.write_text(path.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")


def _replace_encoded_payload_preserving_size_and_mtime(
    path: Path,
    old_payload: bytes,
    new_payload: bytes,
) -> None:
    old_encoded = "".join(f"\\{byte}" for byte in old_payload)
    new_encoded = "".join(f"\\{byte}" for byte in new_payload)
    before = path.stat()
    text = path.read_text(encoding="utf-8")
    assert text.count(old_encoded) == 1
    updated = text.replace(old_encoded, new_encoded, 1)
    assert len(updated.encode("utf-8")) == len(text.encode("utf-8"))

    path.write_text(updated, encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    after = path.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns


def _append_invalid_utf8(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"\n-- invalid: \xff\n")


def _write_mplus_generation(root: Path, score: int, pit_level: int) -> None:
    _write_test_db(
        root,
        _record(3200, 15, 14, 1, 0) + _record(score, 0, pit_level, 0, 2),
    )


def _write_raid_generation(root: Path, boss_kills: tuple[int, int, int]) -> None:
    _write_test_raid_db(
        root,
        _raid_record((1, (0, 0, 0)), (0, (0, 0, 0)))
        + _raid_record((3, boss_kills), (2, (1, 1, 1))),
    )


def _record(score: int, skyreach: int, pit: int, skyreach_upgrades: int, pit_upgrades: int) -> bytes:
    values = [
        (score, 13),
        (1, 7),
        (skyreach, 6),
        (skyreach_upgrades, 2),
        (pit, 6),
        (pit_upgrades, 2),
    ]
    out = bytearray(5)
    offset = 0
    for value, width in values:
        for bit_idx in range(width):
            if value & (1 << bit_idx):
                out[offset // 8] |= 1 << (offset % 8)
            offset += 1
    return bytes(out)


def _pack_bits(values: list[tuple[int, int]], size: int) -> bytes:
    out = bytearray(size)
    offset = 0
    for value, width in values:
        for bit_idx in range(width):
            if value & (1 << bit_idx):
                out[offset // 8] |= 1 << (offset % 8)
            offset += 1
    return bytes(out)


def _raid_record(
    first: tuple[int, tuple[int, ...]],
    second: tuple[int, tuple[int, ...]],
    *,
    size: int = 6,
) -> bytes:
    values: list[tuple[int, int]] = []
    for difficulty, boss_kills in (first, second):
        values.append((difficulty - 1, 2))
        values.extend((kills, 5) for kills in boss_kills)
    values.extend([(0, 2), (0, 4), (0, 2), (0, 4)])
    return _pack_bits(values, size)


def test_reader_decodes_timed_dungeon_rows_from_local_raiderio_db(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    reader = RaiderIOLocalReader(tmp_path)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 3074
    assert profile.dungeons == [{"name": "Pit of Saron", "key_level": 12}]
    assert profile.raid_progress == {}


def test_reader_name_index_preserves_first_casefold_match(tmp_path: Path):
    first_record = _record(3200, 15, 14, 1, 0)
    second_record = _record(3074, 0, 12, 0, 2)
    _write_test_db(tmp_path, first_record + second_record)
    _write_mplus_character_names(tmp_path, ["Alpha", "ALPHA"])
    cache_dir = tmp_path / "cache"
    reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    profile = reader.lookup_profile("alpha", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 3200
    assert profile.dungeons == [{"name": "Skyreach", "key_level": 15}]

    cache_file = next(cache_dir.rglob("*.payload.bin"))
    cache_file.write_bytes(first_record)
    repaired_reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    repaired = repaired_reader.lookup_profile("alpha", "Ragnaros", "EU")

    assert repaired is not None
    assert repaired.current_score == 3200
    assert cache_file.read_bytes() == first_record + second_record


def test_region_load_parses_each_character_layout_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    _write_test_raid_db(
        tmp_path,
        _raid_record((1, (0, 0, 0)), (0, (0, 0, 0)))
        + _raid_record((3, (2, 0, 1)), (2, (1, 1, 1))),
    )
    original_parse = raiderio_local_mod._parse_character_layout
    parsed_texts: list[str] = []

    def count_parse(text: str, record_size: int):
        parsed_texts.append(text)
        return original_parse(text, record_size)

    monkeypatch.setattr(raiderio_local_mod, "_parse_character_layout", count_parse)

    db = raiderio_local_mod._RegionDB.load(
        tmp_path,
        "eu",
        payload_cache_dir=tmp_path / "payload-cache",
    )

    assert db is not None
    assert len(parsed_texts) == 2


def test_reader_large_repeated_lookups_use_one_prebuilt_name_index_get(
    tmp_path: Path,
):
    names = [f"Player{index}" for index in range(2_000)]
    records = b"".join(
        _record(2_000 + index, 0, 10 + (index % 5), 0, 0)
        for index in range(len(names))
    )
    _write_test_db(tmp_path, records)
    _write_mplus_character_names(tmp_path, names)
    reader = RaiderIOLocalReader(tmp_path)
    initial = reader.lookup_profile("PLAYER1999", "Ragnaros", "EU")
    assert initial is not None
    assert initial.current_score == 3999

    entry = reader._cache["eu"]
    assert entry.db is not None
    realm_data = entry.db._mplus_realm_cache["ragnaros"]

    class CountingIndex(dict[str, int]):
        get_calls = 0

        def get(self, key: str, default: int | None = None) -> int | None:
            self.get_calls += 1
            return super().get(key, default)

    counting_index = CountingIndex(realm_data.name_indexes)
    entry.db._mplus_realm_cache["ragnaros"] = raiderio_local_mod._RealmData(
        base_offset=realm_data.base_offset,
        name_indexes=counting_index,
    )

    for _ in range(250):
        profile = entry.db.lookup_profile("PLAYER1999", "Ragnaros")
        assert profile is not None
        assert profile.current_score == 3999

    assert counting_index.get_calls == 250


def test_reader_fingerprint_reload_replaces_realm_name_index(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    _write_mplus_character_names(tmp_path, ["Alphapack", "Oldname"])
    reader = RaiderIOLocalReader(tmp_path)
    old_profile = reader.lookup_profile("Oldname", "Ragnaros", "EU")
    assert old_profile is not None
    assert old_profile.current_score == 3074

    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3333, 0, 16, 0, 1),
    )
    _write_mplus_character_names(tmp_path, ["Alphapack", "Newname"])
    _mark_test_db_changed(tmp_path)

    new_profile = reader.lookup_profile("Newname", "Ragnaros", "EU")

    assert new_profile is not None
    assert new_profile.current_score == 3333
    assert reader.lookup_profile("Oldname", "Ragnaros", "EU") is None


def test_reader_decodes_current_raid_progress_from_local_raiderio_db(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    _write_test_raid_db(
        tmp_path,
        _raid_record((1, (0, 0, 0)), (0, (0, 0, 0)))
        + _raid_record((3, (2, 0, 1)), (2, (1, 1, 1))),
    )
    reader = RaiderIOLocalReader(tmp_path)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 3074
    assert profile.dungeons == [{"name": "Pit of Saron", "key_level": 12}]
    assert profile.raid_progress == {
        "M": {
            "killed": 2,
            "total": 3,
            "boss_kills": [2, 0, 1],
            "raid_name": "Test Raid",
        },
        "H": {
            "killed": 3,
            "total": 3,
            "boss_kills": [1, 1, 1],
            "raid_name": "Test Raid",
        },
    }


def test_decoder_preserves_boss_offsets_across_current_raids():
    main_raid = raiderio_local_mod._RaidInfo(
        name="MN Tier 1 (VS / DR / MQD)",
        short_name="VS/DR/MQD",
        boss_count=9,
    )
    sporefall = raiderio_local_mod._RaidInfo(
        name="Sporefall",
        short_name="SF",
        boss_count=1,
    )
    values: list[tuple[int, int]] = []
    for difficulty, boss_kills in (
        (1, (0,) * 9),
        (1, (0,) * 9),
        (2, (3,)),
        (3, (4,)),
    ):
        values.append((difficulty - 1, 2))
        values.extend((kills, 5) for kills in boss_kills)
    record = _pack_bits(values, size=14)

    progress = raiderio_local_mod._decode_raid_progress(
        record,
        (1,),
        [main_raid, sporefall],
        [],
    )

    assert progress == {
        "H": {
            "killed": 1,
            "total": 10,
            "boss_kills": [0] * 9 + [3],
            "raid_name": "Sporefall",
        },
        "M": {
            "killed": 1,
            "total": 10,
            "boss_kills": [0] * 9 + [4],
            "raid_name": "Sporefall",
        },
    }


def test_reader_keeps_mplus_available_when_raid_db_is_invalid(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    _write_invalid_test_raid_db(tmp_path)
    reader = RaiderIOLocalReader(tmp_path)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 3074
    assert profile.dungeons == [{"name": "Pit of Saron", "key_level": 12}]
    assert profile.raid_progress == {}


def test_reader_keeps_raid_available_when_mplus_db_is_invalid(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
        encoding_order=(1, 99, 10),
    )
    _write_test_raid_db(
        tmp_path,
        _raid_record((1, (0, 0, 0)), (0, (0, 0, 0)))
        + _raid_record((3, (2, 0, 1)), (2, (1, 1, 1))),
    )
    reader = RaiderIOLocalReader(tmp_path)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 0
    assert profile.dungeons == []
    assert profile.has_mplus_profile is False
    assert profile.raid_progress == {
        "M": {
            "killed": 2,
            "total": 3,
            "boss_kills": [2, 0, 1],
            "raid_name": "Test Raid",
        },
        "H": {
            "killed": 3,
            "total": 3,
            "boss_kills": [1, 1, 1],
            "raid_name": "Test Raid",
        },
    }


@pytest.mark.parametrize("encoding_order", ((), (1,), (10,)))
def test_reader_rejects_incomplete_mplus_encoding_order(
    tmp_path: Path,
    encoding_order: tuple[int, ...],
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0),
        encoding_order=encoding_order,
    )
    _write_test_raid_db(
        tmp_path,
        _raid_record((1, (0, 0, 0)), (0, (0, 0, 0)))
        + _raid_record((3, (2, 0, 1)), (2, (1, 1, 1))),
    )
    reader = RaiderIOLocalReader(tmp_path)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 0
    assert profile.dungeons == []
    assert profile.has_mplus_profile is False
    assert profile.raid_progress["M"]["killed"] == 2


def test_reader_matches_display_realm_against_raiderio_normalized_realm_key(
    tmp_path: Path,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    characters_path = (
        tmp_path
        / "Interface"
        / "AddOns"
        / "RaiderIO"
        / "db"
        / "db_mythicplus_eu_characters.lua"
    )
    characters_path.write_text(
        'local provider={name=...,data=1,region="eu",date="test-mplus",'
        'numCharacters=2,db={}}\n'
        'provider.db["Корольлич"]={5,"Arthas"}\n',
        encoding="utf-8",
    )
    reader = RaiderIOLocalReader(tmp_path)

    profile = reader.lookup_profile("Arthas", "Король-лич", "EU")

    assert profile is not None
    assert profile.current_score == 3074
    assert profile.dungeons == [{"name": "Pit of Saron", "key_level": 12}]


def test_reader_returns_none_when_raiderio_db_is_missing(tmp_path: Path):
    reader = RaiderIOLocalReader(tmp_path)

    assert reader.lookup_profile("Chinie", "Ragnaros", "EU") is None


def test_preload_region_async_invokes_completion_after_cache_load(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    reader = RaiderIOLocalReader(tmp_path)
    completed = threading.Event()

    reader.preload_region_async("EU", on_loaded=completed.set)

    assert completed.wait(timeout=2.0)
    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU", allow_load=False)
    assert profile is not None
    assert profile.dungeons == [{"name": "Pit of Saron", "key_level": 12}]


def test_preload_region_async_does_not_fingerprint_on_caller_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    main_thread = threading.get_ident()
    original_fingerprint = raiderio_local_mod._region_db_fingerprint

    def fail_on_caller_thread(retail_root: Path, token: str):
        if threading.get_ident() == main_thread:
            raise AssertionError("fingerprint ran on caller thread")
        return original_fingerprint(retail_root, token)

    monkeypatch.setattr(
        raiderio_local_mod,
        "_region_db_fingerprint",
        fail_on_caller_thread,
    )
    reader = RaiderIOLocalReader(tmp_path)
    completed = threading.Event()

    reader.preload_region_async("EU", on_loaded=completed.set)

    assert completed.wait(timeout=2.0)
    assert reader.lookup_profile("Chinie", "Ragnaros", "EU", allow_load=False)


def test_preload_region_async_hot_cache_does_not_fingerprint_on_caller_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    reader = RaiderIOLocalReader(tmp_path)
    first_completed = threading.Event()

    reader.preload_region_async("EU", on_loaded=first_completed.set)

    assert first_completed.wait(timeout=2.0)
    assert reader.lookup_profile("Chinie", "Ragnaros", "EU", allow_load=False)

    main_thread = threading.get_ident()
    original_fingerprint = raiderio_local_mod._region_db_fingerprint

    def fail_on_caller_thread(retail_root: Path, token: str):
        if threading.get_ident() == main_thread:
            raise AssertionError("fingerprint ran on caller thread")
        return original_fingerprint(retail_root, token)

    monkeypatch.setattr(
        raiderio_local_mod,
        "_region_db_fingerprint",
        fail_on_caller_thread,
    )
    second_completed = threading.Event()

    reader.preload_region_async("EU", on_loaded=second_completed.set)

    assert second_completed.wait(timeout=2.0)
    assert reader.lookup_profile("Chinie", "Ragnaros", "EU", allow_load=False)


def test_preload_region_async_hydrates_distinct_realm_off_caller_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_multirealm_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    main_thread = threading.get_ident()
    original_read_text = Path.read_text
    original_realm_data = raiderio_local_mod._RegionDB._realm_data

    def guard_read_text(self, *args, **kwargs):
        if self.name.endswith("_characters.lua") and threading.get_ident() == main_thread:
            raise AssertionError("character file read on caller thread")
        return original_read_text(self, *args, **kwargs)

    def guard_realm_data(self, characters_path, realm_cache, realm):
        if (
            raiderio_local_mod._realm_lookup_key(realm) not in realm_cache
            and threading.get_ident() == main_thread
        ):
            raise AssertionError("realm block parsed on caller thread")
        return original_realm_data(self, characters_path, realm_cache, realm)

    monkeypatch.setattr(Path, "read_text", guard_read_text)
    monkeypatch.setattr(raiderio_local_mod._RegionDB, "_realm_data", guard_realm_data)
    reader = RaiderIOLocalReader(tmp_path, cache_dir=tmp_path / "cache")
    completed = threading.Event()

    reader.preload_region_async("EU", on_loaded=completed.set)

    assert completed.wait(timeout=2.0)
    profile = reader.lookup_profile("Moonie", "Silvermoon", "EU", allow_load=False)

    assert profile is not None
    assert profile.current_score == 3074


def test_preload_region_async_invokes_completion_for_missing_db(tmp_path: Path):
    reader = RaiderIOLocalReader(tmp_path)
    completed = threading.Event()

    reader.preload_region_async("EU", on_loaded=completed.set)

    assert completed.wait(timeout=2.0)
    assert reader.lookup_profile("Chinie", "Ragnaros", "EU", allow_load=False) is None


def test_preload_region_async_retries_missing_db_when_files_appear(tmp_path: Path):
    reader = RaiderIOLocalReader(tmp_path)
    first_completed = threading.Event()

    reader.preload_region_async("EU", on_loaded=first_completed.set)

    assert first_completed.wait(timeout=2.0)
    assert reader.lookup_profile("Chinie", "Ragnaros", "EU", allow_load=False) is None

    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    second_completed = threading.Event()
    reader.preload_region_async("EU", on_loaded=second_completed.set)

    assert second_completed.wait(timeout=2.0)
    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU", allow_load=False)
    assert profile is not None
    assert profile.dungeons == [{"name": "Pit of Saron", "key_level": 12}]


def test_lookup_profile_reloads_positive_cache_when_fingerprint_changes(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    reader = RaiderIOLocalReader(tmp_path)

    first = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074

    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3333, 0, 16, 0, 1),
    )
    _mark_test_db_changed(tmp_path)

    refreshed = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert refreshed is not None
    assert refreshed.current_score == 3333
    assert refreshed.dungeons == [{"name": "Pit of Saron", "key_level": 16}]


def test_reader_reuses_decoded_lookup_payload_cache_across_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    cache_dir = tmp_path / "cache"
    first_reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)
    first = first_reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074

    def fail_decode(*_args: object) -> bytes:
        raise AssertionError("lookup payload should load from the decoded cache")

    monkeypatch.setattr(raiderio_local_mod, "_decode_lua_string_bytes", fail_decode)
    second_reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    second = second_reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert second is not None
    assert second.current_score == 3074


def test_reader_hardens_decoded_lookup_payload_cache_parent_temp_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    cache_dir = tmp_path / "cache"
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        raiderio_local_mod,
        "apply_private_directory_mode",
        lambda path: calls.append(("dir", Path(path))),
        raising=False,
    )
    monkeypatch.setattr(
        raiderio_local_mod,
        "apply_private_file_mode",
        lambda path: calls.append(("file", Path(path))),
        raising=False,
    )
    reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    cache_file = next(cache_dir.rglob("*.payload.bin"))
    cache_parent = cache_dir / "raiderio-local"
    dir_index = calls.index(("dir", cache_parent))
    temp_index = next(
        idx
        for idx, (kind, path) in enumerate(calls)
        if kind == "file" and path.name.endswith(".tmp")
    )
    target_index = calls.index(("file", cache_file))
    assert dir_index < temp_index < target_index


def test_reader_hardens_existing_decoded_lookup_payload_cache_on_read_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    cache_dir = tmp_path / "cache"
    first_reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)
    assert first_reader.lookup_profile("Chinie", "Ragnaros", "EU") is not None
    cache_file = next(cache_dir.rglob("*.payload.bin"))
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        raiderio_local_mod,
        "apply_private_directory_mode",
        lambda path: calls.append(("dir", Path(path))),
        raising=False,
    )
    monkeypatch.setattr(
        raiderio_local_mod,
        "apply_private_file_mode",
        lambda path: calls.append(("file", Path(path))),
        raising=False,
    )

    def fail_decode(*_args: object) -> bytes:
        raise AssertionError("lookup payload should load from the decoded cache")

    monkeypatch.setattr(raiderio_local_mod, "_decode_lua_string_bytes", fail_decode)
    second_reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    profile = second_reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert ("dir", cache_file.parent) in calls
    assert ("file", cache_file) in calls


def test_lookup_payload_cache_private_mode_failure_does_not_block_profile_load_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    cache_dir = tmp_path / "cache"
    file_calls: list[Path] = []
    monkeypatch.setattr(
        raiderio_local_mod,
        "apply_private_directory_mode",
        lambda _path: None,
        raising=False,
    )

    def fail_temp_mode(path: Path) -> None:
        path = Path(path)
        file_calls.append(path)
        if path.name.endswith(".tmp"):
            raise PermissionError("private mode rejected")

    monkeypatch.setattr(
        raiderio_local_mod,
        "apply_private_file_mode",
        fail_temp_mode,
        raising=False,
    )
    reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 3074
    assert file_calls
    assert list(cache_dir.rglob("*.tmp")) == []
    assert list(cache_dir.rglob("*.payload.bin")) == []


def test_reader_invalidates_decoded_lookup_payload_cache_when_lookup_file_changes(
    tmp_path: Path,
):
    cache_dir = tmp_path / "cache"
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    first_reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)
    first = first_reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074

    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3333, 0, 16, 0, 1),
    )
    _mark_test_db_changed(tmp_path)
    second_reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    refreshed = second_reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert refreshed is not None
    assert refreshed.current_score == 3333
    assert refreshed.dungeons == [{"name": "Pit of Saron", "key_level": 16}]


def test_reader_invalidates_mplus_caches_when_content_changes_with_same_stat(
    tmp_path: Path,
):
    old_payload = _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2)
    new_payload = _record(3200, 15, 14, 1, 0) + _record(3000, 0, 16, 0, 2)
    _write_test_db(tmp_path, old_payload)
    cache_dir = tmp_path / "cache"
    reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)
    first = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074
    old_cache_files = list(cache_dir.rglob("*.payload.bin"))
    assert len(old_cache_files) == 1
    assert ".v2." in old_cache_files[0].name

    lookup_path = (
        tmp_path
        / "Interface"
        / "AddOns"
        / "RaiderIO"
        / "db"
        / "db_mythicplus_eu_lookup.lua"
    )
    _replace_encoded_payload_preserving_size_and_mtime(
        lookup_path,
        old_payload,
        new_payload,
    )

    restarted = RaiderIOLocalReader(
        tmp_path,
        cache_dir=cache_dir,
    ).lookup_profile("Chinie", "Ragnaros", "EU")
    new_cache_files = list(cache_dir.rglob("*.payload.bin"))
    refreshed = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert refreshed is not None
    assert refreshed.current_score == 3000
    assert refreshed.dungeons == [{"name": "Pit of Saron", "key_level": 16}]
    assert restarted == refreshed
    assert len(new_cache_files) == 1
    assert new_cache_files[0] != old_cache_files[0]


def test_reader_invalidates_raid_caches_when_content_changes_with_same_stat(
    tmp_path: Path,
):
    mplus_payload = _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2)
    old_raid_payload = _raid_record(
        (1, (0, 0, 0)),
        (0, (0, 0, 0)),
    ) + _raid_record(
        (3, (2, 0, 1)),
        (2, (1, 1, 1)),
    )
    new_raid_payload = _raid_record(
        (1, (0, 0, 0)),
        (0, (0, 0, 0)),
    ) + _raid_record(
        (3, (0, 1, 0)),
        (2, (1, 1, 1)),
    )
    _write_test_db(tmp_path, mplus_payload)
    _write_test_raid_db(tmp_path, old_raid_payload)
    cache_dir = tmp_path / "cache"
    reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)
    first = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.raid_progress["M"]["boss_kills"] == [2, 0, 1]

    lookup_path = (
        tmp_path
        / "Interface"
        / "AddOns"
        / "RaiderIO"
        / "db"
        / "db_raiding_eu_lookup.lua"
    )
    _replace_encoded_payload_preserving_size_and_mtime(
        lookup_path,
        old_raid_payload,
        new_raid_payload,
    )

    restarted = RaiderIOLocalReader(
        tmp_path,
        cache_dir=cache_dir,
    ).lookup_profile("Chinie", "Ragnaros", "EU")
    refreshed = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert refreshed is not None
    assert refreshed.raid_progress["M"]["boss_kills"] == [0, 1, 0]
    assert restarted == refreshed


def test_reader_rejects_shifted_records_from_invalid_lookup_utf8_and_keeps_fallback(
    tmp_path: Path,
):
    _write_mplus_generation(tmp_path, 3074, 12)
    cache_dir = tmp_path / "cache"
    reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)
    first = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074

    lookup_path = (
        tmp_path
        / "Interface"
        / "AddOns"
        / "RaiderIO"
        / "db"
        / "db_mythicplus_eu_lookup.lua"
    )
    raw = lookup_path.read_bytes()
    assert raw.count(b"\\0") >= 1
    lookup_path.write_bytes(raw.replace(b"\\0", b"\\\xff", 1))

    fallback = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    fresh = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir).lookup_profile(
        "Chinie",
        "Ragnaros",
        "EU",
    )

    assert fallback == first
    assert fresh is None


@pytest.mark.parametrize(
    ("filename", "expect_mplus", "expect_raid"),
    [
        ("db_mythicplus_eu_characters.lua", False, True),
        ("db_mythicplus_eu_lookup.lua", False, True),
        ("db_dungeons.lua", False, True),
        ("db_raiding_eu_characters.lua", True, False),
        ("db_raiding_eu_lookup.lua", True, False),
    ],
)
def test_reader_rejects_invalid_utf8_in_each_generated_source(
    tmp_path: Path,
    filename: str,
    expect_mplus: bool,
    expect_raid: bool,
):
    _write_mplus_generation(tmp_path, 3074, 12)
    _write_test_raid_db(
        tmp_path,
        _raid_record((1, (0, 0, 0)), (0, (0, 0, 0)))
        + _raid_record((3, (2, 0, 1)), (2, (1, 1, 1))),
    )
    source_path = (
        tmp_path / "Interface" / "AddOns" / "RaiderIO" / "db" / filename
    )
    _append_invalid_utf8(source_path)

    profile = RaiderIOLocalReader(tmp_path).lookup_profile(
        "Chinie",
        "Ragnaros",
        "EU",
    )

    assert profile is not None
    assert profile.has_mplus_profile is expect_mplus
    assert bool(profile.raid_progress) is expect_raid


@pytest.mark.parametrize(
    ("filename", "source_date", "expect_mplus", "expect_raid"),
    [
        ("db_mythicplus_eu_lookup.lua", "test-mplus", False, True),
        ("db_raiding_eu_lookup.lua", "test-raid", True, False),
    ],
)
def test_reader_rejects_stable_cross_generation_provider_pairs(
    tmp_path: Path,
    filename: str,
    source_date: str,
    expect_mplus: bool,
    expect_raid: bool,
):
    _write_mplus_generation(tmp_path, 3074, 12)
    _write_raid_generation(tmp_path, (2, 0, 1))
    lookup_path = (
        tmp_path / "Interface" / "AddOns" / "RaiderIO" / "db" / filename
    )
    text = lookup_path.read_text(encoding="utf-8")
    assert text.count(f'date="{source_date}"') == 1
    lookup_path.write_text(
        text.replace(f'date="{source_date}"', 'date="other-generation"', 1),
        encoding="utf-8",
    )

    profile = RaiderIOLocalReader(tmp_path).lookup_profile(
        "Chinie",
        "Ragnaros",
        "EU",
    )

    assert profile is not None
    assert profile.has_mplus_profile is expect_mplus
    assert bool(profile.raid_progress) is expect_raid


def test_reader_rejects_invalid_utf8_escaped_in_raid_metadata(tmp_path: Path):
    _write_mplus_generation(tmp_path, 3074, 12)
    _write_test_raid_db(
        tmp_path,
        _raid_record((1, (0, 0, 0)), (0, (0, 0, 0)))
        + _raid_record((3, (2, 0, 1)), (2, (1, 1, 1))),
    )
    raid_lookup_path = (
        tmp_path
        / "Interface"
        / "AddOns"
        / "RaiderIO"
        / "db"
        / "db_raiding_eu_lookup.lua"
    )
    text = raid_lookup_path.read_text(encoding="utf-8")
    assert text.count('["name"]="Test Raid"') == 1
    raid_lookup_path.write_text(
        text.replace('["name"]="Test Raid"', '["name"]="\\255 Raid"', 1),
        encoding="utf-8",
    )

    profile = RaiderIOLocalReader(tmp_path).lookup_profile(
        "Chinie",
        "Ragnaros",
        "EU",
    )

    assert profile is not None
    assert profile.current_score == 3074
    assert profile.raid_progress == {}


def test_reader_redecodes_lookup_payload_when_decoded_cache_is_too_short_for_character_layout(
    tmp_path: Path,
):
    cache_dir = tmp_path / "cache"
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    first_reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)
    first = first_reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074

    cache_files = list(cache_dir.rglob("*.payload.bin"))
    assert len(cache_files) == 1
    cache_files[0].write_bytes(_record(3200, 15, 14, 1, 0))
    second_reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    repaired = second_reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert repaired is not None
    assert repaired.current_score == 3074
    assert repaired.dungeons == [{"name": "Pit of Saron", "key_level": 12}]
    assert cache_files[0].read_bytes() == (
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2)
    )


def test_reader_does_not_recreate_decoded_lookup_payload_cache_after_clear_during_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    cache_dir = tmp_path / "cache"
    original_decode = raiderio_local_mod._decode_lua_string_bytes
    cleared = False

    def clear_during_decode(value: str) -> bytes:
        nonlocal cleared
        payload = original_decode(value)
        if not cleared:
            cleared = True
            raiderio_local_mod.clear_lookup_payload_cache(cache_dir)
        return payload

    monkeypatch.setattr(
        raiderio_local_mod,
        "_decode_lua_string_bytes",
        clear_during_decode,
    )
    reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 3074
    assert list(cache_dir.rglob("*.payload.bin")) == []


def test_source_retry_does_not_recreate_payload_cache_cleared_during_first_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_mplus_generation(tmp_path, 3074, 12)
    cache_dir = tmp_path / "cache"
    original_decode = raiderio_local_mod._decode_lua_string_bytes
    cleared = False

    def clear_and_replace_during_decode(value: str) -> bytes:
        nonlocal cleared
        payload = original_decode(value)
        if not cleared:
            cleared = True
            raiderio_local_mod.clear_lookup_payload_cache(cache_dir)
            _write_mplus_generation(tmp_path, 3333, 16)
        return payload

    monkeypatch.setattr(
        raiderio_local_mod,
        "_decode_lua_string_bytes",
        clear_and_replace_during_decode,
    )
    reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 3333
    assert profile.dungeons == [{"name": "Pit of Saron", "key_level": 16}]
    assert list(cache_dir.rglob("*.payload.bin")) == []


def test_clear_lookup_payload_cache_only_suppresses_matching_cache_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    blocked_cache_dir = tmp_path / "blocked-cache"
    active_cache_dir = tmp_path / "active-cache"
    original_decode = raiderio_local_mod._decode_lua_string_bytes
    cleared = False

    def clear_other_cache_during_decode(value: str) -> bytes:
        nonlocal cleared
        payload = original_decode(value)
        if not cleared:
            cleared = True
            raiderio_local_mod.clear_lookup_payload_cache(blocked_cache_dir)
        return payload

    monkeypatch.setattr(
        raiderio_local_mod,
        "_decode_lua_string_bytes",
        clear_other_cache_during_decode,
    )
    reader = RaiderIOLocalReader(tmp_path, cache_dir=active_cache_dir)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 3074
    assert list(active_cache_dir.rglob("*.payload.bin"))
    assert list(blocked_cache_dir.rglob("*.payload.bin")) == []


def test_preload_region_async_reloads_positive_cache_when_fingerprint_changes(
    tmp_path: Path,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    reader = RaiderIOLocalReader(tmp_path)
    first_completed = threading.Event()
    reader.preload_region_async("EU", on_loaded=first_completed.set)
    assert first_completed.wait(timeout=2.0)

    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3333, 0, 16, 0, 1),
    )
    _mark_test_db_changed(tmp_path)
    second_completed = threading.Event()

    reader.preload_region_async("EU", on_loaded=second_completed.set)

    assert second_completed.wait(timeout=2.0)
    refreshed = reader.lookup_profile("Chinie", "Ragnaros", "EU", allow_load=False)
    assert refreshed is not None
    assert refreshed.current_score == 3333
    assert refreshed.dungeons == [{"name": "Pit of Saron", "key_level": 16}]


def test_reader_retries_when_mplus_source_changes_during_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_mplus_generation(tmp_path, 3074, 12)
    reader = RaiderIOLocalReader(tmp_path)
    first = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074
    _write_mplus_generation(tmp_path, 3333, 16)

    load_region_db = raiderio_local_mod._RegionDB.load
    calls = 0

    def load_then_replace(
        retail_root: Path,
        token: str,
        *,
        payload_cache_dir: Path | None = None,
        payload_cache_generation: int | None = None,
    ) -> raiderio_local_mod._RegionDB | None:
        nonlocal calls
        candidate = load_region_db(
            retail_root,
            token,
            payload_cache_dir=payload_cache_dir,
            payload_cache_generation=payload_cache_generation,
        )
        calls += 1
        if calls == 1:
            _write_mplus_generation(tmp_path, 3444, 17)
        return candidate

    monkeypatch.setattr(raiderio_local_mod._RegionDB, "load", load_then_replace)

    refreshed = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert calls == 2
    assert refreshed is not None
    assert refreshed.current_score == 3444
    assert refreshed.dungeons == [{"name": "Pit of Saron", "key_level": 17}]


def test_reader_retries_when_raid_source_changes_during_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_mplus_generation(tmp_path, 3074, 12)
    _write_raid_generation(tmp_path, (2, 0, 1))
    reader = RaiderIOLocalReader(tmp_path)
    first = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.raid_progress["M"]["boss_kills"] == [2, 0, 1]
    _write_raid_generation(tmp_path, (1, 1, 0))

    load_region_db = raiderio_local_mod._RegionDB.load
    calls = 0

    def load_then_replace(
        retail_root: Path,
        token: str,
        *,
        payload_cache_dir: Path | None = None,
        payload_cache_generation: int | None = None,
    ) -> raiderio_local_mod._RegionDB | None:
        nonlocal calls
        candidate = load_region_db(
            retail_root,
            token,
            payload_cache_dir=payload_cache_dir,
            payload_cache_generation=payload_cache_generation,
        )
        calls += 1
        if calls == 1:
            _write_raid_generation(tmp_path, (0, 1, 2))
        return candidate

    monkeypatch.setattr(raiderio_local_mod._RegionDB, "load", load_then_replace)

    refreshed = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert calls == 2
    assert refreshed is not None
    assert refreshed.raid_progress["M"]["boss_kills"] == [0, 1, 2]


def test_reader_bounds_unstable_retries_and_does_not_poison_payload_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cache_dir = tmp_path / "cache"
    _write_mplus_generation(tmp_path, 3000, 11)
    reader = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir)
    previous = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert previous is not None
    assert previous.current_score == 3000
    _write_mplus_generation(tmp_path, 3111, 12)
    load_region_db = raiderio_local_mod._RegionDB.load
    generations = [(3333, 16), (3444, 17)]
    calls = 0

    def replace_then_load(
        retail_root: Path,
        token: str,
        *,
        payload_cache_dir: Path | None = None,
        payload_cache_generation: int | None = None,
    ) -> raiderio_local_mod._RegionDB | None:
        nonlocal calls
        score, pit_level = generations[calls]
        calls += 1
        _write_mplus_generation(tmp_path, score, pit_level)
        return load_region_db(
            retail_root,
            token,
            payload_cache_dir=payload_cache_dir,
            payload_cache_generation=payload_cache_generation,
        )

    monkeypatch.setattr(raiderio_local_mod._RegionDB, "load", replace_then_load)

    assert reader.lookup_profile("Chinie", "Ragnaros", "EU") == previous
    assert reader.lookup_profile("Chinie", "Ragnaros", "EU") == previous
    assert calls == 2

    _write_mplus_generation(tmp_path, 3333, 16)
    monkeypatch.setattr(raiderio_local_mod._RegionDB, "load", load_region_db)
    fresh = RaiderIOLocalReader(tmp_path, cache_dir=cache_dir).lookup_profile(
        "Chinie",
        "Ragnaros",
        "EU",
    )

    assert fresh is not None
    assert fresh.current_score == 3333
    assert fresh.dungeons == [{"name": "Pit of Saron", "key_level": 16}]


def test_concurrent_region_loads_share_one_stable_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_mplus_generation(tmp_path, 3074, 12)
    reader = RaiderIOLocalReader(tmp_path)
    load_region_db = raiderio_local_mod._RegionDB.load
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def slow_load(
        retail_root: Path,
        token: str,
        *,
        payload_cache_dir: Path | None = None,
        payload_cache_generation: int | None = None,
    ) -> raiderio_local_mod._RegionDB | None:
        nonlocal calls
        with call_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=2.0)
        return load_region_db(
            retail_root,
            token,
            payload_cache_dir=payload_cache_dir,
            payload_cache_generation=payload_cache_generation,
        )

    monkeypatch.setattr(raiderio_local_mod._RegionDB, "load", slow_load)
    results: list[RaiderIOLocalProfile | None] = []

    def lookup(started: threading.Event | None = None) -> None:
        if started is not None:
            started.set()
        results.append(reader.lookup_profile("Chinie", "Ragnaros", "EU"))

    first_thread = threading.Thread(target=lookup)
    second_thread = threading.Thread(target=lookup, args=(second_started,))
    first_thread.start()
    assert entered.wait(timeout=2.0)
    second_thread.start()
    assert second_started.wait(timeout=2.0)
    release.set()
    first_thread.join(timeout=2.0)
    second_thread.join(timeout=2.0)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert calls == 1
    assert len(results) == 2
    assert all(profile is not None for profile in results)
    assert {profile.current_score for profile in results if profile is not None} == {
        3074
    }


def test_positive_cache_reload_failure_keeps_previous_working_db(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    reader = RaiderIOLocalReader(tmp_path)
    first = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074

    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3333, 0, 16, 0, 1),
        encoding_order=(1, 99, 10),
    )
    _mark_test_db_changed(tmp_path)

    fallback = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert fallback is not None
    assert fallback.current_score == 3074
    assert fallback.dungeons == [{"name": "Pit of Saron", "key_level": 12}]


def test_failed_concurrent_positive_reload_does_not_overwrite_newer_good_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    reader = RaiderIOLocalReader(tmp_path)
    first = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074

    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3333, 0, 16, 0, 1),
    )
    _mark_test_db_changed(tmp_path)
    new_db = raiderio_local_mod._RegionDB.load(tmp_path, "eu")
    assert new_db is not None

    def fail_after_another_refresh(*_args: object, **_kwargs: object) -> object:
        fingerprint = raiderio_local_mod._region_db_fingerprint(tmp_path, "eu")
        with reader._lock:
            reader._cache["eu"] = raiderio_local_mod._RegionCacheEntry(
                db=new_db,
                fingerprint=fingerprint,
                cached_at=raiderio_local_mod.time.monotonic(),
            )
        return None

    monkeypatch.setattr(
        raiderio_local_mod._RegionDB,
        "load",
        fail_after_another_refresh,
    )

    refreshed = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert refreshed is not None
    assert refreshed.current_score == 3333
    assert refreshed.dungeons == [{"name": "Pit of Saron", "key_level": 16}]


def test_failed_positive_reload_does_not_overwrite_newer_fingerprint_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )
    reader = RaiderIOLocalReader(tmp_path)
    first = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 3074

    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3333, 0, 16, 0, 1),
    )
    _mark_test_db_changed(tmp_path)

    load_region_db = raiderio_local_mod._RegionDB.load

    def fail_after_newer_refresh(*_args: object, **_kwargs: object) -> object:
        _write_test_db(
            tmp_path,
            _record(3200, 15, 14, 1, 0) + _record(3444, 0, 17, 0, 1),
        )
        _mark_test_db_changed(tmp_path)
        newer_db = load_region_db(tmp_path, "eu")
        newer_fingerprint = raiderio_local_mod._region_db_fingerprint(tmp_path, "eu")
        assert newer_db is not None
        with reader._lock:
            reader._cache["eu"] = raiderio_local_mod._RegionCacheEntry(
                db=newer_db,
                fingerprint=newer_fingerprint,
                cached_at=raiderio_local_mod.time.monotonic(),
            )
        return None

    monkeypatch.setattr(
        raiderio_local_mod._RegionDB,
        "load",
        fail_after_newer_refresh,
    )

    refreshed = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert refreshed is not None
    assert refreshed.current_score == 3444
    assert refreshed.dungeons == [{"name": "Pit of Saron", "key_level": 17}]


def test_lookup_profile_retries_malformed_db_when_files_become_valid(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 1),
        encoding_order=(1, 99, 10),
    )
    reader = RaiderIOLocalReader(tmp_path)

    assert reader.lookup_profile("Chinie", "Ragnaros", "EU") is None

    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 0) + _record(3074, 0, 12, 0, 2),
    )

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")
    assert profile is not None
    assert profile.dungeons == [{"name": "Pit of Saron", "key_level": 12}]


def test_reader_rejects_unknown_encoding_field_id(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 1),
        encoding_order=(1, 99, 10),
    )
    reader = RaiderIOLocalReader(tmp_path)

    assert reader.lookup_profile("Chinie", "Ragnaros", "EU") is None
    assert "unsupported RaiderIO encoding field" in caplog.text


def test_reader_rejects_known_field_bit_budget_overrun(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    _write_test_db(
        tmp_path,
        _record(3200, 15, 14, 1, 1),
        record_size=4,
        encoding_order=(1, 2, 10),
    )
    reader = RaiderIOLocalReader(tmp_path)

    assert reader.lookup_profile("Chinie", "Ragnaros", "EU") is None
    assert "exceeds record size" in caplog.text


def test_reader_allows_exact_fit_encoding_budget(tmp_path: Path):
    _write_test_db(
        tmp_path,
        _record(3000, 10, 10, 1, 1) + _record(3200, 15, 14, 1, 1),
        record_size=5,
        encoding_order=(1, 2, 10),
    )
    reader = RaiderIOLocalReader(tmp_path)

    profile = reader.lookup_profile("Chinie", "Ragnaros", "EU")

    assert profile is not None
    assert profile.current_score == 3200


def test_lookup_profile_returns_none_when_record_decode_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_test_db(
        tmp_path,
        _record(3000, 10, 10, 1, 1) + _record(3200, 15, 14, 1, 1),
    )

    def fail_decode(*_args: object) -> object:
        raise ValueError("decode drift")

    monkeypatch.setattr(raiderio_local_mod, "_decode_profile", fail_decode)
    reader = RaiderIOLocalReader(tmp_path)

    assert reader.lookup_profile("Chinie", "Ragnaros", "EU") is None
    assert "could not decode RaiderIO local profile" in caplog.text
