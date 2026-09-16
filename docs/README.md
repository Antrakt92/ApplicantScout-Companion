# ApplicantScout Companion Docs

This folder holds setup instructions, privacy information, contributor docs
and generated visual fixtures.

- [Overview and support](../README.md).
- [Contributor setup and Windows builds](../CONTRIBUTING.md).
- [Overlay columns and estimates](OVERLAY.md).
- [Settings, troubleshooting and protocol reference](REFERENCE.md).
- [Step-by-step setup](GETTING_STARTED.md).
- [Optional usage statistics and privacy](PRIVACY.md).
- [Release history](../RELEASE_NOTES.md) and [release checklist](../RELEASE_CHECKLIST.md).
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
- Strict local baseline checks are required before release prep or public media
  refreshes. CI/release smoke uses `.\scripts\check.ps1 -VisualMode Smoke` to
  render every scenario without treating GitHub-hosted Windows raster drift as a
  committed baseline update.
- Public addon media exports are generated from anonymized overlay baselines.
  Check them with
  `.\.venv\Scripts\python scripts\export_public_visual_assets.py --addon-root ..\ApplicantScout-Addon --check`;
  refresh them with the same command without `--check` after intentional
  baseline changes and visual inspection.
- Manual WCL fetch helper: [manual_wcl_fetch.py](../scripts/manual_wcl_fetch.py).
- Seasonal WCL verification and encounter tuple helper:
  [verify_wcl_season.py](../scripts/seasonal/verify_wcl_season.py). Live calls require
  `--confirm-spend-wcl-quota`; add `--print-mplus-tuples` when refreshing
  `MPLUS_ENCOUNTERS`.
- Seasonal LFG activity ID helper:
  [get_mplus_activity_ids.py](../scripts/seasonal/get_mplus_activity_ids.py).
- Seasonal M+ challenge-map helper:
  [get_mplus_challenge_map_ids.py](../scripts/seasonal/get_mplus_challenge_map_ids.py).
