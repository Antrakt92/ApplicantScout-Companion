# ApplicantScout Companion Release Notes

## 0.2.0 - 15-May-2026

Overlay polish, safer updates, and clearer release compatibility for
ApplicantScout addon `0.1.2`.

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

- Requires the ApplicantScout WoW addon `0.1.2`.
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
