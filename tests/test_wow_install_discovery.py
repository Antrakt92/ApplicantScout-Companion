from pathlib import Path

import applicant_scout.wow_install_discovery as discovery


def _install(drive: Path) -> Path:
    retail = drive / "Games" / "World of Warcraft" / "_retail_"
    (retail / "Screenshots").mkdir(parents=True)
    (retail / "WTF").mkdir()
    return retail


def test_finds_moved_install_on_another_drive(monkeypatch, tmp_path: Path):
    old = tmp_path / "D"
    new = tmp_path / "F"
    old.mkdir()
    new.mkdir()
    retail = _install(new)
    monkeypatch.setattr(discovery, "_running_wow_roots", lambda: [])
    monkeypatch.setattr(discovery, "_fixed_drive_roots", lambda: [old, new])

    assert discovery.discover_wow_screenshots(
        old / "Games" / "World of Warcraft" / "_retail_" / "Screenshots"
    ) == retail / "Screenshots"


def test_running_client_wins_when_multiple_installs_exist(monkeypatch, tmp_path: Path):
    first = _install(tmp_path / "D")
    active = _install(tmp_path / "F")
    monkeypatch.setattr(discovery, "_running_wow_roots", lambda: [active])
    monkeypatch.setattr(discovery, "_fixed_drive_roots", lambda: [tmp_path / "D", tmp_path / "F"])

    assert discovery.discover_wow_screenshots(first / "Screenshots") == active / "Screenshots"


def test_ambiguous_stopped_install_needs_manual_selection(monkeypatch, tmp_path: Path):
    first = _install(tmp_path / "D")
    _install(tmp_path / "F")
    monkeypatch.setattr(discovery, "_running_wow_roots", lambda: [])
    monkeypatch.setattr(discovery, "_fixed_drive_roots", lambda: [tmp_path / "D", tmp_path / "F"])

    assert discovery.discover_wow_screenshots(first / "Screenshots") is None
