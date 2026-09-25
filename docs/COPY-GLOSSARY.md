# Copy Glossary — ApplicantScout Companion

Approved user-facing terms. Use the left column everywhere: overlay,
dialogs, tooltips, docs, and release notes. The right column lists
banned variants that must not appear in outward copy.

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
  `format_percent`, updater progress messages). Phase 1 is plain
  centralization; there is no Qt translation infrastructure yet.
