"""GitHub Releases update checks for the companion."""

from __future__ import annotations

import re
import hashlib
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from .config import user_cache_dir


DEFAULT_RELEASE_REPO = "Antrakt92/ApplicantScout-Companion"
GITHUB_API_BASE = "https://api.github.com"
_SEMVER_RE = re.compile(r"^\s*[vV]?(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?\s*$")

UpdateStatus = Literal["available", "up_to_date", "unavailable"]
_INSTALLER_PREFIX = "ApplicantScoutCompanionSetup-"
_INSTALLER_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
_SELF_UPDATE_FLAG = "/APSCOUT_SELFUPDATE=1"
_MAX_INSTALLER_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_CHECKSUM_DOWNLOAD_BYTES = 8 * 1024


@dataclass(frozen=True)
class UpdateResult:
    status: UpdateStatus
    message: str
    current_version: str
    reason: str | None = None
    latest_version: str | None = None
    release_url: str | None = None
    asset_url: str | None = None
    asset_name: str | None = None
    checksum_url: str | None = None
    checksum_name: str | None = None

    @property
    def open_url(self) -> str | None:
        return self.asset_url or self.release_url


@dataclass(frozen=True)
class InstallerLaunch:
    installer_path: Path
    _process: subprocess.Popen[Any] = field(repr=False, compare=False)

    @property
    def pid(self) -> int | None:
        return getattr(self._process, "pid", None)

    def poll(self) -> int | None:
        return self._process.poll()


def _semver_key(version: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.match(version)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _semver_text(version: str) -> str | None:
    key = _semver_key(version)
    if key is None:
        return None
    return f"{key[0]}.{key[1]}.{key[2]}"


def _version_tuple(version: str) -> tuple[int, int, int]:
    return _semver_key(version) or (0, 0, 0)


def _is_newer(latest: str, current: str) -> bool:
    latest_key = _semver_key(latest)
    if latest_key is None:
        return False
    return latest_key > _version_tuple(current)


def _asset_download_url(asset: dict[str, Any]) -> str | None:
    value = asset.get("browser_download_url")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _asset_name(asset: dict[str, Any]) -> str:
    value = asset.get("name")
    return value if isinstance(value, str) else ""


def _select_asset(
    assets: list[dict[str, Any]], release_version: str
) -> tuple[str | None, str | None, str | None, str | None]:
    version = _semver_text(release_version)
    if version is None:
        return None, None, None, None
    installer_pattern = re.compile(
        rf"^ApplicantScoutCompanionSetup-{re.escape(version)}\.exe$", re.I
    )
    checksum_pattern = re.compile(
        rf"^ApplicantScoutCompanionSetup-{re.escape(version)}\.exe\.sha256$", re.I
    )
    installer_name = installer_url = checksum_name = checksum_url = None
    for asset in assets:
        name = _asset_name(asset)
        url = _asset_download_url(asset)
        if not url:
            continue
        if installer_pattern.match(name):
            installer_name = name
            installer_url = url
        elif checksum_pattern.match(name):
            checksum_name = name
            checksum_url = url
    if installer_name and installer_url:
        return installer_name, installer_url, checksum_name, checksum_url
    return None, None, None, None


def _select_latest_stable_release(releases: list[Any]) -> dict[str, Any] | None:
    candidates: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    for release in releases:
        if (
            not isinstance(release, dict)
            or release.get("draft")
            or release.get("prerelease")
        ):
            continue
        tag_name = release.get("tag_name")
        if not isinstance(tag_name, str):
            continue
        version_key = _semver_key(tag_name)
        if version_key is None:
            continue
        candidates.append((version_key, release))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def check_for_update(
    current_version: str,
    *,
    repo: str = DEFAULT_RELEASE_REPO,
    client: httpx.Client | None = None,
) -> UpdateResult:
    """Return latest non-prerelease GitHub Release status."""
    owns_client = client is None
    try:
        http = client or httpx.Client(timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        return UpdateResult(
            status="unavailable",
            message=f"GitHub update check failed: {exc}",
            current_version=current_version,
            reason="client_error",
        )
    try:
        resp = http.get(
            f"{GITHUB_API_BASE}/repos/{repo}/releases",
            params={"per_page": 100},
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ApplicantScout-Companion",
            },
        )
        if resp.status_code == 404:
            return UpdateResult(
                status="unavailable",
                message=f"No GitHub Releases found for {repo}.",
                current_version=current_version,
                reason="not_found",
            )
        if resp.status_code >= 400:
            return UpdateResult(
                status="unavailable",
                message=f"GitHub update check failed (HTTP {resp.status_code}).",
                current_version=current_version,
                reason="http_error",
            )
        try:
            releases = resp.json()
        except ValueError:
            return UpdateResult(
                status="unavailable",
                message="GitHub update check returned malformed JSON.",
                current_version=current_version,
                reason="malformed_json",
            )
        if not isinstance(releases, list):
            return UpdateResult(
                status="unavailable",
                message="GitHub update check returned an unexpected response.",
                current_version=current_version,
                reason="unexpected_response",
            )
        latest = _select_latest_stable_release(releases)
        if latest is None:
            return UpdateResult(
                status="unavailable",
                message="No stable semantic GitHub Releases are published yet.",
                current_version=current_version,
                reason="no_stable_releases",
            )
        tag_name = latest.get("tag_name")
        latest_version = tag_name if isinstance(tag_name, str) else ""
        release_url = latest.get("html_url")
        release_url = release_url if isinstance(release_url, str) else None
        if not latest_version:
            return UpdateResult(
                status="unavailable",
                message="Latest GitHub Release has no version tag.",
                current_version=current_version,
                reason="missing_version_tag",
                release_url=release_url,
            )
        if not _is_newer(latest_version, current_version):
            return UpdateResult(
                status="up_to_date",
                message=f"ApplicantScout Companion is up to date ({current_version}).",
                current_version=current_version,
                latest_version=latest_version,
                release_url=release_url,
            )
        raw_assets = latest.get("assets", [])
        assets = raw_assets if isinstance(raw_assets, list) else []
        asset_name, asset_url, checksum_name, checksum_url = _select_asset(
            [asset for asset in assets if isinstance(asset, dict)],
            latest_version,
        )
        if (
            asset_name
            and _is_setup_asset_name(asset_name)
            and (checksum_name is None or checksum_url is None)
        ):
            return UpdateResult(
                status="available",
                message=(
                    f"Version {latest_version} is available, but the installer "
                    "checksum asset was not published."
                ),
                current_version=current_version,
                latest_version=latest_version,
                release_url=release_url,
            )
        if asset_url is None:
            if release_url is None:
                return UpdateResult(
                    status="unavailable",
                    message="Latest GitHub Release has no release URL.",
                    current_version=current_version,
                    reason="missing_release_url",
                    latest_version=latest_version,
                )
            return UpdateResult(
                status="available",
                message=(
                    f"Version {latest_version} is available, but no installer "
                    "asset was published."
                ),
                current_version=current_version,
                latest_version=latest_version,
                release_url=release_url,
            )
        return UpdateResult(
            status="available",
            message=f"Version {latest_version} is available.",
            current_version=current_version,
            latest_version=latest_version,
            release_url=release_url,
            asset_url=asset_url,
            asset_name=asset_name,
            checksum_url=checksum_url,
            checksum_name=checksum_name,
        )
    except httpx.HTTPError as exc:
        return UpdateResult(
            status="unavailable",
            message=f"GitHub update check failed: {exc}",
            current_version=current_version,
            reason="network_error",
        )
    finally:
        if owns_client:
            http.close()


def _is_setup_asset_name(name: str) -> bool:
    if "/" in name or "\\" in name:
        return False
    normalized = name.lower()
    return normalized.startswith(_INSTALLER_PREFIX.lower()) and normalized.endswith(".exe")


def _default_update_download_dir() -> Path:
    return user_cache_dir() / "updates"


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw_length = headers.get("content-length")
    except AttributeError:
        return None
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        return None
    if length < 0:
        return None
    return length


def _raise_if_response_too_large(response: Any, *, limit: int, label: str) -> None:
    length = _content_length(response)
    if length is not None and length > limit:
        raise RuntimeError(f"Update {label} is too large.")


def _read_capped_response_bytes(response: Any, *, limit: int, label: str) -> bytes:
    response.raise_for_status()
    _raise_if_response_too_large(response, limit=limit, label=label)
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise RuntimeError(f"Update {label} is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_sha256_checksum(text: str, *, expected_name: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) > 2:
            raise RuntimeError("Malformed update checksum.")
        digest = parts[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("Malformed update checksum.")
        if len(parts) == 2:
            checksum_name = parts[1].lstrip("*")
            if checksum_name.lower() != expected_name.lower():
                raise RuntimeError("Update checksum filename does not match installer.")
        return digest
    raise RuntimeError("Malformed update checksum.")


def _write_capped_response_to_file(
    response: Any,
    handle: Any,
    *,
    limit: int,
    label: str,
) -> str:
    response.raise_for_status()
    _raise_if_response_too_large(response, limit=limit, label=label)
    digest = hashlib.sha256()
    total = 0
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise RuntimeError(f"Update {label} is too large.")
        digest.update(chunk)
        handle.write(chunk)
    return digest.hexdigest()


def download_update_installer(
    result: UpdateResult,
    *,
    download_dir: Path | None = None,
    client: httpx.Client | None = None,
) -> Path:
    """Download the selected setup asset and return its local path.

    Only installer assets are accepted. Portable zips are intentionally not
    launched from the in-app updater because they cannot update the installed
    application safely.
    """
    asset_url = result.asset_url.strip() if result.asset_url else ""
    asset_name = result.asset_name.strip() if result.asset_name else ""
    checksum_url = result.checksum_url.strip() if result.checksum_url else ""
    checksum_name = result.checksum_name.strip() if result.checksum_name else ""
    if result.status != "available" or not asset_url or not asset_name:
        raise RuntimeError("No update installer asset is available.")
    if not _is_setup_asset_name(asset_name):
        raise RuntimeError("Latest release does not include an installer asset.")
    if not checksum_url or not checksum_name:
        raise RuntimeError("Latest release does not include an installer checksum.")

    target_dir = download_dir or _default_update_download_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / asset_name

    owns_client = client is None
    http = client or httpx.Client(timeout=120.0, follow_redirects=True)
    fd = -1
    tmp_path: Path | None = None
    try:
        with http.stream("GET", checksum_url, follow_redirects=True) as checksum_response:
            checksum_bytes = _read_capped_response_bytes(
                checksum_response,
                limit=_MAX_CHECKSUM_DOWNLOAD_BYTES,
                label="checksum",
            )
        try:
            checksum_text = checksum_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Malformed update checksum.") from exc
        expected_digest = _parse_sha256_checksum(
            checksum_text,
            expected_name=asset_name,
        )
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target_dir,
        )
        tmp_path = Path(tmp_name)
        with http.stream("GET", asset_url, follow_redirects=True) as response:
            with open(fd, "wb", closefd=True) as handle:
                fd = -1
                actual_digest = _write_capped_response_to_file(
                    response,
                    handle,
                    limit=_MAX_INSTALLER_DOWNLOAD_BYTES,
                    label="installer",
                )
        if actual_digest.lower() != expected_digest.lower():
            raise RuntimeError("Update installer checksum mismatch.")
        tmp_path.replace(target)
        tmp_path = None
        return target
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        if owns_client:
            http.close()


def launch_update_installer(installer_path: Path) -> InstallerLaunch:
    if not installer_path.is_file():
        raise RuntimeError(f"Update installer was not downloaded: {installer_path}")
    process = subprocess.Popen(
        [
            str(installer_path),
            *_INSTALLER_ARGS,
            *_installer_self_update_args(),
            *_installer_current_dir_args(),
        ],
        close_fds=True,
        cwd=str(installer_path.parent),
    )
    return InstallerLaunch(installer_path=installer_path, _process=process)


def _installer_self_update_args() -> list[str]:
    return [
        _SELF_UPDATE_FLAG,
        f"/APSCOUT_SOURCE_PID={os.getpid()}",
        f"/APSCOUT_SOURCE_PATH={sys.executable}",
    ]


def _installer_current_dir_args() -> list[str]:
    if not getattr(sys, "frozen", False):
        return []
    executable = Path(sys.executable)
    if executable.name.lower() != "applicantscout.exe":
        return []
    if not (executable.parent / "unins000.exe").is_file():
        return []
    return [f"/DIR={executable.parent}"]
