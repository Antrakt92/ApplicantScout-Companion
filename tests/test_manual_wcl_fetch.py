from __future__ import annotations

from pathlib import Path

from applicant_scout.config import Config
from applicant_scout.metric_preferences import DEFAULT_METRIC_PREFERENCES
from applicant_scout.wcl import CharacterRanks
from scripts import manual_wcl_fetch


def _cfg(tmp_path: Path) -> Config:
    return Config(
        wcl_client_id="client",
        wcl_client_secret="secret",
        chatlog_path=tmp_path / "World of Warcraft" / "_retail_" / "Logs" / "WoWChatLog.txt",
        region="US",
        cache_dir=tmp_path / "cache",
        config_dir=tmp_path / "config",
        metric_preferences=DEFAULT_METRIC_PREFERENCES,
    )


def _empty_ranks() -> CharacterRanks:
    return CharacterRanks.empty()


def test_manual_wcl_fetch_cli_passes_character_realm_region_spec_and_role(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    seen: dict[str, object] = {}

    class FakeAuth:
        def __init__(self, client_id: str, client_secret: str, cache_dir: Path) -> None:
            seen["auth"] = (client_id, client_secret, cache_dir)

    class FakeClient:
        last_quota = None

        def __init__(self, auth: FakeAuth, *, region: str) -> None:
            seen["region"] = region

        def fetch_character_ranks(
            self,
            name: str,
            server_slug: str,
            spec_id: int,
            role: str,
        ) -> CharacterRanks:
            seen["fetch"] = (name, server_slug, spec_id, role)
            return _empty_ranks()

        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(manual_wcl_fetch, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(manual_wcl_fetch, "WCLAuth", FakeAuth)
    monkeypatch.setattr(manual_wcl_fetch, "WCLClient", FakeClient)

    rc = manual_wcl_fetch.main(
        [
            "Bites",
            "Twisting Nether",
            "--region",
            "EU",
            "--spec-id",
            "1480",
            "--role",
            "DAMAGER",
        ]
    )

    assert rc == 0
    assert seen["region"] == "EU"
    assert seen["fetch"] == ("Bites", "twisting-nether", 1480, "DAMAGER")
    assert seen["closed"] is True
    assert "Bites" in capsys.readouterr().out


def test_manual_wcl_fetch_defaults_region_from_config(monkeypatch, tmp_path: Path):
    seen: dict[str, object] = {}

    class FakeAuth:
        def __init__(self, *_args: object) -> None:
            pass

    class FakeClient:
        last_quota = None

        def __init__(self, _auth: FakeAuth, *, region: str) -> None:
            seen["region"] = region

        def fetch_character_ranks(self, *_args: object) -> CharacterRanks:
            return _empty_ranks()

        def close(self) -> None:
            pass

    monkeypatch.setattr(manual_wcl_fetch, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(manual_wcl_fetch, "WCLAuth", FakeAuth)
    monkeypatch.setattr(manual_wcl_fetch, "WCLClient", FakeClient)

    assert manual_wcl_fetch.main(["Bites", "Ravencrest"]) == 0
    assert seen["region"] == "US"


def test_manual_wcl_fetch_rejects_unknown_region():
    try:
        manual_wcl_fetch.parse_args(["Bites", "Ravencrest", "--region", "moon"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("manual WCL fetch must reject unknown regions")


def test_manual_wcl_fetch_healer_role_controls_empty_mplus_output(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    class FakeAuth:
        def __init__(self, *_args: object) -> None:
            pass

    class FakeClient:
        last_quota = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def fetch_character_ranks(self, *_args: object) -> CharacterRanks:
            return _empty_ranks()

        def close(self) -> None:
            pass

    monkeypatch.setattr(manual_wcl_fetch, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(manual_wcl_fetch, "WCLAuth", FakeAuth)
    monkeypatch.setattr(manual_wcl_fetch, "WCLClient", FakeClient)

    assert manual_wcl_fetch.main(["Healz", "Ravencrest", "--role", "HEALER"]) == 0

    assert "M+ HPS Headline" in capsys.readouterr().out
