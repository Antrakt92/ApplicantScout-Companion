from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
import shutil
import subprocess
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
                f"    [Console]::Error.WriteLine({stderr!r})",
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
    assert 'Invoke-NativeChecked -Label "Python tests"' in script
    assert 'Invoke-NativeChecked -Label "Ruff"' in script
    assert 'Invoke-NativeChecked -Label "Pyright"' in script
    assert 'Invoke-NativeChecked -Label "Lua syntax"' in script


def test_check_script_does_not_accept_generic_luac_for_wow_syntax():
    script = _read_repo_text("scripts/check.ps1")

    assert "Get-Command luac5.1" in script
    assert "Get-Command luac " not in script


def test_artifact_name_contract_stays_aligned():
    build_script = _read_repo_text("scripts/build-windows.ps1")
    inno_script = _read_repo_text("packaging/inno/ApplicantScoutCompanion.iss")
    updater = _read_repo_text("src/applicant_scout/updater.py")

    assert "ApplicantScoutCompanion-$Version-portable.zip" in build_script
    assert "ApplicantScoutCompanionSetup-$Version.exe.sha256" in build_script
    assert "Get-FileHash" in build_script
    assert "ApplicantScoutCompanionSetup-{#MyAppVersion}" in inno_script
    assert "ApplicantScoutCompanionSetup-" in updater
    assert "portable.zip" not in updater
    assert "checksum_url" in updater


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

    build_script = _read_repo_text("scripts/build-windows.ps1")

    assert "Copy-ReleaseTextArtifacts" in build_script
    assert "Copy-DependencyLicenseArtifacts" in build_script
    assert "collect_dependency_licenses.py" in build_script
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


def test_release_build_refuses_dirty_release_inputs_by_default():
    build_script = _read_repo_text("scripts/build-windows.ps1")

    assert "Assert-CleanReleaseInputs" in build_script
    assert "AllowDirtyReleaseInputs" in build_script
    assert "Refusing to build release artifacts from dirty release inputs" in build_script
    assert "RELEASE_NOTES.md" in build_script
    assert "pyproject.toml" in build_script


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


def test_release_readiness_test_name_is_not_tied_to_current_version():
    source = _read_repo_text("tests/test_packaging_contract.py")

    assert re.search(r"def test_release_version_metadata_is_ready_for_\d+", source) is None


def test_release_version_check_script_documents_asset_contract():
    script = _read_repo_text("scripts/check-release-version.ps1")
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "ApplicantScoutCompanionSetup-$TagVersion.exe" in script
    assert "$InstallerName.sha256" in script
    assert "ApplicantScoutCompanion-$TagVersion-portable.zip" in script
    assert "RequireAssets" in script
    assert "constraints-release.txt" in script
    assert "Release constraints header" in script
    assert ".\\scripts\\check-release-version.ps1 -Tag v<companion version> -RequireAssets" in checklist


def test_release_workflow_runs_existing_gates_before_publishing():
    workflow = _read_repo_text(".github/workflows/release.yml")
    release = _job_block(workflow, "release")

    assert "tags:" in workflow
    assert "'v*'" in workflow
    assert "github.event.created == true" in workflow
    assert "github.event.forced == false" in workflow
    assert "github.event.deleted == false" in workflow
    assert re.search(r"(?m)^    runs-on: windows-2022\s*$", release)
    assert "contents: write" in workflow
    assert "python-version: '3.13'" in workflow
    assert "constraints-release.txt" in workflow
    assert ".\\.venv\\Scripts\\python -m pip install -r constraints-release.txt" in workflow
    assert "APPLICANT_SCOUT_VISUAL_BASELINE: smoke" in workflow
    assert "choco install lua51 --version=5.1.5" in workflow
    assert "choco install innosetup --version=6.7.1" in workflow
    assert "repository: Antrakt92/ApplicantScout-Addon" in workflow
    assert "id: paired-addon" in workflow
    assert "-PairedAddonRefOutputPath $env:GITHUB_OUTPUT" in workflow
    assert "ref: ${{ steps.paired-addon.outputs.ref }}" in workflow
    assert "Validate paired addon metadata" in workflow
    assert "-PairedAddonRoot ..\\ApplicantScout-Addon" in workflow
    paired_version_idx = workflow.index("-PairedAddonRoot ..\\ApplicantScout-Addon")
    check_idx = workflow.index(".\\scripts\\check.ps1 -AddonRoot")
    version_idx = workflow.index(".\\scripts\\check-release-version.ps1 -Tag", check_idx)
    build_idx = workflow.index(".\\scripts\\build-windows.ps1 -SkipChecks")
    assets_idx = workflow.index(".\\scripts\\check-release-version.ps1 -Tag $env:GITHUB_REF_NAME -RequireAssets")
    assert "-RequirePublishedPairedAddonAssets" not in workflow
    release_idx = workflow.index("gh release create")
    publish_idx = workflow.index("gh release edit")

    assert (
        paired_version_idx
        < check_idx
        < version_idx
        < build_idx
        < assets_idx
        < release_idx
        < publish_idx
    )


def test_release_workflow_refuses_existing_release_before_build_or_create():
    workflow = _read_repo_text(".github/workflows/release.yml")

    refuse_idx = workflow.index("Refuse existing release")
    build_idx = workflow.index(".\\scripts\\build-windows.ps1 -SkipChecks")
    release_idx = workflow.index("gh release create")
    publish_idx = workflow.index("gh release edit")

    assert "gh release view $env:GITHUB_REF_NAME" in workflow
    assert "--repo $env:GITHUB_REPOSITORY" in workflow
    assert "already exists; refusing" in workflow
    assert refuse_idx < build_idx < release_idx < publish_idx


def test_release_workflow_pins_external_actions_to_commit_shas():
    workflow = _read_repo_text(".github/workflows/release.yml")
    action_refs = _workflow_action_refs(workflow)

    assert Counter(action for action, _ in action_refs) == Counter(
        {
            "actions/checkout": 2,
            "actions/setup-python": 1,
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
    assert "& $Python -m pytest -q tests" in addon_pytest_block
    assert "finally" in addon_pytest_block
    assert "Pop-Location" in addon_pytest_block


def test_release_workflow_checks_out_paired_addon_tag_from_release_notes():
    workflow = _read_repo_text(".github/workflows/release.yml")
    release = _job_block(workflow, "release")

    resolve_idx = workflow.index("Resolve paired addon tag")
    wait_idx = workflow.index("Wait for paired addon tag")
    checkout_idx = workflow.index("Checkout addon")
    tag_wait_step = release[
        release.index("Wait for paired addon tag") : release.index("Checkout addon")
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

    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "tags:" not in workflow
    assert re.search(r"(?m)^    runs-on: windows-2022\s*$", check)
    assert "contents: read" in workflow
    assert "contents: write" not in workflow
    assert "path: ApplicantScout-Companion" in workflow
    assert "repository: Antrakt92/ApplicantScout-Addon" in workflow
    assert "path: ApplicantScout-Addon" in workflow
    assert "APPLICANT_SCOUT_VISUAL_BASELINE: smoke" in workflow
    assert ".\\scripts\\check.ps1 -AddonRoot ..\\ApplicantScout-Addon" in workflow
    assert "gh release" not in workflow
    assert "build-windows.ps1" not in workflow


def test_check_workflow_pins_external_actions_to_commit_shas():
    workflow = _read_repo_text(".github/workflows/check.yml")
    action_refs = _workflow_action_refs(workflow)

    assert Counter(action for action, _ in action_refs) == Counter(
        {
            "actions/checkout": 2,
            "actions/setup-python": 1,
        }
    )
    for action, ref in action_refs:
        assert _SHA_REF_RE.fullmatch(ref), f"{action} must be pinned to a full commit SHA"


def test_release_workflow_uploads_exact_updater_assets_as_draft_first():
    workflow = _read_repo_text(".github/workflows/release.yml")

    assert "ApplicantScoutCompanionSetup-$TagVersion.exe" in workflow
    assert "ApplicantScoutCompanionSetup-$TagVersion.exe.sha256" in workflow
    assert "ApplicantScoutCompanion-$TagVersion-portable.zip" in workflow
    assert "--draft" in workflow
    assert "--verify-tag" in workflow
    assert "isDraft" in workflow
    assert "isPrerelease" in workflow
    assert "assets" in workflow
    assert "draft=false" in workflow


def test_release_workflow_extracts_top_release_notes_entry_only():
    workflow = _read_repo_text(".github/workflows/release.yml")

    assert "release-body.md" in workflow
    assert "[regex]::Escape($TagVersion)" in workflow
    assert "RELEASE_NOTES.md" in workflow
    assert "(?=^##\\s+\\d+\\.\\d+\\.\\d+\\s+-\\s+|\\z)" in workflow


def test_public_docs_do_not_reference_private_audit_backlog():
    docs_index = _read_repo_text("docs/README.md")
    screenshot_tests = _read_repo_text("tests/test_screenshot.py")

    assert "../AUDIT.md" not in docs_index
    assert "AUDIT.md T2-" not in screenshot_tests


def test_readme_documents_current_wire_support():
    readme = _read_repo_text("README.md")

    assert "wire payloads through v7" in readme
    assert "wire payloads through v5" not in readme


def test_readme_documents_shotnow_requires_enabled_addon():
    readme = _read_repo_text("README.md")

    assert "/apscout shotnow        force a snapshot now while enabled" in readme
    assert "keep ApplicantScout enabled and run `/apscout shotnow`" in readme
    assert "/apscout shotnow        force a snapshot now\n" not in readme


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
    (dist / portable_name).write_bytes(b"zip")

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
    dist.mkdir()
    installer_name = f"ApplicantScoutCompanionSetup-{project_version}.exe"
    checksum_name = f"{installer_name}.sha256"
    portable_name = f"ApplicantScoutCompanion-{project_version}-portable.zip"
    installer = dist / installer_name
    installer.write_bytes(b"setup-bytes")
    digest = hashlib.sha256(b"setup-bytes").hexdigest()
    (dist / checksum_name).write_text(
        f"{digest}  Other.exe\n",
        encoding="ascii",
    )
    (dist / portable_name).write_bytes(b"zip")

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "checksum filename" in (result.stdout + result.stderr).lower()


def test_release_version_check_require_assets_rejects_malformed_checksum(tmp_path):
    repo = _copy_release_check_fixture(tmp_path)
    project_version = _project_version()
    dist = repo / "dist"
    dist.mkdir()
    installer_name = f"ApplicantScoutCompanionSetup-{project_version}.exe"
    checksum_name = f"{installer_name}.sha256"
    portable_name = f"ApplicantScoutCompanion-{project_version}-portable.zip"
    (dist / installer_name).write_bytes(b"setup-bytes")
    (dist / checksum_name).write_text(
        f"not-a-sha  {installer_name}\n",
        encoding="ascii",
    )
    (dist / portable_name).write_bytes(b"zip")

    result = _run_release_check(repo, "-Tag", f"v{project_version}", "-RequireAssets")

    assert result.returncode != 0
    assert "malformed checksum" in (result.stdout + result.stderr).lower()


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
    assert "trusted signed installer" in readme
    assert "%LOCALAPPDATA%\\Programs\\ApplicantScout Companion" in readme
    assert "downloads the installer, verifies its `.sha256` checksum" not in readme
    assert "Start and stop with WoW" in readme


def test_release_checklist_uses_policy_placeholders_not_stale_versions():
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "<companion version>" in checklist
    assert "<paired addon version>" in checklist
    assert "previous stable or explicitly chosen baseline" in checklist
    assert "trusted signed installer" in checklist
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
