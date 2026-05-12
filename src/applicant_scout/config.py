"""Configuration loading and persistence for the companion."""

from __future__ import annotations

import os
import sys
import json
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .atomic_io import atomic_write_text
from .metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences


MAX_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
CONFIG_ENV_FILENAME = "config.env"


@dataclass
class Config:
    wcl_client_id: str
    wcl_client_secret: str
    chatlog_path: Path
    region: str
    cache_dir: Path
    config_dir: Path
    # Optional override for Screenshots/ folder used by screenshot-transport.
    # If None, resolve_screenshots_path() derives from the legacy chatlog_path.
    # Override via APSCOUT_SCREENSHOTS_PATH env.
    screenshots_path: Path | None = None
    # Optional CharacterCache TTL override. None keeps the cache default.
    cache_ttl_seconds: int | None = None
    config_path: Path | None = None
    log_dir: Path | None = None
    metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES
    sync_with_wow: bool = False
    draft_wcl_client_id: str = ""
    draft_wcl_client_secret: str = ""


class ConfigError(RuntimeError):
    """Actionable configuration/setup error shown before app startup."""


def _screenshots_setup_message(details: str) -> str:
    return (
        f"{details}. Set APSCOUT_SCREENSHOTS_PATH to your active "
        r"_retail_\Screenshots folder."
    )


def _default_chatlog_path() -> Path:
    """Default WoW retail chatlog path."""
    candidates = [
        Path(r"C:\Games\World of Warcraft\_retail_\Logs\WoWChatLog.txt"),
        Path(r"C:\Program Files (x86)\World of Warcraft\_retail_\Logs\WoWChatLog.txt"),
        Path.home() / "World of Warcraft" / "_retail_" / "Logs" / "WoWChatLog.txt",
    ]
    for p in candidates:
        if p.parent.exists():
            return p
    return candidates[0]


def _retail_root_from_legacy_path(path: Path) -> Path:
    """Infer _retail_ root from legacy chatlog path or Logs directory."""
    if path.name.lower() == "wowchatlog.txt" and path.parent.name.lower() == "logs":
        retail_root = path.parent.parent
    elif path.name.lower() == "logs":
        retail_root = path.parent
    else:
        raise ConfigError(
            _screenshots_setup_message(
                f"Cannot infer Screenshots folder from legacy path {path}"
            )
        )
    if retail_root.name.lower() != "_retail_":
        raise ConfigError(
            _screenshots_setup_message(
                f"Legacy path {path} is not under a _retail_ folder"
            )
        )
    return retail_root


def _looks_like_wow_retail_root(retail_root: Path) -> bool:
    file_markers = (retail_root / "Wow.exe",)
    dir_markers = (
        retail_root / "Interface",
        retail_root / "Interface" / "AddOns",
        retail_root / "WTF",
    )
    return any(marker.is_file() for marker in file_markers) or any(
        marker.is_dir() for marker in dir_markers
    )


def screenshots_path_health_warning(path: Path) -> str | None:
    """Return a non-fatal warning for paths that look unlike WoW screenshots."""
    path = Path(path)
    problems: list[str] = []
    if path.name.lower() != "screenshots":
        problems.append("folder is not named Screenshots")

    retail_root = next(
        (parent for parent in (path, *path.parents) if parent.name.lower() == "_retail_"),
        None,
    )
    if retail_root is None:
        problems.append(r"path is not under a _retail_ folder")
    elif not retail_root.exists():
        problems.append(r"_retail_ folder does not exist")
    elif not _looks_like_wow_retail_root(retail_root):
        problems.append(r"_retail_ folder has no WoW install markers")

    if not problems:
        return None
    return "Screenshots folder warning: " + "; ".join(problems) + "."


def screenshots_path_validation_error(path: Path) -> str | None:
    """Return a blocking validation error for user-provided watcher paths."""
    return screenshots_path_health_warning(path)


def resolve_screenshots_path(cfg: Config) -> Path:
    """Return the Screenshots directory without creating inferred bogus roots."""
    if cfg.screenshots_path is not None:
        screenshots_path = Path(cfg.screenshots_path)
        if screenshots_path.exists() and screenshots_path.is_file():
            raise ConfigError(
                f"APSCOUT_SCREENSHOTS_PATH points to a file, not a folder: "
                f"{screenshots_path}"
            )
        warning = screenshots_path_validation_error(screenshots_path)
        if warning is not None:
            raise ConfigError(warning)
        return screenshots_path

    legacy_path = Path(cfg.chatlog_path)
    retail_root = _retail_root_from_legacy_path(legacy_path)
    if not retail_root.exists() or not retail_root.is_dir():
        raise ConfigError(
            _screenshots_setup_message(
                f"Inferred WoW retail root does not exist: {retail_root}"
            )
        )
    if not _looks_like_wow_retail_root(retail_root):
        raise ConfigError(
            _screenshots_setup_message(
                f"Inferred WoW retail root does not look like a WoW install: {retail_root}"
            )
        )
    return retail_root / "Screenshots"


def _user_data_dir() -> Path:
    """Per-user app data dir (Windows: %LOCALAPPDATA%\\applicant-scout)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "applicant-scout"


def user_config_dir() -> Path:
    return _user_data_dir() / "config"


def user_cache_dir() -> Path:
    return _user_data_dir() / "cache"


def user_log_dir() -> Path:
    return _user_data_dir() / "logs"


def user_config_path() -> Path:
    return user_config_dir() / CONFIG_ENV_FILENAME


def _legacy_env_path() -> Path | None:
    """Developer/backcompat .env path.

    Packaged builds must not read a random process CWD. In source/dev runs we
    still accept repo-local `.env` so existing checkouts do not break.
    """
    if getattr(sys, "frozen", False):
        return None
    candidate = Path.cwd() / ".env"
    return candidate if candidate.exists() else None


def _read_env_file(path: Path) -> dict[str, str]:
    raw = dotenv_values(path)
    values: dict[str, str] = {}
    for key, value in raw.items():
        if value is not None:
            values[key] = value
    return values


def _config_values() -> dict[str, str]:
    config_path = user_config_path()
    if config_path.exists():
        return _read_env_file(config_path)
    legacy_path = _legacy_env_path()
    if legacy_path is not None:
        return _read_env_file(legacy_path)
    return {}


def _value(values: dict[str, str], key: str, default: str = "") -> str:
    env_value = os.environ.get(key)
    if env_value is not None:
        return env_value.strip()
    return values.get(key, default).strip()


def is_config_ready(cfg: Config) -> bool:
    return bool(cfg.wcl_client_id.strip() and cfg.wcl_client_secret.strip())


def _env_line(key: str, value: str) -> str:
    clean = value.replace("\r", " ").replace("\n", " ").strip()
    return f"{key}={json.dumps(clean)}\n"


def _bool_env_line(key: str, value: bool) -> str:
    return _env_line(key, "1" if value else "0")


def _write_private_text(path: Path, text: str) -> None:
    atomic_write_text(path, text, private=True)


def save_config_values(
    *,
    wcl_client_id: str,
    wcl_client_secret: str,
    region: str,
    draft_wcl_client_id: str = "",
    draft_wcl_client_secret: str = "",
    screenshots_path: str = "",
    cache_ttl_seconds: int | None = None,
    metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
    sync_with_wow: bool = False,
    chatlog_path: str = "",
    config_path: Path | None = None,
) -> Path:
    """Persist user-editable settings to the local companion config area."""
    target = config_path or user_config_path()
    lines = [
        _env_line("WCL_CLIENT_ID", wcl_client_id),
        _env_line("WCL_CLIENT_SECRET", wcl_client_secret),
        _env_line("APSCOUT_DRAFT_WCL_CLIENT_ID", draft_wcl_client_id),
        _env_line("APSCOUT_DRAFT_WCL_CLIENT_SECRET", draft_wcl_client_secret),
        _env_line("APSCOUT_REGION", region.upper() or "EU"),
        _bool_env_line("APSCOUT_FETCH_MPLUS", metric_preferences.mplus),
        _bool_env_line("APSCOUT_FETCH_RAID_NORMAL", metric_preferences.raid_normal),
        _bool_env_line("APSCOUT_FETCH_RAID_HEROIC", metric_preferences.raid_heroic),
        _bool_env_line("APSCOUT_FETCH_RAID_MYTHIC", metric_preferences.raid_mythic),
        _bool_env_line("APSCOUT_SYNC_WITH_WOW", sync_with_wow),
    ]
    if screenshots_path.strip():
        lines.append(_env_line("APSCOUT_SCREENSHOTS_PATH", screenshots_path))
    if cache_ttl_seconds is not None:
        lines.append(_env_line("APSCOUT_CACHE_TTL_SECONDS", str(cache_ttl_seconds)))
    if chatlog_path.strip():
        lines.append(_env_line("APSCOUT_CHATLOG_PATH", chatlog_path))
    _write_private_text(target, "".join(lines))
    return target


def _parse_cache_ttl_seconds(raw: str | None) -> int | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not raw.isdecimal():
        raise ConfigError(
            "APSCOUT_CACHE_TTL_SECONDS must be a positive integer number of seconds"
        )
    value = int(raw)
    if value <= 0 or value > MAX_CACHE_TTL_SECONDS:
        raise ConfigError(
            "APSCOUT_CACHE_TTL_SECONDS must be between 1 and "
            f"{MAX_CACHE_TTL_SECONDS} seconds"
        )
    return value


def _parse_bool_setting(raw: str | None, *, default: bool = True) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def load_config() -> Config:
    """Load config values without prompting or depending on process CWD."""
    values = _config_values()
    client_id = _value(values, "WCL_CLIENT_ID")
    client_secret = _value(values, "WCL_CLIENT_SECRET")
    draft_client_id = _value(values, "APSCOUT_DRAFT_WCL_CLIENT_ID")
    draft_client_secret = _value(values, "APSCOUT_DRAFT_WCL_CLIENT_SECRET")

    chatlog_override = _value(values, "APSCOUT_CHATLOG_PATH")
    chatlog_path = (
        Path(chatlog_override) if chatlog_override else _default_chatlog_path()
    )

    screenshots_override = _value(values, "APSCOUT_SCREENSHOTS_PATH")
    screenshots_path = Path(screenshots_override) if screenshots_override else None

    region = _value(values, "APSCOUT_REGION", "EU").upper()
    cache_ttl_seconds = _parse_cache_ttl_seconds(_value(values, "APSCOUT_CACHE_TTL_SECONDS", ""))
    metric_preferences = MetricPreferences(
        mplus=_parse_bool_setting(_value(values, "APSCOUT_FETCH_MPLUS", "1")),
        raid_normal=_parse_bool_setting(_value(values, "APSCOUT_FETCH_RAID_NORMAL", "1")),
        raid_heroic=_parse_bool_setting(_value(values, "APSCOUT_FETCH_RAID_HEROIC", "1")),
        raid_mythic=_parse_bool_setting(_value(values, "APSCOUT_FETCH_RAID_MYTHIC", "1")),
    )
    sync_with_wow = _parse_bool_setting(
        _value(values, "APSCOUT_SYNC_WITH_WOW", "0"),
        default=False,
    )

    cache_dir = user_cache_dir()
    config_dir = user_config_dir()
    log_dir = user_log_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        wcl_client_id=client_id,
        wcl_client_secret=client_secret,
        chatlog_path=chatlog_path,
        region=region,
        cache_dir=cache_dir,
        config_dir=config_dir,
        screenshots_path=screenshots_path,
        cache_ttl_seconds=cache_ttl_seconds,
        config_path=user_config_path(),
        log_dir=log_dir,
        metric_preferences=metric_preferences,
        sync_with_wow=sync_with_wow,
        draft_wcl_client_id=draft_client_id,
        draft_wcl_client_secret=draft_client_secret,
    )
