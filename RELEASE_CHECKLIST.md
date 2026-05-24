# ApplicantScout Companion Release Checklist

Use this checklist for public companion releases. The tag push starts the gated
GitHub Actions workflow; the workflow builds, validates, uploads, and publishes
the matching installer assets only after all release checks pass.

## Prepare

1. Update `pyproject.toml`, `src/applicant_scout/__init__.py`,
   `RELEASE_NOTES.md`, and README wire-compatibility copy. Keep exact paired
   release versions in `RELEASE_NOTES.md`; README install links should continue
   to use `releases/latest`.
2. Confirm the paired addon release train is prepared. Companion `0.7.0` pairs
   with ApplicantScout addon `0.4.1`; the addon `CHANGELOG.md` top entry must
   name Companion `0.7.0`.
3. Run:

   ```powershell
   .\.venv\Scripts\python -m pytest
   .\scripts\check.ps1
   .\scripts\check-release-version.ps1 -Tag v0.7.0
   ```

## Build

1. Commit release-prep changes first. `scripts\build-windows.ps1` refuses dirty
   release inputs by default so public assets are built from committed source.
2. Optional local smoke: build the installer and portable archive before the tag
   if you want to test the exact Windows artifacts locally:

   ```powershell
   .\scripts\build-windows.ps1
   .\scripts\check-release-version.ps1 -Tag v0.7.0 -RequireAssets
   ```

3. Expected assets:
   - `dist\ApplicantScoutCompanionSetup-0.7.0.exe`
   - `dist\ApplicantScoutCompanionSetup-0.7.0.exe.sha256`
   - `dist\ApplicantScoutCompanion-0.7.0-portable.zip`

## Publish

1. Push the paired addon/companion tags close together after release-prep
   changes are committed. The addon release is the first public artifact; the
   companion workflow waits for the addon GitHub Release to publish
   `ApplicantScout-v0.4.1.zip` and `release.json` before it creates the
   companion draft.
2. Confirm the paired addon `Package and release` workflow completed and the
   addon GitHub Release is public with its ZIP and `release.json`.
3. Confirm the companion `Build and release` GitHub Actions workflow completed.
4. Confirm the companion GitHub Release contains all expected assets before
   announcing it.
5. Do not publish an update release without the `.exe` and `.exe.sha256` pair;
   in-app updates intentionally refuse incomplete releases.
6. Smoke-test from an installed `0.5.1` companion: update check should show the
   blue install icon, download the installer, verify the checksum, install
   silently, close the old process, and relaunch `0.7.0`.
