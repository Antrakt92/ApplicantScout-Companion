"""Keep real startup collaborators inside each test's temporary user profile.

Display policy: the suite is headless-quiet by default. Unless the caller
opts into a real display, ``QT_QPA_PLATFORM`` defaults to ``offscreen`` (set
here, before any Qt import) so ``show()``/``activateWindow()``/mouse calls
render virtually instead of popping windows and moving the real cursor.
An explicit ``QT_QPA_PLATFORM`` value always wins; ``APSCOUT_REAL_DISPLAY=1``
opts out of the default entirely.

Layout-sensitive tests that need real-display font metrics are marked
``@pytest.mark.real_display``. They skip while the effective platform is
offscreen; run them explicitly with::

    APSCOUT_REAL_DISPLAY=1 .venv\\Scripts\\python -m pytest tests -m real_display

``scripts/check.ps1`` runs that group as a separate step.
"""

import os

if os.environ.get("APSCOUT_REAL_DISPLAY") != "1":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--native-lua51", help="Lua 5.1 executable for real native decoder checks")
    parser.addoption("--native-addon-root", help="Paired addon checkout for real QR fixtures")


@pytest.fixture(autouse=True)
def isolated_user_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Stubbing load_config does not isolate independently created usage state,
    # caches, or startup shortcuts. Never let those reach the actual user profile.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user-local-data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "user-roaming-data"))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Real-display tests assert native font/layout metrics, so they cannot run
    # on the virtual offscreen platform. Skip them there (the reason names the
    # explicit command) instead of failing; scripts/check.ps1 runs the group
    # separately with a real display.
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        return
    skip_offscreen = pytest.mark.skip(
        reason="needs real-display font metrics: "
        "APSCOUT_REAL_DISPLAY=1 .venv\\Scripts\\python -m pytest tests -m real_display"
    )
    for item in items:
        if "real_display" in item.keywords:
            item.add_marker(skip_offscreen)
