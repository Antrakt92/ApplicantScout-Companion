# ApplicantScout Companion Release Checklist

Use this checklist for public companion releases. The tag push starts the gated
GitHub Actions workflow; the workflow builds, validates, uploads, and leaves a
verified draft GitHub Release with the matching installer assets. The separate
manual `Publish release` workflow makes that draft public only after the
checksum-gated updater smoke has been attested.

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
    .\scripts\check.ps1 -SeasonalOnlineChecks -SeasonalWCLChecks
    .\.venv\Scripts\python scripts\export_public_visual_assets.py --addon-root ..\ApplicantScout-Addon --check
    .\scripts\check-release-version.ps1 -Tag v<companion version>
   ```

   The local `.\scripts\check.ps1` release gate must keep local strict visual
   baselines enabled; this local strict visual baselines check is the approval
   gate for committed media. Do not use `-VisualMode Smoke` for this local release gate;
   CI/release smoke is only a render-health check and does not approve committed
   baseline or public-media updates. `-SeasonalOnlineChecks` validates
   `MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME` against Wago's GroupFinderActivity data
   and `MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME` against Wago's
   MythicPlusSeasonTrackedMap / MapChallengeMode data before seasonal release
   prep relies on the shipped localized-listing or Party leader-key fallback.
   `-SeasonalWCLChecks` is an explicit quota-spend acknowledgment: it makes one
   authenticated Warcraft Logs GraphQL request, validates the shipped M+ and
   raid zone/encounter constants, reports the post-query quota snapshot, and
   fails if fewer than 50 points remain.

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
   consistency, and portable ZIP integrity/root/files. It confirms the static
   asset contract for checksum-gated in-app updater eligibility, but does not
   exercise the normal GitHub latest-release feed while the release is still draft.

3. Expected local build artifacts:
   - `dist\ApplicantScoutCompanionSetup-<companion version>.exe`
   - `dist\ApplicantScoutCompanionSetup-<companion version>.exe.sha256`
   - `dist\ApplicantScoutCompanion-<companion version>-portable.zip`

## Publish

Before creating or pushing release tags, enable **Release immutability** under
the companion repository's release settings. Configure the repository secret
`RELEASE_SETTINGS_READ_TOKEN` as a fine-grained token limited to this repository
with **Administration: read** and **Metadata: read**, and no write permission.
The manual publish workflow uses it only to read the immutable-release setting
immediately before making the draft public, restores the normal workflow token
for publication, and accepts the result only after GitHub reports the published
release as immutable with the exact authoritative assets. Enabling the setting
does not retrofit existing releases; the next published release is the first
live immutability proof.

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
2. Confirm the companion `Build and release` GitHub Actions workflow completed
   with a verified draft release, then record its numeric run ID. The publish
   gate downloads the retained run-attempt-specific release Actions artifact from
   that exact successful tag/head-SHA run and fails closed if it is missing or
   expired. The draft should contain:
   - `ApplicantScoutCompanionSetup-<companion version>.exe`
   - `ApplicantScoutCompanionSetup-<companion version>.exe.sha256`
   - `ApplicantScoutCompanion-<companion version>-portable.zip`
   - `ApplicantScoutCompanion-<companion version>-release-manifest.json`

   If the workflow fails before any draft exists, use **Re-run failed jobs**;
   the draft writer reuses the exact build-attempt artifact. Do not rerun all
   jobs after a draft exists. An existing or partially uploaded draft makes the
   protocol fail closed; do not delete/recreate tags or retry publication from
   unverified bytes. Retire that draft explicitly and recover forward with a
   new PATCH release after reviewing its assets.
3. Smoke-test from the latest published stable companion. Record that baseline
   in release notes or release-prep notes. The publish workflow rejects an older
   arbitrary baseline so an updater-affecting release cannot skip the current
   public upgrade path. Normal
   stable updater feed ignores draft releases, so this pre-public gate confirms
   the checksum-gated installer candidate and release asset contract; a future
   private/canary update feed would be required for full GitHub-feed smoke while
   still draft.
   Record the SHA-256 of the exact installer exercised by the smoke test; the
   publish gate requires it to match the authoritative manifest and GitHub's
   server-reported asset digest.
4. Run the manual `Publish release` GitHub Actions workflow with:
   - `tag`: `v<companion version>`
   - `release_run_id`: the successful `Build and release` run ID from step 2
   - `smoke_tested_from_version`: the latest published stable companion version
   - `smoke_tested_installer_sha256`: the 64-character lowercase SHA-256 of the
     exact candidate installer used by the smoke test
   - `confirm_checksum_gated_update_smoke`: checked
5. Confirm the companion GitHub Release is public with all expected assets
   before the paired addon `Package and release` workflow reaches marketplace
   publishing.
6. Confirm the paired addon GitHub Release is public with its ZIP and
   `release.json`, and confirm the addon's separate read-only
   `verify-curseforge` job reports the expected ZIP as the first public
   Approved/Released file. If marketplace propagation is delayed, rerun only
   that verification job; never retry the already completed upload.
7. If the addon workflow fails only because companion assets were not public
   inside the 180-second wait, rerun the failed addon workflow after the
   companion assets exist. Do not delete/recreate or force-push release tags for
   that timeout path.
8. Do not publish an update release without the `.exe` and `.exe.sha256` pair;
   in-app updates intentionally refuse incomplete releases.
9. For wire-breaking changes, do not rely on companion-first ordering alone:
   stop at draft/manual publish or use an explicit orchestrated release gate so
   users cannot install a companion that requires an unavailable addon.
10. Signing remains the future publisher-identity path for broader distribution.
    The narrow draft-writer has an optional `signtool` hook after the
    credentialless build handoff: set
    `APSCOUT_SIGNING_CERT_SHA1` (and optionally
    `APSCOUT_SIGNING_TIMESTAMP_URL` / `APSCOUT_SIGNTOOL_PATH`) so the installer
    is signed after Inno Setup and before final `.sha256` generation. Without a
    configured certificate, release builds remain unsigned and the checksum
    still proves integrity, not publisher identity.
11. After the addon release is public, verify it once through a separately
    managed Retail installation or isolated test instance and keep automatic
    updates enabled there. Do not overwrite a linked or development checkout;
    a manually copied folder is not owned by CurseForge and will not receive
    marketplace updates even when the public file is current.
