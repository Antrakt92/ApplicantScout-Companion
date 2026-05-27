# ApplicantScout Companion Release Checklist

Use this checklist for public companion releases. The tag push starts the gated
GitHub Actions workflow; the workflow builds, validates, uploads, and publishes
the matching installer assets only after all release checks pass.

## Prepare

1. Update `pyproject.toml`, `src/applicant_scout/__init__.py`,
   `RELEASE_NOTES.md`, and README wire-compatibility copy. Keep exact paired
   release versions in `RELEASE_NOTES.md`; README install links should continue
   to use `releases/latest`.
2. Confirm the paired addon release train is prepared. Companion
   `<companion version>` pairs with ApplicantScout addon `<paired addon version>`;
   the addon `CHANGELOG.md` top entry must name Companion `<companion version>`.
3. Run:

   ```powershell
   .\.venv\Scripts\python -m pytest
   .\scripts\check.ps1
   .\scripts\check-release-version.ps1 -Tag v<companion version>
   ```

## Build

1. Commit release-prep changes first. `scripts\build-windows.ps1` refuses dirty
   release inputs by default so public assets are built from committed source.
2. Optional local smoke: build the installer and portable archive before the tag
   if you want to test the exact Windows artifacts locally:

   ```powershell
   .\scripts\build-windows.ps1
   .\scripts\check-release-version.ps1 -Tag v<companion version> -RequireAssets
   ```

   `-RequireAssets` validates artifact presence and checksum consistency only.
   It does not make an unsigned installer self-update capable.

3. Expected assets:
   - `dist\ApplicantScoutCompanionSetup-<companion version>.exe`
   - `dist\ApplicantScoutCompanionSetup-<companion version>.exe.sha256`
   - `dist\ApplicantScoutCompanion-<companion version>-portable.zip`

## Publish

1. Push the paired addon/companion tags close together after release-prep
   changes are committed. The companion release publishes the installer assets
   first; the addon release workflow waits for those public companion assets
   before BigWigs publishes marketplace files.
2. Confirm the companion `Build and release` GitHub Actions workflow completed.
3. Confirm the companion GitHub Release contains all expected assets before the
   paired addon `Package and release` workflow reaches marketplace publishing.
4. Confirm the paired addon GitHub Release is public with its ZIP and
   `release.json`.
5. Do not publish an update release without the `.exe` and `.exe.sha256` pair;
   in-app updates intentionally refuse incomplete releases.
6. For wire-breaking changes, do not rely on companion-first ordering alone:
   stop at draft/manual publish or use an explicit orchestrated release gate so
   users cannot install a companion that requires an unavailable addon.
7. Smoke-test from a previous stable or explicitly chosen baseline companion.
   Record the chosen baseline in release notes or release-prep notes. In-app
   installer smoke requires a trusted signed installer; unsigned builds should
   use manual installer smoke from the GitHub Release page.
