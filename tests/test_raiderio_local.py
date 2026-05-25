from __future__ import annotations

import threading
from pathlib import Path

import pytest

import applicant_scout.raiderio_local as raiderio_local_mod
from applicant_scout.raiderio_local import RaiderIOLocalReader


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
        'local provider={name=...,data=1,region="eu",db={}}\n'
        'provider.db["Ragnaros"]={0,"Alphapack","Chinie"}\n',
        encoding="utf-8",
    )
    encoded = "".join(f"\\{byte}" for byte in lookup_payload)
    order = ",".join(str(value) for value in encoding_order)
    (db / "db_mythicplus_eu_lookup.lua").write_text(
        'local provider={name=...,data=1,region="eu",lookup={},'
        f"recordSizeInBytes={record_size},encodingOrder={{{order}}}}}\n"
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
        'local provider={name=...,data=2,region="eu",db={}}\n'
        'provider.db["Ragnaros"]={0,"Alphapack","Chinie"}\n',
        encoding="utf-8",
    )
    encoded = "".join(f"\\{byte}" for byte in lookup_payload)
    order = ",".join(str(value) for value in encoding_order)
    (db / "db_raiding_eu_lookup.lua").write_text(
        'local provider={name=...,data=2,region="eu",lookup={},'
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
        'local provider={name=...,data=2,region="eu",db={}}\n'
        'provider.db["Ragnaros"]={0,"Chinie"}\n',
        encoding="utf-8",
    )
    (db / "db_raiding_eu_lookup.lua").write_text(
        'local provider={name=...,data=2,region="eu",lookup={},'
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
        'local provider={name=...,data=1,region="eu",db={}}\n'
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
