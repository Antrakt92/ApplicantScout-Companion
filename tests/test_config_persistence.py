from pathlib import Path

import pytest

from applicant_scout.config import (
    load_config,
    read_user_config_values,
    save_config_values,
    save_discovered_screenshots_path,
    user_config_path,
)


@pytest.mark.parametrize("directory", ["Игры", "遊戲", "Jeux été", "Games 🎮"])
def test_saved_unicode_screenshot_path_survives_restart(tmp_path: Path, directory: str):
    screenshots = tmp_path / directory / "_retail_" / "Screenshots"
    save_config_values(
        wcl_client_id="synthetic-client",
        wcl_client_secret="synthetic-secret",
        region="EU",
        screenshots_path=str(screenshots),
    )

    assert load_config().screenshots_path == screenshots


def test_discovered_unicode_path_survives_restart(tmp_path: Path):
    save_config_values(
        wcl_client_id="synthetic-client",
        wcl_client_secret="synthetic-secret",
        region="EU",
        screenshots_path="C:/old/_retail_/Screenshots",
    )
    screenshots = tmp_path / "Игры 遊戲" / "_retail_" / "Screenshots"

    save_discovered_screenshots_path(screenshots, config_path=user_config_path())

    cfg = load_config()
    assert cfg.screenshots_path == screenshots
    assert cfg.wcl_client_secret == "synthetic-secret"


def test_settings_save_preserves_unknown_multiline_bindings(tmp_path: Path):
    target = tmp_path / "settings.env"
    unknown = 'FUTURE_VALUE="first\nWCL_CLIENT_ID=literal text\nlast"\n'
    target.write_text(
        '# preserved comment\nWCL_CLIENT_ID="old\nvalue"\n' + unknown,
        encoding="utf-8",
    )

    save_config_values(
        wcl_client_id="new-client",
        wcl_client_secret="synthetic-secret",
        region="EU",
        config_path=target,
    )

    values = read_user_config_values(target)
    assert values["WCL_CLIENT_ID"] == "new-client"
    assert values["FUTURE_VALUE"] == "first\nWCL_CLIENT_ID=literal text\nlast"
    assert unknown in target.read_text(encoding="utf-8")
    assert "# preserved comment\n" in target.read_text(encoding="utf-8")


def test_path_discovery_replaces_only_the_actual_binding(tmp_path: Path):
    target = tmp_path / "settings.env"
    unknown = 'FUTURE_VALUE="first\nAPSCOUT_SCREENSHOTS_PATH=literal text\nlast"\n'
    target.write_text(
        unknown + 'export APSCOUT_SCREENSHOTS_PATH="old\npath"\n',
        encoding="utf-8",
    )
    screenshots = tmp_path / "new" / "_retail_" / "Screenshots"

    save_discovered_screenshots_path(screenshots, config_path=target)

    values = read_user_config_values(target)
    assert values["APSCOUT_SCREENSHOTS_PATH"] == str(screenshots)
    assert values["FUTURE_VALUE"] == "first\nAPSCOUT_SCREENSHOTS_PATH=literal text\nlast"
    assert unknown in target.read_text(encoding="utf-8")
