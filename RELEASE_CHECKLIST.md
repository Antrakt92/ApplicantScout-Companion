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
3. Do manual reconciliation before tagging. Review the companion and addon
   commit ranges plus their pending notes, then promote only user-facing changes
   into the new top versioned release entries:

   ```powershell
   git log --oneline <last companion tag>..HEAD
   git log --oneline <last addon tag>..HEAD
   ```

   Check companion `RELEASE_NOTES.md::Unreleased` and addon
   `CHANGELOG.md::Unreleased`; clear them or leave only intentionally
   post-release work after the new top versioned entries are written.
4. Run:

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

   `-RequireAssets` validates artifact presence, installer checksum
   consistency, and portable ZIP integrity/root/files. It does not make an
   unsigned installer self-update capable.

3. Expected assets:
   - `dist\ApplicantScoutCompanionSetup-<companion version>.exe`
   - `dist\ApplicantScoutCompanionSetup-<companion version>.exe.sha256`
   - `dist\ApplicantScoutCompanion-<companion version>-portable.zip`

## Publish

1. Create both local tags only after both release-prep commits are on `main`.
   Push both tags inside the paired workflows' 120-second wait window:

   ```powershell
   git push origin v<companion version>
   git push origin v<paired addon version>
   ```

   Do not wait for the companion workflow to finish before pushing the addon
   tag. The companion and addon workflows first wait for the opposite tag; the
   addon workflow later has a separate 180-second wait for published companion
   assets before BigWigs publishes marketplace files.
2. Confirm the companion `Build and release` GitHub Actions workflow completed.
3. Confirm the companion GitHub Release contains all expected assets before the
   paired addon `Package and release` workflow reaches marketplace publishing.
4. Confirm the paired addon GitHub Release is public with its ZIP and
   `release.json`.
5. If the addon workflow fails only because companion assets were not public
   inside the 180-second wait, rerun the failed addon workflow after the
   companion assets exist. Do not delete/recreate or force-push release tags for
   that timeout path.
6. Do not publish an update release without the `.exe` and `.exe.sha256` pair;
   in-app updates intentionally refuse incomplete releases.
7. For wire-breaking changes, do not rely on companion-first ordering alone:
   stop at draft/manual publish or use an explicit orchestrated release gate so
   users cannot install a companion that requires an unavailable addon.
8. Smoke-test from a previous stable or explicitly chosen baseline companion.
   Record the chosen baseline in release notes or release-prep notes. In-app
   installer smoke requires a trusted signed installer; unsigned builds should
   use manual installer smoke from the GitHub Release page.
