# Reading the overlay

The **Fit** column shows an estimate such as **~65** for the target key or
raid difficulty, including a combined rating for grouped applicants. **Normal**, **Heroic**,
**Mythic**, and **M+** show the player's WCL results in every context. Each
available parse keeps its percentile colour, independently of Fit. Raid and M+
results belong to the applying specialization. Raid healers use HPS; other raid
roles and every M+ role use DPS. Displayed percentiles round down so the number
stays in the same colour band as the underlying result.

Fit uses the same colour thresholds: grey below 25, green from 25, blue from 50,
purple from 75, orange from 95, pink from 99, and gold at 100. Its colour follows
the displayed estimate, including the combined estimate for grouped applicants.
Changing the target key updates both Fit and its colour. Missing Fit stays neutral.

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

Before a heading is clicked, applications sort by the M+ best percentile for a
dungeon listing, or by the selected raid difficulty's best percentile.
Grouped applicants stay together and use their lowest member percentile;
groups with missing parses follow those with complete results. Fit remains a
separate estimate and breaks ties or orders applicants without a parse.

The table's parse pairs read **best / median**. Raid values are WCL's performance
averages across encounters. A missing median stays missing; it does not prove
there was only one log. M+ summaries show the available numeric result; the
dungeon value tooltips identify single-run samples when there is no repeat-run median.
Dungeon columns separate the best completed key, the logged key, and the DPS
parse. Hover Fit or the group summary for the evidence behind the estimate.
The **+key** beside that summary is the highest key represented in those WCL
results, not necessarily the run that produced the best percentile.

In the boss details, **N / H / M** mean Normal / Heroic / Mythic. **H×2** means
two Heroic kills recorded by RaiderIO. The **Parse** header tooltip identifies
boss pairs as **overall / item level**, comparing the result with all matching logs and with
the matching item-level bracket. Boss details load only when requested.

**M+ Fit is an estimate of how the available evidence matches the target key,
not a success probability or a Warcraft Logs percentile.** Named dungeon
evidence from RaiderIO and WCL is combined once per dungeon; unnamed summaries
are treated conservatively when their overlap is unknown. Hover the Fit badge
for evidence strength, dungeon coverage and the limits of the estimate. Strong
evidence can support a low Fit when the completed keys are below the target.
M+ WCL values measure damage for every role, including tanks and healers; they
do not assess healing, survival, interrupts, or other utility. A low Fit is a
limit of the available evidence, not a verdict on the player's overall skill.

The RIO column shows the applying character's current score. If the RaiderIO
addon is installed in WoW and exposes a higher current-season main score for an
alt, the overlay can display `current [main]` and use the stronger context for
sorting fallback support. RaiderIO dungeon summaries and highest timed keys
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
ignore leader-key calibration. Changing the target key recalculates Fit; WCL
parse values and colours stay unchanged.

For raid groups, Party uses the raid difficulty selected in WoW: Normal,
Heroic, or Mythic. Inside a raid, the instance's actual difficulty takes
priority. Fit works without a Group Finder listing and updates when the
difficulty changes. Raid groups with fewer than six players also qualify.
Unsupported or unavailable difficulty leaves Fit empty.

Applicants and Party stay on the tab you are viewing when applicants arrive,
the group changes, or a listing closes. The first data received can select Party
when there is no listing or applicant data. After that, use the tabs to switch.


[Back to ApplicantScout Companion](../README.md)
