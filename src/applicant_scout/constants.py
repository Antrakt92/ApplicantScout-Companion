"""Static lookups: WCL zones, class colours, spec ID → short name."""

from __future__ import annotations


# Verified by scripts/seasonal/verify_wcl_season.py against the WCL GraphQL API.
# Re-run the quota-aware release gate whenever the active season changes.
CURRENT_MPLUS_ZONE_ID = 55  # Midnight Season 2
CURRENT_RAID_ZONE_ID = 53  # The Venomous Abyss + Tidebound Grotto
CURRENT_RAID_ENCOUNTER_ZONE_IDS: tuple[int, ...] = (CURRENT_RAID_ZONE_ID,)
SEASON_NAME = "Midnight Season 2"

# WCL exposes the eight-boss Venomous Abyss raid and Nymrissa Wavecaller from
# the Tidebound Grotto under one seasonal raid zone. Keep the main raid first
# and the separate lair encounter last in the detail panel.
CURRENT_RAID_ENCOUNTERS: list[tuple[str, int, str]] = [
    ("nz", 3470, "Nek'zali the Soulcoiler"),
    ("es", 3445, "Entombed Sentinels"),
    ("vm", 3455, "Vashnik the Malignant"),
    ("le", 3497, "The Lost Explorers"),
    ("ss", 3420, "Sszorak"),
    ("tf", 3421, "The Twin Fangs"),
    ("ca", 3429, "The Coiled Altar"),
    ("ut", 3492, "Ula'tek"),
    ("nw", 3379, "Nymrissa Wavecaller"),
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


# Raid metric per role. Tanks are ranked by their damage in WCL (no separate
# tank metric), healers by healing, and everyone else by damage. M+ separately
# uses DPS for every role. Keep this explicit rather than relying on WCL's
# undocumented default-metric behavior.
ROLE_TO_RAID_METRIC: dict[str, str] = {
    "TANK": "dps",
    "DAMAGER": "dps",
    "HEALER": "hps",
}


# WCL encounter IDs for current Midnight S2 M+ dungeons. Used to build
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
    ("af", 12993, "Altar of Fangs"),
    ("dn", 12825, "Den of Nalorakk"),
    ("kr", 61762, "Kings' Rest"),
    ("mr", 12813, "Murder Row"),
    ("rl", 112521, "Ruby Life Pools"),
    ("ts", 61877, "Temple of Sethraliss"),
    ("bv", 12859, "The Blinding Vale"),
    ("va", 12923, "Voidscar Arena"),
]

# RaiderIO packs dungeon levels in the exact order of ns.dungeons from its
# generated db_dungeons.lua (sorted by RaiderIO dungeon id, not display name or
# ChallengeMapID). A same-sized table from another season is structurally valid
# but would relabel every packed level, so keep this contract explicit.
# SYNC: refresh alongside MPLUS_ENCOUNTERS during the seasonal data update.
MPLUS_RAIDERIO_DUNGEON_ORDER: tuple[str, ...] = (
    "Kings' Rest",
    "Temple of Sethraliss",
    "Ruby Life Pools",
    "Murder Row",
    "The Blinding Vale",
    "Den of Nalorakk",
    "Voidscar Arena",
    "Altar of Fangs",
)

# WoW LFG activity IDs for the current season's Mythic+ listings. The addon
# emits the raw activityID from C_LFGList; using it as a companion-side fallback
# keeps same-dungeon scoring and target-row ordering stable on localized clients
# where listing.dungeon_name is not the English WCL display name.
MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME: dict[int, str] = {
    503: "Temple of Sethraliss",
    504: "Temple of Sethraliss",
    505: "Temple of Sethraliss",
    512: "Kings' Rest",
    513: "Kings' Rest",
    514: "Kings' Rest",
    515: "Kings' Rest",
    542: "Temple of Sethraliss",
    645: "Temple of Sethraliss",
    660: "Kings' Rest",
    661: "Kings' Rest",
    1173: "Ruby Life Pools",
    1174: "Ruby Life Pools",
    1175: "Ruby Life Pools",
    1176: "Ruby Life Pools",
    1699: "The Blinding Vale",
    1700: "The Blinding Vale",
    1701: "The Blinding Vale",
    1721: "Den of Nalorakk",
    1722: "Den of Nalorakk",
    1723: "Den of Nalorakk",
    1749: "Murder Row",
    1750: "Murder Row",
    1751: "Murder Row",
    1754: "Voidscar Arena",
    1755: "Voidscar Arena",
    1756: "Voidscar Arena",
    1930: "Altar of Fangs",
    1931: "Altar of Fangs",
    1932: "Altar of Fangs",
    1933: "Altar of Fangs",
    1949: "The Blinding Vale",
    1950: "Murder Row",
    1951: "Voidscar Arena",
    1952: "Den of Nalorakk",
}


def _int_lookup_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def mplus_dungeon_name_for_activity_id(activity_id: object) -> str:
    clean = _int_lookup_value(activity_id)
    return MPLUS_ACTIVITY_ID_TO_DUNGEON_NAME.get(clean, "") if clean is not None else ""


# ChallengeMapID values from Blizzard MapChallengeMode, filtered to the current
# MythicPlusSeasonTrackedMap season. The addon emits this ID for party leader
# keystones; it is a separate namespace from GroupFinderActivity activity IDs.
MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME: dict[int, str] = {
    249: "Kings' Rest",
    250: "Temple of Sethraliss",
    399: "Ruby Life Pools",
    584: "The Blinding Vale",
    585: "Voidscar Arena",
    586: "Den of Nalorakk",
    587: "Murder Row",
    588: "Altar of Fangs",
}


def mplus_dungeon_name_for_challenge_map_id(challenge_map_id: object) -> str:
    clean = _int_lookup_value(challenge_map_id)
    return (
        MPLUS_CHALLENGE_MAP_ID_TO_DUNGEON_NAME.get(clean, "")
        if clean is not None
        else ""
    )


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
