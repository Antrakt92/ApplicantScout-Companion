# ApplicantScout Companion Docs

This folder holds contributor-facing companion docs and generated visual
fixtures.

- User setup and current support links: `../README.md`.
- Installer and portable build notes: `../RELEASE_NOTES.md`.
- Overlay visual baseline: `visual/overlay-polish-fixture.png`. From the
  repository root, check it with
  `.\.venv\Scripts\python scripts\render_overlay_fixture.py --check`; refresh it
  with `.\.venv\Scripts\python scripts\render_overlay_fixture.py` only after an
  intentional overlay UI/layout change and a visual inspection.
- Manual WCL fetch helper: `../scripts/manual_wcl_fetch.py`.
- Seasonal WCL encounter helper:
  `../scripts/seasonal/get_mplus_encounter_ids.py`.
