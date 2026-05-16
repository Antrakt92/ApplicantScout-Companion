from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_repo_text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


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


def test_release_version_metadata_is_ready_for_024():
    pyproject = _read_repo_text("pyproject.toml")
    runtime = _read_repo_text("src/applicant_scout/__init__.py")
    notes = _read_repo_text("RELEASE_NOTES.md")
    readme = _read_repo_text("README.md")

    assert 'version = "0.2.4"' in pyproject
    assert '__version__ = "0.2.4"' in runtime
    assert notes.startswith("# ApplicantScout Companion Release Notes\n\n## 0.2.4 - ")
    assert "ApplicantScout WoW addon `0.1.6`" in notes
    assert "ApplicantScout addon `0.1.6`" in _read_repo_text("RELEASE_CHECKLIST.md")
    assert "ApplicantScout Companion `0.2.4`" not in readme
    assert "https://github.com/Antrakt92/ApplicantScout-Addon/releases/latest" in readme
    assert "ApplicantScout-0.1.0.zip" not in readme
    assert "releases/tag/v0.1.0" not in readme
    assert "releases/tag/v0.1.2" not in readme


def test_release_version_check_script_documents_asset_contract():
    script = _read_repo_text("scripts/check-release-version.ps1")
    checklist = _read_repo_text("RELEASE_CHECKLIST.md")

    assert "ApplicantScoutCompanionSetup-$TagVersion.exe" in script
    assert "$InstallerName.sha256" in script
    assert "ApplicantScoutCompanion-$TagVersion-portable.zip" in script
    assert "RequireAssets" in script
    assert "constraints-release.txt" in script
    assert "Release constraints header" in script
    assert ".\\scripts\\check-release-version.ps1 -Tag v0.2.4 -RequireAssets" in checklist


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
    assert "%LOCALAPPDATA%\\Programs\\ApplicantScout Companion" in readme
    assert "downloads the installer, verifies its `.sha256` checksum" in readme
    assert "relaunches it after the update" in readme
    assert "Start and stop with WoW" in readme


def test_readme_points_to_packaged_addon_zip_not_source_archive():
    readme = _read_repo_text("README.md")

    assert "ApplicantScout-*.zip" in readme
    assert "https://github.com/Antrakt92/ApplicantScout-Addon/releases/latest" in readme
    assert "_retail_\\Interface\\AddOns\\ApplicantScout\\ApplicantScout.toc" in readme
    assert "automatic source-code ZIP" in readme
    assert "wrong folder name for WoW" in readme
    assert "ApplicantScout-<version>.zip" in readme
    assert "separate from the companion portable ZIP" in readme
