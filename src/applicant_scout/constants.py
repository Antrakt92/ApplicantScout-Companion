"""Static lookups: WCL zones, class colours, spec ID → short name."""

from __future__ import annotations


# Verified via Test B 2026-04-29 (https://www.warcraftlogs.com/zones).
# Update once per season. M+ zones change every 3 months, raid zones every ~6.
CURRENT_MPLUS_ZONE_ID = 47  # Midnight Season 1
CURRENT_RAID_ZONE_ID = 46  # VS / DR / MQD
SEASON_NAME = "Midnight Season 1"

CURRENT_RAID_ENCOUNTERS: list[tuple[str, int, str]] = [
    ("ia", 3176, "Imperator Averzian"),
    ("vo", 3177, "Vorasius"),
    ("fk", 3179, "Fallen-King Salhadaar"),
    ("ve", 3178, "Vaelgor & Ezzorak"),
    ("lv", 3180, "Lightblinded Vanguard"),
    ("cc", 3181, "Crown of the Cosmos"),
    ("cu", 3306, "Chimaerus"),
    ("ba", 3182, "Belo'ren"),
    ("mf", 3183, "Midnight Falls"),
]


# WoW class file tokens (locale-stable English) → hex colours from Blizzard's RAID_CLASS_COLORS.
CLASS_COLOURS: dict[str, str] = {
    "DEATHKNIGHT": "#C41E3A",
    "DEMONHUNTER": "#A330C9",
    "DRUID": "#FF7C0A",
    "EVOKER": "#33937F",
    "HUNTER": "#AAD372",
    "MAGE": "#3FC7EB",
    "MONK": "#00FF98",
    "PALADIN": "#F48CBA",
    "PRIEST": "#FFFFFF",
    "ROGUE": "#FFF468",
    "SHAMAN": "#0070DD",
    "WARLOCK": "#8788EE",
    "WARRIOR": "#C69B6D",
}


# Spec ID → compact spec name. Class is already encoded by the table cell
# background, so duplicate spec names like Holy/Prot/Resto intentionally stay
# class-neutral to keep the column focused on "which spec to inspect".
# IDs from https://wago.tools/db2/ChrSpecialization (verified for Midnight 12.x).
SPEC_SHORT_NAMES: dict[int, str] = {
    # Death Knight
    250: "Blood",
    251: "Frost",
    252: "Unholy",
    # Demon Hunter
    577: "Havoc",
    581: "Veng",
    1480: "Devour",
    # Druid
    102: "Boomy",
    103: "Feral",
    104: "Guardian",
    105: "Resto",
    # Evoker
    1467: "Devast",
    1468: "Preserv",
    1473: "Augment",
    # Hunter
    253: "BM",
    254: "MM",
    255: "SV",
    # Mage
    62: "Arcane",
    63: "Fire",
    64: "Frost",
    # Monk
    268: "Brm",
    269: "Wind",
    270: "Mist",
    # Paladin
    65: "Holy",
    66: "Prot",
    70: "Ret",
    # Priest
    256: "Disc",
    257: "Holy",
    258: "Shadow",
    # Rogue
    259: "Assa",
    260: "Outlaw",
    261: "Sub",
    # Shaman
    262: "Ele",
    263: "Enh",
    264: "Resto",
    # Warlock
    265: "Aff",
    266: "Demo",
    267: "Destro",
    # Warrior
    71: "Arms",
    72: "Fury",
    73: "Prot",
}


# Percentile bracket → background colour (matching WCL ranking colours).
PERCENTILE_BUCKETS: list[tuple[int, str]] = [
    (100, "#e5cc80"),  # tan / rank 1
    (99, "#e268a8"),  # pink
    (95, "#ff8000"),  # orange
    (75, "#a335ee"),  # purple
    (50, "#0070ff"),  # blue
    (25, "#1eff00"),  # green
    (0, "#666666"),  # grey
]


def percentile_colour(value: float | None) -> str:
    """Returns the bracket colour for a percentile value, or grey for None."""
    if value is None:
        return "#5d5d5d"
    for threshold, colour in PERCENTILE_BUCKETS:
        if value >= threshold:
            return colour
    return "#5d5d5d"


# RaiderIO M+ score → tier colour. Mirrors the parse-tier palette used for raid
# percentile cells so the overlay reads consistently — same gold/purple/blue/
# green/white visual language. Thresholds are mid-Midnight-S1 approximations
# (3200+ ≈ top ~1-2% of M+ raters, 2700+ ≈ top ~10%, 2200+ ≈ top ~25%, 1700+
# ≈ top ~50%); revisit late-season when rating creep shifts the distribution.
RIO_SCORE_BUCKETS: list[tuple[int, str]] = [
    (3200, "#e5cc80"),  # gold (legendary)
    (2700, "#a335ee"),  # purple (epic)
    (2200, "#0070dd"),  # blue (rare)
    (1700, "#1eff00"),  # green (uncommon)
    (0, "#ffffff"),  # white (anyone with a score)
]


def rio_score_colour(score: int) -> str:
    """Returns the RIO tier colour for the overlay's effective RIO score.

    score=0 / missing → "—" cell shows in dim grey via the regular missing-data
    path; non-zero scores route here for the tier band.
    """
    if not score:
        return "#5d5d5d"
    for threshold, colour in RIO_SCORE_BUCKETS:
        if score >= threshold:
            return colour
    return "#5d5d5d"


def group_id_colour(raw_aid: str) -> str:
    """Per-group band colour for multi-member group app rows in the overlay.
    Knuth multiplicative hash on the int aid → HSV hue, fixed S+V kept muted
    to avoid competing with class colours / RIO tier bands. Returns '#rrggbb'
    lowercase.

    Used by the overlay's row delegate to paint a 2px coloured chip at the
    leftmost edge of multi-member group rows so the host can see at a glance
    which rows apply together. Solo applicants don't get a band — chrome is
    reserved for actual grouping signal.

    PyQt6 import is lazy so constants.py stays Qt-import-free at module load
    (cheap to import for non-overlay consumers like CLI dump or pure-data tests)."""
    from PyQt6.QtGui import QColor

    try:
        n = int(raw_aid)
    except ValueError:
        n = sum(ord(c) for c in raw_aid) if raw_aid else 0
    # [B-7] Mask to 32-bit before mod-360. Python ints are arbitrary precision;
    # without the mask very large aids produce inputs to %360 that don't behave
    # like the classical 32-bit Knuth hash.
    hue = ((n * 2654435761) & 0xFFFFFFFF) % 360
    return QColor.fromHsv(hue, 200, 220).name()


# WoW region ID (from GetCurrentRegion()) → WCL serverRegion string.
REGION_ID_TO_WCL: dict[int, str] = {
    1: "US",
    2: "KR",
    3: "EU",
    4: "TW",
    5: "CN",
}


# WoW retail classID 1-13 → file token. Mirrors LOCALIZED_CLASS_NAMES_MALE keys.
# Used by screenshot-transport StateMachine to translate addon's class_id byte
# back to the locale-stable token Applicant.cls expects.
CLASS_ID_TO_NAME: dict[int, str] = {
    0: "?",
    1: "WARRIOR",
    2: "PALADIN",
    3: "HUNTER",
    4: "ROGUE",
    5: "PRIEST",
    6: "DEATHKNIGHT",
    7: "SHAMAN",
    8: "MAGE",
    9: "WARLOCK",
    10: "MONK",
    11: "DRUID",
    12: "DEMONHUNTER",
    13: "EVOKER",
}


# Role byte (0=tank, 1=healer, 2=damager, 3=unknown→damager fallback) →
# Applicant.role string token expected by overlay's role-icon mapping.
ROLE_BYTE_TO_NAME: dict[int, str] = {
    0: "TANK",
    1: "HEALER",
    2: "DAMAGER",
    3: "DAMAGER",
}


# Role visual identity — pill colour + label. PNG role icons are the primary
# visual treatment in the overlay; glyphs remain only as a no-asset fallback.
# Colours chosen distinct from the saturated class colours (CLASS_COLOURS)
# and the percentile palette (gold/purple/blue/green/grey) so adjacent
# pills don't visually merge in the panel.
ROLE_COLOURS: dict[str, str] = {
    "TANK": "#3a6fb0",
    "HEALER": "#2f9450",
    "DAMAGER": "#b04545",
}

ROLE_GLYPHS: dict[str, str] = {
    "TANK": "🛡",
    "HEALER": "✚",
    "DAMAGER": "⚔",
}

ROLE_LABELS: dict[str, str] = {
    "TANK": "TANK",
    "HEALER": "HEAL",
    "DAMAGER": "DPS",
}

# All-3-selected on the RoleFilterBar is semantically equivalent to "no
# filter" for count-display purposes (title shows just `(20)` not `(20/20)`).
ALL_ROLES: frozenset[str] = frozenset(ROLE_LABELS.keys())


# Raid metric per role — single value per cell (no DPS/HPS pair like M+).
# Tanks ranked by their damage in WCL (no separate tank metric); healers by
# healing; everyone else by damage. Explicit to avoid any reliance on WCL's
# undocumented default-metric behavior.
ROLE_TO_RAID_METRIC: dict[str, str] = {
    "TANK": "dps",
    "DAMAGER": "dps",
    "HEALER": "hps",
}


# WCL encounter IDs for current Midnight S1 M+ dungeons. Used to build
# 8 aliased `encounterRankings(encounterID: ...)` calls in one query — needed
# because zoneRankings only gives aggregate best/median across ALL bracket
# levels (so a player who pushes +20 gets 99% inflated by +5 farm runs).
# Per-encounter querying gives per-RUN data, letting us filter to the
# applicant's HIGHEST timed key per dungeon — what raid leads actually care
# about when scouting for high-key push.
# Aliases are 2-letter GraphQL field aliases (kept short for readable query).
# Tuple order = stable display order in tooltip.
MPLUS_ENCOUNTERS: list[tuple[str, int, str]] = [
    # (alias, encounter_id, display_name)
    ("aa", 112526, "Algeth'ar Academy"),
    ("mt", 12811, "Magisters' Terrace"),
    ("mc", 12874, "Maisara Caverns"),
    ("np", 12915, "Nexus-Point Xenas"),
    ("ps", 10658, "Pit of Saron"),
    ("st", 361753, "Seat of the Triumvirate"),
    ("sr", 61209, "Skyreach"),
    ("ws", 12805, "Windrunner Spire"),
]

# WoW LFG activity IDs for the current season's Mythic+ listings. The addon
# emits the raw activityID from C_LFGList; using it as a companion-side fallback
# keeps same-dungeon scoring and target-row ordering stable on localized clients
# where listing.dungeon_name is not the English WCL display name.
MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME: dict[int, str] = {
    115: "Pit of Saron",
    131: "Pit of Saron",
    1769: "Pit of Saron",
    1770: "Pit of Saron",
    24: "Skyreach",
    32: "Skyreach",
    182: "Skyreach",
    404: "Skyreach",
    484: "Seat of the Triumvirate",
    485: "Seat of the Triumvirate",
    486: "Seat of the Triumvirate",
    1622: "Seat of the Triumvirate",
    1644: "Seat of the Triumvirate",
    1157: "Algeth'ar Academy",
    1158: "Algeth'ar Academy",
    1159: "Algeth'ar Academy",
    1160: "Algeth'ar Academy",
    1539: "Windrunner Spire",
    1540: "Windrunner Spire",
    1541: "Windrunner Spire",
    1542: "Windrunner Spire",
    1757: "Magisters' Terrace",
    1758: "Magisters' Terrace",
    1759: "Magisters' Terrace",
    1760: "Magisters' Terrace",
    1761: "Maisara Caverns",
    1762: "Maisara Caverns",
    1763: "Maisara Caverns",
    1764: "Maisara Caverns",
    1765: "Nexus-Point Xenas",
    1766: "Nexus-Point Xenas",
    1767: "Nexus-Point Xenas",
    1768: "Nexus-Point Xenas",
}


def mplus_dungeon_name_for_activity_id(activity_id: object) -> str:
    if isinstance(activity_id, bool):
        return ""
    if isinstance(activity_id, int):
        clean = activity_id
    elif isinstance(activity_id, str):
        try:
            clean = int(activity_id)
        except ValueError:
            return ""
    else:
        return ""
    return MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME.get(clean, "")


# Spec ID → spec NAME (no class qualifier) as returned by WCL in encounterRankings
# `ranks[].spec` fields. Used to filter per-run results to applicant's
# current spec — example proved spec-filtering critical: same character as
# Blood DK at +15 → 82% avg, as Unholy at +15 → 7% avg. Class+spec uniquely
# identifies, so within ONE applicant query (a single character), spec name
# alone is unambiguous.
# WCL formats vary ("Beast Mastery" w/ space, "Brewmaster" single word).
# wcl._spec_norm lowercases + strips spaces before matching to handle both.
SPEC_ID_TO_WCL_NAME: dict[int, str] = {
    250: "Blood",
    251: "Frost",
    252: "Unholy",
    577: "Havoc",
    581: "Vengeance",
    1480: "Devourer",
    102: "Balance",
    103: "Feral",
    104: "Guardian",
    105: "Restoration",
    1467: "Devastation",
    1468: "Preservation",
    1473: "Augmentation",
    253: "Beast Mastery",
    254: "Marksmanship",
    255: "Survival",
    62: "Arcane",
    63: "Fire",
    64: "Frost",
    268: "Brewmaster",
    269: "Windwalker",
    270: "Mistweaver",
    65: "Holy",
    66: "Protection",
    70: "Retribution",
    256: "Discipline",
    257: "Holy",
    258: "Shadow",
    259: "Assassination",
    260: "Outlaw",
    261: "Subtlety",
    262: "Elemental",
    263: "Enhancement",
    264: "Restoration",
    265: "Affliction",
    266: "Demonology",
    267: "Destruction",
    71: "Arms",
    72: "Fury",
    73: "Protection",
}
