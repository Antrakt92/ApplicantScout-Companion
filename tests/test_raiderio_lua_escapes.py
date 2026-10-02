from pathlib import Path
import shutil
import subprocess

import pytest

from applicant_scout import raiderio_local as rio
from test_raiderio_local import _record, _write_test_db


@pytest.mark.parametrize("literal, expected", [
    (r"\a\b\f\n\r\t\v", bytes([7, 8, 12, 10, 13, 9, 11])),
    (r'\\\"\'', b"\\\"'"),
    (r"\0\255\0100", bytes([0, 255, 10, 48])),
    ("before\\\nafter", b"before\nafter"),
    ("before\\\r\nafter", b"before\nafter"),
    ("before\\\n\rafter", b"before\nafter"),
    ("é", "é".encode("utf-8")),
])
def test_supported_lua_escapes_match_native_bytes(request, tmp_path, literal, expected):
    assert rio._decode_lua_string_bytes(literal) == expected
    lua = request.config.getoption("--native-lua51") or shutil.which("lua5.1")
    assert lua, "Lua 5.1 is required to independently check binary string semantics"
    source = tmp_path / "escapes.lua"
    source.write_text(
        'local value = "' + literal + '"\n'
        'for i = 1, #value do io.write(string.byte(value, i), " ") end\n',
        encoding="utf-8", newline="",
    )
    result = subprocess.run([lua, str(source)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert bytes(int(value) for value in result.stdout.split()) == expected


@pytest.mark.parametrize("literal", [r"\256", r"\999", "trailing\\", r"\z", r"\x0a"])
def test_invalid_or_unsupported_escapes_fail_closed(literal):
    with pytest.raises(ValueError):
        rio._decode_lua_string_bytes(literal)


def test_named_newline_keeps_score_and_legacy_decoded_cache_is_not_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(rio, "_EXPECTED_RAIDERIO_DUNGEON_ORDER", ("Skyreach", "Pit of Saron"))
    _write_test_db(tmp_path, _record(2570, 15, 14, 1, 0) + _record(2570, 0, 12, 0, 2))
    source = tmp_path / "Interface/AddOns/RaiderIO/db/db_mythicplus_eu_lookup.lua"
    source.write_text(source.read_text(encoding="utf-8").replace(r"\10", r"\n"), encoding="utf-8")
    cache_dir = tmp_path / "cache"
    first = rio.RaiderIOLocalReader(tmp_path, cache_dir=cache_dir).lookup_profile("Chinie", "Ragnaros", "EU")
    assert first is not None
    assert first.current_score == 2570
    current = next(cache_dir.rglob("*.payload.bin"))
    legacy = Path(str(current).replace(".v4.", ".v3."))
    payload = current.read_bytes()
    legacy.write_bytes(b"ASRIOv3\0" + payload[8:])
    current.unlink()
    second = rio.RaiderIOLocalReader(tmp_path, cache_dir=cache_dir).lookup_profile("Chinie", "Ragnaros", "EU")
    assert second is not None
    assert second.current_score == 2570
    assert current.exists()
