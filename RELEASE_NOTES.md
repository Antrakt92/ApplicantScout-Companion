# ApplicantScout Companion Release Notes

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
