# ApplicantScout Companion Release Notes

## 0.3.0 - 16-May-2026

RaiderIO completion-aware Mythic+ scoring, local RaiderIO per-dungeon evidence
next to Warcraft Logs rows, plus screenshot startup, settings launch, WCL
configuration, and release-readiness hardening for live applicant scouting.

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
- Fixed localized RaiderIO dungeon names splitting target-dungeon RIO and WCL
  evidence into separate hover-panel rows.
- Fixed RIO-backed fit badges being hidden in the hover panel when Warcraft Logs
  has no logs for the applicant.
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
