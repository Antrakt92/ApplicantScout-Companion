from pathlib import Path

import applicant_scout.wow_install_discovery as discovery


def _install(drive: Path, client_name: str = "_retail_") -> Path:
    client_root = drive / "Games" / "World of Warcraft" / client_name
    (client_root / "Screenshots").mkdir(parents=True)
    (client_root / "WTF").mkdir()
    return client_root


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


def test_moved_ptr_discovers_only_same_client_even_when_retail_is_running(
    monkeypatch, tmp_path: Path,
):
    ptr = _install(tmp_path / "F", "_ptr_")
    retail = _install(tmp_path / "D")
    monkeypatch.setattr(discovery, "_running_wow_roots", lambda: [retail, ptr])
    monkeypatch.setattr(discovery, "_fixed_drive_roots", lambda: [tmp_path / "D", tmp_path / "F"])

    assert discovery.discover_wow_screenshots(
        tmp_path / "old" / "Games" / "World of Warcraft" / "_ptr_" / "Screenshots"
    ) == ptr / "Screenshots"


def test_moved_retail_never_switches_to_ptr(monkeypatch, tmp_path: Path):
    ptr = _install(tmp_path / "F", "_ptr_")
    monkeypatch.setattr(discovery, "_running_wow_roots", lambda: [ptr])
    monkeypatch.setattr(discovery, "_fixed_drive_roots", lambda: [tmp_path / "F"])

    assert discovery.discover_wow_screenshots(
        tmp_path / "old" / "Games" / "World of Warcraft" / "_retail_" / "Screenshots"
    ) is None


def test_moved_xptr_can_be_discovered_without_running_client(monkeypatch, tmp_path: Path):
    xptr = _install(tmp_path / "F", "_xptr_")
    monkeypatch.setattr(discovery, "_running_wow_roots", lambda: [])
    monkeypatch.setattr(discovery, "_fixed_drive_roots", lambda: [tmp_path / "F"])

    assert discovery.discover_wow_screenshots(
        tmp_path / "old" / "Games" / "World of Warcraft" / "_xptr_" / "Screenshots"
    ) == xptr / "Screenshots"
