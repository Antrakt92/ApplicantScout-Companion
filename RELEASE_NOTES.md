# ApplicantScout Companion Release Notes

## Unreleased

## 0.8.3 - 02-Jun-2026

Performance and responsiveness patch paired with ApplicantScout addon `0.4.6`.
This release keeps the current APS1 v8 QR wire format while reducing overlay
hitches, startup blocking, screenshot watcher stalls, RaiderIO local cache work,
Settings path probes, startup-shortcut work, and WCL/character-cache disk
pauses.

### Changed

- Screenshot watcher replacement now starts the new watcher first, commits a
  generation gate, marks the old watcher stopped immediately, and lets slow old
  observer cleanup finish off-thread.
- Snapshot and decode-failure signals from screenshot workers are coalesced onto
  the Qt event loop, keeping bursts of screenshot events from repeatedly
  interrupting overlay rendering.
- Local RaiderIO preload now owns fingerprinting, decoded lookup cache loading,
  and realm-block hydration instead of letting the first overlay lookup do that
  work on the UI path.
- WoW lifecycle startup no longer performs the expensive current-process check
  on the normal startup/apply path; duplicate current-session watchers are
  tracked directly.

### Improved

- Reduced overlay open/collapse, tab switching, button, and Alt-Tab hitches by
  debouncing transient foreground changes, preserving interaction grace while
  the cursor is over the overlay, and avoiding immediate hide/show flicker when
  Windows briefly reports the companion or overlay as foreground.
- Reduced table refresh cost during applicant bursts by keeping sort, grouping,
  package-fit, and group-marker state current while rewriting only rows whose
  rendered data actually changed when row IDs/order/count stay stable.
- Reduced hover/pin repaint cost by repainting only the rows whose interaction
  highlight changed instead of repainting the full table surface.
- Reused grouped-applicant package-fit results from sorting during rendering,
  avoiding duplicate package score computation for the same snapshot.
- Reduced startup and Settings pauses by lazy-loading the QR decoder, deferring
  Screenshots path health checks, and moving Windows startup shortcut updates
  off the Qt UI thread.
- Settings now reuses the already-computed Screenshots path warning during
  autosave/test status updates, avoiding duplicate slow health probes.
- Moved local RaiderIO fingerprinting, decoded payload loading, and realm
  hydration work off the UI path and into the preload/cache flow.
- Decoded local RaiderIO lookup payloads continue to use the configured cache
  directory, so relaunches can reuse fingerprinted lookup data.
- Kept WCL character-cache disk writes out of cache read locks so applicant
  refreshes can continue while slow storage writes finish.
- Cached Windows private ACL setup for config/cache paths, reducing repeated
  `icacls` subprocess work during autosaves and cache writes.

### Fixed

- Replaced screenshot watchers without blocking the UI on old watcher cleanup,
  while generation gates prevent stale watchers from applying fresh snapshots.
- Prevented a stale watcher from decoding or deleting a fresh marker screenshot
  after the user changes the Screenshots folder.
- Reported lazy pyzbar/zbar load failures as decode health failures and kept
  screenshots in place for support/debugging instead of silently treating them
  as unrelated no-QR images.
- Screenshot cleanup and manual decode now treat decoder-unavailable states as
  explicit decode failures and preserve matching screenshots.
- Distinguished rapid reused WoW screenshot filenames by precise stat metadata
  instead of dropping same-second updates.
- Prevented slow, stale character-cache writes from overwriting newer cache
  entries and kept WCL same-target/cache-hit completion maps in sync.
- Preserved dirty WCL cache state after failed writes so a later flush can retry
  instead of losing the pending save.
- Kept cache-hit rows updated even while a same-target WCL worker is still
  pending, so a row does not stay in loading state just because an older waiter
  owns the network request.
- Kept WoW lifecycle watcher re-arm behavior explicit: startup can avoid the
  expensive initial process scan, while re-arm still checks for an existing
  watcher before launching another one.
- Reported Windows startup shortcut update failures in Settings after the save,
  without freezing the UI or rolling back the already-applied runtime setting.
- Paired addon `0.4.6` chunks large QR painting across frames and preserves
  dirty snapshots that arrive while a QR is painting or settling for capture.
- Paired addon `0.4.6` cancels stale QR paint/capture jobs on session end,
  guards screenshots by completed paint generation, and reuses addon-side
  RaiderIO M+ summaries during large applicant payloads.

### Release Assets

- Requires the ApplicantScout WoW addon `0.4.6`.
- Installer: `ApplicantScoutCompanionSetup-0.8.3.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.8.3.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.8.3-portable.zip`

## 0.8.2 - 30-May-2026

Self-update restoration patch paired with ApplicantScout addon `0.4.5`. This
release keeps the current APS1 v8 QR wire format and addon runtime unchanged
while restoring checksum-gated in-app installer launches for unsigned Windows
builds.

### Fixed

- Restored one-click in-app updates for unsigned installer releases after
  `.sha256` verification; checksums verify file integrity, not publisher
  identity.

### Release Assets

- Requires the ApplicantScout WoW addon `0.4.5`.
- Installer: `ApplicantScoutCompanionSetup-0.8.2.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.8.2.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.8.2-portable.zip`

## 0.8.1 - 30-May-2026

WCL retry and release-media hardening patch paired with ApplicantScout addon
`0.4.4`. This release keeps the current APS1 v8 QR wire format while improving
Warcraft Logs retry handling and refreshing anonymized public visual assets.

### Improved

- Refreshed anonymized public overlay media and added a release-prep check so
  the addon README and public screenshots stay aligned with committed visual
  baselines.
- Hardened release preparation checks so existing companion releases are refused
  by the shared release-version script before Windows artifacts are rebuilt.

### Fixed

- Made temporary Warcraft Logs OAuth outages reuse the normal rate-limit/server
  retry blocks, and added a scoped Retry WCL action for malformed or GraphQL
  row/detail failures.

### Release Assets

- Requires the ApplicantScout WoW addon `0.4.4`.
- Installer: `ApplicantScoutCompanionSetup-0.8.1.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.8.1.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.8.1-portable.zip`

## 0.8.0 - 28-May-2026

APS1 v8, screenshot cleanup, and local reliability release paired with
ApplicantScout addon `0.4.3`. This release keeps ApplicantScout's QR/screenshot
transport as the public API bridge, adds safer handling for temporary
LFG-read lockdown snapshots, and improves local cleanup, settings, updater,
and Warcraft Logs resilience.

### Changed

- Added APS1 v8 QR decoding flags so temporary LFG-read lockdown snapshots can
  refresh roster/version state without clearing active listing/applicant rows,
  while explicit terminal clears remain authoritative.
- Made unsigned companion updates manual-install only until a trusted signed
  installer path is configured; checksum sidecars remain integrity checks, not
  publisher identity.

### Improved

- Improved raid listing display by keeping target raid evidence distinct from
  supporting M+ context and showing per-boss raid parse segments more clearly.
- Added an explicit screenshot cleanup support command so leftover ApplicantScout
  QR screenshots can be checked or removed without starting the overlay.
- Hardened Screenshots path validation plus local config/cache privacy
  boundaries, reducing accidental bad-folder setup and private artifact leaks.
- Improved tray/settings restore behavior so tray-opened overlay windows can
  return to focus without requiring WoW to be the foreground window.
- Improved Settings accessibility and tooltips for support, update, and close
  controls.
- Hardened release preparation and publishing gates with explicit visual smoke
  mode, existing-release refusal, tag ancestry checks, portable ZIP validation,
  paired tag-push guidance, and paired asset checks.

### Fixed

- Fixed addon-side GitHub checks using the companion wrapper so they pass the
  explicit smoke visual-fixture mode instead of accidentally running strict
  overlay baseline comparisons after intentional UI changes.
- Fixed updater handoff edge cases so unsigned or incomplete update paths stay
  visible as manual installs instead of silently losing the pending update.
- Fixed WoW lifecycle shutdown handling so the companion does not quit until the
  startup watcher has been re-armed successfully.
- Fixed preserved applicant and Warcraft Logs state being reused across LFG
  region/default-realm identity changes.
- Fixed placeholder roster identities being accepted from snapshots, preventing
  duplicate `UNKNOWN` rows from poisoning Party state.
- Hardened local RaiderIO fallback handling so temporary lookup/cache failures
  preserve existing character evidence when the applicant identity is unchanged.
- Fixed settings close/quit paths so failed config writes block shutdown instead
  of discarding user changes, and made cache reset failures report cleanly.
- Fixed malformed or incomplete Warcraft Logs raid alias responses being treated
  as valid empty raid evidence.

### Release Assets

- Requires the ApplicantScout WoW addon `0.4.3`.
- Installer: `ApplicantScoutCompanionSetup-0.8.0.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.8.0.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.8.0-portable.zip`

## 0.7.1 - 26-May-2026

Reliability, updater, and startup-polish release paired with ApplicantScout
addon `0.4.2`. This release keeps the current QR wire format while making
bursty applicant loads, duplicate Warcraft Logs lookups, update handoffs,
screenshot scanning, and release-train validation more predictable.

### Improved

- Reduced startup and applicant-burst stutter by deferring character-cache
  writes and reusing decoded RaiderIO lookup payloads.
- Coalesced duplicate Warcraft Logs post-fetch work so party/applicant rows that
  share a character receive cached or completed results consistently.
- Preserved full grouped-applicant visibility while avoiding duplicate group
  marker rebuild work.
- Hardened local RaiderIO recovery paths so invalid or partially written cache
  files are handled cleanly instead of blocking startup.
- Refreshed setup, trust, updater, and Warcraft Logs OAuth copy for the current
  addon/companion install flow.

### Fixed

- Fixed stalled self-update handoffs so the installed companion can recover when
  an installer launch, process handoff, or relaunch marker does not complete as
  expected.
- Fixed stale screenshot watcher snapshots being processed after startup, which
  could replay old QR payloads before the current WoW session produced fresh
  screenshots.
- Fixed lazy raid-detail Warcraft Logs failures showing weak feedback or getting
  stuck too long; transient failures now show clearer row state and retry after a
  cooldown.
- Hardened private config/cache writes on Windows by applying stricter ACL
  handling to sensitive local files.
- Fixed stale Warcraft Logs fetch waiters that could leave later duplicate
  requests waiting on work that had already failed or completed.
- Hardened paired release gates so companion releases validate the required
  addon version, release assets, and release notes before publishing.
- Fixed release-check coverage around deterministic raid-detail retry expiry.

### Release Assets

- Requires the ApplicantScout WoW addon `0.4.2`.
- Installer: `ApplicantScoutCompanionSetup-0.7.1.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.7.1.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.7.1-portable.zip`

## 0.7.0 - 24-May-2026

Raid-fit and Party-focus release paired with ApplicantScout addon `0.4.1`.
This release makes raid applicants read like raid applicants: the main fit signal
moves into the relevant Normal/Heroic/Mythic column, while M+ remains supporting
evidence instead of looking like the primary recommendation.

### Added

- Added raid-target fit cells for active raid listings. Heroic listings now use
  the `H` column, Mythic listings use `M`, and Normal listings use `N`.
- Added estimated raid fit in the target raid column when the exact raid
  difficulty is disabled but other raid evidence is available.
- Added raid detail rows in the hover/pin panel, including per-boss Warcraft
  Logs parses and local RaiderIO raid kill progress.
- Added Raid/M+ detail tabs so raid applicants can still expose their M+
  support evidence without replacing the raid-first view.
- Added local RaiderIO raid-progress enrichment and current-score fallback for
  applicants and current party/raid members.

### Improved

- Raid listings now force the target raid column visible when needed, so
  disabled metric settings do not hide the column that explains the active raid.
- Raid fit columns now auto-size to their rendered recommendation text instead
  of truncating `FIT`, `OK`, `RISK`, or `SUP` cells.
- The info panel now previews the first visible applicant or party member by
  default instead of showing an empty hover placeholder while rows are present.
- Empty applicant and party views now keep the full info-panel height, avoiding
  window jumps while rows disappear or reload.
- Party view now keeps the last raid listing difficulty in memory after the LFG
  listing closes, so Heroic/Mythic raid fit does not collapse to unknown context
  while the formed group remains visible.
- M+ cells in raid context now render as neutral support text instead of using a
  saturated fit-style background.
- Party view now respects a manual click on the already-selected Party tab, so a
  new applicant or refresh does not pull you back to Applicants while you are
  reviewing the group.
- Raid contexts now hide the manual M+ target-key control, avoiding a
  meaningless key selector while reviewing Normal/Heroic/Mythic applicants.
- Transient zero spec/item-level snapshots now preserve the last known identity
  data instead of wiping usable WCL state.
- Raid boss detail fetches now ignore stale character, difficulty, or metric
  completions and retry transient WCL failures without overwriting current
  M+/raid evidence.
- Overlay visual baselines and raid/M+ panel layout coverage were refreshed for
  the new raid detail surface.

### Fixed

- Fixed raid target cells showing fit-like output while WCL data was still
  loading.
- Fixed grouped raid applicants showing a premature package fit before every
  group member had finished loading.
- Fixed raid listings where a green M+ support row could be mistaken for the
  raid recommendation.

### Release Assets

- Requires the ApplicantScout WoW addon `0.4.1`.
- Installer: `ApplicantScoutCompanionSetup-0.7.0.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.7.0.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.7.0-portable.zip`

## 0.6.0 - 23-May-2026

Leader-key and party-context release paired with ApplicantScout addon `0.4.0`.
This release keeps the overlay focused on the active search while adding
automatic Mythic+ target-key calibration from the current group leader.

### Added

- Added APS1 v7 payload decoding, including the optional leader-keystone block
  emitted by the addon.
- Added automatic Party target-key calibration from the current leader's key.
  Manual Party key overrides still win, and raid contexts ignore leader-key
  calibration.
- Added Party context from leader-key data when you are in a group but the
  original listing is no longer visible.

### Improved

- The overlay now stays on Applicants while an active listing/search is open,
  even if the applicant list temporarily drops to zero.
- Party roster refreshes are more reliable when group composition changes; the
  paired addon waits briefly for inspect/spec data and then sends a fallback
  snapshot instead of forgetting the update.
- Cache reset and WCL cache recovery are more responsive around locked files,
  stale state, and partial cache data.
- WCL character-not-found cache entries are scoped by character identity with a
  short TTL, so a missing character result no longer poisons later spec, role,
  or metric-scope lookups indefinitely.
- Malformed Warcraft Logs Mythic+ payloads now fail with explicit malformed-data
  errors instead of being treated like valid empty evidence.
- Mythic+ evidence display is more explicit for sparse or low-evidence rows;
  all-single-run rows are marked as `N=1` instead of presenting best as a
  stable median-like signal.
- RaiderIO local database loading retries after missing or malformed DB files
  appear or become valid, instead of caching the failure for the whole session.
- Cache reset also invalidates WCL OAuth token state, and OAuth invalidation is
  protected against parallel refresh races.
- Update checks now preserve an already pending installable update across
  transient GitHub/network failures while still clearing stale pending state for
  confirmed no-release or incomplete-asset responses.
- Screenshots path validation now rejects nested `_retail_` folders such as
  addon subdirectories without creating the bad path.
- Setup, updater, seasonal activity mapping, overlay visual fixtures, overlay
  tab behavior, Lua payload golden coverage, and paired release gates all have
  stronger regression coverage.

### Fixed

- Fixed Party key context staying stale when the companion receives a newer
  leader key from the addon.
- Fixed active-search tab fallback cases that could make the user look at Party
  view while they were still scouting applicants.
- Fixed several release/setup hardening edges around paired addon metadata,
  local cache handling, and update validation.

### Release Assets

- Requires the ApplicantScout WoW addon `0.4.0`.
- Installer: `ApplicantScoutCompanionSetup-0.6.0.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.6.0.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.6.0-portable.zip`

## 0.5.5 - 22-May-2026

Release and log-rollover hardening patch paired with the latest addon roster
transport fix.

### Fixed

- Fixed startup/log rollover recovery when the previous companion log file is
  still locked by Windows or another process.
- Fixed companion release metadata checks so paired addon requirements can
  accept a newer already-published addon release.
- Hardened paired release validation around addon metadata before publishing
  companion installer assets.

### Release Assets

- Requires the ApplicantScout WoW addon `0.3.4`.
- Installer: `ApplicantScoutCompanionSetup-0.5.5.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.5.5.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.5.5-portable.zip`

## 0.5.4 - 21-May-2026

Lifecycle and fetch-scope hardening patch for live applicant scouting.
This release focuses on cleaner startup/shutdown behavior, safer update/cache
I/O, and avoiding unnecessary Warcraft Logs work when an applicant snapshot
temporarily lacks spec context.

### Improved

- Launcher hover and click handling is more responsive during fast interactions
  and overlay refreshes.
- Mythic+ fit scoring now uses the same representative per-dungeon WCL rows that
  the details panel shows, so hidden older/worse brackets no longer overrule the
  visible evidence for a dungeon.
- Broad Mythic+ listings now treat strong near-target RaiderIO completion
  history as real evidence even when sparse WCL logs are weak, keeping risky
  applicants in a reviewable `Fit` range instead of collapsing to a misleading
  zero-like score.
- Update downloads now stream to disk and private cache/config writes are more
  defensive against partial writes and file-permission edge cases.
- Developer watcher startup and detection are more explicit, making packaged
  and local development launches easier to distinguish.

### Fixed

- Fixed WoW lifecycle re-arm warnings caused by the watcher detector matching
  the PowerShell probe process instead of a real companion watcher.
- Fixed unknown-spec applicants temporarily queueing Mythic+ Warcraft Logs
  fetches with `spec=0`; M+ evidence now waits until the addon supplies a real
  spec, while raid-only scopes remain available.
- Fixed stale WCL completions from an older applicant/spec snapshot overwriting
  the current ready/no-data state.
- Fixed the debug cache-TTL environment override persisting into the saved
  settings file.
- The paired addon now prioritizes empty applicant-list clears after applicants
  were previously shown, so the overlay does not keep a stale applicant visible
  while the in-game list is already empty.

### Release Assets

- Requires the ApplicantScout WoW addon `0.3.3`.
- Installer: `ApplicantScoutCompanionSetup-0.5.4.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.5.4.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.5.4-portable.zip`

## 0.5.1 - 20-May-2026

Overlay reliability and release hardening patch for live applicant scouting.
This release focuses on keeping the collapsed launcher, Applicants/Party tabs,
and update/install lifecycle predictable during real WoW foreground changes.

### Improved

- Settings now includes an in-app changelog viewer and an explicit Quit action
  in the secondary actions menu, making release notes and full shutdown easier
  to find.
- The cache reset action copy is clearer about what will be deleted before the
  user confirms it.
- The collapsed launcher is now much more stable during long drags, fast mouse
  movement, WoW foreground changes, and background overlay refreshes.
- The launcher can now reach the physical screen edge and restores saved edge
  positions without being pulled back to the Windows work area.
- Overlay hover/details refreshes are batched more carefully, reducing visible
  table and top-panel jitter while moving between applicant rows.
- New Mythic+ listings created after a roster-only Party view now return focus
  to `Applicants`, while manually selected Party views stay intact for group
  review.
- Release workflows now pin more build dependencies, and branch checks cover
  more companion/addon contract, release metadata, and parser surfaces before
  packaging.

### Fixed

- Fixed the Party roster staying hidden or losing focus after the last applicant
  leaves the list while the current group still needs review.
- Fixed the collapsed launcher sometimes appearing above non-game windows after
  companion restart while WoW was running in the background.
- Fixed launcher clicks hiding the small icon and then restoring it instead of
  opening the full overlay.
- Fixed launcher drag stalls caused by synchronous WoW lifecycle checks running
  on the GUI event loop.
- Fixed launcher position resets, disappearing launcher states, and stale
  foreground polling during drag/release edge cases.
- Fixed the overlay staying on `Party` after creating a new key when the next
  thing the host needs to see is incoming applicants.
- Fixed stopped screenshot watcher snapshots and stale watcher callbacks that
  could otherwise update the overlay after a watcher replacement.
- Fixed settings save/apply and update-install handoff races that could leave
  stale config, lost pending saves, or unclear blocked-quit behavior.
- Fixed overlay geometry and launcher-position clamps around monitor/work-area
  changes so saved positions recover safely.
- Fixed hidden role-filter state and malformed applicant/roster snapshot inputs
  that could otherwise leave stale rows, stale focus, or unclear rejected data.
- Hardened config, updater, screenshot, Warcraft Logs, cache, and malformed
  snapshot boundaries that could leave stale UI state or unclear runtime
  errors.

### Release Assets

- Requires the ApplicantScout WoW addon `0.3.2`.
- Installer: `ApplicantScoutCompanionSetup-0.5.1.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.5.1.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.5.1-portable.zip`

## 0.5.0 - 18-May-2026

Mythic+ ranking calibration release for making applicant order, group packages,
and hover explanations match the evidence more closely.

### Improved

- Reworked Mythic+ fit scoring around a clearer evidence ladder: RaiderIO
  completion readiness carries key experience, Warcraft Logs quality carries
  performance, and missing logs stay a low-confidence unknown rather than a
  hidden failure.
- Warcraft Logs Mythic+ quality now blends best and median percentile, so one
  isolated parse spike no longer outranks steadier nearby-key performance.
- Gray Warcraft Logs evidence now counts only as weak completion experience;
  clean logs, RaiderIO timed keys, and broad near-target evidence keep their
  intended order across `+10`, `+16`, and `+20` style listings.
- Group applications now rank with a softer weak-link package score and
  smoother carry credit, avoiding cases where improving a strong group member
  could lower the package score.
- Unknown-key group sorting now uses the weakest visible member as the headline
  M+ signal, so one very strong player no longer hides a risky grouped friend.
- The overlay table now labels known Mythic+ cells as `Fit <score> +<key>`, and
  hover details show compact confidence, coverage, Warcraft Logs/RaiderIO
  source, and group high/average/low context.
- Hover details now clamp against the top of the screen when they expand above
  the table, keeping the frameless title controls reachable near the display
  edge.
- Release checks now run the paired addon contract tests from the companion
  wrapper, making companion releases catch addon transport regressions before
  packaging.
- Quit requests from the tray, Settings, WoW lifecycle watcher, or control
  socket now respect an in-progress update and surface a clear blocked-quit
  message instead of interrupting installer handoff.

### Fixed

- Fixed Party manual target keys carrying across newly detected real listings
  or raid context while keeping M+ key-step overrides clickable.
- Fixed first-snapshot local RaiderIO dungeon rows sometimes staying empty until
  a later QR snapshot after the companion preloaded the RaiderIO addon DB.
- Fixed partial RaiderIO dungeon rows double-counting summary best-key evidence
  and inflating applicant fit above equivalent explicit dungeon evidence.
- Fixed repeated Warcraft Logs brackets for the same dungeon inflating breadth
  as if they were separate dungeon coverage.
- Fixed high gray Warcraft Logs keys showing their raw overqualified key in the
  visible `+key` headline even though scoring had already downgraded that
  evidence.
- Fixed loading or pending scorecard rows sorting above ready rows while still
  displaying as `...`.
- Fixed RaiderIO-only and Warcraft Logs-error hover states so the panel explains
  when fit is based on RaiderIO fallback evidence.
- Fixed malformed ApplicantScout snapshots with duplicate applicant or roster
  identities being accepted as valid overlay state.
- Fixed Warcraft Logs GraphQL "not found" responses showing as generic API
  errors instead of normal missing-character state.
- Fixed malformed local RaiderIO profile records so one bad record no longer
  breaks local dungeon-row enrichment for the rest of the overlay.

### Release Assets

- Requires the ApplicantScout WoW addon `0.3.1`.
- Installer: `ApplicantScoutCompanionSetup-0.5.0.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.5.0.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.5.0-portable.zip`

## 0.4.0 - 18-May-2026

Party roster overlay release for reviewing the current group or raid alongside
normal Mythic+ applicants.

### Added

- Added `Applicants` / `Party` tabs in the overlay so the same window can switch
  between incoming applicants and the current party or raid roster.
- Added full party/raid roster display from addon snapshots, including
  companion-side Warcraft Logs and RaiderIO context for current group members.
- Added a manual Mythic+ key control beside the source tabs for cases where the
  addon cannot read the hosted key from the current listing or group context.
- Added a draggable launcher: hiding the overlay now collapses it to a small
  always-available in-game icon, and the launcher keeps its position across
  companion restarts.

### Improved

- Party fit scoring now refreshes immediately when the manual key changes and
  can combine partial RaiderIO and Warcraft Logs key evidence more accurately.
- The overlay can be resized much smaller while keeping the table usable.
- Hover details now expand upward above the roster instead of pushing the player
  list downward, so the roster scroll position stays stable while scouting.
- The target-key control now uses visible, separate up/down buttons instead of
  relying on hard-to-see native spinbox arrows.
- The overlay now limits its visible window/launcher behavior to the WoW
  foreground context instead of sitting above unrelated desktop apps.

### Fixed

- Fixed party-only snapshots not showing the overlay when the player was in a
  group but had no active applicant listing.
- Fixed stale party roster state and row mapping issues when switching between
  Applicants and Party views.
- Fixed the Party title and scoring context losing the active listing key.
- Fixed target-key increase clicks being swallowed by the spinbox hitbox.
- Fixed background visibility edge cases around the collapsed launcher and WoW
  foreground detection.

### Release Assets

- Requires the ApplicantScout WoW addon `0.3.0`.
- Installer: `ApplicantScoutCompanionSetup-0.4.0.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.4.0.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.4.0-portable.zip`

## 0.3.2 - 17-May-2026

Screenshot decode startup/performance release for faster first overlay display
during live applicant waves.

### Improved

- QR decoding now scans the normal top-left transport region first and only
  falls back to a full-screen scan when needed, reducing decode work on large
  1440p/4K screenshots.
- Screenshot processing now logs slow stable-file wait and decode stages, making
  it clear whether future startup delays come from WoW writing the JPG or from
  the QR decoder itself.

### Release Assets

- Requires the ApplicantScout WoW addon `0.2.2`.
- Installer: `ApplicantScoutCompanionSetup-0.3.2.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.3.2.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.3.2-portable.zip`

## 0.3.1 - 17-May-2026

Warcraft Logs resilience hotfix for busy applicant waves when the WCL API is
slow, timing out, or returning transient server errors.

### Fixed

- Fixed repeated Warcraft Logs network timeouts causing the overlay to spend
  minutes draining the applicant queue one 15-second timeout at a time.
- Fixed retry behavior so the first WCL network failure starts a short global
  retry pause and queued applicants fail fast into the normal retry path instead
  of hammering the API.
- Moved cached WCL lookups back onto the worker path so opening/updating the
  overlay stays responsive even with a large local character cache.

### Release Assets

- Requires the ApplicantScout WoW addon `0.2.1`.
- Installer: `ApplicantScoutCompanionSetup-0.3.1.exe`
- Installer checksum: `ApplicantScoutCompanionSetup-0.3.1.exe.sha256`
- Portable archive: `ApplicantScoutCompanion-0.3.1-portable.zip`

## 0.3.0 - 17-May-2026

RaiderIO completion-aware Mythic+ scoring, local RaiderIO per-dungeon evidence
next to Warcraft Logs rows, plus screenshot startup, settings launch, WCL
configuration, faster cached WCL display, and release-readiness hardening for
live applicant scouting.

### Improved

- Mythic+ fit scoring now uses ApplicantScout addon RaiderIO completion
  summaries as experience evidence, so players with near-target keys completed
  are no longer punished as heavily when Warcraft Logs has no current data.
- Mythic+ applicant ranking now uses a combined scorecard: RaiderIO per-dungeon
  keys define completion readiness, Warcraft Logs defines performance quality,
  missing logs stay unknown instead of bad, and bad relevant logs are worse than
  no logs.
- Mythic+ fit cells now show the numeric score and key only, coloured with the
  Warcraft Logs palette, instead of adding extra `TOP` / `FIT` / `OK` / `RISK`
  wording on top of the colour.
- Applicant hover details now show the highest timed RaiderIO key per dungeon
  next to the Warcraft Logs key/percentile row by reading the installed
  RaiderIO addon database locally, making stale or missing log coverage easier
  to judge without bloating the QR payload.
- Applicant hover details now separate the Warcraft Logs key and percentile
  into distinct columns, making side-by-side RaiderIO/WCL evidence easier to
  scan during live invites.
- Cached Warcraft Logs results now apply before new API work is queued, so
  already-known applicants can become ready immediately instead of waiting
  behind slower network fetches.
- Applicant add/update bursts and Warcraft Logs completion bursts now coalesce
  overlay table refreshes, reducing UI churn during fast applicant waves.
- The WCL footer now shows active queued/running fetches while quota data is
  still pending instead of implying no fetch has happened yet.
- RaiderIO dungeon rows remain visible when Warcraft Logs has no logs for the
  applicant, so the card can still show real timed-key experience.
- RaiderIO local database loading runs in the background; the overlay stays
  responsive and fills dungeon rows on later snapshots once the cache is ready.
- The RaiderIO completion signal can lift missing or low-key WCL evidence, but
  bad relevant same-level logs still cap the score to avoid overrating risky
  applicants.
- M+ rows with no current Warcraft Logs report can now still rank and display
  from RaiderIO timed-key evidence instead of being forced into the `not found`
  bucket.
- Manual companion launches now bring the Settings window forward instead of
  silently staying in the tray/background flow.
- Re-launching the companion while it is already running now asks the existing
  instance to show Settings, which makes recovery from duplicate clicks clearer.
- Raid scoring now ignores listings with unknown raid difficulty instead of
  ranking applicants from an ambiguous context.
- Release checks now validate paired addon metadata, release-note asset names,
  release constraints, and malformed release inputs before publishing.

### Fixed

- Fixed startup backlog scanning so a fresh screenshot that is still being
  written is not decoded or deleted before the watchdog path can wait for the
  completed file.
- Fixed invalid Warcraft Logs region settings being accepted from saved config,
  environment overrides, or manual WCL fetch tooling.
- Fixed terminal Warcraft Logs states masking RaiderIO-backed M+ fit scores in
  the table and applicant sort order.
- Fixed localized Mythic+ listing names losing same-dungeon fit and target-row
  priority by falling back to the addon-emitted LFG activity ID.
- Fixed RaiderIO per-dungeon keys being shown in the hover panel but ignored by
  the M+ fit formula when the compact same-dungeon summary was missing.
- Fixed RaiderIO per-dungeon rows lowering the displayed same-dungeon key when
  the compact RaiderIO summary already had stronger same-dungeon evidence.
- Fixed lower-key Warcraft Logs evidence making an overqualified applicant's
  M+ fit cell display the lower WCL key instead of the stronger RaiderIO
  timed-key evidence.
- Fixed explicit hyphenated realm names such as `Король-лич` being split at
  the wrong dash for local RaiderIO database enrichment, which could leave RIO
  dungeon rows blank for those realms.
- Fixed WoW-normalized Russian realm names such as `Ревущийфьорд` and
  `Корольлич` falling through to invalid Warcraft Logs Cyrillic slugs or
  missing the local RaiderIO realm row.
- Fixed localized RaiderIO dungeon names splitting target-dungeon RIO and WCL
  evidence into separate hover-panel rows.
- Fixed RIO-backed fit badges being hidden in the hover panel when Warcraft Logs
  has no logs for the applicant.
- Fixed hover-panel dungeon rows showing empty placeholder RIO/WCL badges when
  only one evidence source exists for that dungeon.
- Fixed the prepared QR payload growing huge when per-dungeon RaiderIO strings
  were packed into every screenshot. The paired addon now sends only compact
  live state and the companion enriches static RIO dungeon rows locally.
- Fixed one-level-under RaiderIO completion overriding mixed lower-key WCL logs
  into a `TOP` score for the hosted key.
- Fixed RaiderIO completion floors crossing the `TOP` threshold when the
  visible fit evidence is still below the hosted key level.
- Fixed hosted-dungeon `target-1` RaiderIO evidence tying target-level evidence
  in M+ ranking, which could put a broader but less relevant applicant above a
  cleaner fit for the listed key.
- Fixed malformed decoded QR payloads with trailing bytes after the CRC being
  accepted as valid snapshots.
- Fixed several screenshot, updater, WCL, and cache data-boundary edge cases
  that could otherwise leave stale UI state or unclear runtime errors.

### Compatibility

- Requires the ApplicantScout WoW addon `0.2.0`.
- Supports ApplicantScout wire payloads through v5, keeping RaiderIO dungeon
  rows as companion-side local enrichment instead of QR transport data.
- In-app updates require GitHub Release assets named
  `ApplicantScoutCompanionSetup-0.3.0.exe` and
  `ApplicantScoutCompanionSetup-0.3.0.exe.sha256`.

## 0.2.4 - 16-May-2026

Live Mythic+ context and Warcraft Logs retry hardening for applicant sorting.

### Improved

- Mythic+ applicant sorting now keeps using visible M+ log evidence when the
  game exposes the hosted listing as generic `Mythic+` instead of a concrete
  dungeon/key context.
- Generic Mythic+ fallback sorting now prioritizes the highest completed key
  level before parse percentile, so a small low-key spike no longer beats
  stronger high-key evidence.
- Temporary Warcraft Logs server errors now pause API calls briefly instead of
  hammering WCL and leaving applicants stuck.
- Warcraft Logs network timeouts are retryable from the overlay retry loop.

### Fixed

- Fixed hosted Mythic+ listings sometimes arriving at the companion as `+0`,
  which could make scoring and sorting fall back to the wrong signal.
- Fixed WCL HTTP 5xx responses and read timeouts sometimes leaving applicants
  with permanent `?` rows until the listing refreshed.

### Compatibility

- Requires the ApplicantScout WoW addon `0.1.6`.
- Supports ApplicantScout wire payloads through v4.
- In-app updates require GitHub Release assets named
  `ApplicantScoutCompanionSetup-0.2.4.exe` and
  `ApplicantScoutCompanionSetup-0.2.4.exe.sha256`.

## 0.2.3 - 15-May-2026

Screenshot transport and update-flow hardening for live applicant sessions.

### Improved

- Marker-bearing screenshot decode failures now surface in the overlay footer
  as `shot failed`, with the screenshot path and parse/CRC reason in the
  tooltip.
- Settings and tray update actions now share an update-in-progress state, so
  repeated clicks cannot start duplicate installer/download workers.

### Fixed

- Fixed corrupt ApplicantScout QR screenshots being deleted after a parse
  failure without any visible companion feedback.
- Fixed stale screenshot watcher signals after changing the Screenshots folder;
  old-path snapshots and clears are ignored once a replacement watcher is
  active.
- Fixed no-system-tray sessions so the companion does not disable
  last-window-close quitting when there is no tray control surface.
- Fixed pending overlay geometry saves being lost on tray/control quit paths
  immediately after moving or resizing the window.

### Compatibility

- Requires the ApplicantScout WoW addon `0.1.5`.
- Supports ApplicantScout wire payloads through v4.
- In-app updates require GitHub Release assets named
  `ApplicantScoutCompanionSetup-0.2.3.exe` and
  `ApplicantScoutCompanionSetup-0.2.3.exe.sha256`.

## 0.2.2 - 15-May-2026

Mythic+ fit scoring is now more honest about weak logs, sparse evidence, and
low-key farm parses, with a smoother in-app update prompt.

### Changed

- Reworked Mythic+ fit scoring to prioritize relevant Warcraft Logs bracket
  performance instead of letting key level, broad coverage, or RaiderIO carry
  weak logs into a good-looking score.
- Key level, same-dungeon evidence, profile consistency, and RaiderIO still
  contribute, but RaiderIO is now only a small nudge when WCL evidence is
  already decent or a capped fallback when WCL data is missing.
- Sparse WCL coverage is now treated as weaker evidence instead of a score
  bonus, and poor extra dungeon logs no longer reduce the sparse-evidence
  penalty.
- Low-key farm parses are capped so they cannot distort fit for much higher
  hosted keys; very high-key evidence still helps but low parses at high keys
  are bounded.
- Fit labels now line up with the visible WCL-style score bands: `RISK` below
  50, `OK` from 50, `FIT` from 70, and `TOP` from 85.

### Improved

- The update install action now uses a clearer title-bar download icon.
- When the companion starts with WoW and finds an installable update, Settings
  can open with a direct update prompt instead of staying silently hidden.
- Self-updates now pass update context into the installer so an update launched
  from the open companion can close the old process and relaunch into the
  visible Settings flow.

### Fixed

- Fixed all-grey or mostly-grey Mythic+ profiles appearing as blue or overly
  positive fit scores.
- Fixed high current or main RaiderIO from rescuing weak WCL evidence into an
  inflated Mythic+ fit.
- Fixed mixed-bracket cases where old low-key logs could beat relevant hosted
  key evidence.
- Fixed the old visual contradiction where a blue-range numeric score could
  still be labelled `RISK`.

### Compatibility

- Requires the ApplicantScout WoW addon `0.1.4`.
- Supports ApplicantScout wire payloads through v4.
- In-app updates require GitHub Release assets named
  `ApplicantScoutCompanionSetup-0.2.2.exe` and
  `ApplicantScoutCompanionSetup-0.2.2.exe.sha256`.

## 0.2.1 - 15-May-2026

Small companion polish release for supportability.

### Improved

- Settings and first-run windows now show the running ApplicantScout Companion
  version in the title bar, so screenshots and support reports include the
  exact installed version.

### Compatibility

- Requires the ApplicantScout WoW addon `0.1.3`.
- Supports ApplicantScout wire payloads through v4.
- In-app updates require GitHub Release assets named
  `ApplicantScoutCompanionSetup-0.2.1.exe` and
  `ApplicantScoutCompanionSetup-0.2.1.exe.sha256`.

## 0.2.0 - 15-May-2026

Overlay polish, safer updates, and clearer release compatibility for
ApplicantScout addon `0.1.3`.

### Added

- Added a small Ko-fi support heart in Settings.
- Added a compact blue update install icon that appears only when an installable
  companion release is available.

### Improved

- First-run Warcraft Logs data scope now defaults to Mythic+ only.
- Settings actions are cleaner: secondary actions moved into the footer/menu,
  the window close button hides to tray, and full quit lives in the tray menu.
- Update and support controls now use compact color-icon styling instead of
  large footer buttons.
- Mythic+ percentile colors now match Warcraft Logs buckets.
- Group applicant rows now separate package score from individual member fit so
  group and member performance are not visually mixed.
- Group Mythic+ cells use a compact centered package lane.

### Fixed

- Fixed the updater repository target so checks query the public companion repo.
- Fixed update availability so Settings shows the install icon only when the
  GitHub Release includes a complete installer plus checksum pair.
- Fixed updater error handling so installer/download failures remain visible.
- Hardened update install boundaries to avoid duplicate or unsafe installer runs.
- Hardened WCL runtime cache, quota handling, and stale worker lifecycle edges.
- Hardened group scoring and update edge cases.
- Fixed Test WCL status so a successful credential check stays visible as
  `WCL credentials are valid.` instead of being overwritten by `Saved.`

### Compatibility

- Requires the ApplicantScout WoW addon `0.1.3`.
- Supports ApplicantScout wire payloads through v4.
- In-app updates require GitHub Release assets named
  `ApplicantScoutCompanionSetup-0.2.0.exe` and
  `ApplicantScoutCompanionSetup-0.2.0.exe.sha256`.

## 0.1.0 - 10-May-2026

Initial Windows companion build for ApplicantScout addon `0.1.0`.

### Added

- QR screenshot watcher for ApplicantScout `APS1` payloads through wire v4.
- Warcraft Logs raid and Mythic+ percentile overlay.
- RaiderIO `current [main]` display support when the WoW addon includes main
  score data.
- Settings dialog for WCL credentials, screenshots folder, data scope, logs,
  cache cleanup, and update checks.
- Windows portable ZIP and Inno Setup installer artifacts.
- In-app installer updates with matching `.exe.sha256` checksum verification.
- Devourer Demon Hunter spec display and WCL Mythic+ filtering.
- Local startup helper guidance for fresh source checkouts.
- Optional Start and stop with WoW lifecycle integration.

### Improved

- Settings and startup now reject configured Screenshots folders that do not
  look like the active WoW `_retail_\Screenshots` path.
- Settings now save automatically, can be hidden to the system tray, and have
  an explicit Quit ApplicantScout action for fully closing the companion.
- Normal launches now avoid starting a second companion watcher when another
  instance is already running.
- Enabling Start and stop with WoW now starts the watcher immediately for the
  current Windows session, not only after the next sign-in.
- Installer shutdown handling now targets the installed per-user companion
  process instead of every process named `ApplicantScout.exe`.

### Compatibility

- Requires the ApplicantScout WoW addon `0.1.0`.
- Public companion releases are published from
  `Antrakt92/ApplicantScout-Companion`.
- Windows installer builds install under
  `%LOCALAPPDATA%\Programs\ApplicantScout Companion`; unsigned builds may still
  trigger Windows trust prompts until a code-signing path is chosen.
