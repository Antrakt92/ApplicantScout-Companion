# ApplicantScout Companion Release Notes

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
