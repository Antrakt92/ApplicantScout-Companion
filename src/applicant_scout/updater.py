"""GitHub Releases update checks for the companion."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import httpx

from .config import user_cache_dir
from .ui_text import (
    format_update_download,
    format_update_install,
    update_phase_message,
)


DEFAULT_RELEASE_REPO = "Antrakt92/ApplicantScout-Companion"
GITHUB_API_BASE = "https://api.github.com"
UPDATE_DOWNLOADS_DIR_NAME = "updates"
_GITHUB_API_VERSION = "2026-03-10"
_SEMVER_RE = re.compile(r"^\s*[vV]?([0-9]+)\.([0-9]+)\.([0-9]+)(?:\+[0-9A-Za-z.-]+)?\s*$")

UpdateStatus = Literal["available", "up_to_date", "unavailable"]
_INSTALLER_PREFIX = "ApplicantScoutCompanionSetup-"
_INSTALLER_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
_SELF_UPDATE_FLAG = "/APSCOUT_SELFUPDATE=1"
_MAX_INSTALLER_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_CHECKSUM_DOWNLOAD_BYTES = 8 * 1024
_AUTHENTICODE_TIMEOUT_SECONDS = 15
_UPDATE_NETWORK_TIMEOUT_SECONDS = 15
_TRUSTED_SIGNER_CERT_SHA256: frozenset[str] = frozenset()
_UPDATE_INSTALLER_STALE_AGE_SECONDS = 30 * 24 * 60 * 60
_UPDATE_INSTALLER_MAX_FILES = 4
_STRICT_UPDATE_INSTALLER_RE = re.compile(
    rf"^{re.escape(_INSTALLER_PREFIX)}"
    r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))\.exe$",
    re.I,
)
_UPDATE_ALLOWED_HOSTS = frozenset(
    {"github.com", "api.github.com", "objects.githubusercontent.com"}
)
_UPDATE_ALLOWED_HOST_SUFFIX = ".githubusercontent.com"
_MAX_UPDATE_REDIRECTS = 5
_log = logging.getLogger("applicant_scout.updater")


@dataclass(frozen=True)
class UpdateResult:
    status: UpdateStatus
    message: str
    reason: str | None = None
    latest_version: str | None = None
    asset_url: str | None = None
    asset_name: str | None = None
    checksum_url: str | None = None
    checksum_name: str | None = None


@dataclass(frozen=True)
class UpdateProgress:
    phase: Literal["checking", "downloading", "verifying", "installing"]
    downloaded_bytes: int = 0
    total_bytes: int | None = None

    @property
    def message(self) -> str:
        if self.phase == "downloading":
            return format_update_download(self.downloaded_bytes, self.total_bytes)
        if self.phase == "installing":
            return format_update_install(self.downloaded_bytes, self.total_bytes)
        return update_phase_message(self.phase)


class UpdateCancelled(RuntimeError):
    """Cancellation acknowledged before installer handoff."""


class UpdateDownloadControl:
    """Per-attempt cancellation and bounded progress delivery across GUI/worker threads."""

    def __init__(self, on_progress: Callable[[UpdateProgress], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._installing = False
        self._on_progress = on_progress
        self._last_progress: UpdateProgress | None = None
        self._last_report_at = 0.0

    def cancel(self) -> bool:
        with self._lock:
            if self._installing:
                return False
            self._cancelled = True
            return True

    def checkpoint(self) -> None:
        with self._lock:
            if self._cancelled:
                raise UpdateCancelled("Update cancelled.")

    def begin_installation(self, *, total_bytes: int | None = None) -> None:
        with self._lock:
            if self._cancelled:
                raise UpdateCancelled("Update cancelled.")
            self._installing = True
        self.report(UpdateProgress("installing", 0, total_bytes))

    def report(self, progress: UpdateProgress) -> None:
        now = time.monotonic()
        with self._lock:
            previous = self._last_progress
            if previous is not None and previous.phase == progress.phase and now - self._last_report_at < 0.1:
                return
            self._last_progress = progress
            self._last_report_at = now
        if self._on_progress is not None:
            self._on_progress(progress)


@dataclass(frozen=True)
class InstallerLaunch:
    _process: subprocess.Popen[Any] = field(repr=False, compare=False)

    def poll(self) -> int | None:
        return self._process.poll()


# Staging directory the Inno installer copies the candidate payload into
# before promotion (mirrors `[Files] DestDir: "{app}\.apscout-next"` in
# packaging/inno/ApplicantScoutCompanion.iss).
_INSTALL_STAGING_DIR_NAME = ".apscout-next"
# Cadence for measuring the staging directory while the installer runs.
_INSTALL_PROGRESS_POLL_INTERVAL_SECONDS = 0.3
# Expected installed payload = downloaded installer size × this factor.
#
# Anchor choice (no new fetch for estimation): the release manifest
# (scripts/release-artifact-manifest.ps1) records sizes only for the
# installer exe, its .sha256 sidecar, the portable zip, and five small
# required portable entries — not the full installed payload (_internal/,
# Qt DLLs, licenses/). The in-app updater additionally never downloads the
# manifest (installer + checksum sidecar only), so per-file manifest sizes
# are unavailable at runtime. The fallback therefore anchors on the
# downloaded installer file the app already holds. The installer compresses
# with Inno lzma2 solid compression and its own disk-space check budgets
# ~2-3x the payload; the upper bound (3.0) errs toward under-reporting, and
# any residual error is absorbed by the monotonic high-water mark plus the
# 99% cap below.
_INSTALL_SIZE_UNCOMPRESSED_FACTOR = 3.0


def expected_install_total_bytes(*, installer_size_bytes: int) -> int | None:
    """Expected installed payload size anchored on the downloaded installer."""
    if installer_size_bytes <= 0:
        return None
    return int(installer_size_bytes * _INSTALL_SIZE_UNCOMPRESSED_FACTOR)


def measure_directory_bytes(path: Path) -> int:
    """Best-effort recursive byte size; missing/unreadable parts count as 0."""
    total = 0
    stack = [path]
    try:
        if not path.is_dir():
            return 0
    except OSError:
        return 0
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


class InstallProgressEstimator:
    """Monotonic, 99%-capped translation of staged bytes into progress.

    The installer gives no progress API, so while it runs the app measures
    the `{app}\\.apscout-next` staging directory. During commit the payload
    moves (`.apscout-next` → `current`), so the high-water mark never
    reports backwards and reports stop at 99%: the final 100% is emitted
    only after the installer process exit is observed (see `complete()`),
    never from byte counting alone.
    """

    def __init__(self, total_bytes: int | None) -> None:
        self._total_bytes = (
            total_bytes if total_bytes is not None and total_bytes > 0 else None
        )
        self._high_water_mark = 0

    @property
    def total_bytes(self) -> int | None:
        return self._total_bytes

    def observe(self, staged_bytes: int) -> UpdateProgress:
        staged = max(0, staged_bytes)
        self._high_water_mark = max(self._high_water_mark, staged)
        if self._total_bytes is None:
            return UpdateProgress("installing", self._high_water_mark, None)
        reported = min(self._high_water_mark, self._total_bytes - 1)
        return UpdateProgress("installing", max(0, reported), self._total_bytes)

    def complete(self) -> UpdateProgress:
        """Final 100%: call only after the installer exit was observed."""
        if self._total_bytes is None:
            return UpdateProgress("installing")
        return UpdateProgress("installing", self._total_bytes, self._total_bytes)


def _install_root_for_staging() -> Path | None:
    """Best-effort install root for staging measurement (no fetch, no I/O)."""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        if executable.name.lower() != "applicantscout.exe":
            return None
        install_root = executable.parent
        if executable.parent.name.casefold() == "current":
            install_root = executable.parent.parent
        return install_root
    # Unfrozen/dev runs launch without /DIR, so Inno falls back to its
    # DefaultDirName; measure there best-effort.
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    return Path(local_app_data) / "Programs" / "ApplicantScout Companion"


def install_staging_dir(install_root: Path | None = None) -> Path | None:
    """Staging directory the installer fills, or None when unresolvable."""
    root = install_root if install_root is not None else _install_root_for_staging()
    if root is None:
        return None
    return root / _INSTALL_STAGING_DIR_NAME


def installer_path_from_launch(launch: object) -> Path | None:
    """Best-effort installer path from a launch handle (Popen args only)."""
    process = getattr(launch, "_process", None)
    args = getattr(process, "args", None)
    if isinstance(args, (list, tuple)) and args:
        candidate = args[0]
        if isinstance(candidate, (str, os.PathLike)):
            return Path(candidate)
    return None


def install_progress_basis(
    launch: object, *, installer_size_bytes: int | None = None
) -> tuple[int | None, Path | None]:
    """Best-effort (total_bytes, staging_dir); unknown parts are None.

    The total anchors on the downloaded installer size (measured from the
    launched installer file unless given); the staging dir resolves from
    the local install layout. Nothing is fetched.
    """
    size = installer_size_bytes
    if size is None:
        installer_path = installer_path_from_launch(launch)
        if installer_path is not None:
            try:
                size = installer_path.stat().st_size
            except OSError:
                size = None
    total = (
        expected_install_total_bytes(installer_size_bytes=size)
        if size is not None
        else None
    )
    return total, install_staging_dir()


@dataclass(frozen=True)
class InstallerAuthenticity:
    status: str
    cert_sha256: str | None


def _semver_key(version: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.match(version)
    if match is None:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        # Over-long digit runs exceed the int() string-digit limit.
        return None


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
    """Return latest immutable, non-prerelease GitHub Release status."""
    if _semver_key(current_version) is None:
        return UpdateResult(
            status="unavailable",
            message=(
                "Cannot check for updates: current version "
                f"{current_version!r} is not valid semantic versioning."
            ),
            reason="invalid_current_version",
        )
    owns_client = client is None
    try:
        http = client or httpx.Client(timeout=10.0)
    except (httpx.HTTPError, OSError) as exc:
        return UpdateResult(
            status="unavailable",
            message=f"GitHub update check failed: {exc}",
            reason="client_error",
        )
    try:
        resp = http.get(
            f"{GITHUB_API_BASE}/repos/{repo}/releases",
            params={"per_page": 100},
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ApplicantScout-Companion",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
        )
        if resp.status_code == 404:
            return UpdateResult(
                status="unavailable",
                message=f"No GitHub Releases found for {repo}.",
                reason="not_found",
            )
        if resp.status_code >= 400:
            return UpdateResult(
                status="unavailable",
                message=f"GitHub update check failed (HTTP {resp.status_code}).",
                reason="http_error",
            )
        try:
            releases = resp.json()
        except ValueError:
            return UpdateResult(
                status="unavailable",
                message="GitHub update check returned malformed JSON.",
                reason="malformed_json",
            )
        if not isinstance(releases, list):
            return UpdateResult(
                status="unavailable",
                message="GitHub update check returned an unexpected response.",
                reason="unexpected_response",
            )
        latest = _select_latest_stable_release(releases)
        if latest is None:
            return UpdateResult(
                status="unavailable",
                message="No stable semantic GitHub Releases are published yet.",
                reason="no_stable_releases",
            )
        tag_name = latest.get("tag_name")
        latest_version = tag_name if isinstance(tag_name, str) else ""
        # The checksum is published beside the installer, so it proves only
        # that those two downloaded assets agree. Releases are unsigned: checksum
        # agreement is the only update trust signal. Require GitHub's immutable
        # release state before trusting either asset; truthy/malformed values
        # and an older immutable release are not safe substitutes.
        if latest.get("immutable") is not True:
            return UpdateResult(
                status="unavailable",
                message=(
                    "Latest stable GitHub Release is not immutable; "
                    "automatic update is disabled."
                ),
                reason="release_not_immutable",
                latest_version=latest_version or None,
            )
        if not latest_version:
            return UpdateResult(
                status="unavailable",
                message="Latest GitHub Release has no version tag.",
                reason="missing_version_tag",
            )
        if not _is_newer(latest_version, current_version):
            return UpdateResult(
                status="up_to_date",
                message=f"ApplicantScout Companion is up to date ({current_version}).",
                latest_version=latest_version,
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
                latest_version=latest_version,
            )
        if asset_url is None:
            return UpdateResult(
                status="available",
                message=(
                    f"Version {latest_version} is available, but no installer "
                    "asset was published."
                ),
                latest_version=latest_version,
            )
        return UpdateResult(
            status="available",
            message=f"Version {latest_version} is available.",
            latest_version=latest_version,
            asset_url=asset_url,
            asset_name=asset_name,
            checksum_url=checksum_url,
            checksum_name=checksum_name,
        )
    except httpx.HTTPError as exc:
        return UpdateResult(
            status="unavailable",
            message=f"GitHub update check failed: {exc}",
            reason="network_error",
        )
    except OSError as exc:
        return UpdateResult(
            status="unavailable",
            message=f"GitHub update check failed: {exc}",
            reason="os_error",
        )
    except RecursionError as exc:
        return UpdateResult(
            status="unavailable",
            message=f"GitHub update check failed: {exc}",
            reason="response_too_deep",
        )
    except ValueError as exc:
        return UpdateResult(
            status="unavailable",
            message=f"GitHub update check failed: {exc}",
            reason="invalid_response",
        )
    finally:
        if owns_client:
            http.close()


def _is_setup_asset_name(name: str) -> bool:
    if "/" in name or "\\" in name:
        return False
    normalized = name.lower()
    return normalized.startswith(_INSTALLER_PREFIX.lower()) and normalized.endswith(
        ".exe"
    )


def _is_strict_update_installer_asset(name: str, version: str | None) -> bool:
    """Re-verify the installer name against the selected release version.

    check_for_update already matches names per release, but the download path
    re-verifies with the canonical pattern so a hand-built UpdateResult (or a
    renamed asset) can never smuggle a non-versioned executable into the handoff.
    """
    match = _STRICT_UPDATE_INSTALLER_RE.fullmatch(name)
    if match is None:
        return False
    expected = _semver_text(version or "")
    if expected is None:
        return False
    return _semver_text(match.group(1)) == expected


def _is_allowed_update_host(host: str | None) -> bool:
    if not isinstance(host, str) or not host:
        return False
    normalized = host.strip().lower().rstrip(".")
    if normalized in _UPDATE_ALLOWED_HOSTS:
        return True
    return normalized.endswith(_UPDATE_ALLOWED_HOST_SUFFIX) and len(
        normalized
    ) > len(_UPDATE_ALLOWED_HOST_SUFFIX)


def _is_allowed_update_url(url: object) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = httpx.URL(url.strip())
    except Exception:  # noqa: BLE001 — unparseable URLs are never trusted
        return False
    if (parsed.scheme or "").lower() != "https":
        return False
    return _is_allowed_update_host(parsed.host)


def _require_allowed_update_url(url: str, label: str) -> None:
    if not _is_allowed_update_url(url):
        raise RuntimeError(f"Update {label} URL is not from a trusted host.")


def _update_redirect_target(response: Any) -> str | None:
    if getattr(response, "status_code", 200) not in (301, 302, 303, 307, 308):
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    location = headers.get("location")
    if not isinstance(location, str) or not location.strip():
        return None
    return location.strip()


def _resolve_update_redirect(current_url: str, location: str, label: str) -> str:
    try:
        return str(httpx.URL(current_url).join(location))
    except Exception as exc:  # noqa: BLE001 — unparseable targets are untrusted
        raise RuntimeError(f"Update {label} redirect target is invalid.") from exc


@contextmanager
def _update_response_stream(
    http: Any, url: str, *, label: str
) -> Iterator[Any]:
    """Stream one update URL, following only allowlisted redirect hops.

    Redirects are followed manually (the client never auto-follows) so every
    hop is re-checked against the trusted-host allowlist before it is fetched.
    The caller owns the yielded response and must finish reading inside the block.
    """
    current_url = url
    for _hop in range(_MAX_UPDATE_REDIRECTS + 1):
        _require_allowed_update_url(current_url, label)
        streamer = http.stream("GET", current_url, follow_redirects=False)
        response = streamer.__enter__()
        redirect = _update_redirect_target(response)
        if redirect is None:
            try:
                yield response
            finally:
                streamer.__exit__(None, None, None)
            return
        streamer.__exit__(None, None, None)
        current_url = _resolve_update_redirect(current_url, redirect, label)
    raise RuntimeError(f"Update {label} redirected too many times.")


def update_result_has_installable_asset(result: object) -> bool:
    asset_name = getattr(result, "asset_name", None)
    asset_url = getattr(result, "asset_url", None)
    checksum_name = getattr(result, "checksum_name", None)
    checksum_url = getattr(result, "checksum_url", None)
    metadata = (asset_name, asset_url, checksum_name, checksum_url)
    return bool(
        all(isinstance(value, str) and value.strip() for value in metadata)
        and isinstance(asset_name, str)
        and _is_setup_asset_name(asset_name)
    )


def _default_update_download_dir() -> Path:
    return user_cache_dir() / UPDATE_DOWNLOADS_DIR_NAME


def _strict_update_installer_version(name: str) -> tuple[int, int, int] | None:
    match = _STRICT_UPDATE_INSTALLER_RE.fullmatch(name)
    if match is None:
        return None
    return _semver_key(match.group(1))


def _prune_stale_update_installers(
    download_dir: Path,
    *,
    active_installer: Path,
) -> int:
    """Best-effort cleanup for installers owned by the in-app updater."""
    try:
        children = list(download_dir.iterdir())
    except OSError as exc:
        _log.warning("Could not inspect the update cache for cleanup: %s", exc)
        return 0

    candidates: list[tuple[tuple[int, int, int], float, Path]] = []
    for path in children:
        version = _strict_update_installer_version(path.name)
        if version is None:
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            modified_at = path.stat().st_mtime
        except OSError as exc:
            _log.warning("Could not inspect cached update %s: %s", path.name, exc)
            continue
        candidates.append((version, modified_at, path))

    candidates.sort(
        key=lambda item: (item[0], item[1], item[2].name.casefold()),
        reverse=True,
    )
    active_name = active_installer.name.casefold()
    inactive = [
        candidate
        for candidate in candidates
        if candidate[2].name.casefold() != active_name
    ]

    # Keep the newest inactive payload as a rollback candidate. The hard cap
    # bounds frequent patch accumulation even before the age threshold elapses.
    prunable = inactive[1:]
    hard_excess = max(0, len(candidates) - _UPDATE_INSTALLER_MAX_FILES)
    hard_prune = {
        candidate[2] for candidate in (prunable[-hard_excess:] if hard_excess else [])
    }
    now = time.time()
    removed = 0
    for _version, modified_at, path in prunable:
        stale = now - modified_at >= _UPDATE_INSTALLER_STALE_AGE_SECONDS
        if not stale and path not in hard_prune:
            continue
        try:
            path.unlink()
        except OSError as exc:
            _log.warning("Could not remove cached update %s: %s", path.name, exc)
        else:
            removed += 1
    return removed


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


def _read_capped_response_bytes(
    response: Any, *, limit: int, label: str, control: UpdateDownloadControl | None = None,
) -> bytes:
    response.raise_for_status()
    _raise_if_response_too_large(response, limit=limit, label=label)
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        if control is not None:
            control.checkpoint()
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise RuntimeError(f"Update {label} is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_sha256_checksum(text: str, *, expected_name: str) -> str:
    """Extract the installer digest, requiring every named line to match.

    Bare digest lines stay accepted (some publishers omit the filename), but
    any line carrying a filename must name this installer — a multi-line file
    mixing digests or pointing at another asset is rejected.
    """
    digest: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) > 2:
            raise RuntimeError("Malformed update checksum.")
        candidate = parts[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", candidate):
            raise RuntimeError("Malformed update checksum.")
        if len(parts) == 2:
            checksum_name = parts[1].lstrip("*")
            if checksum_name.lower() != expected_name.lower():
                raise RuntimeError("Update checksum filename does not match installer.")
        if digest is None:
            digest = candidate
        elif digest != candidate:
            raise RuntimeError("Malformed update checksum.")
    if digest is None:
        raise RuntimeError("Malformed update checksum.")
    return digest


def _write_capped_response_to_file(
    response: Any,
    handle: Any,
    *,
    limit: int,
    label: str,
    control: UpdateDownloadControl | None = None,
) -> str:
    response.raise_for_status()
    _raise_if_response_too_large(response, limit=limit, label=label)
    digest = hashlib.sha256()
    total = 0
    length = _content_length(response)
    if control is not None:
        control.report(UpdateProgress("downloading", 0, length))
    for chunk in response.iter_bytes():
        if control is not None:
            control.checkpoint()
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise RuntimeError(f"Update {label} is too large.")
        digest.update(chunk)
        handle.write(chunk)
        if control is not None:
            control.report(UpdateProgress("downloading", total, length))
    return digest.hexdigest()


def download_update_installer(
    result: UpdateResult,
    *,
    download_dir: Path | None = None,
    client: httpx.Client | None = None,
    control: UpdateDownloadControl | None = None,
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
    if not _is_strict_update_installer_asset(asset_name, result.latest_version):
        raise RuntimeError("Latest release does not include an installer asset.")
    if not checksum_url or not checksum_name:
        raise RuntimeError("Latest release does not include an installer checksum.")
    _require_allowed_update_url(checksum_url, "checksum")
    _require_allowed_update_url(asset_url, "installer")

    if control is not None:
        control.checkpoint()
    target_dir = download_dir or _default_update_download_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / asset_name

    owns_client = client is None
    # Bound cancellation latency while an HTTP operation is waiting for data.
    # Redirects are never auto-followed: every hop is re-checked against the
    # trusted-host allowlist inside _update_response_stream.
    http = client or httpx.Client(timeout=_UPDATE_NETWORK_TIMEOUT_SECONDS, follow_redirects=False)
    fd = -1
    tmp_path: Path | None = None
    try:
        with _update_response_stream(
            http, checksum_url, label="checksum"
        ) as checksum_response:
            checksum_bytes = _read_capped_response_bytes(
                checksum_response,
                limit=_MAX_CHECKSUM_DOWNLOAD_BYTES,
                label="checksum",
                control=control,
            )
        try:
            checksum_text = checksum_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Malformed update checksum.") from exc
        expected_digest = _parse_sha256_checksum(
            checksum_text,
            expected_name=asset_name,
        )
        if control is not None:
            control.checkpoint()
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target_dir,
        )
        tmp_path = Path(tmp_name)
        with _update_response_stream(
            http, asset_url, label="installer"
        ) as response:
            with open(fd, "wb", closefd=True) as handle:
                fd = -1
                actual_digest = _write_capped_response_to_file(
                    response,
                    handle,
                    limit=_MAX_INSTALLER_DOWNLOAD_BYTES,
                    label="installer",
                    control=control,
                )
        if control is not None:
            control.checkpoint()
            control.report(UpdateProgress("verifying"))
            control.checkpoint()
        if actual_digest.lower() != expected_digest.lower():
            raise RuntimeError("Update installer checksum mismatch.")
        tmp_path.replace(target)
        tmp_path = None
        _prune_stale_update_installers(target_dir, active_installer=target)
        return target
    except httpx.HTTPError:
        if control is not None:
            control.checkpoint()
        raise
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


def launch_update_installer(
    installer_path: Path,
    *,
    require_trusted_signature: bool = True,
) -> InstallerLaunch:
    if not installer_path.is_file():
        raise RuntimeError(f"Update installer was not downloaded: {installer_path}")
    if require_trusted_signature:
        verify_update_installer_authenticity(installer_path)
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
    return InstallerLaunch(_process=process)


def verify_update_installer_authenticity(installer_path: Path) -> None:
    authenticity = _read_installer_authenticity(installer_path)
    if authenticity.status.lower() != "valid":
        raise RuntimeError(
            "Update installer is not trusted: Authenticode status is "
            f"{authenticity.status or 'unknown'}."
        )
    cert_sha256 = (authenticity.cert_sha256 or "").lower()
    trusted = {fingerprint.lower() for fingerprint in _TRUSTED_SIGNER_CERT_SHA256}
    if not cert_sha256 or cert_sha256 not in trusted:
        raise RuntimeError(
            "Update installer is not trusted: signer certificate is not pinned."
        )


def _read_installer_authenticity(installer_path: Path) -> InstallerAuthenticity:
    command = r"""
$ErrorActionPreference = 'Stop'
$sig = Get-AuthenticodeSignature -LiteralPath $env:APSCOUT_INSTALLER_PATH
$cert = $sig.SignerCertificate
$certSha256 = $null
if ($null -ne $cert) {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash($cert.RawData)
        $certSha256 = (($hash | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        $sha256.Dispose()
    }
}
[pscustomobject]@{
    Status = [string]$sig.Status
    CertSha256 = $certSha256
} | ConvertTo-Json -Compress
"""
    env = os.environ.copy()
    env["APSCOUT_INSTALLER_PATH"] = str(installer_path)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=_AUTHENTICODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Update installer is not trusted: Authenticode verification timed out."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            "Update installer is not trusted: Authenticode verification is unavailable."
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        suffix = f" {detail}" if detail else ""
        raise RuntimeError(
            "Update installer is not trusted: Authenticode verification failed."
            f"{suffix}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Update installer is not trusted: Authenticode verification returned malformed JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "Update installer is not trusted: Authenticode verification returned malformed JSON."
        )
    return InstallerAuthenticity(
        status=str(payload.get("Status") or ""),
        cert_sha256=_optional_json_text(payload.get("CertSha256")),
    )


def _optional_json_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


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
    install_root = executable.parent
    if executable.parent.name.casefold() == "current":
        install_root = executable.parent.parent
    if not (install_root / "unins000.exe").is_file():
        return []
    return [f"/DIR={install_root}"]
