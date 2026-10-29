# Copy Glossary — ApplicantScout Companion

Use these terms for UI labels and documentation. Keep names consistent with
the controls players see.

| Use | Do not use |
|---|---|
| applicant | candidate |
| companion | app |
| overlay | table, panel (in outward copy; code identifiers are unaffected) |
| update | upgrade |
| install (action) | — |
| Warcraft Logs, abbreviated WCL in tight UI | — |
| RaiderIO | — |
| Screenshots folder | — |
| iLvl, item-level | — |
| M+ DPS | — |
| "…" (single ellipsis character) | "..." (three ASCII periods) |

Notes:

- Missing data renders as the single token `—`, centralized in
  `src/applicant_scout/ui_text.py` as `MISSING_DATA_TEXT`.
- `src/applicant_scout/ui_text.py` is the single home for shared
  user-facing strings (`format_age`, `format_duration`,
  `format_percent`, updater progress messages). The companion does not yet have Qt translation support.
