"""Keep real startup collaborators inside each test's temporary user profile."""

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--native-lua51", help="Lua 5.1 executable for real native decoder checks")
    parser.addoption("--native-addon-root", help="Paired addon checkout for real QR fixtures")


@pytest.fixture(autouse=True)
def isolated_user_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Stubbing load_config does not isolate independently created usage state,
    # caches, or startup shortcuts. Never let those reach the actual user profile.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user-local-data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "user-roaming-data"))
