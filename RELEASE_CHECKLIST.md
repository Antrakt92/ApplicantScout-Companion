# ApplicantScout Companion Release Checklist

Use this checklist for public companion releases. The tag push starts the gated
GitHub Actions workflow; the workflow builds, validates, uploads, and publishes
the matching installer assets only after all release checks pass.

## Prepare

1. Update `pyproject.toml`, `src/applicant_scout/__init__.py`,
   `RELEASE_NOTES.md`, and README compatibility copy to the target version.
2. Confirm the paired addon release is current. Companion `0.5.4` pairs with
   ApplicantScout addon `0.3.3`.
3. Run:

   ```powershell
   .\.venv\Scripts\python -m pytest
   .\scripts\check.ps1
   .\scripts\check-release-version.ps1 -Tag v0.5.4
   ```

## Build

1. Commit release-prep changes first. `scripts\build-windows.ps1` refuses dirty
   release inputs by default so public assets are built from committed source.
2. Optional local smoke: build the installer and portable archive before the tag
   if you want to test the exact Windows artifacts locally:

   ```powershell
   .\scripts\build-windows.ps1
   .\scripts\check-release-version.ps1 -Tag v0.5.4 -RequireAssets
   ```

3. Expected assets:
   - `dist\ApplicantScoutCompanionSetup-0.5.4.exe`
   - `dist\ApplicantScoutCompanionSetup-0.5.4.exe.sha256`
   - `dist\ApplicantScoutCompanion-0.5.4-portable.zip`

## Publish

1. Push tag `v0.5.4` after release-prep changes are committed.
2. Confirm the `Build and release` GitHub Actions workflow completed.
3. Confirm the GitHub Release contains all expected assets before announcing it.
4. Do not publish an update release without the `.exe` and `.exe.sha256` pair;
   in-app updates intentionally refuse incomplete releases.
5. Smoke-test from an installed `0.5.1` companion: update check should show the
   blue install icon, download the installer, verify the checksum, install
   silently, close the old process, and relaunch `0.5.4`.
