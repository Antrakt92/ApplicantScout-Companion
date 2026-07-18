from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
import pytest
import re
import shutil
import subprocess
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
_ACTION_USES_RE = re.compile(r"(?m)^\s*uses:\s*([^\s#]+)\s*(?:#.*)?$")
_SHA_REF_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
_CHOCO_INSTALL_LINE_RE = re.compile(
    r"(?im)^\s*(?:run:\s*)?choco\s+install\s+([A-Za-z0-9_.-]+)\b([^\r\n]*)"
)
_RELEASE_TOOL_PACKAGES = {
    "lua51": "5.1.5",
    "innosetup": "6.7.1",
}


def _read_repo_text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _job_block(workflow: str, job_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert match is not None, f"Missing workflow job: {job_name}"
    return match.group(0)


def _step_block(container: str, step_name: str) -> str:
    match = re.search(
        rf"(?ms)^      - name: {re.escape(step_name)}\n(?P<body>.*?)(?=^      - name:|\Z)",
        container,
    )
    assert match is not None, f"Missing workflow step: {step_name}"
    return match.group(0)


def _assert_order(container: str, *needles: str) -> None:
    positions = [container.index(needle) for needle in needles]
    assert positions == sorted(positions), f"Workflow order is wrong for {needles}"


def _assert_copy_contains(text: str, phrase: str) -> None:
    normalized_text = re.sub(r"\s+", " ", text)
    normalized_phrase = re.sub(r"\s+", " ", phrase)
    assert normalized_phrase in normalized_text


def _workflow_action_refs(workflow: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for uses_target in _ACTION_USES_RE.findall(workflow):
        if uses_target.startswith("./"):
            continue
        action, separator, ref = uses_target.rpartition("@")
        assert separator, f"External action is missing an explicit ref: {uses_target}"
        refs.append((action, ref))
    return refs


def _release_tool_install_args(workflow: str) -> dict[str, list[str]]:
    install_args: dict[str, list[str]] = {}
    for package, args in _CHOCO_INSTALL_LINE_RE.findall(workflow):
        package_name = package.lower()
        extra_release_tools = [
            tool
            for tool in _RELEASE_TOOL_PACKAGES
            if tool != package_name
            and re.search(rf"(?<![-\w]){re.escape(tool)}(?![-\w])", args, re.I)
        ]
        assert not extra_release_tools, (
            "Install release-critical Chocolatey packages in separate commands "
            f"so each package has its own version pin: {package} {args}"
        )
        if package_name in _RELEASE_TOOL_PACKAGES:
            install_args.setdefault(package_name, []).append(args)
    return install_args


def _project_version() -> str:
    match = re.search(
        r'^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        _read_repo_text("pyproject.toml"),
        re.M,
    )
    assert match is not None
    return match.group(1)


def _runtime_version() -> str:
    match = re.search(
        r'^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        _read_repo_text("src/applicant_scout/__init__.py"),
        re.M,
    )
    assert match is not None
    return match.group(1)


def _top_release_notes_entry() -> str:
    notes = _read_repo_text("RELEASE_NOTES.md")
    match = re.search(
        r"(?ms)^##\s+\d+\.\d+\.\d+\s+-\s+.*?(?=^##\s+\d+\.\d+\.\d+\s+-\s+|\Z)",
        notes,
    )
    assert match is not None
    return match.group(0)


def _paired_addon_version() -> str:
    match = re.search(
        r"Requires the ApplicantScout WoW addon `([0-9]+\.[0-9]+\.[0-9]+)`",
        _top_release_notes_entry(),
    )
    assert match is not None
    return match.group(1)


def _minimum_runtime_addon_version() -> str:
    match = re.search(
        r'^MINIMUM_ADDON_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        _read_repo_text("src/applicant_scout/compatibility.py"),
        re.M,
    )
    assert match is not None
    return match.group(1)


def _previous_patch_version(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    if patch > 0:
        return f"{major}.{minor}.{patch - 1}"
    if minor > 0:
        return f"{major}.{minor - 1}.0"
    if major > 0:
        return f"{major - 1}.0.0"
    raise AssertionError("Cannot derive a prior stale version before 0.0.0")


def _next_patch_version(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _copy_release_check_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "src" / "applicant_scout").mkdir(parents=True)
    for path in (
        "scripts/check-release-version.ps1",
        "pyproject.toml",
        "src/applicant_scout/__init__.py",
        "RELEASE_NOTES.md",
        "README.md",
        "constraints-release.txt",
    ):
        source = REPO_ROOT / path
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return repo


def _run_release_check(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repo / "scripts" / "check-release-version.ps1"),
            *args,
        ],
        cwd=repo,
        check=False,
        text=True,
        capture_output=True,
    )


def _run_release_manifest(
    root: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO_ROOT / "scripts" / "release-artifact-manifest.ps1"),
            "-RootPath",
            str(root),
            *args,
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


def _run_installer_signer(
    installer: Path,
    checksum: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in (
        "APSCOUT_SIGNING_CERT_SHA1",
        "APSCOUT_SIGNING_TIMESTAMP_URL",
        "APSCOUT_SIGNTOOL_PATH",
    ):
        env.pop(name, None)
    env.update(extra_env or {})
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO_ROOT / "scripts" / "sign-windows-installer.ps1"),
            "-InstallerPath",
            str(installer),
            "-ChecksumPath",
            str(checksum),
            *args,
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )


def _write_manifest_bundle(root: Path, *, purpose: str) -> dict[str, str]:
    version = _project_version()
    root.mkdir(parents=True)
    installer_name = f"ApplicantScoutCompanionSetup-{version}.exe"
    checksum_name = f"{installer_name}.sha256"
    portable_name = f"ApplicantScoutCompanion-{version}-portable.zip"
    installer_bytes = b"setup-bytes"
    (root / installer_name).write_bytes(installer_bytes)
    digest = hashlib.sha256(installer_bytes).hexdigest()
    (root / checksum_name).write_text(
        f"{digest}  {installer_name}\n",
        encoding="ascii",
    )
    _write_valid_portable_zip(root / portable_name)
    release_body = (
        root / "release-body.md"
        if purpose == "Build"
        else root.parent / "release-body.md"
    )
    release_body.write_bytes(b"## Release notes\n\nExact tag copy.\n")
    return {
        "installer": installer_name,
        "checksum": checksum_name,
        "portable": portable_name,
        "release_body": str(release_body),
    }


def _manifest_identity_args(tag: str, commit: str) -> tuple[str, ...]:
    return (
        "-Tag",
        tag,
        "-CommitSha",
        commit,
        "-PairedAddonTag",
        "v1.2.3",
        "-PairedAddonCommit",
        "d" * 40,
        "-WorkflowRunId",
        "123456",
        "-WorkflowRunAttempt",
        "2",
    )


def _release_input_paths(build_script: str) -> set[str]:
    match = re.search(
        r'(?ms)\$ReleaseInputPaths\s*=\s*@\(\n(?P<body>.*?)\n\s*\)',
        build_script,
    )
    assert match is not None, "Missing $ReleaseInputPaths block"
    return set(re.findall(r'"([^"]+)"', match.group("body")))


def _write_valid_portable_zip(
    archive: Path,
    *,
    root: str = "ApplicantScout",
    omit: set[str] | None = None,
    extra_entries: dict[str, bytes] | None = None,
) -> None:
    entries = {
        f"{root}/ApplicantScout.exe": b"exe-bytes",
        f"{root}/LICENSE": b"license text",
        f"{root}/THIRD-PARTY-NOTICES.md": b"third-party notices",
        f"{root}/RELEASE_NOTES.md": b"release notes",
        f"{root}/licenses/PyQt6/LICENSE.txt": b"dependency license",
    }
    for name in omit or set():
        entries.pop(name, None)
    entries.update(extra_entries or {})
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _write_release_assets(repo: Path) -> tuple[str, str, str]:
    project_version = _project_version()
    dist = repo / "dist"
    dist.mkdir()
    installer_name = f"ApplicantScoutCompanionSetup-{project_version}.exe"
    checksum_name = f"{installer_name}.sha256"
    portable_name = f"ApplicantScoutCompanion-{project_version}-portable.zip"
    installer = dist / installer_name
    installer.write_bytes(b"setup-bytes")
    digest = hashlib.sha256(b"setup-bytes").hexdigest()
    (dist / checksum_name).write_text(
        f"{digest}  {installer_name}\n",
        encoding="ascii",
    )
    _write_valid_portable_zip(dist / portable_name)
    return installer_name, checksum_name, portable_name


def _fake_gh_release_view(
    tmp_path: Path,
    *,
    release_json: dict[str, object] | None = None,
    stdout_text: str | None = None,
    exit_code: int = 0,
    expected_repo: str = "Antrakt92/ApplicantScout-Addon",
    expected_tag: str | None = None,
    expected_json: str = "tagName,isDraft,isPrerelease,assets",
    stderr: str = "",
) -> Path:
    script = tmp_path / "fake-gh.ps1"
    args_path = tmp_path / "fake-gh-args.txt"
    stdout = stdout_text if stdout_text is not None else json.dumps(release_json or {})
    script.write_text(
        "\n".join(
            [
                f"Set-Content -LiteralPath {str(args_path)!r} -Value ($args -join \"`n\") -Encoding UTF8",
                "if ($args.Count -ne 7 -or $args[0] -ne 'release' -or $args[1] -ne 'view') {",
                "    Write-Error 'unexpected gh invocation'",
                "    exit 2",
                "}",
                f"if ($args[3] -ne '--repo' -or $args[4] -ne {expected_repo!r}) {{",
                "    Write-Error 'unexpected gh repo'",
                "    exit 2",
                "}",
                f"if ($args[5] -ne '--json' -or $args[6] -ne {expected_json!r}) {{",
                "    Write-Error 'unexpected gh json fields'",
                "    exit 2",
                "}",
                (
                    f"if ($args[2] -ne {expected_tag!r}) {{ Write-Error 'unexpected gh tag'; exit 2 }}"
                    if expected_tag is not None
                    else ""
                ),
                f"if ({exit_code} -ne 0) {{",
                f"    if ({stderr!r}) {{ [Console]::Error.WriteLine({stderr!r}); Write-Error {stderr!r} }}",
                f"    exit {exit_code}",
                "}",
                f"Write-Output {stdout!r}",
                "exit 0",
            ]
        ),
        encoding="utf-8",
    )
    return script


def _paired_addon_fixture(
    tmp_path: Path,
    *,
    addon_version: str,
    companion_version: str,
) -> Path:
    addon = tmp_path / "ApplicantScout-Addon"
    addon.mkdir()
    (addon / "ApplicantScout.toc").write_text(
        f"## Version: {addon_version}\n",
        encoding="utf-8",
    )
    (addon / "CHANGELOG.md").write_text(
        "\n".join(
            [
                "# Changelog",
                "",
                f"## {addon_version} - 21-May-2026 - Companion {companion_version} release train",
                "",
                "This paired addon release is paired with "
                f"ApplicantScout Companion `{companion_version}`.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return addon


def _set_paired_addon_changelog(addon: Path, text: str) -> None:
    (addon / "CHANGELOG.md").write_text(text, encoding="utf-8")


def test_inno_script_requires_build_env_version_and_source_dir():
    script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")

    assert '"0.0.0"' not in script
    assert "..\\..\\dist\\ApplicantScout" not in script
    assert "APSCOUT_INNO_VERSION" in script
    assert "APSCOUT_INNO_SOURCE_DIR" in script
    assert re.search(r"#error\s+\"Missing APSCOUT_INNO_VERSION", script)
    assert re.search(r"#error\s+\"Missing APSCOUT_INNO_SOURCE_DIR", script)


def test_build_script_checks_native_command_exit_codes_and_restores_inno_env():
    script = _read_repo_text("scripts/build-windows.ps1")

    assert "Invoke-NativeChecked" in script
    assert script.count("Invoke-NativeChecked") >= 3
    assert "New-VersionInfoFile" in script
    assert "--version-file" in script
    assert "$LASTEXITCODE" in script
    assert "try {" in script
    assert "finally {" in script
    assert "Find-InnoSetupCompiler" in script
    assert "LOCALAPPDATA" in script
    assert "APSCOUT_INNO_VERSION" in script
    assert "APSCOUT_INNO_SOURCE_DIR" in script


def test_check_script_checks_native_command_exit_codes():
    script = _read_repo_text("scripts/check.ps1")

    assert "Invoke-NativeChecked" in script
    assert "[switch]$SeasonalOnlineChecks" in script
    assert "[switch]$SeasonalWCLChecks" in script
    assert '[string]$VisualMode = "Strict"' in script
    assert 'Invoke-NativeChecked -Label "Python tests"' in script
    assert 'Invoke-NativeChecked -Label "Seasonal activity IDs"' in script
    assert 'Invoke-NativeChecked -Label "Seasonal challenge map IDs"' in script
    assert (
        'Invoke-NativeChecked -Label "Seasonal WCL zones and encounters"' in script
    )
    assert 'Invoke-NativeChecked -Label "Seasonal online checks"' not in script
    assert 'Invoke-NativeChecked -Label "Overlay visual baselines"' in script
    assert 'Invoke-NativeChecked -Label "Settings dialog visual baselines"' in script
    assert 'Invoke-NativeChecked -Label "Public visual assets"' in script
    assert "render_overlay_fixture.py" in script
    assert "render_settings_dialog_fixture.py" in script
    assert "scripts\\seasonal\\get_mplus_activity_ids.py --check" in script
    assert "scripts\\seasonal\\get_mplus_challenge_map_ids.py --check" in script
    assert (
        "scripts\\seasonal\\verify_wcl_season.py --confirm-spend-wcl-quota"
        in script
    )
    assert "export_public_visual_assets.py" in script
    assert "--addon-root $AddonRoot --check" in script
    assert 'if ($VisualMode -eq "Strict")' in script
    assert "Public visual assets skipped in Smoke mode" in script
    assert "--check --all" in script
    assert '[ValidateSet("Strict", "Smoke")]' in script
    assert "--visual-mode $VisualModeArg" in script
    assert 'Invoke-NativeChecked -Label "Ruff"' in script
    assert 'Invoke-NativeChecked -Label "Pyright"' in script
    assert 'Invoke-NativeChecked -Label "Lua syntax"' in script
    assert script.index('Write-Host "== Python tests =="') < script.index(
        'Write-Host "== Seasonal online checks =="'
    )
    assert script.index('Write-Host "== Seasonal online checks =="') < script.index(
        'Write-Host "== Seasonal WCL check (spends one authenticated query) =="'
    )
    assert script.index(
        'Write-Host "== Seasonal WCL check (spends one authenticated query) =="'
    ) < script.index(
        'Write-Host "== Overlay visual baselines =="'
    )


def test_check_script_does_not_accept_generic_luac_for_wow_syntax():
    script = _read_repo_text("scripts/check.ps1")

    assert "Get-Command luac5.1" in script
    assert "Get-Command luac " not in script


def test_check_script_requires_lua51_interpreter_for_addon_golden_generation():
    script = _read_repo_text("scripts/check.ps1")

    assert "Get-Command lua5.1" in script
    assert "Get-Command lua " not in script
    assert "$Lua51" in script
    assert "Missing lua 5.1" in script


def test_artifact_name_contract_stays_aligned():
    build_script = _read_repo_text("scripts/build-windows.ps1")
    signer = _read_repo_text("scripts/sign-windows-installer.ps1")
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")
    updater = _read_repo_text("src/applicant_scout/updater.py")

    assert "ApplicantScoutCompanion-$Version-portable.zip" in build_script
    assert "ApplicantScoutCompanionSetup-$Version.exe.sha256" in build_script
    assert "System.Security.Cryptography.SHA256" in signer
    assert "ApplicantScoutCompanionSetup-{#MyAppVersion}" in inno_script
    assert "ApplicantScoutCompanionSetup-" in updater
    assert "portable.zip" not in updater
    assert "checksum_url" in updater


def test_windows_build_supports_optional_installer_signing_before_checksum():
    build_script = _read_repo_text("scripts/build-windows.ps1")
    signer = _read_repo_text("scripts/sign-windows-installer.ps1")

    assert "sign-windows-installer.ps1" in build_script
    assert "Find-SignTool" in signer
    assert "APSCOUT_SIGNING_CERT_SHA1" in signer
    assert "APSCOUT_SIGNING_TIMESTAMP_URL" in signer
    assert "/fd SHA256" in signer
    assert "/td SHA256" in signer
    assert "RequireSigning" in signer

    inno_idx = build_script.index('Invoke-NativeChecked -Label "Inno Setup compiler"')
    sign_idx = build_script.index("& $InstallerSigner")
    signer_sign_idx = signer.index("& $SignTool sign")
    checksum_idx = signer.index("System.Security.Cryptography.SHA256")

    assert inno_idx < sign_idx
    assert signer_sign_idx < checksum_idx


def test_installer_signing_helper_refreshes_checksum_and_fails_closed(tmp_path):
    installer = tmp_path / "ApplicantScoutCompanionSetup-1.2.3.exe"
    checksum = tmp_path / f"{installer.name}.sha256"
    installer.write_bytes(b"installer-bytes")

    unsigned = _run_installer_signer(installer, checksum)

    assert unsigned.returncode == 0, unsigned.stdout + unsigned.stderr
    expected = hashlib.sha256(b"installer-bytes").hexdigest()
    assert checksum.read_text(encoding="ascii").strip() == f"{expected}  {installer.name}"

    required = _run_installer_signer(installer, checksum, "-RequireSigning")
    assert required.returncode != 0
    assert "signing is required" in (required.stdout + required.stderr).lower()

    malformed_thumbprint = _run_installer_signer(
        installer,
        checksum,
        extra_env={"APSCOUT_SIGNING_CERT_SHA1": "not-a-thumbprint"},
    )
    assert malformed_thumbprint.returncode != 0
    assert "40-character" in (malformed_thumbprint.stdout + malformed_thumbprint.stderr)


def test_release_workflow_exposes_optional_signing_environment_without_requiring_cert():
    workflow = _read_repo_text(".github/workflows/release.yml")
    build = _job_block(workflow, "build")
    sign_step = _step_block(_job_block(workflow, "draft"), "Sign installer and refresh checksum")

    assert "APSCOUT_SIGNING_" not in build
    assert "APSCOUT_SIGNING_CERT_SHA1" in sign_step
    assert "APSCOUT_SIGNING_TIMESTAMP_URL" in sign_step
    assert "secrets.APSCOUT_SIGNING_CERT_SHA1" in sign_step
    assert "secrets.APSCOUT_SIGNING_TIMESTAMP_URL" in sign_step
    assert "-RequireSigning" not in sign_step


def test_windows_build_uses_app_icon_for_exe_installer_and_runtime():
    icon = REPO_ROOT / "src" / "applicant_scout" / "assets" / "app_icon.ico"
    svg_source = REPO_ROOT / "src" / "applicant_scout" / "assets" / "app_icon.svg"
    build_script = _read_repo_text("scripts/build-windows.ps1")
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")
    main = _read_repo_text("src/applicant_scout/__main__.py")
    pyproject = _read_repo_text("pyproject.toml")

    assert icon.is_file()
    assert svg_source.is_file()
    assert "app_icon.ico" in build_script
    assert "--icon" in build_script
    assert "APSCOUT_INNO_ICON" in build_script
    assert "SetupIconFile={#EnvIcon}" in inno_script
    assert "APSCOUT_INNO_ICON" in inno_script
    assert "setWindowIcon" in main
    assert "assets/*" in pyproject


def test_windows_taskbar_identity_uses_app_icon_and_app_user_model_id():
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")
    main = _read_repo_text("src/applicant_scout/__main__.py")

    assert "Antrakt.ApplicantScout.Companion" in main
    assert "SetCurrentProcessExplicitAppUserModelID" in main
    assert "Antrakt.ApplicantScout.Companion" in inno_script
    assert 'IconFilename: "{app}\\ApplicantScout.exe"' in inno_script
    assert "AppUserModelID: {#MyAppUserModelID}" in inno_script


def test_installer_selects_desktop_shortcut_by_default():
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")
    desktop_task = re.search(r'Name:\s*"desktopicon";[^\n]+', inno_script)

    assert desktop_task is not None
    assert "Flags: unchecked" not in desktop_task.group(0)


def test_installer_uses_per_user_install_dir_for_no_uac_updates():
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")

    assert "DefaultDirName={localappdata}\\Programs\\ApplicantScout Companion" in inno_script
    assert "PrivilegesRequired=lowest" in inno_script
    assert "UsePreviousAppDir=no" in inno_script
    assert "DefaultDirName={autopf}" not in inno_script


def test_installer_best_effort_removes_legacy_per_machine_shortcuts():
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")

    assert "procedure RemoveLegacyPerMachineShortcuts();" in inno_script
    assert "{commondesktop}\\ApplicantScout Companion.lnk" in inno_script
    assert "{commonprograms}\\ApplicantScout Companion\\ApplicantScout Companion.lnk" in inno_script
    assert "RemoveLegacyPerMachineShortcuts();" in inno_script


def test_installer_closes_running_companion_without_restart_manager_prompt():
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")

    assert "CloseApplications=no" in inno_script
    assert "SetupMutex=Antrakt.ApplicantScout.Companion.Setup" in inno_script
    assert "function PrepareToInstall(var NeedsRestart: Boolean): String;" in inno_script
    assert "function InitializeUninstall(): Boolean;" in inno_script
    assert "function ShouldRelaunchAfterInstall(): Boolean;" in inno_script
    assert "--shutdown-running-instance" in inno_script
    assert "ewNoWait" in inno_script
    assert "taskkill /IM ApplicantScout.exe" not in inno_script
    assert "Win32_Process" in inno_script
    assert "ExecutablePath" in inno_script
    assert "{app}\\ApplicantScout.exe" in inno_script
    assert "function IsCompanionRunning(): Boolean;" in inno_script
    assert "ewWaitUntilTerminated" in inno_script
    assert "skipifnotsilent" in inno_script
    assert "Check: ShouldRelaunchAfterInstall" in inno_script
    assert "Result := ''" in inno_script


def test_installer_accepts_self_update_context_for_portable_or_legacy_paths():
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")

    assert "{param:APSCOUT_SELFUPDATE|0}" in inno_script
    assert "{param:APSCOUT_SOURCE_PID|0}" in inno_script
    assert "{param:APSCOUT_SOURCE_PATH|}" in inno_script
    assert "SelfUpdateWasRequested" in inno_script
    assert "function SelfUpdateRequested(): Boolean;" in inno_script
    assert "function SelfUpdateSourcePid(): Integer;" in inno_script
    assert "function SelfUpdateProcessScript(Terminate: Boolean): String;" in inno_script
    assert "procedure CloseSelfUpdateSource();" in inno_script
    assert "CloseSelfUpdateSource();" in inno_script
    assert "CompanionWasRunning or SelfUpdateWasRequested" in inno_script


def test_installer_self_update_uses_control_shutdown_before_cim_fallback():
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")
    match = re.search(
        r"(?ms)^procedure CloseSelfUpdateSource\(\);\n(?P<body>.*?)(?=^procedure CloseRunningCompanion\(\);)",
        inno_script,
    )

    assert match is not None
    body = match.group("body")
    assert "SelfUpdateSourcePath()" in body
    assert "'--shutdown-running-instance'" in body
    assert re.search(r"for\s+\w+\s*:=\s*1\s+to\s+\d+\s+do", body, re.I)

    graceful_idx = body.index("'--shutdown-running-instance'")
    probe_idx = body.index("SelfUpdateProcessScript(False)", graceful_idx)
    fallback_idx = body.index("SelfUpdateProcessScript(True)", probe_idx)

    assert graceful_idx < probe_idx < fallback_idx


def test_interactive_and_silent_update_relaunch_open_settings():
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")
    postinstall_launch = re.search(
        r'Filename:\s*"\{app\}\\ApplicantScout\.exe";[^\n]+Description: "Launch ApplicantScout Companion";[^\n]+',
        inno_script,
    )
    silent_relaunch = re.search(
        r'Filename:\s*"\{app\}\\ApplicantScout\.exe";[^\n]+skipifnotsilent[^\n]+',
        inno_script,
    )

    assert postinstall_launch is not None
    assert 'Parameters: "--show-settings"' in postinstall_launch.group(0)
    assert silent_relaunch is not None
    assert 'Parameters: "--show-settings"' in silent_relaunch.group(0)


def test_release_license_artifacts_exist_and_are_copied_into_dist():
    assert (REPO_ROOT / "LICENSE").is_file()
    assert (REPO_ROOT / "THIRD-PARTY-NOTICES.md").is_file()
    assert (REPO_ROOT / "RELEASE_NOTES.md").is_file()
    assert (REPO_ROOT / "packaging/dependency-license-overrides.toml").is_file()

    build_script = _read_repo_text("scripts/build-windows.ps1")

    assert "Copy-ReleaseTextArtifacts" in build_script
    assert "Copy-DependencyLicenseArtifacts" in build_script
    assert "collect_dependency_licenses.py" in build_script
    assert "--overrides $LicenseOverrides" in build_script
    assert "packages = [" not in build_script
    assert "THIRD-PARTY-NOTICES.md" in build_script
    assert "RELEASE_NOTES.md" in build_script
    assert "LICENSE" in build_script


def test_release_build_uses_pinned_constraints():
    constraints = REPO_ROOT / "constraints-release.txt"
    assert constraints.is_file()

    text = constraints.read_text(encoding="utf-8")
    for package in (
        "PyQt6",
        "PyInstaller",
        "pyinstaller-hooks-contrib",
        "pyzbar",
        "Pillow",
        "httpx",
        "watchdog",
    ):
        assert re.search(rf"^{re.escape(package)}==", text, re.M)

    build_script = _read_repo_text("scripts/build-windows.ps1")
    assert "constraints-release.txt" in build_script
    assert "Assert-ReleaseConstraints" in build_script
    assert "Malformed release constraint" in build_script
    assert "APSCOUT_PYPROJECT_FILE" in build_script
    assert "missing release constraint for pyproject dependency" in build_script
    assert "missing_pyproject_constraints" in build_script


def test_dependency_license_collection_validates_pyproject_constraint_coverage():
    build_script = _read_repo_text("scripts/build-windows.ps1")
    collector = _read_repo_text("scripts/collect_dependency_licenses.py")

    assert "--pyproject $Pyproject" in build_script
    assert "--overrides $LicenseOverrides" in build_script
    assert "release_dependency_names_from_pyproject" in collector
    assert "missing_pyproject_constraints" in collector


def test_release_constraints_are_all_exact_pins():
    constraints = _read_repo_text("constraints-release.txt")

    for raw in constraints.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==.+", line), line


def test_previous_patch_version_handles_major_boundary():
    assert _previous_patch_version("0.2.4") == "0.2.3"
    assert _previous_patch_version("0.3.0") == "0.2.0"
    assert _previous_patch_version("1.0.0") == "0.0.0"


def test_release_constraints_header_matches_project_version():
    constraints = _read_repo_text("constraints-release.txt")
    pyproject = _read_repo_text("pyproject.toml")

    project_version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    constraints_version = re.search(
        r"^# Release build constraints for ApplicantScout Companion ([0-9]+\.[0-9]+\.[0-9]+)\.",
        constraints,
        re.M,
    )

    assert project_version is not None
    assert constraints_version is not None
    assert constraints_version.group(1) == project_version.group(1)


def test_runtime_addon_warning_matches_paired_release_version():
    assert _minimum_runtime_addon_version() == _paired_addon_version()


def test_release_build_refuses_dirty_release_inputs_by_default():
    build_script = _read_repo_text("scripts/build-windows.ps1")
    release_inputs = _release_input_paths(build_script)

    assert "Assert-CleanReleaseInputs" in build_script
    assert "AllowDirtyReleaseInputs" in build_script
    assert "Refusing to build release artifacts from dirty release inputs" in build_script
    assert {
        "pyproject.toml",
        "constraints-release.txt",
        "LICENSE",
        "THIRD-PARTY-NOTICES.md",
        "RELEASE_NOTES.md",
        "src",
        "packaging",
        "scripts\\build-windows.ps1",
        "scripts\\check.ps1",
        "scripts\\check-release-version.ps1",
        "scripts\\sign-windows-installer.ps1",
        "scripts\\collect_dependency_licenses.py",
        "scripts\\export_public_visual_assets.py",
        "scripts\\overlay_visual_fixture.py",
        "scripts\\render_overlay_fixture.py",
        "scripts\\settings_dialog_visual_fixture.py",
        "scripts\\render_settings_dialog_fixture.py",
        "scripts\\visual_fixture_checks.py",
        "docs\\visual",
    }.issubset(release_inputs)
    assert "--untracked-files=all" in build_script


def test_release_version_metadata_is_ready_for_current_version():
    readme = _read_repo_text("README.md")
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")
    project_version = _project_version()
    paired_addon_version = _paired_addon_version()

    assert _runtime_version() == project_version
    assert _top_release_notes_entry().startswith(f"## {project_version} - ")
    assert f"ApplicantScout addon `{paired_addon_version}`" not in checklist
    assert "<paired addon version>" in checklist
    assert f"ApplicantScout Companion `{project_version}`" not in readme
    assert "https://github.com/Antrakt92/ApplicantScout-Addon/releases/latest" in readme
    assert "ApplicantScout-0.1.0.zip" not in readme
    assert "releases/tag/v0.1.0" not in readme
    assert "releases/tag/v0.1.2" not in readme


@pytest.mark.parametrize(
    "bad_install_copy",
    [
        "https://github.com/Antrakt92/ApplicantScout-Addon/releases/download/v0.4.3/ApplicantScout-v0.4.3.zip",
        "https://github.com/Antrakt92/ApplicantScout-Companion/releases/download/v0.8.0/ApplicantScoutCompanionSetup-0.8.0.exe",
        "https://github.com/Antrakt92/ApplicantScout-Addon/releases",
        "https://github.com/Antrakt92/ApplicantScout-Companion/releases",
        "https://github.com/Antrakt92/ApplicantScout-Addon/archive/refs/tags/v0.4.3.zip",
        "https://github.com/Antrakt92/ApplicantScout-Addon/zipball/v0.4.3",
        "https://github.com/Antrakt92/ApplicantScout-Companion/tarball/v0.8.0",
        "ApplicantScout WoW addon `0.4.3`",
        "ApplicantScout Companion `0.8.0`",
        "Install `ApplicantScout-0.4.3.zip` from GitHub.",
        "Install `ApplicantScoutCompanionSetup-0.8.0.exe` from GitHub.",
    ],
)
def test_release_check_rejects_pinned_public_install_links(
    tmp_path,
    bad_install_copy,
):
    repo = _copy_release_check_fixture(tmp_path)
    readme_path = repo / "README.md"
    readme_path.write_text(
        readme_path.read_text(encoding="utf-8") + f"\n{bad_install_copy}\n",
        encoding="utf-8",
    )

    result = _run_release_check(repo, "-Tag", f"v{_project_version()}")

    assert result.returncode != 0
    assert "use releases/latest" in (result.stdout + result.stderr)


def test_release_readiness_test_name_is_not_tied_to_current_version():
    source = _read_repo_text("tests/test_packaging_contract.py")

    assert re.search(r"def test_release_version_metadata_is_ready_for_\d+", source) is None


def test_release_version_check_script_documents_asset_contract():
    script = _read_repo_text("scripts/check-release-version.ps1")
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "Test-PortableZipContract" in script
    assert "ApplicantScoutCompanionSetup-$TagVersion.exe" in script
    assert "$InstallerName.sha256" in script
    assert "ApplicantScoutCompanion-$TagVersion-portable.zip" in script
    assert "RequireAssets" in script
    assert "constraints-release.txt" in script
    assert "Release constraints header" in script
    assert ".\\scripts\\check-release-version.ps1 -Tag v<companion version> -RequireAssets" in checklist
    assert "portable ZIP integrity" in checklist
    assert "presence and checksum consistency only" not in checklist


def test_release_checklist_documents_last_tag_reconciliation():
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "git log --oneline <last companion tag>..HEAD" in checklist
    assert "git log --oneline <last addon tag>..HEAD" in checklist
    assert "RELEASE_NOTES.md::Unreleased" in checklist
    assert "CHANGELOG.md::Unreleased" in checklist
    assert "top versioned" in checklist


def test_release_checklist_keeps_release_notes_reconciliation_manual_not_script_gate():
    script = _read_repo_text("scripts/check-release-version.ps1")
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "commit subject" not in script.lower()
    assert "git log --oneline" not in script
    assert "manual reconciliation" in checklist.lower()


def test_release_checklist_documents_paired_tag_push_sequence():
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    companion_push = "git push origin v<companion version>"
    addon_push = "git push origin v<paired addon version>"
    assert companion_push in checklist
    assert addon_push in checklist
    assert checklist.index(companion_push) < checklist.index(addon_push)
    assert "120-second" in checklist
    assert "Do not wait for the companion workflow to finish" in checklist


def test_release_checklist_documents_asset_wait_rerun_path():
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "180-second" in checklist
    assert "rerun the failed addon workflow" in checklist
    assert "Do not delete/recreate or force-push release tags" in checklist


def test_release_checklist_documents_optional_signing_gate():
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "APSCOUT_SIGNING_CERT_SHA1" in checklist
    assert "signtool" in checklist.lower()
    assert "before final `.sha256` generation" in checklist
    assert "unsigned" in checklist.lower()


def test_readme_explains_signing_ready_but_unsigned_until_certificate():
    readme = _read_repo_text("README.md")

    assert "signing-ready release pipeline" in readme
    assert "APSCOUT_SIGNING_CERT_SHA1" in readme
    assert "unsigned until a code-signing certificate is configured" in readme


def test_release_checklist_requires_local_strict_visual_and_media_export_gate():
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "local strict visual baselines" in checklist.lower()
    assert (
        ".\\scripts\\check.ps1 -SeasonalOnlineChecks -SeasonalWCLChecks"
        in checklist
    )
    assert "check-applicantscout-copy.ps1" not in checklist
    assert "MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME" in checklist
    assert "MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME" in checklist
    assert "MythicPlusSeasonTrackedMap" in checklist
    assert "MapChallengeMode" in checklist
    assert "authenticated Warcraft Logs GraphQL request" in checklist
    assert "fewer than 50 points remain" in checklist
    assert "Do not use `-VisualMode Smoke` for this local release gate" in checklist
    assert "CI/release smoke" in checklist
    assert ".\\scripts\\check.ps1" in checklist
    assert (
        ".\\.venv\\Scripts\\python scripts\\export_public_visual_assets.py "
        "--addon-root ..\\ApplicantScout-Addon --check"
    ) in checklist


def test_workflows_do_not_upgrade_bootstrap_pip():
    for workflow_path in (
        ".github/workflows/check.yml",
        ".github/workflows/release.yml",
        ".github/workflows/windows-vs2026-canary.yml",
    ):
        workflow = _read_repo_text(workflow_path)

        assert "--upgrade pip" not in workflow


def test_docs_readme_explains_public_media_export_and_strict_gate():
    docs_index = _read_repo_text("docs/README.md")

    assert "Strict local baseline checks" in docs_index
    assert "CI/release smoke" in docs_index
    assert "Seasonal M+ challenge-map helper" in docs_index
    assert (
        ".\\.venv\\Scripts\\python scripts\\export_public_visual_assets.py "
        "--addon-root ..\\ApplicantScout-Addon --check"
    ) in docs_index


def test_release_workflow_runs_existing_gates_before_verified_draft():
    workflow = _read_repo_text(".github/workflows/release.yml")
    build = _job_block(workflow, "build")
    draft = _job_block(workflow, "draft")

    assert "tags:" in workflow
    assert "'v*'" in workflow
    assert "github.event.created == true" in workflow
    assert "github.event.forced == false" in workflow
    assert "github.event.deleted == false" in workflow
    assert re.search(r"(?m)^    runs-on: windows-2022\s*$", build)
    assert re.search(r"(?m)^    runs-on: windows-2022\s*$", draft)
    assert "contents: read" in build
    assert "contents: write" not in build
    assert "contents: write" in draft
    assert "python-version: '3.13'" in build
    assert "constraints-release.txt" in build
    assert ".\\.venv\\Scripts\\python -m pip install -r constraints-release.txt" in build
    assert "APPLICANT_SCOUT_VISUAL_BASELINE" not in build
    assert ".\\scripts\\check.ps1 -AddonRoot ..\\ApplicantScout-Addon -VisualMode Smoke" in build
    assert "choco install lua51 --version=5.1.5" in build
    assert "choco install innosetup --version=6.7.1" in build
    assert "repository: Antrakt92/ApplicantScout-Addon" in build
    assert "id: paired-addon" in build
    assert "-PairedAddonRefOutputPath $env:GITHUB_OUTPUT" in build
    assert "ref: ${{ steps.paired-addon.outputs.ref }}" in build
    assert "Validate paired addon metadata" in build
    assert "-PairedAddonRoot ..\\ApplicantScout-Addon" in build
    paired_version_idx = build.index("-PairedAddonRoot ..\\ApplicantScout-Addon")
    check_idx = build.index(".\\scripts\\check.ps1 -AddonRoot")
    version_idx = build.index(".\\scripts\\check-release-version.ps1 -Tag", check_idx)
    build_idx = build.index(".\\scripts\\build-windows.ps1 -SkipChecks")
    assets_idx = build.index(".\\scripts\\check-release-version.ps1 -Tag $env:GITHUB_REF_NAME -RequireAssets")
    assert "-RequirePublishedPairedAddonAssets" not in workflow
    assert paired_version_idx < check_idx < version_idx < build_idx < assets_idx
    _assert_order(
        draft,
        "Verify credentialless build manifest",
        "Create exact-tag release manifest",
        "Verify exact-tag release manifest",
        "Create draft release with assets",
        "Verify draft release assets",
        "Verify remote draft bytes against authoritative manifest",
    )
    assert "gh release edit" not in workflow
    assert "draft=false" not in workflow


def test_release_artifact_manifest_binds_build_bundle_to_tag_and_commit(tmp_path):
    root = tmp_path / "release-bundle"
    names = _write_manifest_bundle(root, purpose="Build")
    version = _project_version()
    tag = f"v{version}"
    commit = "a" * 40

    created = _run_release_manifest(
        root,
        "-Mode",
        "Create",
        "-Purpose",
        "Build",
        *_manifest_identity_args(tag, commit),
    )

    assert created.returncode == 0, created.stdout + created.stderr
    manifest_path = root / "release-build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    assert manifest["schemaVersion"] == 2
    assert manifest["repository"] == "Antrakt92/ApplicantScout-Companion"
    assert manifest["tag"] == tag
    assert manifest["commit"] == commit
    assert {entry["name"] for entry in manifest["files"]} == {
        names["installer"],
        names["checksum"],
        names["portable"],
        "release-body.md",
    }
    assert {entry["name"] for entry in manifest["portableEntries"]} >= {
        "ApplicantScout/LICENSE",
        "ApplicantScout/THIRD-PARTY-NOTICES.md",
        "ApplicantScout/RELEASE_NOTES.md",
    }

    verified = _run_release_manifest(
        root,
        "-Mode",
        "Verify",
        "-Purpose",
        "Build",
        *_manifest_identity_args(tag, commit),
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_release_artifact_manifest_rejects_tag_commit_asset_and_notice_drift(tmp_path):
    root = tmp_path / "release-assets"
    names = _write_manifest_bundle(root, purpose="Release")
    version = _project_version()
    tag = f"v{version}"
    commit = "b" * 40
    created = _run_release_manifest(
        root,
        "-Mode",
        "Create",
        "-Purpose",
        "Release",
        "-ReleaseBodyPath",
        names["release_body"],
        *_manifest_identity_args(tag, commit),
    )
    assert created.returncode == 0, created.stdout + created.stderr

    wrong_tag = _run_release_manifest(
        root,
        "-Mode",
        "Verify",
        "-Purpose",
        "Release",
        *_manifest_identity_args("v9.9.9", commit),
    )
    assert wrong_tag.returncode != 0
    assert "manifest" in (wrong_tag.stdout + wrong_tag.stderr).lower()

    wrong_commit = _run_release_manifest(
        root,
        "-Mode",
        "Verify",
        "-Purpose",
        "Release",
        *_manifest_identity_args(tag, "c" * 40),
    )
    assert wrong_commit.returncode != 0
    assert "commit" in (wrong_commit.stdout + wrong_commit.stderr).lower()

    (root / names["installer"]).write_bytes(b"changed-installer")
    changed_asset = _run_release_manifest(
        root,
        "-Mode",
        "Verify",
        "-Purpose",
        "Release",
        *_manifest_identity_args(tag, commit),
    )
    assert changed_asset.returncode != 0
    assert "sha-256" in (changed_asset.stdout + changed_asset.stderr).lower()

    (root / names["installer"]).write_bytes(b"setup-bytes")
    _write_valid_portable_zip(
        root / names["portable"],
        extra_entries={
            "ApplicantScout/THIRD-PARTY-NOTICES.md": b"changed notices",
        },
    )
    changed_notice = _run_release_manifest(
        root,
        "-Mode",
        "Verify",
        "-Purpose",
        "Release",
        *_manifest_identity_args(tag, commit),
    )
    assert changed_notice.returncode != 0
    assert "sha-256" in (changed_notice.stdout + changed_notice.stderr).lower()


def test_release_artifact_manifest_binds_and_rejects_tampered_release_copy(tmp_path):
    root = tmp_path / "release-assets"
    names = _write_manifest_bundle(root, purpose="Release")
    version = _project_version()
    tag = f"v{version}"
    commit = "f" * 40
    created = _run_release_manifest(
        root,
        "-Mode",
        "Create",
        "-Purpose",
        "Release",
        "-ReleaseBodyPath",
        names["release_body"],
        *_manifest_identity_args(tag, commit),
    )
    assert created.returncode == 0, created.stdout + created.stderr

    manifest_path = root / f"ApplicantScoutCompanion-{version}-release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 2
    assert manifest["releaseCopy"]["title"] == tag
    body = manifest["releaseCopy"]["body"]
    expected_body = Path(names["release_body"]).read_bytes()
    assert body["encoding"] == "utf-8"
    assert body["size"] == len(expected_body)
    assert body["sha256"] == hashlib.sha256(expected_body).hexdigest()
    assert body["contentBase64"]

    body["contentBase64"] = "dGFtcGVyZWQK"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    verified = _run_release_manifest(
        root,
        "-Mode",
        "Verify",
        "-Purpose",
        "Release",
        *_manifest_identity_args(tag, commit),
    )
    assert verified.returncode != 0
    assert "release copy body" in (verified.stdout + verified.stderr).lower()


def test_release_artifact_manifest_rejects_noncanonical_tag_and_extra_files(tmp_path):
    root = tmp_path / "release-assets"
    names = _write_manifest_bundle(root, purpose="Release")
    commit = "e" * 40

    noncanonical = _run_release_manifest(
        root,
        "-Mode",
        "Create",
        "-Purpose",
        "Release",
        "-ReleaseBodyPath",
        names["release_body"],
        *_manifest_identity_args("v01.2.3", commit),
    )
    assert noncanonical.returncode != 0
    assert "strict vx.y.z" in (noncanonical.stdout + noncanonical.stderr).lower()

    tag = f"v{_project_version()}"
    created = _run_release_manifest(
        root,
        "-Mode",
        "Create",
        "-Purpose",
        "Release",
        "-ReleaseBodyPath",
        names["release_body"],
        *_manifest_identity_args(tag, commit),
    )
    assert created.returncode == 0, created.stdout + created.stderr
    (root / "unexpected.bin").write_bytes(b"unexpected")

    verified = _run_release_manifest(
        root,
        "-Mode",
        "Verify",
        "-Purpose",
        "Release",
        *_manifest_identity_args(tag, commit),
    )
    assert verified.returncode != 0
    assert "wrong exact file set" in (verified.stdout + verified.stderr).lower()


def test_release_workflow_separates_read_only_build_from_narrow_draft_writer():
    workflow = _read_repo_text(".github/workflows/release.yml")
    build = _job_block(workflow, "build")
    draft = _job_block(workflow, "draft")

    assert "contents: read" in build
    assert "contents: write" not in build
    assert "persist-credentials: false" in _step_block(build, "Checkout companion")
    assert "persist-credentials: false" in _step_block(build, "Checkout addon")
    assert "Install release dependencies" in build
    assert "APSCOUT_SIGNING_" not in build
    assert "gh release create" not in build
    create_manifest = _step_block(build, "Create credentialless build manifest")
    assert "release-artifact-manifest.ps1" in create_manifest
    assert "-Mode Create" in create_manifest
    assert "-Purpose Build" in create_manifest
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in build
    assert "companion-release-build-attempt-${{ github.run_attempt }}" in build
    assert "build_attempt: ${{ steps.release-identity.outputs.attempt }}" in build

    assert re.search(r"(?m)^    needs: build\s*$", draft)
    assert "contents: write" in draft
    assert "persist-credentials: false" in _step_block(draft, "Checkout companion")
    assert "Install release dependencies" not in draft
    assert "choco install" not in draft
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in draft
    assert "companion-release-build-attempt-${{ needs.build.outputs.build_attempt }}" in draft
    assert "-WorkflowRunAttempt $env:BUILD_ATTEMPT" in draft
    assert "companion-release-assets-attempt-${{ github.run_attempt }}" in draft
    _assert_order(
        draft,
        "Verify credentialless build manifest",
        "Sign installer and refresh checksum",
        "Create exact-tag release manifest",
        "Verify exact-tag release manifest",
        "Upload authoritative release assets",
        "Create draft release with assets",
    )


def test_release_workflow_carries_exact_tag_copy_through_authoritative_manifest():
    workflow = _read_repo_text(".github/workflows/release.yml")
    build = _job_block(workflow, "build")
    draft = _job_block(workflow, "draft")
    extract = _step_block(build, "Extract release notes")
    create_manifest = _step_block(draft, "Create exact-tag release manifest")
    create_draft = _step_block(draft, "Create draft release with assets")
    verify_draft = _step_block(
        draft,
        "Verify remote draft bytes against authoritative manifest",
    )

    assert "WriteAllText" in extract
    assert "UTF8Encoding]::new($false)" in extract
    assert '-replace "`r`n?", "`n"' in extract
    assert '-ReleaseBodyPath (Join-Path $Bundle "release-body.md")' in create_manifest
    assert "$Manifest.releaseCopy.title" in create_draft
    assert "$Manifest.releaseCopy.body.contentBase64" in create_draft
    assert "WriteAllBytes" in create_draft
    assert "--title $Title" in create_draft
    assert "--notes-file $Body" in create_draft
    assert "$Release.name -cne [string]$Manifest.releaseCopy.title" in verify_draft
    assert "$Release.body -cne $ExpectedBody" in verify_draft
    assert "authoritative exact-tag manifest" in verify_draft


def test_release_workflow_requires_tag_commit_reachable_from_origin_main():
    workflow = _read_repo_text(".github/workflows/release.yml")
    build = _job_block(workflow, "build")
    gate = _step_block(build, "Verify release tag is reachable from origin/main")

    assert "working-directory: ApplicantScout-Companion" in gate
    assert "git fetch --no-tags --prune origin +refs/heads/main:refs/remotes/origin/main" in gate
    assert "git rev-parse HEAD" in gate
    assert "git rev-parse refs/remotes/origin/main" in gate
    assert "git merge-base --is-ancestor" in gate
    assert "$LASTEXITCODE" in gate
    assert "not reachable from origin/main" in gate
    assert "Could not verify release tag ancestry" in gate
    _assert_order(
        build,
        "Checkout companion",
        "Verify release tag is reachable from origin/main",
        "Resolve paired addon tag",
        "Refuse existing release",
        "Wait for paired addon tag",
        "Build unsigned Windows artifacts",
        "Upload credentialless build bundle",
    )


def test_release_workflow_requires_paired_addon_tag_reachable_from_origin_main():
    workflow = _read_repo_text(".github/workflows/release.yml")
    build = _job_block(workflow, "build")
    gate = _step_block(
        build,
        "Verify paired addon tag is reachable from origin/main",
    )

    assert "working-directory: ApplicantScout-Addon" in gate
    assert "git fetch --no-tags --prune origin +refs/heads/main:refs/remotes/origin/main" in gate
    assert "git rev-parse HEAD" in gate
    assert "git rev-parse refs/remotes/origin/main" in gate
    assert "git merge-base --is-ancestor" in gate
    assert "$LASTEXITCODE" in gate
    assert "not reachable from addon origin/main" in gate
    assert "Could not verify paired addon tag ancestry" in gate
    _assert_order(
        build,
        "Checkout addon",
        "Verify paired addon tag is reachable from origin/main",
        "Validate paired addon metadata",
        "Build unsigned Windows artifacts",
        "Upload credentialless build bundle",
    )


def test_release_workflow_refuses_existing_release_before_build_or_create():
    workflow = _read_repo_text(".github/workflows/release.yml")
    script = _read_repo_text("scripts/check-release-version.ps1")
    build = _job_block(workflow, "build")
    draft = _job_block(workflow, "draft")
    refuse_step = _step_block(build, "Refuse existing release")
    writer_refuse = _step_block(draft, "Refuse existing release before writer boundary")

    refuse_idx = workflow.index("Refuse existing release")
    build_idx = workflow.index(".\\scripts\\build-windows.ps1 -SkipChecks")
    release_idx = workflow.index("gh release create")

    assert "-RefuseExistingRelease" in refuse_step
    assert "-GitHubRepository $env:GITHUB_REPOSITORY" in refuse_step
    assert "working-directory: ApplicantScout-Companion" in refuse_step
    assert "GH_TOKEN: ${{ github.token }}" in refuse_step
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in writer_refuse
    assert "gh release view $env:GITHUB_REF_NAME" not in refuse_step
    assert "already exists; refusing" in script
    assert refuse_idx < build_idx < release_idx


def test_release_version_check_accepts_missing_own_release(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        expected_json="tagName,isDraft,isPrerelease",
        exit_code=1,
        stderr="release not found",
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RefuseExistingRelease",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    gh_args = (tmp_path / "fake-gh-args.txt").read_text(encoding="utf-8")
    assert "Antrakt92/ApplicantScout-Companion" in gh_args
    assert f"v{project_version}" in gh_args


def test_release_version_check_refuses_existing_own_release(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        expected_json="tagName,isDraft,isPrerelease",
        release_json={
            "tagName": f"v{project_version}",
            "isDraft": True,
            "isPrerelease": False,
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RefuseExistingRelease",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    assert "already exists; refusing" in (result.stdout + result.stderr)


def test_release_version_check_rejects_empty_stderr_own_release_lookup(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        expected_json="tagName,isDraft,isPrerelease",
        exit_code=2,
        stderr="",
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RefuseExistingRelease",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Could not determine whether release" in output
    assert "exit code 2" in output


def test_release_version_check_rejects_generic_not_found_own_release_lookup(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        expected_json="tagName,isDraft,isPrerelease",
        exit_code=1,
        stderr="HTTP 404: Not Found",
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RefuseExistingRelease",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Could not determine whether release" in output
    assert "HTTP 404: Not Found" in output


def test_release_version_check_refuse_existing_release_requires_repository(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        expected_json="tagName,isDraft,isPrerelease",
        exit_code=1,
        stderr="release not found",
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RefuseExistingRelease",
        "-GitHubRepository",
        "",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    assert "Missing GitHub repository" in (result.stdout + result.stderr)


def test_release_version_check_accepts_own_draft_release_assets(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    installer_name = f"ApplicantScoutCompanionSetup-{project_version}.exe"
    checksum_name = f"{installer_name}.sha256"
    portable_name = f"ApplicantScoutCompanion-{project_version}-portable.zip"
    manifest_name = f"ApplicantScoutCompanion-{project_version}-release-manifest.json"
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        release_json={
            "tagName": f"v{project_version}",
            "isDraft": True,
            "isPrerelease": False,
            "assets": [
                {"name": installer_name},
                {"name": checksum_name},
                {"name": portable_name},
                {"name": manifest_name},
            ],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequireDraftReleaseAssets",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    gh_args = (tmp_path / "fake-gh-args.txt").read_text(encoding="utf-8")
    assert "Antrakt92/ApplicantScout-Companion" in gh_args
    assert f"v{project_version}" in gh_args


def test_release_version_check_rejects_unexpected_own_draft_release_asset(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    stale_version = _previous_patch_version(project_version)
    installer_name = f"ApplicantScoutCompanionSetup-{project_version}.exe"
    checksum_name = f"{installer_name}.sha256"
    portable_name = f"ApplicantScoutCompanion-{project_version}-portable.zip"
    manifest_name = f"ApplicantScoutCompanion-{project_version}-release-manifest.json"
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        release_json={
            "tagName": f"v{project_version}",
            "isDraft": True,
            "isPrerelease": False,
            "assets": [
                {"name": installer_name},
                {"name": checksum_name},
                {"name": portable_name},
                {"name": manifest_name},
                {"name": f"ApplicantScoutCompanionSetup-{stale_version}.exe"},
            ],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequireDraftReleaseAssets",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    output = re.sub(r"\s+", "", result.stdout + result.stderr)
    assert (
        f"unexpectedasset:ApplicantScoutCompanionSetup-{stale_version}.exe" in output
    )


def test_release_version_check_rejects_own_draft_when_already_public(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        release_json={
            "tagName": f"v{project_version}",
            "isDraft": False,
            "isPrerelease": False,
            "assets": [],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequireDraftReleaseAssets",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    assert "expected draft" in (result.stdout + result.stderr).lower()


def test_release_version_check_rejects_missing_own_draft_asset(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        release_json={
            "tagName": f"v{project_version}",
            "isDraft": True,
            "isPrerelease": False,
            "assets": [
                {"name": f"ApplicantScoutCompanionSetup-{project_version}.exe"},
                {"name": f"ApplicantScoutCompanion-{project_version}-portable.zip"},
            ],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequireDraftReleaseAssets",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    output = re.sub(r"\s+", "", result.stdout + result.stderr)
    assert f"missingasset:ApplicantScoutCompanionSetup-{project_version}.exe.sha256" in output


def test_release_version_check_rejects_prerelease_own_draft(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        release_json={
            "tagName": f"v{project_version}",
            "isDraft": True,
            "isPrerelease": True,
            "assets": [],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequireDraftReleaseAssets",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    assert "marked prerelease" in (result.stdout + result.stderr)


def test_release_version_check_accepts_own_published_release_assets(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    installer_name = f"ApplicantScoutCompanionSetup-{project_version}.exe"
    checksum_name = f"{installer_name}.sha256"
    portable_name = f"ApplicantScoutCompanion-{project_version}-portable.zip"
    manifest_name = f"ApplicantScoutCompanion-{project_version}-release-manifest.json"
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        release_json={
            "tagName": f"v{project_version}",
            "isDraft": False,
            "isPrerelease": False,
            "assets": [
                {"name": installer_name},
                {"name": checksum_name},
                {"name": portable_name},
                {"name": manifest_name},
            ],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequirePublishedReleaseAssets",
        "-PublishedReleaseWaitSeconds",
        "0",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    gh_args = (tmp_path / "fake-gh-args.txt").read_text(encoding="utf-8")
    assert "Antrakt92/ApplicantScout-Companion" in gh_args
    assert f"v{project_version}" in gh_args


def test_release_version_check_rejects_unexpected_own_published_release_asset(
    tmp_path,
):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    stale_version = _previous_patch_version(project_version)
    installer_name = f"ApplicantScoutCompanionSetup-{project_version}.exe"
    checksum_name = f"{installer_name}.sha256"
    portable_name = f"ApplicantScoutCompanion-{project_version}-portable.zip"
    manifest_name = f"ApplicantScoutCompanion-{project_version}-release-manifest.json"
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_repo="Antrakt92/ApplicantScout-Companion",
        expected_tag=f"v{project_version}",
        release_json={
            "tagName": f"v{project_version}",
            "isDraft": False,
            "isPrerelease": False,
            "assets": [
                {"name": installer_name},
                {"name": checksum_name},
                {"name": portable_name},
                {"name": manifest_name},
                {"name": f"ApplicantScoutCompanion-{stale_version}-portable.zip"},
            ],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequirePublishedReleaseAssets",
        "-PublishedReleaseWaitSeconds",
        "0",
        "-GitHubRepository",
        "Antrakt92/ApplicantScout-Companion",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    output = re.sub(r"\s+", "", result.stdout + result.stderr)
    assert (
        f"unexpectedasset:ApplicantScoutCompanion-{stale_version}-portable.zip"
        in output
    )


def test_release_version_check_own_release_assets_require_repository(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequireDraftReleaseAssets",
        "-GitHubRepository",
        "",
    )

    assert result.returncode != 0
    assert "Missing GitHub repository" in (result.stdout + result.stderr)


def test_release_workflow_pins_external_actions_to_commit_shas():
    workflow = _read_repo_text(".github/workflows/release.yml")
    action_refs = _workflow_action_refs(workflow)

    assert Counter(action for action, _ in action_refs) == Counter(
        {
            "actions/checkout": 3,
            "actions/setup-python": 1,
            "actions/upload-artifact": 2,
            "actions/download-artifact": 1,
        }
    )
    for action, ref in action_refs:
        assert _SHA_REF_RE.fullmatch(ref), f"{action} must be pinned to a full commit SHA"


def test_publish_release_workflow_requires_smoke_attestation_and_verified_assets():
    workflow = _read_repo_text(".github/workflows/publish-release.yml")
    verify = _job_block(workflow, "verify")
    publish = _job_block(workflow, "publish")

    assert "workflow_dispatch:" in workflow
    assert "tag:" in workflow
    assert "release_run_id:" in workflow
    assert "smoke_tested_from_version:" in workflow
    assert "smoke_tested_installer_sha256:" in workflow
    assert "confirm_checksum_gated_update_smoke:" in workflow
    assert "type: boolean" in workflow
    assert (
        "CONFIRM_CHECKSUM_GATED_UPDATE_SMOKE: "
        "${{ inputs.confirm_checksum_gated_update_smoke }}"
    ) in workflow
    assert re.search(r"(?m)^    runs-on: windows-2022\s*$", publish)
    assert "actions: read" in verify
    assert "contents: read" in verify
    assert "contents: write" not in verify
    assert "contents: write" in publish

    checkout = _step_block(verify, "Checkout companion")
    ancestry = _step_block(verify, "Verify release tag is reachable from origin/main")
    run_check = _step_block(verify, "Verify successful release workflow run")
    manifest_check = _step_block(verify, "Verify authoritative exact-tag manifest")
    attestation = _step_block(verify, "Validate smoke attestation")
    draft_check = _step_block(verify, "Verify draft release assets")
    publish_step = _step_block(publish, "Revalidate exact bytes and publish release")
    published_check = _step_block(publish, "Verify published release assets")

    assert "ref: ${{ inputs.tag }}" in checkout
    assert '"$env:RELEASE_TAG^{commit}"' in ancestry
    assert "does not match release tag" in ancestry
    assert "release.yml" in run_check
    assert ".event" in run_check
    assert ".status" in run_check
    assert ".conclusion" in run_check
    assert "head_sha" in run_check
    assert "run_attempt" in run_check
    assert "companion-release-assets-attempt-$Attempt" in run_check
    assert ".expired" in run_check
    assert "GH_TOKEN: ${{ github.token }}" in manifest_check
    assert "git/ref/tags/$Tag" in manifest_check
    assert "git/tags/$Sha" in manifest_check
    assert "/commits/$PairedAddonTag" not in manifest_check
    assert "Checksum-gated updater smoke confirmation is required" in attestation
    assert "CONFIRM_CHECKSUM_GATED_UPDATE_SMOKE" in attestation
    assert "SMOKE_TESTED_FROM_VERSION" in attestation
    assert "SMOKE_TESTED_INSTALLER_SHA256" in attestation
    assert "authoritative release artifact" in attestation
    assert 'gh api "repos/$env:GITHUB_REPOSITORY/releases/latest" --jq .tag_name' in attestation
    assert "must match latest published stable" in attestation
    assert "GH_TOKEN: ${{ github.token }}" in attestation
    assert "-RequireDraftReleaseAssets" in draft_check
    assert "gh release edit $env:RELEASE_TAG" in publish_step
    assert "--draft=false" in publish_step
    assert "--prerelease=false" in publish_step
    assert "RELEASE_SETTINGS_READ_TOKEN: ${{ secrets.RELEASE_SETTINGS_READ_TOKEN }}" in publish_step
    assert "IsNullOrWhiteSpace($env:RELEASE_SETTINGS_READ_TOKEN)" in publish_step
    assert "$env:GH_TOKEN = $env:RELEASE_SETTINGS_READ_TOKEN" in publish_step
    assert "$env:GH_TOKEN = $PublishToken" in publish_step
    assert 'X-GitHub-Api-Version: 2026-03-10' in publish_step
    assert 'repos/$env:GITHUB_REPOSITORY/immutable-releases' in publish_step
    assert "$ImmutableSettings.enabled -isnot [bool]" in publish_step
    assert "-not $ImmutableSettings.enabled" in publish_step
    assert "AddSeconds(120)" in published_check
    assert "$Release.immutable -is [bool]" in published_check
    assert re.search(
        r"\$Release\.immutable -is \[bool\]\s+-and\s+\$Release\.immutable",
        published_check,
    )
    assert 'X-GitHub-Api-Version: 2026-03-10' in published_check
    assert "did not become immutable" in published_check
    assert "authoritative copy and asset contract" in published_check
    _assert_order(
        verify,
        "Checkout companion",
        "Verify release tag is reachable from origin/main",
        "Verify successful release workflow run",
        "Download authoritative release assets",
        "Verify authoritative exact-tag manifest",
        "Validate smoke attestation",
        "Verify draft release assets",
        "Verify remote draft against authoritative Actions artifact",
    )
    _assert_order(
        publish,
        "Revalidate exact bytes and publish release",
        "Verify published release assets",
    )


def test_publish_release_workflow_serializes_dispatches_and_rechecks_latest():
    workflow = _read_repo_text(".github/workflows/publish-release.yml")
    publish = _job_block(workflow, "publish")
    writer = _step_block(publish, "Revalidate exact bytes and publish release")

    assert re.search(
        r"(?m)^concurrency:\n"
        r"  group: applicantscout-companion-release-publication\n"
        r"  cancel-in-progress: false\n"
        r"  queue: max$",
        workflow,
    )
    assert writer.count('repos/$env:GITHUB_REPOSITORY/releases/latest') == 2
    assert "$FinalLatestTag" in writer
    assert "changed immediately before publication" in writer
    _assert_order(
        writer,
        "$env:GH_TOKEN = $PublishToken",
        "$FinalLatestTag",
        "changed immediately before publication",
        "gh release edit $env:RELEASE_TAG",
    )


def test_publish_release_workflow_verifies_exact_tag_manifest_before_publish():
    workflow = _read_repo_text(".github/workflows/publish-release.yml")
    verify = _job_block(workflow, "verify")
    publish = _job_block(workflow, "publish")
    checkout = _step_block(verify, "Checkout companion")
    manifest_check = _step_block(verify, "Verify remote draft against authoritative Actions artifact")

    assert "persist-credentials: false" in checkout
    assert "gh release download $env:RELEASE_TAG" in manifest_check
    assert "release-artifact-manifest.ps1" in manifest_check
    assert "-Mode Verify" in manifest_check
    assert "-Purpose Release" in manifest_check
    assert "WorkflowRunId $env:RELEASE_RUN_ID" in manifest_check
    assert "Get-FileHash" in manifest_check
    assert ".digest" in manifest_check
    assert "Checkout companion" not in publish
    assert "pip install" not in publish
    assert "choco install" not in publish
    writer = _step_block(publish, "Revalidate exact bytes and publish release")
    _assert_order(
        writer,
        "-Repo $env:GITHUB_REPOSITORY",
        '-Repo "Antrakt92/ApplicantScout-Addon"',
        "releases/latest",
        "releases/tags/$env:RELEASE_TAG",
        "IsNullOrWhiteSpace($env:RELEASE_SETTINGS_READ_TOKEN)",
        "$env:GH_TOKEN = $env:RELEASE_SETTINGS_READ_TOKEN",
        "immutable-releases",
        "$env:GH_TOKEN = $PublishToken",
        "gh release edit $env:RELEASE_TAG",
    )
    assert "git/ref/tags/$Tag" in writer
    assert "git/tags/$Sha" in writer
    assert "/commits/$env:RELEASE_TAG" not in writer
    assert "/commits/$env:PAIRED_ADDON_TAG" not in writer
    assert ".digest" in writer
    assert ".size" in writer
    _assert_order(
        verify,
        "Verify draft release assets",
        "Verify remote draft against authoritative Actions artifact",
    )


def test_publish_release_workflow_restores_mutated_draft_copy_before_publication():
    workflow = _read_repo_text(".github/workflows/publish-release.yml")
    publish = _job_block(workflow, "publish")
    writer = _step_block(publish, "Revalidate exact bytes and publish release")
    published_check = _step_block(publish, "Verify published release assets")

    assert "$ExpectedReleaseTitle = [string]$Manifest.releaseCopy.title" in writer
    assert "$Manifest.releaseCopy.body.contentBase64" in writer
    assert "WriteAllBytes($ExpectedReleaseBodyPath" in writer
    assert "$Release.name -cne $ExpectedReleaseTitle" in writer
    assert "$Release.body -cne $ExpectedReleaseBody" in writer
    assert "will be restored atomically during publication" in writer
    assert re.search(
        r"(?ms)gh release edit \$env:RELEASE_TAG\s+`\s*"
        r"--repo \$env:GITHUB_REPOSITORY\s+`\s*"
        r"--title \$ExpectedReleaseTitle\s+`\s*"
        r"--notes-file \$ExpectedReleaseBodyPath\s+`\s*"
        r"--draft=false\s+`\s*"
        r"--prerelease=false",
        writer,
    )
    assert "$Release.name -ceq $ExpectedReleaseTitle" in published_check
    assert "$Release.body -ceq $ExpectedReleaseBody" in published_check
    assert "copy and assets match the authoritative Actions artifact" in published_check


def test_publish_release_workflow_pins_external_actions_to_commit_shas():
    workflow = _read_repo_text(".github/workflows/publish-release.yml")
    action_refs = _workflow_action_refs(workflow)

    assert Counter(action for action, _ in action_refs) == Counter(
        {
            "actions/checkout": 1,
            "actions/download-artifact": 1,
        }
    )
    for action, ref in action_refs:
        assert _SHA_REF_RE.fullmatch(ref), f"{action} must be pinned to a full commit SHA"


def test_release_workflow_pins_windows_tool_versions():
    workflow = _read_repo_text(".github/workflows/release.yml")
    install_args = _release_tool_install_args(workflow)

    assert set(install_args) == set(_RELEASE_TOOL_PACKAGES)
    for package, version in _RELEASE_TOOL_PACKAGES.items():
        assert len(install_args[package]) == 1
        assert re.search(
            rf"(?i)(?:^|\s)--version(?:=|\s+){re.escape(version)}(?:\s|$)",
            install_args[package][0],
        ), f"{package} must be installed with --version={version}"


def test_check_wrapper_runs_addon_contract_tests_after_lua_syntax():
    script = _read_repo_text("scripts/check.ps1")

    lua_idx = script.index('Write-Host "== Lua syntax =="')
    addon_pytest_idx = script.index('Write-Host "== Addon Python contract tests =="')
    addon_pytest_block = script[addon_pytest_idx:]

    assert lua_idx < addon_pytest_idx
    assert "Push-Location $AddonRoot" in addon_pytest_block
    assert "Select-String" in addon_pytest_block
    assert "$AddonContractArgs" in addon_pytest_block
    assert "& $Python -m pytest -q tests @AddonContractArgs" in addon_pytest_block
    assert "--companion-root" in addon_pytest_block
    assert "$RepoRoot" in addon_pytest_block
    assert "--lua51" in addon_pytest_block
    assert "$Lua51" in addon_pytest_block
    assert "finally" in addon_pytest_block
    assert "Pop-Location" in addon_pytest_block


def test_release_workflow_checks_out_paired_addon_tag_from_release_notes():
    workflow = _read_repo_text(".github/workflows/release.yml")
    build = _job_block(workflow, "build")

    resolve_idx = build.index("Resolve paired addon tag")
    wait_idx = build.index("Wait for paired addon tag")
    checkout_idx = build.index("Checkout addon")
    tag_wait_step = build[
        build.index("Wait for paired addon tag") : build.index("Checkout addon")
    ]

    assert resolve_idx < wait_idx < checkout_idx
    assert "RELEASE_NOTES.md" in workflow
    assert "paired-addon" in workflow
    assert ".\\scripts\\check-release-version.ps1 -Tag $env:GITHUB_REF_NAME" in workflow
    assert "-PairedAddonRefOutputPath $env:GITHUB_OUTPUT" in workflow
    assert 'git/ref/tags/$Ref' in tag_wait_step
    assert "Antrakt92/ApplicantScout-Addon" in tag_wait_step
    assert "$Deadline" in tag_wait_step
    assert "while ($true)" in tag_wait_step
    assert '"ref=v$($Match.Groups[1].Value)"' not in workflow
    assert "$env:GITHUB_OUTPUT" in workflow


def test_release_version_check_accepts_paired_addon_metadata(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    addon = _paired_addon_fixture(
        tmp_path,
        addon_version=_paired_addon_version(),
        companion_version=project_version,
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-PairedAddonRoot",
        str(addon),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_version_check_rejects_stale_paired_addon_release_train_copy(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    stale_version = _previous_patch_version(project_version)
    addon = _paired_addon_fixture(
        tmp_path,
        addon_version=_paired_addon_version(),
        companion_version=stale_version,
    )
    notes = repo / "RELEASE_NOTES.md"
    notes.write_text(
        notes.read_text(encoding="utf-8")
        .replace("Companion-only reliability patch", "Reliability patch", 1)
        .replace(" No addon update is required for this release.", "", 1),
        encoding="utf-8",
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-PairedAddonRoot",
        str(addon),
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "paired addon changelog.md top entry names companion" in output.lower()
    assert stale_version in output
    assert f"expected {project_version}" in output


def test_release_version_check_accepts_companion_only_patch_with_existing_addon(
    tmp_path,
):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    stale_version = _previous_patch_version(project_version)
    addon = _paired_addon_fixture(
        tmp_path,
        addon_version=_paired_addon_version(),
        companion_version=stale_version,
    )
    notes = repo / "RELEASE_NOTES.md"
    notes.write_text(
        notes.read_text(encoding="utf-8").replace(
            f"## {project_version} -",
            f"## {project_version} - companion-only",
            1,
        ),
        encoding="utf-8",
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-PairedAddonRoot",
        str(addon),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_version_check_rejects_companion_only_patch_with_newer_addon_copy(
    tmp_path,
):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    newer_version = _next_patch_version(project_version)
    addon = _paired_addon_fixture(
        tmp_path,
        addon_version=_paired_addon_version(),
        companion_version=newer_version,
    )
    notes = repo / "RELEASE_NOTES.md"
    notes.write_text(
        notes.read_text(encoding="utf-8").replace(
            f"## {project_version} -",
            f"## {project_version} - companion-only",
            1,
        ),
        encoding="utf-8",
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-PairedAddonRoot",
        str(addon),
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "newer than companion-only release" in output
    assert newer_version in output


def test_release_version_check_rejects_missing_paired_addon_release_train_copy(
    tmp_path,
):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    addon = _paired_addon_fixture(
        tmp_path,
        addon_version=_paired_addon_version(),
        companion_version=project_version,
    )
    _set_paired_addon_changelog(
        addon,
        "\n".join(
            [
                "# Changelog",
                "",
                f"## {_paired_addon_version()} - 21-May-2026 - Release train",
                "",
                "This paired addon release refreshes public copy.",
                "",
            ]
        ),
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-PairedAddonRoot",
        str(addon),
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "paired addon changelog.md top entry must name exactly one" in output.lower()


def test_release_version_check_rejects_multiple_paired_addon_release_train_versions(
    tmp_path,
):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    stale_version = _previous_patch_version(project_version)
    addon = _paired_addon_fixture(
        tmp_path,
        addon_version=_paired_addon_version(),
        companion_version=project_version,
    )
    _set_paired_addon_changelog(
        addon,
        "\n".join(
            [
                "# Changelog",
                "",
                f"## {_paired_addon_version()} - 21-May-2026 - Companion {project_version} release train",
                "",
                f"This release names ApplicantScout Companion `{project_version}`.",
                f"A stale note also names Companion `{stale_version}`.",
                "",
            ]
        ),
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-PairedAddonRoot",
        str(addon),
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "paired addon changelog.md top entry names multiple" in output.lower()
    assert project_version in output
    assert stale_version in output


def test_release_version_check_rejects_paired_addon_older_than_minimum(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    addon = _paired_addon_fixture(
        tmp_path,
        addon_version="0.3.2",
        companion_version=project_version,
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-PairedAddonRoot",
        str(addon),
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "paired addon version is 0.3.2" in output.lower()
    assert f"older than required {_paired_addon_version()}" in output


def test_release_version_check_accepts_newer_addon_than_release_minimum(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    addon = _paired_addon_fixture(
        tmp_path,
        addon_version=_next_patch_version(_paired_addon_version()),
        companion_version=project_version,
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-PairedAddonRoot",
        str(addon),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_version_check_accepts_published_paired_addon_assets(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    paired_addon_version = _paired_addon_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_tag=f"v{paired_addon_version}",
        release_json={
            "tagName": f"v{paired_addon_version}",
            "isDraft": False,
            "isPrerelease": False,
            "assets": [
                {"name": f"ApplicantScout-v{paired_addon_version}.zip"},
                {"name": "release.json"},
            ],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequirePublishedPairedAddonAssets",
        "-PublishedReleaseWaitSeconds",
        "0",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    gh_args = (tmp_path / "fake-gh-args.txt").read_text(encoding="utf-8")
    assert "Antrakt92/ApplicantScout-Addon" in gh_args
    assert f"v{paired_addon_version}" in gh_args


def test_release_version_check_rejects_unexpected_published_paired_addon_asset(
    tmp_path,
):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    paired_addon_version = _paired_addon_version()
    stale_addon_version = _previous_patch_version(paired_addon_version)
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_tag=f"v{paired_addon_version}",
        release_json={
            "tagName": f"v{paired_addon_version}",
            "isDraft": False,
            "isPrerelease": False,
            "assets": [
                {"name": f"ApplicantScout-v{paired_addon_version}.zip"},
                {"name": "release.json"},
                {"name": f"ApplicantScout-v{stale_addon_version}.zip"},
            ],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequirePublishedPairedAddonAssets",
        "-PublishedReleaseWaitSeconds",
        "0",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    output = re.sub(r"\s+", "", result.stdout + result.stderr)
    assert (
        f"unexpectedasset:ApplicantScout-v{stale_addon_version}.zip" in output
    )


def test_release_version_check_rejects_missing_published_paired_addon_asset(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    paired_addon_version = _paired_addon_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_tag=f"v{paired_addon_version}",
        release_json={
            "tagName": f"v{paired_addon_version}",
            "isDraft": False,
            "isPrerelease": False,
            "assets": [{"name": "release.json"}],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequirePublishedPairedAddonAssets",
        "-PublishedReleaseWaitSeconds",
        "0",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    assert f"missing asset: ApplicantScout-v{paired_addon_version}.zip" in (
        result.stdout + result.stderr
    )


def test_release_version_check_rejects_draft_published_paired_addon_release(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    paired_addon_version = _paired_addon_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_tag=f"v{paired_addon_version}",
        release_json={
            "tagName": f"v{paired_addon_version}",
            "isDraft": True,
            "isPrerelease": False,
            "assets": [
                {"name": f"ApplicantScout-v{paired_addon_version}.zip"},
                {"name": "release.json"},
            ],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequirePublishedPairedAddonAssets",
        "-PublishedReleaseWaitSeconds",
        "0",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    assert "is still draft" in (result.stdout + result.stderr)


def test_release_version_check_rejects_prerelease_paired_addon_release(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    paired_addon_version = _paired_addon_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_tag=f"v{paired_addon_version}",
        release_json={
            "tagName": f"v{paired_addon_version}",
            "isDraft": False,
            "isPrerelease": True,
            "assets": [
                {"name": f"ApplicantScout-v{paired_addon_version}.zip"},
                {"name": "release.json"},
            ],
        },
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequirePublishedPairedAddonAssets",
        "-PublishedReleaseWaitSeconds",
        "0",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    assert "marked prerelease" in (result.stdout + result.stderr)


def test_release_version_check_reports_paired_addon_gh_failure(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    paired_addon_version = _paired_addon_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_tag=f"v{paired_addon_version}",
        exit_code=1,
        stderr="release not found",
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequirePublishedPairedAddonAssets",
        "-PublishedReleaseWaitSeconds",
        "0",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "gh release view failed for Antrakt92/ApplicantScout-Addon" in output
    assert "release not found" in output


def test_release_version_check_rejects_malformed_paired_addon_release_json(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    paired_addon_version = _paired_addon_version()
    fake_gh = _fake_gh_release_view(
        tmp_path,
        expected_tag=f"v{paired_addon_version}",
        stdout_text="not-json",
    )

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-RequirePublishedPairedAddonAssets",
        "-PublishedReleaseWaitSeconds",
        "0",
        "-GitHubCliPath",
        str(fake_gh),
    )

    assert result.returncode != 0
    assert "malformed JSON" in (result.stdout + result.stderr)


def test_release_version_check_does_not_invoke_addon_release_script():
    script = _read_repo_text("scripts/check-release-version.ps1")

    assert "ApplicantScout-Addon\\scripts\\check-release-version.ps1" not in script
    assert "ApplicantScout-Addon/scripts/check-release-version.ps1" not in script


def test_check_workflow_runs_non_release_companion_and_addon_gates():
    workflow = _read_repo_text(".github/workflows/check.yml")
    check = _job_block(workflow, "check")
    package = _job_block(workflow, "package")

    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "paired_addon_ref:" in workflow
    assert "tags:" not in workflow
    assert re.search(r"(?m)^    runs-on: windows-2022\s*$", check)
    assert "contents: read" in workflow
    assert "contents: write" not in workflow
    assert "path: ApplicantScout-Companion" in workflow
    assert "repository: Antrakt92/ApplicantScout-Addon" in workflow
    addon_checkout = _step_block(check, "Checkout addon")
    assert "ref: ${{ github.event.inputs.paired_addon_ref || 'main' }}" in (
        addon_checkout
    )
    assert "default: main" in workflow
    assert "type: string" in workflow
    assert "path: ApplicantScout-Addon" in workflow
    assert "APPLICANT_SCOUT_VISUAL_BASELINE" not in workflow
    assert ".\\scripts\\check.ps1 -AddonRoot ..\\ApplicantScout-Addon -VisualMode Smoke" in workflow
    assert "gh release" not in workflow
    assert "build-windows.ps1" not in check
    assert re.search(r"(?m)^    needs: check\s*$", package)
    assert re.search(r"(?m)^    runs-on: windows-2022\s*$", package)
    assert "repository: Antrakt92/ApplicantScout-Addon" not in package
    assert ".\\scripts\\build-windows.ps1 -SkipChecks" in package
    assert "check-release-version.ps1 -Tag \"v$Version\" -RequireAssets" in package
    assert "APSCOUT_SIGNING_" not in package
    assert "upload-artifact" not in package
    assert "choco install innosetup --version=6.7.1 -y --no-progress" in package
    _assert_order(
        package,
        "Install release dependencies",
        "Install Windows packaging tools",
        "Build unsigned Windows artifacts",
        "Validate Windows artifacts",
    )


def test_check_workflow_pins_external_actions_to_commit_shas():
    workflow = _read_repo_text(".github/workflows/check.yml")
    action_refs = _workflow_action_refs(workflow)

    assert Counter(action for action, _ in action_refs) == Counter(
        {
            "actions/checkout": 3,
            "actions/setup-python": 2,
        }
    )
    for action, ref in action_refs:
        assert _SHA_REF_RE.fullmatch(ref), f"{action} must be pinned to a full commit SHA"

    install_args = _release_tool_install_args(workflow)
    assert install_args["innosetup"] == [" --version=6.7.1 -y --no-progress"]


def test_windows_vs2026_canary_runs_both_package_paths_without_publishing():
    workflow = _read_repo_text(".github/workflows/windows-vs2026-canary.yml")
    triggers = workflow.partition("permissions:")[0]
    canary = _job_block(workflow, "canary")

    assert "workflow_dispatch:" in triggers
    assert "paired_addon_ref:" in triggers
    assert "default: main" in triggers
    assert "type: string" in triggers
    assert "schedule:" in triggers
    assert "cron: '17 5 * * 1'" in triggers
    assert "push:" not in triggers
    assert "pull_request:" not in triggers
    assert "tags:" not in triggers
    assert "\npermissions:\n  contents: read\n\nconcurrency:" in workflow
    assert "contents: write" not in workflow
    assert "cancel-in-progress: true" in workflow
    assert re.search(r"(?m)^    runs-on: windows-2025-vs2026\s*$", canary)
    assert re.search(r"(?m)^    timeout-minutes: 30\s*$", canary)
    assert "continue-on-error" not in canary

    companion_checkout = _step_block(canary, "Checkout companion")
    addon_checkout = _step_block(canary, "Checkout addon")
    assert "persist-credentials: false" in companion_checkout
    assert "persist-credentials: false" in addon_checkout
    assert "repository: Antrakt92/ApplicantScout-Addon" in addon_checkout
    assert "ref: ${{ github.event.inputs.paired_addon_ref || 'main' }}" in (
        addon_checkout
    )
    assert "path: ApplicantScout-Companion" in companion_checkout
    assert "path: ApplicantScout-Addon" in addon_checkout
    assert "python-version: '3.13'" in canary
    assert ".\\.venv\\Scripts\\python -m pip install -r constraints-release.txt" in canary
    assert ".\\.venv\\Scripts\\python -m pip install -e '.[dev]' -c constraints-release.txt" in canary
    assert "choco install lua51 --version=5.1.5 -y --no-progress" in canary
    assert "choco install innosetup --version=6.7.1 -y --no-progress" in canary
    check_step = _step_block(canary, "Check companion and addon contracts")
    addon_package_step = _step_block(canary, "Build addon development package")
    companion_build_step = _step_block(canary, "Build unsigned companion artifacts")
    companion_validate_step = _step_block(canary, "Validate companion artifacts")
    assert (
        ".\\scripts\\check.ps1 -AddonRoot ..\\ApplicantScout-Addon -VisualMode Smoke"
        in check_step
    )
    assert "working-directory: ApplicantScout-Companion" in check_step
    assert "ApplicantScout-Addon\\scripts\\package-addon.ps1" in addon_package_step
    assert "working-directory: ApplicantScout-Companion" in addon_package_step
    assert ".\\scripts\\build-windows.ps1 -SkipChecks" in companion_build_step
    assert "working-directory: ApplicantScout-Companion" in companion_build_step
    assert (
        "check-release-version.ps1 -Tag \"v$Version\" -RequireAssets"
        in companion_validate_step
    )
    assert "working-directory: ApplicantScout-Companion" in companion_validate_step
    _assert_order(
        canary,
        "Check companion and addon contracts",
        "Build addon development package",
        "Build unsigned companion artifacts",
        "Validate companion artifacts",
    )

    action_refs = _workflow_action_refs(workflow)
    assert Counter(action for action, _ in action_refs) == Counter(
        {"actions/checkout": 2, "actions/setup-python": 1}
    )
    for action, ref in action_refs:
        assert _SHA_REF_RE.fullmatch(ref), f"{action} must be pinned to a full commit SHA"
    assert "${{ secrets." not in workflow
    assert "GH_TOKEN" not in workflow
    assert "gh release" not in workflow
    assert "gh api" not in workflow
    assert "upload-artifact" not in workflow
    assert "download-artifact" not in workflow
    assert "APSCOUT_SIGNING_" not in workflow
    assert "release-artifact-manifest.ps1" not in workflow

    for stable_workflow_path in (
        ".github/workflows/check.yml",
        ".github/workflows/release.yml",
        ".github/workflows/publish-release.yml",
    ):
        stable_workflow = _read_repo_text(stable_workflow_path)
        assert "windows-2025-vs2026" not in stable_workflow
        assert "runs-on: windows-2022" in stable_workflow


def test_release_workflow_uploads_exact_updater_assets_as_draft_first():
    workflow = _read_repo_text(".github/workflows/release.yml")

    assert "ApplicantScoutCompanionSetup-$env:TAG_VERSION.exe" in workflow
    assert "ApplicantScoutCompanionSetup-$env:TAG_VERSION.exe.sha256" in workflow
    assert "ApplicantScoutCompanion-$env:TAG_VERSION-portable.zip" in workflow
    assert "ApplicantScoutCompanion-$env:TAG_VERSION-release-manifest.json" in workflow
    assert "companion-release-assets" in workflow
    assert "--draft" in workflow
    assert "--verify-tag" in workflow
    assert "-RequireDraftReleaseAssets" in workflow
    assert "draft=false" not in workflow
    assert "gh release edit" not in workflow


def test_release_workflow_extracts_top_release_notes_entry_only():
    workflow = _read_repo_text(".github/workflows/release.yml")

    assert "release-body.md" in workflow
    assert "[regex]::Escape($TagVersion)" in workflow
    assert "RELEASE_NOTES.md" in workflow
    assert "(?=^##\\s+\\d+\\.\\d+\\.\\d+\\s+-\\s+|\\z)" in workflow


def test_public_docs_do_not_reference_private_audit_backlog():
    docs_index = _read_repo_text("docs/README.md")
    release_checklist = _read_repo_text("RELEASE_CHECKLIST.md")
    screenshot_tests = _read_repo_text("tests/test_screenshot.py")

    assert "../AUDIT.md" not in docs_index
    assert "..\\WOW\\" not in release_checklist
    assert "Documents\\GitHub" not in release_checklist
    assert "AUDIT.md T2-" not in screenshot_tests


def _git_check_ignore(path: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "check-ignore",
            "--no-index",
            "--quiet",
            "--",
            path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    debug = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "check-ignore",
            "--no-index",
            "-nv",
            "--",
            path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    raise AssertionError(
        f"git check-ignore failed for {path!r}: {result.stderr or debug.stderr or debug.stdout}"
    )


def test_companion_gitignore_covers_private_local_artifacts():
    for path in (
        ".env",
        ".env.local",
        ".env.production",
        "config.env",
        "config/config.env",
        "window.json",
        "config/window.json",
        "token.json",
        "character-cache.json",
        "cache/token.json",
        "cache/character-cache.json",
        "cache/raiderio-local/foo.payload.bin",
        "logs/applicant-scout.log",
        "logs/applicant-scout.log.1",
        "Screenshots/WoWScrnShot_052826_123456.jpg",
        "screenshots/manual-decode.png",
        "AUDIT.md",
        "PLAN.md",
        "NOTES.md",
        "TODO.md",
        "CLAUDE.md",
        "research.private.md",
        "research.private/file.txt",
        "docs/superpowers/release-plan.md",
    ):
        assert _git_check_ignore(path), f"{path} should be ignored"


def test_companion_gitignore_does_not_hide_public_release_or_doc_inputs():
    for path in (
        ".env.example",
        ".env.sample",
        "config.env.example",
        "README.md",
        "RELEASE_CHECKLIST.md",
        "RELEASE_NOTES.md",
        "constraints-release.txt",
        "docs/README.md",
        "docs/visual/future-public-screenshot.jpg",
        "src/applicant_scout/cache/new_module.py",
        "tests/fixtures/token.json",
        "packaging/inno/ApplicantScoutCompanion.iss",
    ):
        assert not _git_check_ignore(path), f"{path} should not be ignored"


def test_readme_documents_current_wire_support():
    readme = _read_repo_text("README.md")

    assert "wire payloads through v9" in readme
    assert "wire payloads through v5" not in readme


def test_readme_documents_snapshot_action_requires_enabled_addon():
    readme = _read_repo_text("README.md")

    assert (
        "/apscout shotnow        force snapshot now while enabled (debug / manual sync)"
        in readme
    )
    assert re.search(
        r"keep ApplicantScout enabled and click \*\*Snapshot\*\*, or run\s+"
        r"`/apscout shotnow`",
        readme,
    )
    assert "/apscout shotnow        force snapshot now\n" not in readme


def test_readme_discloses_optional_raiderio_local_reads_and_cache():
    readme = _read_repo_text("README.md")

    _assert_copy_contains(readme, "_retail_\\Interface\\AddOns\\RaiderIO\\db")
    _assert_copy_contains(readme, "%LOCALAPPDATA%\\applicant-scout\\cache\\raiderio-local")
    _assert_copy_contains(readme, "reads optional local RaiderIO data")


def test_readme_documents_support_output_redaction():
    readme = _read_repo_text("README.md")

    for sensitive_surface in (
        "/apscout status",
        "/apscout taintcheck",
        "companion logs",
        "QR screenshots",
        "manual decode output",
        "config.env",
        "token.json",
        "character-cache.json",
    ):
        _assert_copy_contains(readme, sensitive_surface)

    for private_detail in (
        "WCL Client ID/Secret",
        "OAuth access token",
        "character names",
        "realm names",
        "listing titles/comments",
        "screenshots folder paths",
    ):
        _assert_copy_contains(readme, private_detail)


def test_readme_documents_residual_qr_screenshot_cleanup_path():
    readme = _read_repo_text("README.md")

    for phrase in (
        "QR screenshots may remain",
        "companion is absent, interrupted",
        "synced/shared before cleanup",
        "applicant-scout cleanup-screenshots",
        "--delete",
    ):
        _assert_copy_contains(readme, phrase)


def test_release_version_check_rejects_stale_constraints_header(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    stale_version = _previous_patch_version(project_version)
    constraints = repo / "constraints-release.txt"
    constraints.write_text(
        constraints.read_text(encoding="utf-8").replace(project_version, stale_version, 1),
        encoding="utf-8",
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}")

    assert result.returncode != 0
    assert f"constraints-release.txt header is {stale_version}" in (
        result.stdout + result.stderr
    )


def test_release_version_check_rejects_stale_release_notes_asset_names(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    stale_version = _previous_patch_version(project_version)
    notes = repo / "RELEASE_NOTES.md"
    notes.write_text(
        notes.read_text(encoding="utf-8")
        .replace(
            f"ApplicantScoutCompanionSetup-{project_version}.exe",
            f"ApplicantScoutCompanionSetup-{stale_version}.exe",
            1,
        )
        .replace(
            f"ApplicantScoutCompanionSetup-{project_version}.exe.sha256",
            f"ApplicantScoutCompanionSetup-{stale_version}.exe.sha256",
            1,
        ),
        encoding="utf-8",
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}")

    assert result.returncode != 0
    assert "RELEASE_NOTES.md top entry does not mention expected installer asset" in (
        result.stdout + result.stderr
    )


def test_release_version_check_writes_paired_addon_ref_output(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    output_path = repo / "paired-addon-output.txt"

    result = _run_release_check(
        repo,
        "-Tag",
        f"v{project_version}",
        "-PairedAddonRefOutputPath",
        str(output_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert output_path.read_text(encoding="utf-8").strip() == (
        f"ref=v{_paired_addon_version()}"
    )


def test_release_version_check_rejects_missing_top_paired_addon_copy(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    paired_line = f"- Requires the ApplicantScout WoW addon `{_paired_addon_version()}`."
    notes = repo / "RELEASE_NOTES.md"
    notes.write_text(
        notes.read_text(encoding="utf-8").replace(paired_line, "", 1),
        encoding="utf-8",
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}")

    assert result.returncode != 0
    assert "top entry does not mention the paired ApplicantScout addon version" in (
        result.stdout + result.stderr
    )


def test_release_version_check_rejects_malformed_top_paired_addon_version(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    notes = repo / "RELEASE_NOTES.md"
    notes.write_text(
        notes.read_text(encoding="utf-8").replace(
            f"ApplicantScout WoW addon `{_paired_addon_version()}`",
            "ApplicantScout WoW addon `0.3`",
            1,
        ),
        encoding="utf-8",
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}")

    assert result.returncode != 0
    assert "paired ApplicantScout addon version is malformed" in (
        result.stdout + result.stderr
    )


def test_release_build_rejects_non_exact_constraint_lines():
    build_script = _read_repo_text("scripts/build-windows.ps1")

    assert "Malformed release constraint" in build_script
    malformed_branch = re.search(
        r"if match is None:(?P<body>.*?)(?=    name, expected = match\.groups\(\))",
        build_script,
        re.S,
    )
    assert malformed_branch is not None
    assert "malformed.append(line)" in malformed_branch.group("body")


def test_release_version_check_require_assets_validates_checksum_digest(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    dist = repo / "dist"
    installer_name, _, _ = _write_release_assets(repo)
    checksum_name = f"{installer_name}.sha256"

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode == 0, result.stdout + result.stderr

    (dist / checksum_name).write_text(
        f"{hashlib.sha256(b'other').hexdigest()}  {installer_name}\n",
        encoding="ascii",
    )
    mismatch = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert mismatch.returncode != 0
    assert "checksum mismatch" in (mismatch.stdout + mismatch.stderr).lower()


def test_release_version_check_require_assets_rejects_checksum_wrong_filename(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    dist = repo / "dist"
    installer_name, _, _ = _write_release_assets(repo)
    checksum_name = f"{installer_name}.sha256"
    digest = hashlib.sha256(b"setup-bytes").hexdigest()
    (dist / checksum_name).write_text(
        f"{digest}  Other.exe\n",
        encoding="ascii",
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "checksum filename" in (result.stdout + result.stderr).lower()


def test_release_version_check_require_assets_rejects_malformed_checksum(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    dist = repo / "dist"
    installer_name, _, _ = _write_release_assets(repo)
    checksum_name = f"{installer_name}.sha256"
    (dist / checksum_name).write_text(
        f"not-a-sha  {installer_name}\n",
        encoding="ascii",
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "malformed checksum" in (result.stdout + result.stderr).lower()


def test_release_version_check_require_assets_validates_portable_zip_contract(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    _write_release_assets(repo)

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_version_check_require_assets_rejects_corrupt_portable_zip(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    _, _, portable_name = _write_release_assets(repo)
    (repo / "dist" / portable_name).write_bytes(b"not a zip")

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "portable zip" in output
    assert "open" in output or "invalid" in output


def test_release_version_check_require_assets_rejects_wrong_portable_root(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    _, _, portable_name = _write_release_assets(repo)
    _write_valid_portable_zip(repo / "dist" / portable_name, root="Other")

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "applicantscout/" in output
    assert "other/" in output


def test_release_version_check_require_assets_rejects_content_only_portable_zip(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    _, _, portable_name = _write_release_assets(repo)
    with zipfile.ZipFile(repo / "dist" / portable_name, "w") as zf:
        zf.writestr("ApplicantScout.exe", b"exe")
        zf.writestr("LICENSE", b"license")
        zf.writestr("THIRD-PARTY-NOTICES.md", b"notices")
        zf.writestr("RELEASE_NOTES.md", b"notes")

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "applicantscout/" in (result.stdout + result.stderr).lower()


def test_release_version_check_require_assets_rejects_missing_portable_required_entry(
    tmp_path,
):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    _, _, portable_name = _write_release_assets(repo)
    _write_valid_portable_zip(
        repo / "dist" / portable_name,
        omit={"ApplicantScout/RELEASE_NOTES.md"},
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "ApplicantScout/RELEASE_NOTES.md" in result.stdout + result.stderr


def test_release_version_check_require_assets_rejects_portable_zip_traversal_entry(
    tmp_path,
):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    _, _, portable_name = _write_release_assets(repo)
    _write_valid_portable_zip(
        repo / "dist" / portable_name,
        extra_entries={"ApplicantScout/../evil.txt": b"bad"},
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "unsafe portable zip entry" in (result.stdout + result.stderr).lower()


def test_release_version_check_require_assets_rejects_portable_zip_without_license_payload(
    tmp_path,
):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    _, _, portable_name = _write_release_assets(repo)
    _write_valid_portable_zip(
        repo / "dist" / portable_name,
        omit={"ApplicantScout/licenses/PyQt6/LICENSE.txt"},
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "ApplicantScout/licenses/" in result.stdout + result.stderr


def test_release_version_check_rejects_missing_license_placeholder(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    _, _, portable_name = _write_release_assets(repo)
    _write_valid_portable_zip(
        repo / "dist" / portable_name,
        extra_entries={
            "ApplicantScout/LiCeNsEs/NoLicenseWheel/no-license-file-found.TxT": (
                b"No license file found."
            )
        },
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "missing-license placeholder" in (result.stdout + result.stderr).lower()


def test_release_version_check_rejects_empty_dependency_license(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    _, _, portable_name = _write_release_assets(repo)
    _write_valid_portable_zip(
        repo / "dist" / portable_name,
        omit={"ApplicantScout/licenses/PyQt6/LICENSE.txt"},
        extra_entries={"ApplicantScout/licenses/Empty/LICENSE.txt": b""},
    )

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "empty dependency license payload" in (result.stdout + result.stderr).lower()


def test_start_batch_explains_missing_local_environment():
    script = _read_repo_text("start.bat")

    assert 'if not exist ".venv\\Scripts\\applicant-scout.exe"' in script
    assert "python -m venv .venv" in script
    assert ".venv\\Scripts\\pip install -e .[dev] -c constraints-release.txt" in script
    assert ".venv\\Scripts\\applicant-scout.exe" in script


def test_readme_wcl_setup_includes_client_creation_screenshot():
    readme = _read_repo_text("README.md")
    screenshot = REPO_ROOT / "docs" / "images" / "wcl-create-client.jpg"

    assert screenshot.is_file()
    assert "![Warcraft Logs Create Client form](docs/images/wcl-create-client.jpg)" in readme
    assert "Redirect URL: exactly `http://localhost`" in readme
    assert "Public Client: leave unchecked" in readme


def test_readme_documents_verified_self_update_flow():
    readme = _read_repo_text("README.md")

    assert "does not self-replace" not in readme
    assert "checks for updates hourly" in readme
    assert "blue download button" in readme
    assert ".exe.sha256" in readme
    assert re.search(
        r"The `\.sha256` sidecar verifies\s+file integrity, not publisher identity",
        readme,
    )
    _assert_copy_contains(readme, "does not prove publisher identity")
    assert "%LOCALAPPDATA%\\Programs\\ApplicantScout Companion" in readme
    _assert_copy_contains(
        readme,
        "downloads the installer and verifies its `.sha256` checksum",
    )
    assert "unsigned builds must be installed manually" not in readme
    assert "Start and stop with WoW" in readme


def test_release_checklist_uses_policy_placeholders_not_stale_versions():
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "<companion version>" in checklist
    assert "<paired addon version>" in checklist
    assert "latest published stable companion" in checklist
    assert "previous stable or explicitly chosen baseline" not in checklist
    assert "checksum-gated in-app updater" in checklist
    assert "Publish release" in checklist
    assert "release_run_id" in checklist
    assert "smoke_tested_installer_sha256" in checklist
    assert "release-manifest.json" in checklist
    assert "authoritative manifest" in checklist
    assert "Re-run failed jobs" in checklist
    assert "Do not rerun all jobs after a draft exists" in " ".join(checklist.split())
    assert "recover forward with a" in checklist
    assert "new PATCH release" in checklist
    assert "verified draft" in checklist.lower()
    _assert_copy_contains(
        checklist.lower(),
        "normal stable updater feed ignores draft releases",
    )
    assert "does not make an unsigned installer self-update capable" not in checklist
    _assert_copy_contains(
        checklist,
        "does not exercise the normal GitHub latest-release feed while the release is still draft",
    )
    assert not re.search(
        r"Smoke-test from an installed `\d+\.\d+\.\d+` companion:.*relaunch `\d+\.\d+\.\d+`",
        checklist,
        re.S,
    )


def test_readme_points_to_packaged_addon_zip_not_source_archive():
    readme = _read_repo_text("README.md")

    assert "ApplicantScout-*.zip" in readme
    assert "https://github.com/Antrakt92/ApplicantScout-Addon/releases/latest" in readme
    assert "_retail_\\Interface\\AddOns\\ApplicantScout\\ApplicantScout.toc" in readme
    assert "automatic source-code ZIP" in readme
    assert "wrong folder name for WoW" in readme
    assert "ApplicantScout-<version>.zip" in readme
    assert "separate from the companion portable ZIP" in readme
