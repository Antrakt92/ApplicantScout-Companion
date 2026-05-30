from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

import applicant_scout.updater as updater_mod
from applicant_scout.updater import (
    DEFAULT_RELEASE_REPO,
    UpdateResult,
    check_for_update,
    download_update_installer,
    launch_update_installer,
)


class _Response:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


class _Client:
    def __init__(self, response: _Response | Exception):
        self.response = response
        self.closed = False

    def get(self, *_args, **_kwargs) -> _Response:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        self.closed = True


class _DownloadResponse:
    def __init__(
        self,
        chunks: bytes | list[bytes] = b"installer",
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._chunks = [chunks] if isinstance(chunks, bytes) else chunks
        self.headers = headers or {}
        self.status_code = 200
        self.closed = False

    @property
    def content(self) -> bytes:
        raise AssertionError("download responses must be streamed, not materialized")

    def __enter__(self) -> "_DownloadResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def iter_bytes(self):
        yield from self._chunks


class _DownloadClient:
    def __init__(self, responses: dict[str, bytes | _DownloadResponse] | None = None) -> None:
        self.urls: list[str] = []
        self.responses = responses or {}

    def get(self, _url: str, **_kwargs) -> _DownloadResponse:
        raise AssertionError("download installer must use client.stream()")

    def stream(self, _method: str, url: str, **_kwargs) -> _DownloadResponse:
        self.urls.append(url)
        response = self.responses.get(url, b"setup-bytes")
        if isinstance(response, _DownloadResponse):
            return response
        return _DownloadResponse(response)


def test_default_release_repo_points_to_public_companion_repo():
    assert DEFAULT_RELEASE_REPO == "Antrakt92/ApplicantScout-Companion"


def _release(
    tag: str,
    *,
    prerelease: bool = False,
    draft: bool = False,
    assets: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "html_url": f"https://github.test/releases/tag/{tag}",
        "prerelease": prerelease,
        "draft": draft,
        "assets": assets or [],
    }


def _installer_result(
    *,
    asset_url: str = "https://example.test/ApplicantScoutCompanionSetup-0.2.0.exe",
    asset_name: str = "ApplicantScoutCompanionSetup-0.2.0.exe",
    checksum_url: str = "https://example.test/setup.exe.sha256",
    checksum_name: str = "ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
) -> UpdateResult:
    return UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="0.2.0",
        asset_url=asset_url,
        asset_name=asset_name,
        checksum_url=checksum_url,
        checksum_name=checksum_name,
    )


def test_update_check_prefers_installer_asset():
    client = _Client(
        _Response(
            200,
            [
                _release(
                    "v0.2.0",
                    assets=[
                        {
                            "name": "ApplicantScoutCompanion-0.2.0-portable.zip",
                            "browser_download_url": "https://example.test/portable.zip",
                        },
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe",
                            "browser_download_url": "https://example.test/setup.exe",
                        },
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
                            "browser_download_url": "https://example.test/setup.exe.sha256",
                        },
                    ],
                )
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.latest_version == "v0.2.0"
    assert result.asset_name == "ApplicantScoutCompanionSetup-0.2.0.exe"
    assert result.checksum_name == "ApplicantScoutCompanionSetup-0.2.0.exe.sha256"
    assert result.checksum_url == "https://example.test/setup.exe.sha256"
    assert result.open_url == "https://example.test/setup.exe"


def test_update_check_does_not_select_portable_asset_for_in_app_update():
    client = _Client(
        _Response(
            200,
            [
                _release(
                    "0.2.0",
                    assets=[
                        {
                            "name": "ApplicantScoutCompanion-0.2.0-portable.zip",
                            "browser_download_url": "https://example.test/portable.zip",
                        }
                    ],
                )
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.asset_name is None
    assert result.asset_url is None
    assert result.open_url == "https://github.test/releases/tag/0.2.0"
    assert "no installer asset" in result.message


def test_update_check_rejects_blank_asset_download_url():
    client = _Client(
        _Response(
            200,
            [
                _release(
                    "v0.2.0",
                    assets=[
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe",
                            "browser_download_url": "   ",
                        },
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
                            "browser_download_url": "https://example.test/setup.exe.sha256",
                        },
                    ],
                )
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.asset_name is None
    assert result.asset_url is None
    assert result.open_url == "https://github.test/releases/tag/v0.2.0"
    assert "no installer asset" in result.message


def test_update_check_ignores_assets_for_other_versions():
    client = _Client(
        _Response(
            200,
            [
                _release(
                    "v0.2.0",
                    assets=[
                        {
                            "name": "ApplicantScoutCompanionSetup-0.1.0.exe",
                            "browser_download_url": "https://example.test/stale.exe",
                        },
                        {
                            "name": "ApplicantScoutCompanion-0.1.0-portable.zip",
                            "browser_download_url": "https://example.test/stale.zip",
                        },
                    ],
                )
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.latest_version == "v0.2.0"
    assert result.asset_name is None
    assert result.asset_url is None
    assert result.open_url == "https://github.test/releases/tag/v0.2.0"


def test_update_check_accepts_v_tag_with_unprefixed_asset_version():
    client = _Client(
        _Response(
            200,
            [
                _release(
                    "v0.2.0",
                    assets=[
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe",
                            "browser_download_url": "https://example.test/setup.exe",
                        },
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
                            "browser_download_url": "https://example.test/setup.exe.sha256",
                        }
                    ],
                )
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.asset_name == "ApplicantScoutCompanionSetup-0.2.0.exe"
    assert result.checksum_name == "ApplicantScoutCompanionSetup-0.2.0.exe.sha256"
    assert result.open_url == "https://example.test/setup.exe"


def test_update_check_reports_available_but_uninstallable_without_checksum_asset():
    client = _Client(
        _Response(
            200,
            [
                _release(
                    "v0.2.0",
                    assets=[
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe",
                            "browser_download_url": "https://example.test/setup.exe",
                        }
                    ],
                )
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.asset_name is None
    assert result.asset_url is None
    assert result.checksum_name is None
    assert "checksum" in result.message.lower()
    assert result.open_url == "https://github.test/releases/tag/v0.2.0"


def test_update_check_selects_highest_stable_semver_when_releases_are_out_of_order():
    client = _Client(
        _Response(
            200,
            [
                _release("v0.1.5"),
                _release(
                    "v0.3.0",
                    assets=[
                        {
                            "name": "ApplicantScoutCompanionSetup-0.3.0.exe",
                            "browser_download_url": "https://example.test/setup-030.exe",
                        },
                        {
                            "name": "ApplicantScoutCompanionSetup-0.3.0.exe.sha256",
                            "browser_download_url": "https://example.test/setup-030.exe.sha256",
                        }
                    ],
                ),
                _release(
                    "v0.2.0",
                    assets=[
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe",
                            "browser_download_url": "https://example.test/setup-020.exe",
                        },
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
                            "browser_download_url": "https://example.test/setup-020.exe.sha256",
                        }
                    ],
                ),
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.latest_version == "v0.3.0"
    assert result.asset_name == "ApplicantScoutCompanionSetup-0.3.0.exe"
    assert result.checksum_name == "ApplicantScoutCompanionSetup-0.3.0.exe.sha256"
    assert result.open_url == "https://example.test/setup-030.exe"


def test_update_check_selects_asset_from_highest_release_not_first_release():
    client = _Client(
        _Response(
            200,
            [
                _release(
                    "v0.2.0",
                    assets=[
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe",
                            "browser_download_url": "https://example.test/setup-020.exe",
                        },
                        {
                            "name": "ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
                            "browser_download_url": "https://example.test/setup-020.exe.sha256",
                        }
                    ],
                ),
                _release("v0.3.0"),
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.latest_version == "v0.3.0"
    assert result.asset_name is None
    assert result.open_url == "https://github.test/releases/tag/v0.3.0"


def test_update_check_reports_unavailable_when_newer_release_has_no_open_url():
    client = _Client(_Response(200, [{"tag_name": "v0.2.0", "assets": []}]))

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert result.reason == "missing_release_url"
    assert "no release URL" in result.message
    assert result.open_url is None


def test_update_check_reports_up_to_date_for_same_version():
    client = _Client(_Response(200, [_release("v0.1.0")]))

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "up_to_date"
    assert result.open_url == "https://github.test/releases/tag/v0.1.0"


def test_update_check_ignores_prereleases_and_drafts():
    client = _Client(
        _Response(
            200,
            [
                _release("v0.3.0", prerelease=True),
                _release("v0.2.0", draft=True),
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert result.reason == "no_stable_releases"
    assert "No stable" in result.message


def test_update_check_ignores_higher_prerelease_and_draft():
    client = _Client(
        _Response(
            200,
            [
                _release("v9.0.0", prerelease=True),
                _release("v8.0.0", draft=True),
                _release("v0.2.0"),
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.latest_version == "v0.2.0"


def test_update_check_skips_missing_or_unparseable_tags_when_valid_release_exists():
    client = _Client(
        _Response(
            200,
            [
                {"html_url": "https://github.test/releases/tag/missing", "assets": []},
                _release("latest"),
                _release("v0.2.0"),
            ],
        )
    )

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "available"
    assert result.latest_version == "v0.2.0"


def test_update_check_handles_github_error():
    client = _Client(_Response(403, {"message": "rate limited"}))

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert result.reason == "http_error"
    assert "HTTP 403" in result.message


def test_update_check_handles_missing_releases_endpoint():
    client = _Client(_Response(404, {"message": "not found"}))

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert result.reason == "not_found"
    assert "No GitHub Releases" in result.message


def test_update_check_handles_network_error():
    client = _Client(httpx.ConnectError("offline"))

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert result.reason == "network_error"
    assert "offline" in result.message


def test_update_check_handles_owned_client_construction_error(monkeypatch):
    def fail_client(**_kwargs):
        raise httpx.ConnectError("bad proxy config")

    monkeypatch.setattr("applicant_scout.updater.httpx.Client", fail_client)

    result = check_for_update("0.1.0")

    assert result.status == "unavailable"
    assert result.reason == "client_error"
    assert "bad proxy config" in result.message


def test_update_check_handles_non_http_owned_client_construction_error(monkeypatch):
    def fail_client(**_kwargs):
        raise FileNotFoundError("missing cert bundle")

    monkeypatch.setattr("applicant_scout.updater.httpx.Client", fail_client)

    result = check_for_update("0.1.0")

    assert result.status == "unavailable"
    assert result.reason == "client_error"
    assert "missing cert bundle" in result.message


def test_update_check_closes_owned_client_on_json_error(monkeypatch):
    client = _Client(_Response(200, ValueError("not json")))

    monkeypatch.setattr("applicant_scout.updater.httpx.Client", lambda **_kwargs: client)

    result = check_for_update("0.1.0")

    assert result.status == "unavailable"
    assert result.reason == "malformed_json"
    assert "malformed JSON" in result.message
    assert client.closed is True


def test_update_check_reports_reason_for_unexpected_response_shape():
    client = _Client(_Response(200, {"message": "not a list"}))

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert result.reason == "unexpected_response"
    assert "unexpected response" in result.message


def test_download_update_installer_saves_setup_asset_atomically(tmp_path):
    digest = hashlib.sha256(b"setup-bytes").hexdigest()
    client = _DownloadClient(
        {"https://example.test/setup.exe.sha256": f"{digest}  ApplicantScoutCompanionSetup-0.2.0.exe\n".encode()}
    )
    result = _installer_result()

    path = download_update_installer(result, download_dir=tmp_path, client=client)  # type: ignore[arg-type]

    assert path == tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    assert path.read_bytes() == b"setup-bytes"
    assert client.urls == [
        "https://example.test/setup.exe.sha256",
        "https://example.test/ApplicantScoutCompanionSetup-0.2.0.exe",
    ]
    assert not list(tmp_path.glob("*.tmp"))


def test_download_update_installer_accepts_case_insensitive_setup_asset(tmp_path):
    digest = hashlib.sha256(b"setup-bytes").hexdigest()
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="0.2.0",
        asset_url="https://example.test/setup.exe",
        asset_name="applicantscoutcompanionsetup-0.2.0.EXE",
        checksum_url="https://example.test/setup.exe.sha256",
        checksum_name="applicantscoutcompanionsetup-0.2.0.EXE.sha256",
    )

    path = download_update_installer(
        result,
        download_dir=tmp_path,
        client=_DownloadClient({"https://example.test/setup.exe.sha256": digest.encode()}),
    )  # type: ignore[arg-type]

    assert path == tmp_path / "applicantscoutcompanionsetup-0.2.0.EXE"


def test_download_update_installer_requires_setup_asset(tmp_path):
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="0.2.0",
        asset_url="https://example.test/portable.zip",
        asset_name="ApplicantScoutCompanion-0.2.0-portable.zip",
    )

    try:
        download_update_installer(result, download_dir=tmp_path, client=_DownloadClient())  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "installer asset" in str(exc)
    else:
        raise AssertionError("portable asset should not be launched as an installer")


def test_download_update_installer_rejects_setup_asset_with_path_separator(tmp_path):
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="v0.2.0",
        asset_url="https://example.test/setup.exe",
        asset_name=r"ApplicantScoutCompanionSetup-0.2.0.exe\evil.exe",
        checksum_url="https://example.test/setup.exe.sha256",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
    )

    try:
        download_update_installer(result, download_dir=tmp_path, client=_DownloadClient())  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "installer asset" in str(exc)
    else:
        raise AssertionError("setup asset names must not contain path separators")


def test_download_update_installer_requires_checksum_asset(tmp_path):
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="0.2.0",
        asset_url="https://example.test/setup.exe",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
    )

    try:
        download_update_installer(result, download_dir=tmp_path, client=_DownloadClient())  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "checksum" in str(exc).lower()
    else:
        raise AssertionError("installer without checksum should not be launched")


def test_download_update_installer_rejects_blank_download_urls(tmp_path):
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="v0.2.0",
        asset_url="   ",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        checksum_url="https://example.test/setup.exe.sha256",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
    )

    try:
        download_update_installer(result, download_dir=tmp_path, client=_DownloadClient())  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "installer asset" in str(exc)
    else:
        raise AssertionError("blank installer URL should be rejected")

    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="v0.2.0",
        asset_url="https://example.test/setup.exe",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        checksum_url="   ",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
    )

    try:
        download_update_installer(result, download_dir=tmp_path, client=_DownloadClient())  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "checksum" in str(exc).lower()
    else:
        raise AssertionError("blank checksum URL should be rejected")


def test_download_update_installer_rejects_malformed_checksum(tmp_path):
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="0.2.0",
        asset_url="https://example.test/setup.exe",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        checksum_url="https://example.test/setup.exe.sha256",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
    )

    try:
        download_update_installer(
            result,
            download_dir=tmp_path,
            client=_DownloadClient({"https://example.test/setup.exe.sha256": b"not-a-sha"}),
        )  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "malformed" in str(exc).lower()
    else:
        raise AssertionError("malformed checksum should be rejected")


def test_download_update_installer_rejects_non_utf8_checksum_as_malformed(tmp_path):
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="0.2.0",
        asset_url="https://example.test/setup.exe",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        checksum_url="https://example.test/setup.exe.sha256",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
    )

    try:
        download_update_installer(
            result,
            download_dir=tmp_path,
            client=_DownloadClient(
                {"https://example.test/setup.exe.sha256": b"\xff\xfe\x00"}
            ),
        )  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "malformed" in str(exc).lower()
    else:
        raise AssertionError("binary checksum should be rejected as malformed")


def test_download_update_installer_rejects_checksum_for_wrong_filename(tmp_path):
    digest = hashlib.sha256(b"setup-bytes").hexdigest()
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="0.2.0",
        asset_url="https://example.test/setup.exe",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        checksum_url="https://example.test/setup.exe.sha256",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
    )

    try:
        download_update_installer(
            result,
            download_dir=tmp_path,
            client=_DownloadClient(
                {"https://example.test/setup.exe.sha256": f"{digest}  Other.exe\n".encode()}
            ),
        )  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "filename" in str(exc).lower()
    else:
        raise AssertionError("checksum for wrong filename should be rejected")


def test_download_update_installer_rejects_hash_mismatch(tmp_path):
    wrong_digest = hashlib.sha256(b"other-bytes").hexdigest()
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="0.2.0",
        asset_url="https://example.test/setup.exe",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        checksum_url="https://example.test/setup.exe.sha256",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
    )

    try:
        download_update_installer(
            result,
            download_dir=tmp_path,
            client=_DownloadClient(
                {"https://example.test/setup.exe.sha256": f"{wrong_digest}  ApplicantScoutCompanionSetup-0.2.0.exe\n".encode()}
            ),
        )  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "checksum mismatch" in str(exc).lower()
    else:
        raise AssertionError("hash mismatch should be rejected")
    assert not (tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe").exists()


def test_download_update_installer_streams_installer_without_response_content(tmp_path):
    chunks = [b"setup-", b"bytes"]
    digest = hashlib.sha256(b"".join(chunks)).hexdigest()
    result = _installer_result()
    client = _DownloadClient(
        {
            "https://example.test/setup.exe.sha256": f"{digest}  {result.asset_name}\n".encode(),
            "https://example.test/ApplicantScoutCompanionSetup-0.2.0.exe": _DownloadResponse(chunks),
        }
    )

    path = download_update_installer(result, download_dir=tmp_path, client=client)  # type: ignore[arg-type]

    assert path.read_bytes() == b"setup-bytes"
    assert client.urls == [
        "https://example.test/setup.exe.sha256",
        "https://example.test/ApplicantScoutCompanionSetup-0.2.0.exe",
    ]


def test_download_update_installer_rejects_oversized_checksum_before_installer_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(updater_mod, "_MAX_CHECKSUM_DOWNLOAD_BYTES", 8, raising=False)
    result = _installer_result()
    client = _DownloadClient(
        {
            "https://example.test/setup.exe.sha256": _DownloadResponse(
                b"9" * 9,
                headers={"content-length": "9"},
            )
        }
    )

    with pytest.raises(RuntimeError, match="checksum.*too large"):
        download_update_installer(result, download_dir=tmp_path, client=client)  # type: ignore[arg-type]

    assert client.urls == ["https://example.test/setup.exe.sha256"]
    assert not (tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe").exists()


def test_download_update_installer_rejects_checksum_that_exceeds_limit_while_streaming(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(updater_mod, "_MAX_CHECKSUM_DOWNLOAD_BYTES", 8, raising=False)
    result = _installer_result()
    client = _DownloadClient(
        {
            "https://example.test/setup.exe.sha256": _DownloadResponse(
                [b"1234", b"56789"],
                headers={"content-length": "8"},
            )
        }
    )

    with pytest.raises(RuntimeError, match="checksum.*too large"):
        download_update_installer(result, download_dir=tmp_path, client=client)  # type: ignore[arg-type]

    assert client.urls == ["https://example.test/setup.exe.sha256"]
    assert not (tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe").exists()


def test_download_update_installer_rejects_oversized_installer_content_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(updater_mod, "_MAX_INSTALLER_DOWNLOAD_BYTES", 8, raising=False)
    digest = hashlib.sha256(b"setup-bytes").hexdigest()
    result = _installer_result()
    client = _DownloadClient(
        {
            "https://example.test/setup.exe.sha256": f"{digest}  {result.asset_name}\n".encode(),
            "https://example.test/ApplicantScoutCompanionSetup-0.2.0.exe": _DownloadResponse(
                b"setup-bytes",
                headers={"content-length": "9"},
            ),
        }
    )

    with pytest.raises(RuntimeError, match="installer.*too large"):
        download_update_installer(result, download_dir=tmp_path, client=client)  # type: ignore[arg-type]

    assert not (tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_download_update_installer_rejects_installer_that_exceeds_limit_while_streaming(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(updater_mod, "_MAX_INSTALLER_DOWNLOAD_BYTES", 8, raising=False)
    chunks = [b"setup", b"-byt"]
    digest = hashlib.sha256(b"".join(chunks)).hexdigest()
    result = _installer_result()
    client = _DownloadClient(
        {
            "https://example.test/setup.exe.sha256": f"{digest}  {result.asset_name}\n".encode(),
            "https://example.test/ApplicantScoutCompanionSetup-0.2.0.exe": _DownloadResponse(
                [b"setup", b"-byte"],
                headers={"content-length": "8"},
            ),
        }
    )

    with pytest.raises(RuntimeError, match="installer.*too large"):
        download_update_installer(result, download_dir=tmp_path, client=client)  # type: ignore[arg-type]

    assert not (tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_download_update_installer_accepts_installer_at_size_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(updater_mod, "_MAX_INSTALLER_DOWNLOAD_BYTES", 8, raising=False)
    content = b"12345678"
    digest = hashlib.sha256(content).hexdigest()
    result = _installer_result()
    client = _DownloadClient(
        {
            "https://example.test/setup.exe.sha256": f"{digest}  {result.asset_name}\n".encode(),
            "https://example.test/ApplicantScoutCompanionSetup-0.2.0.exe": _DownloadResponse(
                [b"1234", b"5678"],
                headers={"content-length": "8"},
            ),
        }
    )

    path = download_update_installer(result, download_dir=tmp_path, client=client)  # type: ignore[arg-type]

    assert path.read_bytes() == content


def test_launch_update_installer_runs_silent_setup(monkeypatch, tmp_path):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    class FakePopen:
        pid = 4242

        def __init__(self, args, **_kwargs) -> None:
            calls.append(args)

    monkeypatch.setattr(
        "applicant_scout.updater.verify_update_installer_authenticity",
        lambda _path: None,
        raising=False,
    )
    monkeypatch.setattr("applicant_scout.updater.subprocess.Popen", FakePopen)

    launch = launch_update_installer(installer)

    assert calls == [
        [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/APSCOUT_SELFUPDATE=1",
            f"/APSCOUT_SOURCE_PID={os.getpid()}",
            f"/APSCOUT_SOURCE_PATH={sys.executable}",
        ]
    ]
    assert launch.installer_path == installer
    assert launch.pid == 4242


def test_launch_update_installer_returns_pollable_launch(monkeypatch, tmp_path):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")

    class FakePopen:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def poll(self) -> int:
            return 7

    monkeypatch.setattr(
        "applicant_scout.updater.verify_update_installer_authenticity",
        lambda _path: None,
        raising=False,
    )
    monkeypatch.setattr("applicant_scout.updater.subprocess.Popen", FakePopen)

    launch = launch_update_installer(installer)

    assert launch.poll() == 7


def test_launch_update_installer_preserves_frozen_install_directory(
    monkeypatch, tmp_path
):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")
    installed_dir = tmp_path / "Custom Apps" / "ApplicantScout Companion"
    installed_dir.mkdir(parents=True)
    (installed_dir / "unins000.exe").write_text("", encoding="utf-8")
    current_exe = str(installed_dir / "ApplicantScout.exe")
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **_kwargs) -> None:
            calls.append(args)

    monkeypatch.setattr("applicant_scout.updater.sys.frozen", True, raising=False)
    monkeypatch.setattr("applicant_scout.updater.sys.executable", current_exe)
    monkeypatch.setattr(
        "applicant_scout.updater.verify_update_installer_authenticity",
        lambda _path: None,
        raising=False,
    )
    monkeypatch.setattr("applicant_scout.updater.subprocess.Popen", FakePopen)

    launch_update_installer(installer)

    assert f"/DIR={os.path.dirname(current_exe)}" in calls[0]


def test_launch_update_installer_does_not_install_into_portable_directory(
    monkeypatch, tmp_path
):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")
    portable_dir = tmp_path / "ApplicantScoutPortable"
    portable_dir.mkdir()
    current_exe = str(portable_dir / "ApplicantScout.exe")
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **_kwargs) -> None:
            calls.append(args)

    monkeypatch.setattr("applicant_scout.updater.sys.frozen", True, raising=False)
    monkeypatch.setattr("applicant_scout.updater.sys.executable", current_exe)
    monkeypatch.setattr(
        "applicant_scout.updater.verify_update_installer_authenticity",
        lambda _path: None,
        raising=False,
    )
    monkeypatch.setattr("applicant_scout.updater.subprocess.Popen", FakePopen)

    launch_update_installer(installer)

    assert all(not arg.startswith("/DIR=") for arg in calls[0])


def test_launch_update_installer_rejects_untrusted_installer_before_popen(
    monkeypatch, tmp_path
):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **_kwargs) -> None:
            calls.append(args)

    def reject(_path: Path) -> None:
        raise RuntimeError("Update installer is not trusted")

    monkeypatch.setattr(
        "applicant_scout.updater.verify_update_installer_authenticity",
        reject,
        raising=False,
    )
    monkeypatch.setattr("applicant_scout.updater.subprocess.Popen", FakePopen)

    with pytest.raises(RuntimeError, match="not trusted"):
        launch_update_installer(installer)

    assert calls == []


def test_launch_update_installer_can_skip_signature_gate_for_checksum_verified_release(
    monkeypatch, tmp_path
):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")
    verified_paths: list[Path] = []
    calls: list[list[str]] = []

    class FakePopen:
        pid = 4343

        def __init__(self, args, **_kwargs) -> None:
            calls.append(args)

    monkeypatch.setattr(
        "applicant_scout.updater.verify_update_installer_authenticity",
        lambda path: verified_paths.append(path),
        raising=False,
    )
    monkeypatch.setattr("applicant_scout.updater.subprocess.Popen", FakePopen)

    launch = launch_update_installer(installer, require_trusted_signature=False)

    assert verified_paths == []
    assert calls and calls[0][0] == str(installer)
    assert launch.installer_path == installer
    assert launch.pid == 4343


def test_verify_update_installer_authenticity_accepts_trusted_signed_installer(
    monkeypatch, tmp_path
):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "applicant_scout.updater._TRUSTED_SIGNER_CERT_SHA256",
        frozenset({"abc123"}),
    )
    monkeypatch.setattr(
        "applicant_scout.updater.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"Status":"Valid","StatusMessage":"","Subject":"CN=Antrakt",'
                '"Issuer":"CN=Test CA","CertSha256":"ABC123","Thumbprint":"00"}'
            ),
            stderr="",
        ),
    )

    updater_mod.verify_update_installer_authenticity(installer)


def test_verify_update_installer_authenticity_rejects_unpinned_valid_signature(
    monkeypatch, tmp_path
):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "applicant_scout.updater._TRUSTED_SIGNER_CERT_SHA256",
        frozenset({"different"}),
    )
    monkeypatch.setattr(
        "applicant_scout.updater.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"Status":"Valid","StatusMessage":"","Subject":"CN=Antrakt",'
                '"Issuer":"CN=Test CA","CertSha256":"ABC123","Thumbprint":"00"}'
            ),
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="not pinned"):
        updater_mod.verify_update_installer_authenticity(installer)


def test_verify_update_installer_authenticity_fails_closed_on_powershell_error(
    monkeypatch, tmp_path
):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "applicant_scout.updater.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Get-AuthenticodeSignature failed",
        ),
    )

    with pytest.raises(RuntimeError, match="verification failed"):
        updater_mod.verify_update_installer_authenticity(installer)


def test_verify_update_installer_authenticity_fails_closed_on_timeout(
    monkeypatch, tmp_path
):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=15)

    monkeypatch.setattr("applicant_scout.updater.subprocess.run", timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        updater_mod.verify_update_installer_authenticity(installer)


def test_verify_update_installer_authenticity_fails_closed_on_malformed_json(
    monkeypatch, tmp_path
):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "applicant_scout.updater.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="{bad json",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="malformed JSON"):
        updater_mod.verify_update_installer_authenticity(installer)
