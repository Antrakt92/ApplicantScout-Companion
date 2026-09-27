"""Damaged local cache files must not prevent startup or screenshot scanning."""

from pathlib import Path

import pytest

from applicant_scout import screenshot as screenshot_mod
from applicant_scout.state import WindowGeometry, load_geometry, load_launcher_position
from applicant_scout.wcl import CharacterCache, WCLAuth


@pytest.fixture(params=["deep-nesting", "oversized-integer"])
def corrupt_json(request):
    if request.param == "deep-nesting":
        return "[" * 20000 + "]" * 20000
    # Exercise Python's JSON integer conversion guard deterministically.
    import sys

    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    request.addfinalizer(lambda: sys.set_int_max_str_digits(previous))
    return '{"value":' + "1" * 5000 + "}"


def test_character_cache_recovers_from_parser_resource_limits(tmp_path, corrupt_json):
    path = tmp_path / "character-cache.json"
    path.write_text(corrupt_json, encoding="utf-8")

    cache = CharacterCache(tmp_path)

    assert cache.get("Scout", "ravencrest", "EU", 71) is None
    backups = list(tmp_path.glob("character-cache.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == corrupt_json
    assert not path.exists()
    cache.close()


def test_auth_cache_recovers_from_parser_resource_limits(tmp_path, corrupt_json):
    path = tmp_path / "token.json"
    path.write_text(corrupt_json, encoding="utf-8")

    auth = WCLAuth("synthetic-client", "synthetic-secret", tmp_path)

    assert auth._token is None


@pytest.mark.parametrize("filename", ["window.json", "launcher.json"])
def test_geometry_recovers_from_parser_resource_limits(tmp_path, corrupt_json, filename):
    path = tmp_path / filename
    path.write_text(corrupt_json, encoding="utf-8")

    if filename == "window.json":
        assert load_geometry(tmp_path) == WindowGeometry()
    else:
        assert load_launcher_position(tmp_path) is None

    backups = list(tmp_path.glob(f"{filename}.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == corrupt_json
    assert not path.exists()


def test_manual_index_recovers_and_persists_new_fingerprints(
    tmp_path: Path, corrupt_json: str
):
    path = tmp_path / "manual-index.json"
    path.write_text(corrupt_json, encoding="utf-8")
    index = screenshot_mod._ManualScreenshotIndex(path)

    assert index.snapshot() == set()
    key = screenshot_mod._ScreenshotWorkKey("synthetic-shot.jpg", 123, 456)
    index.note_manual(key, flush=True)

    assert screenshot_mod._ManualScreenshotIndex(path).contains(key)
