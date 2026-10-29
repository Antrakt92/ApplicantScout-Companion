# ApplicantScout Companion documentation

[Back to the overview](../README.md) · [Download](https://github.com/Antrakt92/ApplicantScout-Companion/releases/latest) · [Get help](https://github.com/Antrakt92/ApplicantScout-Companion/issues)

## For players

| I want to… | Start here |
|---|---|
| Install the addon and companion, then connect Warcraft Logs | [Illustrated setup guide](GETTING_STARTED.md) |
| Understand columns, colours and Fit estimates | [Overlay guide](OVERLAY.md) |
| Change settings, use keyboard controls or troubleshoot a problem | [Reference and troubleshooting](REFERENCE.md) |
| Understand optional usage statistics | [Privacy](PRIVACY.md) |
| See what changed | [Release history](../RELEASE_NOTES.md) |

## For contributors

Start with [source setup, checks and Windows builds](../CONTRIBUTING.md).
Maintainers can use the [release checklist](../RELEASE_CHECKLIST.md).

### Visual checks and media

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

### Data and seasonal helpers

- Manual WCL fetch helper: [manual_wcl_fetch.py](../scripts/manual_wcl_fetch.py).
- Seasonal WCL verification and encounter tuple helper:
  [verify_wcl_season.py](../scripts/seasonal/verify_wcl_season.py). Live calls require
  `--confirm-spend-wcl-quota`; add `--print-mplus-tuples` when refreshing
  `MPLUS_ENCOUNTERS`.
- Seasonal LFG activity ID helper:
  [get_mplus_activity_ids.py](../scripts/seasonal/get_mplus_activity_ids.py).
- Seasonal M+ challenge-map helper:
  [get_mplus_challenge_map_ids.py](../scripts/seasonal/get_mplus_challenge_map_ids.py).

- Seasonal RaiderIO colour refresh: run
  `./scripts/seasonal/refresh-rio-score-tiers.ps1 -Season <season slug> -SeasonName "<season name>"`.
  Review the generated palette and update it together with the seasonal constants.
  Add `-Check` for a public, read-only comparison with the provider; it does not
  require Warcraft Logs credentials or spend WCL quota. The bundled palette is
  a dated snapshot, so it can change during a season.
