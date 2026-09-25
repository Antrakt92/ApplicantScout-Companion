# Settings and troubleshooting

[Setup guide](GETTING_STARTED.md) · [Reading the overlay](OVERLAY.md)

## How group data reaches the companion

WoW addons cannot query Warcraft Logs directly from inside the game client, so
the addon and companion share the work:

- The addon watches Blizzard UI state and emits compact `APS1` QR snapshots.
- WoW writes normal screenshots.
- The companion watches only the configured Screenshots folder, decodes
  ApplicantScout QR payloads, fetches WCL data, reads optional local RaiderIO
  data, and updates the overlay.

The QR frame appears only during the screenshot capture window so it stays out
of the way between snapshots.

QR transport pauses completely before LFG polling or payload/QR work during
combat, for the full active Mythic+ run, and during raid boss encounters. It
remains available out of combat in a raid, so you can keep recruiting between
pulls.

ApplicantScout temporarily raises screenshot quality and uses JPG format only
during each QR capture, then restores your prior screenshot settings after the
screenshot. `/apscout off` and the next `/reload` also restore an interrupted
capture.

## Trust And Local Data

**Optional usage statistics start enabled when no preference has been saved.**
Existing choices are preserved. Reports contain a random installation ID, version
and daily setup/use milestones. Names, screenshots and credentials are excluded.
Turn sharing off at any time in Settings. Participating packaged builds send
reports to the ApplicantScout service hosted on Cloudflare.
Read [what is shared and retained](PRIVACY.md).

ApplicantScout Companion does not ask for Blizzard credentials or account
access. It does not read WoW memory, inject code, automate gameplay, or send
chat messages for transport.

Local files:

- Config and WCL Client ID/Secret:
  `%LOCALAPPDATA%\applicant-scout\config\config.env`
- Optional usage preference and reporting ID:
  `%LOCALAPPDATA%\applicant-scout\config\usage.json`
- OAuth token cache and WCL character cache:
  `%LOCALAPPDATA%\applicant-scout\cache\`
- Decoded local RaiderIO lookup payload cache:
  `%LOCALAPPDATA%\applicant-scout\cache\raiderio-local`
- Logs:
  `%LOCALAPPDATA%\applicant-scout\logs\`

If the RaiderIO addon is installed, the companion can read local RaiderIO addon
database files under `_retail_\Interface\AddOns\RaiderIO\db` to enrich
score/progress context.

Before sharing support material publicly, redact `/apscout status` and
`/apscout status diag` output,
`/apscout taintcheck` output, companion logs, QR screenshots, manual decode
output, `config.env`, `token.json`, `character-cache.json`,
`last-live-snapshot.json`, and `screenshot-manual-index-v2-*.json`. Treat the
entire `%LOCALAPPDATA%\applicant-scout\config\` and
`%LOCALAPPDATA%\applicant-scout\cache\` directories as private; do not attach
either directory wholesale. These files can include WCL Client ID/Secret,
OAuth access token, character names, realm names, applicant/roster snapshots,
listing titles/comments, screenshots folder paths, absolute screenshot file
paths, keystone/listing metadata, and WCL/RaiderIO evidence.

QR screenshots may remain if the companion is absent, interrupted, pointed at
the wrong folder, or the Screenshots folder is synced/shared before cleanup.

Current Windows builds are unsigned. SmartScreen can warn on first install
and may show an unknown publisher. Download from the linked GitHub release
and proceed only if you trust the source.
The `.sha256` sidecar verifies file integrity, not publisher identity.

## Settings

Use the Settings button in the companion title bar to edit WCL credentials,
region fallback, screenshots path, WCL data scope, WoW lifecycle sync, cache,
or logs. Settings save automatically as you change them.

Mythic+, all raid difficulties and **Start and stop with WoW** start enabled
when their preferences are unset. Your saved choices are preserved. After setup,
WoW sync adds a background helper at Windows sign-in. It waits for WoW, then
opens the companion; the companion closes after WoW exits. Turn sync off for
manual launch and quit.

When the system tray is available, closing the settings window hides it back to
the tray; use the tray menu's **Quit ApplicantScout** action to close the
companion completely. If the system tray is unavailable, closing Settings quits
the companion so it cannot keep running without a visible control surface.

### Keyboard and assistive access

Choose **Show overlay** from the system tray to activate the overlay for
keyboard use. The compact in-game launcher restores the overlay in passive mode
without entering its keyboard focus chain.

Once activated, use `Tab` / `Shift+Tab` to move through Settings, Hide,
Applicants/Party, the manual key field, role filters, available detail actions,
and the applicant table. Buttons accept `Space` or `Enter`; the manual key field
and its step buttons accept keyboard input. In the table, `Up`, `Down`, `Home`,
`End`, and page keys move the visible row preview, `Enter` or `Space` pins it,
and `Escape` clears the pin. Hidden actions are skipped automatically. Launcher
drag, title-bar window drag, and the resize grip remain pointer-only geometry
controls; restoring, hiding, and reviewing applicant data have keyboard paths.

The overlay's maximum width follows its visible columns, text and font size,
within the current screen's available width. Narrower manual sizes are preserved;
oversized saved widths are reduced. Background updates do not shrink the window
while browsing the same view.
The detail card scrolls independently when the window is short, leaving the
table available. Badges wrap and raid detail values stack in narrow windows.

Developer/source runs may still use a repo-local `.env` when the local config
file does not exist. Environment variables override both files.

Optional `.env` / `config.env` values:

```env
APSCOUT_SCREENSHOTS_PATH=C:\Games\World of Warcraft\_retail_\Screenshots
APSCOUT_REGION=EU
APSCOUT_CACHE_TTL_SECONDS=43200
APSCOUT_FETCH_MPLUS=1
APSCOUT_FETCH_RAID_NORMAL=1
APSCOUT_FETCH_RAID_HEROIC=1
APSCOUT_FETCH_RAID_MYTHIC=1
APSCOUT_SYNC_WITH_WOW=1
```

`APSCOUT_SCREENSHOTS_PATH` must point at the selected WoW client's
`_retail_\Screenshots`, `_ptr_\Screenshots` or `_xptr_\Screenshots` folder.
Only one client is watched at a time; local RaiderIO data comes from that client.
`APSCOUT_FETCH_*` flags match the WCL data
checkboxes from Settings. Disabled metrics are not included in Warcraft Logs
API requests.

## Updates

The installer is accompanied by an `.exe.sha256` checksum file.

ApplicantScout Companion checks for updates hourly. When an installable stable
GitHub Release is available, Settings shows a blue download button. Clicking it
downloads the installer and verifies its `.sha256` checksum. Settings shows
checking, download progress, verification and installation stages. **Cancel**
stops the download before installer handoff; the update remains available to retry.

Current unsigned builds can still launch from the in-app updater after checksum
verification. The `.sha256` sidecar verifies file integrity; it does not prove
publisher identity. If the companion is running, the installer closes it and
relaunches it after the update. Portable ZIP artifacts are published for
manual/dev use but are not launched by the in-app updater.

Normal installs use the per-user directory
`%LOCALAPPDATA%\Programs\ApplicantScout Companion`, so routine installs and
updates should not require UAC elevation.

## In-Game Commands

The Group Finder panel stays focused on everyday applicant scouting, playstyle,
and Auto Hi controls. Advanced diagnostics and QR recovery remain available
through the slash commands below.

```text
/apscout on | off       enable/disable capture
/apscout toggle         flip enabled state
/apscout config         open/close settings panel
/apscout setup          show companion download and setup
/apscout status         show a short capture summary
/apscout status diag    show detailed QR diagnostics
/apscout playstyle [off|learning|relaxed|competitive|carry] set M+ default playstyle
/apscout reset          clear transport cache, queue fresh snapshot
/apscout shotnow        request snapshot while enabled; defers in combat/M+/boss fights
/apscout qrvisible      toggle persistent QR always-visible mode; off clears it
/apscout qrmove         toggle QR move mode (Alt+drag QR frame)
/apscout qrreset        reset QR frame position to top-left
/apscout taintcheck     probe C_LFGList field secret-tagging
/apscout debug [on|off] toggle debug logging
/apscout competitive [on|off] legacy alias for Competitive / Off
```

## Troubleshooting

- Companion starts but overlay stays empty: open Settings -> Open logs and
  confirm the `Screenshots:` line points at the active `_retail_\Screenshots`
  folder.
- Want the companion to follow your game session: enable
  `Start and stop with WoW` in Settings.
- The option is checked but startup does not work: check the Windows startup
  status shown below it. Click **Enable watcher** or **Repair watcher** if
  offered. If Windows refuses the change, enable **ApplicantScout Companion**
  in Windows Settings → Apps → Startup.
- Companion reports a screenshot setup error: open Settings and set the active
  `_retail_\Screenshots` folder. If `APSCOUT_SCREENSHOTS_PATH` is set as a
  process environment variable, correct or remove that override first.
- WoW side looks idle: run `/apscout status` and check that ApplicantScout is
  enabled while you are hosting a listing or reviewing Party view. Use
  `/apscout status diag` for detailed QR troubleshooting.
- Need a manual sync: keep ApplicantScout enabled and run `/apscout shotnow`;
  the request waits until combat, an active M+ run, or a boss encounter ends. If
  applicant state looks stale, run `/apscout reset` while transport is active.
- QR frame is in the way: run `/apscout qrmove`, Alt-drag it, then run the same
  command again to lock it. Use `/apscout qrreset` to restore the default
  position.
- WCL cells stay empty: open Settings and use Test WCL.
- Screenshot cleanup is marker-safe: the watcher deletes only screenshots that
  decode to an ApplicantScout `APS1` payload. Manual screenshots and unrelated
  QR screenshots are left alone. QR screenshots may remain if the companion is
  absent, interrupted, pointed at the wrong folder, or the Screenshots folder is
  synced/shared before cleanup.

## Version Compatibility

ApplicantScout Companion supports the latest published ApplicantScout WoW addon
release. This source tree supports ordinary logical APS1 snapshots through v9,
applicant-partial authority frames on v11, and bounded v10 overflow fragment
envelopes. Fragmented snapshots are applied only after exact reassembly of the
complete inner logical payload.

Roster flags also carry raid difficulty without adding bytes to the payload:
bit `0x10` marks observed context, and bits `0x0C` encode unknown (`0`), Normal
(`4`), Heroic (`8`), or Mythic (`12`). These bits apply only to raid members
(`0x02`). Older rows omit `0x10`; older companions ignore the added bits.

Decode a saved screenshot manually:

```powershell
.venv\Scripts\python -m applicant_scout.screenshot C:\path\to\WoWScrnShot.jpg
```

Check or remove saved ApplicantScout QR screenshots:

```powershell
applicant-scout cleanup-screenshots
applicant-scout cleanup-screenshots --delete
```

