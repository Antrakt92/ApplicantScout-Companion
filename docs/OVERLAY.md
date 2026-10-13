# Reading the overlay

The **Fit** column shows an estimate such as **~65** for the target key or
raid difficulty, including a combined rating for grouped applicants. **Normal**, **Heroic**,
**Mythic**, and **M+** show the player's WCL results in every context. Each
available parse keeps its percentile colour, independently of Fit. Raid and M+
results belong to the applying specialization. Raid healers use HPS; other raid
roles and every M+ role use DPS. Displayed percentiles round down so the number
stays in the same colour band as the underlying result.

Fit colours help you scan the rounded estimate: grey below 25, green from 25,
blue from 50, purple from 75, orange from 95, pink from 99 and gold at 100.
The colour follows the displayed individual or group value. The `~` identifies
an estimate, not an overall skill rating, WCL percentile or success probability.
Unknown Fit shows a dash; raid Fit still loading shows an ellipsis. An observed
zero remains a numeric estimate.
A grouped application shows `partial` or `unknown` when any member lacks
an estimate, while each available individual result remains visible.

Drag the lower-right corner to resize the window. A wide window stops shrinking
before its visible columns are squeezed below their content width. Hidden
columns reduce the space required. Saved compact layouts and small screens
retain horizontal scrolling so the last column remains readable.

Click any column heading to sort; click it again to reverse the order. Numbers
start highest first; names and specs start alphabetically. Specs with the same
label are separated by class. The arrow shows the
current order. Applicants and Party remember separate choices for the session,
including when data refreshes. Missing values stay at the bottom in either
direction. Parse columns sort by the best percentile, the first number in the
pair. RIO uses the higher character or main score when available.

Joint applications stay together. Numeric columns use the lowest member value;
Fit uses the combined group estimate. Text columns use the alphabetically first
member value. If a sorted column is hidden in Settings, the default order applies
until that column is shown again.

Before a heading is clicked, applications with a complete target estimate sort
by Fit. Factual dungeon or raid evidence breaks ties. Incomplete applications
follow complete estimates. Click a WCL or RIO heading to compare that metric
instead; no-log players are not automatically demoted below every logged parse.
Joint applications stay together.

The table's parse pairs read **best / median**. Raid values are WCL's performance
averages across encounters. A missing median stays missing; it does not prove
there was only one log. With a known M+ target, the M+ summary averages the same
logged brackets shown in the dungeon details. One bracket is selected per
dungeon, within two levels of the target when available; otherwise another
logged bracket is used. The suffix shows their actual levels, such as
**+8–12**, rather than implying that every result came from one key level.
Its median is shown only when every included dungeon has a repeated sample
and a known median. A missing median does not establish inconsistency.
Without an M+ target, the summary retains each dungeon's highest logged bracket;
the suffix then identifies the maximum logged key. Hover the M+ cell for its
selection context. These averages describe the selected logs, not an overall
player percentile. Dungeon columns separate the best timed key, the selected
logged key, and the DPS parse. Hover Fit for the evidence behind its estimate.

In the boss details, **N / H / M** mean Normal / Heroic / Mythic. **H×2** means
two Heroic kills recorded by RaiderIO. The **Parse** header tooltip identifies
boss pairs as **overall / item level**, comparing the result with all matching logs and with
the matching item-level bracket. Boss details load only when requested.

**M+ Fit estimates in-time key experience for the target, for every role.**
It uses only the applying character's verified RaiderIO runs completed within
the timer: dungeon levels, breadth, experience above the target and the target
dungeon. Anonymous timed summaries establish conservative level bounds at the
captured target level, without counting the same dungeon twice. Those recorded
facts can be re-evaluated when the target changes. The bounds are not displayed
as an exact best key. A fresh compact summary can change the precision of those
bounds; named dungeon history provides more detail. Untimed completions never
contribute. General character or main rating alone does not produce Fit; if
timed key history is unavailable, Fit stays unknown.

The current M+ components total at most 89, or 86 without a target dungeon.
Blue and purple identify stronger target experience on this scale; the bands
at 95 and above are available to raid estimates.

WCL damage percentiles and logged key levels do not change M+ Fit. A WCL log
alone does not prove the timer was beaten. Raw results remain visible in the
M+ column and details. An in-time run is a team result and does not prove
individual contribution, current-role mastery or future success.

**Raid Fit estimates output for the selected difficulty and applying spec.**
The estimate uses best and median performance averages. A discounted result from
another difficulty can support the target without being discarded by a smaller
exact-difficulty result. M+ damage and RIO rating do not modify raid Fit.
Available recorded boss coverage and explicit kill counts are shown separately;
unknown sample sizes remain unknown. Logged coverage refers to the WCL zone,
which may contain more than one raid, and does not certify a specific boss.

Group Fit combines available members conservatively. It is shown only when every
member has an estimate. Privacy, lookup failures and absent logs do not subtract
player-quality points. Larger groups do not earn a bonus simply for applying
together. Inspect the members, roles and missing data before deciding.

Fit tooltips show its sources, observed coverage, samples and limitations. Damage,
healing throughput, mechanics, survival, interrupts, composition and communication
are different evidence; these estimates do not replace your judgement.

The RIO column shows the applying character's current score. If the RaiderIO
addon is installed in WoW and exposes a higher current-season main score for an
alt, the overlay can display `current [main]` and use the stronger context for
manual RIO sorting. Its colour follows a dated, bundled RaiderIO seasonal
gradient, rather than fixed rarity thresholds. RaiderIO dungeon summaries and highest timed keys
also feed the M+ scorecard and hover/detail context when local RaiderIO data is
available. When that local database has a past-season score, the RIO cell
tooltip and player detail show its season and approximate score. A main's
past-season score is labelled `main`; warband history is labelled `warband`.
The RIO column shows the highest available past-season character, main or warband score
with its season beneath the current score, hiding it when it is lower than
the applying character's current score and keeping
each row to at most two lines, for example `3064 [3568]` above `S1 4024`.
The detail card also shows
separate history badges. Missing values are omitted
from both views; historical scores do not change Fit or
sorting.

Party view can use the current group leader's keystone as the automatic Mythic+
target key. A manual Party key override still takes priority, raid contexts
ignore leader-key calibration. Changing the target key recalculates Fit and
selects the M+ damage brackets shown in the summary and dungeon details.

For raid groups, Party uses the raid difficulty selected in WoW: Normal,
Heroic, or Mythic. Inside a raid, the instance's actual difficulty takes
priority. Fit works without a Group Finder listing and updates when the
difficulty changes. Raid groups with fewer than six players also qualify.
Unsupported or unavailable difficulty leaves Fit empty.

Applicants and Party stay on the tab you are viewing when applicants arrive,
the group changes, or a listing closes. The first data received can select Party
when there is no listing or applicant data. After that, use the tabs to switch.


[Back to ApplicantScout Companion](../README.md)
