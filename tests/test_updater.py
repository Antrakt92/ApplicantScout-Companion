from __future__ import annotations

import hashlib
import os
import sys
from typing import Any

import httpx

from applicant_scout.updater import (
    DEFAULT_RELEASE_REPO,
    UpdateResult,
    check_for_update,
    download_and_launch_update,
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
    def __init__(self, content: bytes = b"installer") -> None:
        self.content = content
        self.status_code = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def iter_bytes(self):
        yield self.content


class _DownloadClient:
    def __init__(self, responses: dict[str, bytes] | None = None) -> None:
        self.urls: list[str] = []
        self.responses = responses or {}

    def get(self, url: str, **_kwargs) -> _DownloadResponse:
        self.urls.append(url)
        return _DownloadResponse(self.responses.get(url, b"setup-bytes"))


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
    assert "HTTP 403" in result.message


def test_update_check_handles_network_error():
    client = _Client(httpx.ConnectError("offline"))

    result = check_for_update("0.1.0", client=client)  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert "offline" in result.message


def test_update_check_closes_owned_client_on_json_error(monkeypatch):
    client = _Client(_Response(200, ValueError("not json")))

    monkeypatch.setattr("applicant_scout.updater.httpx.Client", lambda **_kwargs: client)

    result = check_for_update("0.1.0")

    assert result.status == "unavailable"
    assert "malformed JSON" in result.message
    assert client.closed is True


def test_download_update_installer_saves_setup_asset_atomically(tmp_path):
    digest = hashlib.sha256(b"setup-bytes").hexdigest()
    client = _DownloadClient(
        {"https://example.test/setup.exe.sha256": f"{digest}  ApplicantScoutCompanionSetup-0.2.0.exe\n".encode()}
    )
    result = UpdateResult(
        status="available",
        message="available",
        current_version="0.1.0",
        latest_version="0.2.0",
        asset_url="https://example.test/ApplicantScoutCompanionSetup-0.2.0.exe",
        asset_name="ApplicantScoutCompanionSetup-0.2.0.exe",
        checksum_url="https://example.test/setup.exe.sha256",
        checksum_name="ApplicantScoutCompanionSetup-0.2.0.exe.sha256",
    )

    path = download_update_installer(result, download_dir=tmp_path, client=client)  # type: ignore[arg-type]

    assert path == tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    assert path.read_bytes() == b"setup-bytes"
    assert client.urls == [
        "https://example.test/ApplicantScoutCompanionSetup-0.2.0.exe",
        "https://example.test/setup.exe.sha256",
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


def test_launch_update_installer_runs_silent_setup(monkeypatch, tmp_path):
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.2.0.exe"
    installer.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **_kwargs) -> None:
            calls.append(args)

    monkeypatch.setattr("applicant_scout.updater.subprocess.Popen", FakePopen)

    launch_update_installer(installer)

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


def test_download_and_launch_update_raises_for_uninstallable_release(monkeypatch):
    result = UpdateResult(
        status="available",
        message=(
            "Version v0.2.0 is available, but the installer checksum asset "
            "was not published."
        ),
        current_version="0.1.0",
        latest_version="v0.2.0",
        release_url="https://github.com/Antrakt92/ApplicantScout-Companion/releases/tag/v0.2.0",
    )

    monkeypatch.setattr("applicant_scout.updater.check_for_update", lambda _version: result)

    def fail_download(*_args, **_kwargs):
        raise AssertionError("uninstallable releases should not be downloaded")

    monkeypatch.setattr("applicant_scout.updater.download_update_installer", fail_download)
    monkeypatch.setattr("applicant_scout.updater.launch_update_installer", fail_download)

    try:
        download_and_launch_update("0.1.0")
    except RuntimeError as exc:
        assert "checksum" in str(exc).lower()
    else:
        raise AssertionError("uninstallable releases must surface as errors")


def test_download_and_launch_update_raises_for_unavailable_update_check(monkeypatch):
    result = UpdateResult(
        status="unavailable",
        message="GitHub update check failed: offline",
        current_version="0.1.0",
    )
    monkeypatch.setattr("applicant_scout.updater.check_for_update", lambda _version: result)

    try:
        download_and_launch_update("0.1.0")
    except RuntimeError as exc:
        assert "offline" in str(exc)
    else:
        raise AssertionError("unavailable update checks must surface as errors")
