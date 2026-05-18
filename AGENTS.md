# Project Rules - ApplicantScout Companion

Global Codex rules apply. This repo is a Windows desktop companion for a WoW
addon, with local OAuth credentials, filesystem watchers, a PyQt overlay, and a
public Windows release pipeline.

## Session Start

1. Read `README.md` for current user behavior, install/update flow, and local
   data locations.
2. Read `RELEASE_CHECKLIST.md` before version bumps, build artifacts, tags, or
   GitHub Release work.
3. Check `git status --short --branch` before edits; this repo may be left on a
   feature branch from previous agent work.
4. If a change touches the wire payload with the WoW addon, inspect the paired
   `ApplicantScout-Addon` checkout and keep compatibility explicit.

## Project Profile

- Python 3.13 package under `src/applicant_scout`.
- PyQt6 desktop overlay and settings UI.
- Watches the active WoW retail `Screenshots` folder, decodes ApplicantScout
  `APS1` QR payloads, queries Warcraft Logs, and renders applicant metrics.
- Local config, OAuth tokens, cache, and logs live under `%LOCALAPPDATA%`.
- Release artifacts are Windows installer/portable builds with checksum-based
  in-app update behavior.

## Risk Areas

- Treat WCL Client ID/Secret, OAuth tokens, logs, character names, realm names,
  and local config files as private.
- Do not broaden screenshot access beyond validated WoW retail Screenshots
  folders without a product decision.
- Watcher, tray, settings-window lifecycle, and updater behavior are user-visible
  desktop lifecycle surfaces; preserve graceful shutdown and visible control
  surfaces.
- WCL quota scope is intentional: disabled metrics must not be requested.
- Release tags trigger gated publishing. Do not tag, publish, or create release
  assets unless explicitly requested and the checklist has passed.

## Verification

Install development dependencies with the pinned release constraints when
needed:

```powershell
.venv\Scripts\pip install -e .[dev] -c constraints-release.txt
```

Default checks:

```powershell
.venv\Scripts\python -m pytest
.\scripts\check.ps1
```

`scripts\check.ps1` also requires the paired addon checkout and Lua 5.1 syntax
checker. If those are unavailable, run the Python subset and report the skipped
part clearly.

For release-prep changes:

```powershell
.\scripts\check-release-version.ps1 -Tag vX.Y.Z
.\scripts\build-windows.ps1
```

Only run release build/tag steps when the user explicitly asks for release work.

## Implementation Guidance

- Prefer narrow, testable changes around QR decoding, WCL request shaping,
  cache behavior, settings persistence, and lifecycle transitions.
- Add regression tests for parser/cache/lifecycle bug fixes when a test surface
  exists.
- Keep local support guidance split correctly: companion issues here, in-game
  addon issues in `ApplicantScout-Addon`.

## Git

- Stage only files changed for the current task.
- Do not add AI co-author trailers.
- Do not push release tags or irreversible release actions without explicit
  confirmation.
