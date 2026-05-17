from __future__ import annotations

from pathlib import Path

from applicant_scout.raiderio_local import RaiderIOLocalReader


def _write_test_db(root: Path, lookup_payload: bytes) -> None:
    db = root / "Interface" / "AddOns" / "RaiderIO" / "db"
    db.mkdir(parents=True)
    (db / "db_dungeons.lua").write_text(
        """
local _, ns = ...
ns.dungeons = {
    [1] = { ["name"] = "Skyreach", ["shortName"] = "SR" },
    [2] = { ["name"] = "Pit of Saron", ["shortName"] = "POS" },
}
""",
        encoding="utf-8",
    )
    (db / "db_mythicplus_eu_characters.lua").write_text(
        'local provider={name=...,data=1,region="eu",db={}}\n'
        'provider.db["Ragnaros"]={0,"Alphapack","Chinie"}\n',
        encoding="utf-8",
    )
    encoded = "".join(f"\\{byte}" for byte in lookup_payload)
    (db / "db_mythicplus_eu_lookup.lua").write_text(
        'local provider={name=...,data=1,region="eu",lookup={},'
        'recordSizeInBytes=5,encodingOrder={1,2,10}}\n'
        f'provider.lookup[1] = "{encoded}"\n',
        encoding="utf-8",
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
