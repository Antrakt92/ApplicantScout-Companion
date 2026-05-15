# ApplicantScout Companion Release Checklist

Use this checklist for public companion releases. Do not create the tag until
the matching installer assets can be published in the same GitHub Release.

## Prepare

1. Update `pyproject.toml`, `src/applicant_scout/__init__.py`,
   `RELEASE_NOTES.md`, and README compatibility copy to the target version.
2. Confirm the paired addon release is current. Companion `0.2.2` pairs with
   ApplicantScout addon `0.1.4`.
3. Run:

   ```powershell
   .\.venv\Scripts\python -m pytest
   .\scripts\check.ps1
   .\scripts\check-release-version.ps1 -Tag v0.2.2
   ```

## Build

1. Commit release-prep changes first. `scripts\build-windows.ps1` refuses dirty
   release inputs by default so public assets are built from committed source.
2. Build the installer and portable archive:

   ```powershell
   .\scripts\build-windows.ps1
   .\scripts\check-release-version.ps1 -Tag v0.2.2 -RequireAssets
   ```

3. Expected assets:
   - `dist\ApplicantScoutCompanionSetup-0.2.2.exe`
   - `dist\ApplicantScoutCompanionSetup-0.2.2.exe.sha256`
   - `dist\ApplicantScoutCompanion-0.2.2-portable.zip`

## Publish

1. Create tag `v0.2.2` only after the build assets are ready.
2. Create the GitHub Release from `RELEASE_NOTES.md`.
3. Upload all expected assets before marking the release ready.
4. Do not publish an update release without the `.exe` and `.exe.sha256` pair;
   in-app updates intentionally refuse incomplete releases.
5. Smoke-test from an installed `0.2.1` companion: update check should show the
   blue install icon, download the installer, verify the checksum, install
   silently, close the old process, and relaunch `0.2.2`.
