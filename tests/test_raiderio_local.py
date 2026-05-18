from __future__ import annotations

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
    db.mkdir(parents=True)
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
