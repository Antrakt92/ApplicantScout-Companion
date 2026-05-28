# ApplicantScout Companion Docs

This folder holds contributor-facing companion docs and generated visual
fixtures.

- User setup and current support links: `../README.md`.
- Installer and portable build notes: `../RELEASE_NOTES.md`.
- Overlay visual baselines: `visual/overlay-polish-fixture*.png`. From the
  repository root, check all scenarios with
  `.\.venv\Scripts\python scripts\render_overlay_fixture.py --check --all`;
  refresh them with `.\.venv\Scripts\python scripts\render_overlay_fixture.py --all`
  only after an intentional overlay UI/layout change and a visual inspection.
- Settings dialog visual baselines: `visual/settings-dialog-fixture*.png`.
  From the repository root, check all scenarios with
  `.\.venv\Scripts\python scripts\render_settings_dialog_fixture.py --check --all`;
  refresh them with
  `.\.venv\Scripts\python scripts\render_settings_dialog_fixture.py --all`
  only after an intentional settings/setup UI layout change and a visual
  inspection.
  CI/release uses `.\scripts\check.ps1 -VisualMode Smoke` to render every
  scenario without treating GitHub-hosted Windows raster drift as a committed
  baseline update.
- Manual WCL fetch helper: `../scripts/manual_wcl_fetch.py`.
- Seasonal WCL encounter helper:
  `../scripts/seasonal/get_mplus_encounter_ids.py`.
- Seasonal LFG activity ID helper:
  `../scripts/seasonal/get_mplus_activity_ids.py`.
