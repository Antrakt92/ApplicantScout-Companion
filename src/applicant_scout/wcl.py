"""Warcraft Logs API v2 client: OAuth, GraphQL, server-slug derivation, cache."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, asdict, field, replace
from pathlib import Path
from typing import Optional

import httpx

from .atomic_io import apply_private_file_mode, atomic_write_text
from .constants import (
    CURRENT_RAID_ENCOUNTERS,
    CURRENT_RAID_ZONE_ID,
    MPLUS_ENCOUNTERS,
    ROLE_TO_RAID_METRIC,
    SPEC_ID_TO_WCL_NAME,
)
from .metric_preferences import (
    DEFAULT_METRIC_PREFERENCES,
    MetricPreferences,
    effective_wcl_preferences_for_spec,
)


_log = logging.getLogger("applicant_scout.wcl")


WCL_OAUTH_URL = "https://www.warcraftlogs.com/oauth/token"
WCL_API_URL = "https://www.warcraftlogs.com/api/v2/client"


WCL_ERROR_QUOTA_GUARD = "quota_guard"
WCL_ERROR_RATE_LIMITED = "rate_limited"
WCL_ERROR_AUTH = "auth"
WCL_ERROR_SERVER = "server"
WCL_ERROR_MALFORMED = "malformed"
WCL_ERROR_GRAPHQL = "graphql"
WCL_ERROR_NETWORK = "network"
WCL_ERROR_HTTP = "http"
WCL_RATE_LIMIT_RETRY_SECONDS = 300.0
WCL_SERVER_RETRY_SECONDS = 30.0
WCL_NETWORK_RETRY_SECONDS = 30.0


# Query builder — encounterRankings encounterID args MUST be literal ints
# (GraphQL variable substitution works but produces 8x repetitive var declarations
# for no benefit — IDs are stable per season). Metric depends on role: damage/tank
# applicants get only DPS data (8 encounter queries), healers get only HPS (8
# queries) — saves quota by not fetching irrelevant data. Cache result per role
# so build cost is paid once.
#
# Why per-encounter (encounterRankings) not zone-level (zoneRankings):
# zoneRankings aggregates across ALL bracket levels — a +20-pusher gets 99%
# inflated by +5 farm runs they did. encounterRankings returns per-RUN data
# (`ranks[]` array) so we can client-side filter to applicant's HIGHEST timed
# key per dungeon — what raid leads care about for high-key push scouting.
#
# byBracket: true: percentile filtered to bracket of player's best timed key.
# Within that bracket, rankPercent = comparison against same-bracket peers.
_QUERY_CACHE: dict[tuple[str, str], str] = {}
_RAID_DETAIL_QUERY_CACHE: dict[tuple[str, str], str] = {}
_RAID_DETAIL_DIFFICULTIES: dict[str, tuple[str, int, str]] = {
    "N": ("raid_n", 3, "raid_normal"),
    "H": ("raid_h", 4, "raid_heroic"),
    "M": ("raid_m", 5, "raid_mythic"),
}


def wcl_metric_role(role: str) -> str:
    """Return the WCL/cache metric shape for an ApplicantScout role."""
    return "HEALER" if role == "HEALER" else "DPS"


def _build_character_ranks_query(
    role: str,
    metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
) -> str:
    """Returns GraphQL query string with M+ encounter aliases inlined.

    role: TANK/DAMAGER/HEALER, or normalized DPS from wcl_metric_role().
    DAMAGER+TANK+DPS get only DPS encounter queries, HEALER gets only HPS —
    irrelevant metric is skipped to save WCL quota (each encounterRankings call
    costs ~1 point; 8 saved per applicant).
    """
    metric_role = wcl_metric_role(role)
    cache_key = (metric_role, metric_preferences.cache_key())
    cached = _QUERY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    metric = "hps" if metric_role == "HEALER" else "dps"
    encounter_lines = []
    if metric_preferences.mplus:
        for alias, eid, _name in MPLUS_ENCOUNTERS:
            encounter_lines.append(
                f"      {alias}: encounterRankings(encounterID: {eid}, "
                f"metric: {metric}, byBracket: true)"
            )
    encounters_block = "\n".join(encounter_lines)
    raid_lines = []
    if metric_preferences.raid_normal:
        raid_lines.append(
            "      raidNormal: zoneRankings(zoneID: $raidZoneID, "
            "difficulty: 3, metric: $raidMetric)"
        )
    if metric_preferences.raid_heroic:
        raid_lines.append(
            "      raidHeroic: zoneRankings(zoneID: $raidZoneID, "
            "difficulty: 4, metric: $raidMetric)"
        )
    if metric_preferences.raid_mythic:
        raid_lines.append(
            "      raidMythic: zoneRankings(zoneID: $raidZoneID, "
            "difficulty: 5, metric: $raidMetric)"
        )
    metric_blocks = "\n".join([*raid_lines, encounters_block]).rstrip()
    raid_vars = ""
    if metric_preferences.raid_enabled:
        raid_vars = ",\n                     $raidZoneID: Int!,\n                     $raidMetric: CharacterPageRankingMetricType"

    q = f"""
query CharacterRanks($name: String!, $serverSlug: String!, $serverRegion: String!{raid_vars}) {{
  rateLimitData {{
    limitPerHour
    pointsSpentThisHour
    pointsResetIn
  }}
  characterData {{
    character(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {{
      name
      classID
{metric_blocks}
    }}
  }}
}}
""".strip()
    _QUERY_CACHE[cache_key] = q
    return q


def _build_raid_boss_detail_query(
    role: str,
    metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
) -> str:
    metric_role = wcl_metric_role(role)
    cache_key = (metric_role, metric_preferences.cache_key())
    cached = _RAID_DETAIL_QUERY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    metric = "hps" if metric_role == "HEALER" else "dps"
    lines: list[str] = []
    for difficulty_key, (alias_prefix, wcl_difficulty, pref_name) in (
        _RAID_DETAIL_DIFFICULTIES.items()
    ):
        if not getattr(metric_preferences, pref_name):
            continue
        for encounter_alias, encounter_id, _name in CURRENT_RAID_ENCOUNTERS:
            base = f"{alias_prefix}_{encounter_alias}"
            lines.append(
                f"      {base}_overall: encounterRankings(encounterID: {encounter_id}, "
                f"difficulty: {wcl_difficulty}, metric: {metric}, "
                "specName: $specName, compare: Parses)"
            )
            lines.append(
                f"      {base}_ilvl: encounterRankings(encounterID: {encounter_id}, "
                f"difficulty: {wcl_difficulty}, metric: {metric}, "
                "specName: $specName, compare: Parses, byBracket: true)"
            )
    metric_blocks = "\n".join(lines).rstrip()
    q = f"""
query RaidBossDetails($name: String!, $serverSlug: String!, $serverRegion: String!, $specName: String!) {{
  rateLimitData {{
    limitPerHour
    pointsSpentThisHour
    pointsResetIn
  }}
  characterData {{
    character(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {{
      name
      classID
{metric_blocks}
    }}
  }}
}}
""".strip()
    _RAID_DETAIL_QUERY_CACHE[cache_key] = q
    return q


@dataclass
class RateLimitInfo:
    """WCL API quota snapshot — piggybacked on every CharacterRanks query.

    limit_per_hour: total points budget per rolling hour (varies by WCL
    subscription tier — free clients ~3600, paid tiers higher).
    points_spent: how many points used since the rolling hour window started.
    reset_in_seconds: seconds until the rolling hour window resets to 0."""

    limit_per_hour: float
    points_spent: float
    reset_in_seconds: float


@dataclass
class _QuotaSnapshot:
    info: RateLimitInfo
    observed_at: float


@dataclass(frozen=True)
class _QuotaReservation:
    points: float
    auth_generation: int


@dataclass
class KeyBracketPerf:
    """One M+ key-level bracket within a dungeon for the applicant's current spec."""

    key_level: int
    parse_percent: Optional[float]
    median_percent: Optional[float] = None
    run_count: int = 0


@dataclass
class DungeonPerf:
    """Per-dungeon M+ breakdown entry — fed to the M+ cell tooltip so user can
    inspect WHICH dungeons + key levels contributed to the headline best/median.

    Each entry represents the applicant's performance at their HIGHEST TIMED
    KEY in this dungeon, in their currently-applying spec only. Values are
    aggregated across ONLY runs at that top key level (lower-key runs excluded
    from this dungeon's stats — see _process_encounter_ranks).

    parse_percent: best DPS/HPS percentile across the top-key runs (max).
    median_percent: median percentile across the same run set.
    key_level: the bracket level these stats are computed at (player's highest
    timed key in this dungeon, for their current spec).
    run_count: how many runs at that key level fed into best/median. Critical
    confidence signal — N=1 means best=median=single run (lucky/unlucky risk),
    N≥3 is statistically meaningful.

    brackets: all key-level summaries from the same WCL response. The top-level
    fields stay highest-key/back-compat; context-fit scoring can inspect lower
    relevant brackets without spending more API quota."""

    name: str
    parse_percent: Optional[float]
    median_percent: Optional[float] = None
    key_level: int = 0  # 0 = unknown / no data extracted
    run_count: int = 0  # Default 0 for cache back-compat with pre-perEncounter entries.
    brackets: list[KeyBracketPerf] = field(default_factory=list)


@dataclass
class CharacterRanks:
    """Result of one CharacterRanks query — already aggregated to display values.

    Raid: each difficulty stores BOTH best and median per-encounter avg. Best
    shows skill ceiling, median shows consistency — overlay displays as
    "best/median" pair. WCL UI calls these "Best Perf. Avg." and "Median Perf.
    Avg." in the character profile.

    M+: only the role-relevant metric is populated — DPS for damage/tank
    applicants (mplus_dps fields filled), HPS for healers (mplus_hps fields
    filled). The OTHER metric stays None — saves quota by skipping 8 unneeded
    encounter queries per applicant.

    mplus_*_breakdown: per-dungeon detail (DungeonPerf list, top-key only).
    mplus_dps / mplus_hps: avg of per-dungeon best % across all dungeons
        (with data). Always computed if any data — represents ceiling.
    mplus_dps_median / mplus_hps_median: avg of per-dungeon median % across
        only dungeons with run_count >= 2 (single-run medians = same as best,
        not informative). None if all dungeons have run_count == 1."""

    raid_normal: Optional[float]
    raid_heroic: Optional[float]
    raid_mythic: Optional[float]
    raid_normal_median: Optional[float]
    raid_heroic_median: Optional[float]
    raid_mythic_median: Optional[float]
    mplus_dps: Optional[float]
    mplus_hps: Optional[float]
    # Median headline averages — None for non-relevant role metric or all-N=1
    # data. Default None for cache back-compat with pre-median entries.
    mplus_dps_median: Optional[float] = None
    mplus_hps_median: Optional[float] = None
    mplus_dps_breakdown: list[DungeonPerf] = field(default_factory=list)
    mplus_hps_breakdown: list[DungeonPerf] = field(default_factory=list)
    not_found: bool = False
    error: str = ""
    error_kind: str = ""

    @classmethod
    def empty(
        cls,
        *,
        error: str = "",
        not_found: bool = False,
        error_kind: str = "",
    ) -> "CharacterRanks":
        """Build a CharacterRanks with all metric fields None — used for error
        paths and not-found responses.

        Centralised because the dataclass has 8 required positional args; raw
        construction at call sites repeatedly forgets a field (silent TypeError
        crashes the QRunnable, applicant row stays on 'loading' forever).
        Always prefer this over `CharacterRanks(None, None, ...)`."""
        return cls(
            raid_normal=None,
            raid_heroic=None,
            raid_mythic=None,
            raid_normal_median=None,
            raid_heroic_median=None,
            raid_mythic_median=None,
            mplus_dps=None,
            mplus_hps=None,
            error=error,
            not_found=not_found,
            error_kind=error_kind,
        )


# ───────────────────────────────────────────────────────────────────
# OAuth


@dataclass
class _Token:
    access_token: str
    expires_at: float  # epoch seconds
    client_fingerprint: str = ""


def _client_fingerprint(client_id: str, client_secret: str) -> str:
    """Return a cache binding for the configured WCL credentials."""
    normalized = f"{client_id.strip()}\0{client_secret.strip()}".encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


class WCLAuth:
    """Manages OAuth client_credentials token, refreshes when near expiry."""

    def __init__(self, client_id: str, client_secret: str, cache_dir: Path):
        self._client_id = client_id
        self._client_secret = client_secret
        self._client_fingerprint = _client_fingerprint(client_id, client_secret)
        self._token_path = cache_dir / "token.json"
        if self._token_path.exists():
            apply_private_file_mode(self._token_path)
        self._token: Optional[_Token] = self._load_cached()
        # Two QRunnable workers can hit get_token() simultaneously for the
        # first request after token expiry. Without this lock both would call
        # _refresh — wasted HTTP roundtrip + theoretical race on token.json
        # write. Lock keeps the second caller waiting for the first refresh
        # to finish, then returns the same fresh token.
        self._refresh_lock = threading.Lock()
        self._token_state_lock = threading.Lock()
        self._invalidate_generation = 0

    def _load_cached(self) -> Optional[_Token]:
        # Failure must not propagate — _load_cached runs synchronously inside
        # WCLAuth.__init__, which is called BEFORE the GUI even paints.
        # Permission-denied / antivirus-quarantined token.json would
        # otherwise crash the app at startup with a stacktrace the user
        # can't recover from. Treat any read failure as "no cached token";
        # _refresh will fetch a fresh one on first need.
        try:
            if not self._token_path.exists():
                return None
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            access_token = data.get("access_token")
            expires_at = data.get("expires_at")
            client_fingerprint = data.get("client_fingerprint")
            if not isinstance(access_token, str) or not access_token.strip():
                return None
            if (
                isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not math.isfinite(float(expires_at))
                or float(expires_at) <= 0.0
            ):
                return None
            if (
                not isinstance(client_fingerprint, str)
                or client_fingerprint != self._client_fingerprint
            ):
                return None
            return _Token(
                access_token=access_token.strip(),
                expires_at=float(expires_at),
                client_fingerprint=client_fingerprint,
            )
        except (json.JSONDecodeError, TypeError, ValueError, OSError):
            return None

    def _save_cached(self, token: _Token) -> None:
        # Failure is non-fatal — the fresh token is still in memory and will
        # serve the next get_token call. Only side effect of a missed save is
        # one extra OAuth refresh on next process start (~1s). Better than
        # propagating OSError up through fetch_character_ranks and surfacing
        # "Permission denied: token.json" to the user as a WCL fetch error.
        try:
            atomic_write_text(
                self._token_path,
                json.dumps(asdict(token)),
                private=True,
            )
        except OSError as e:
            _log.warning("Could not persist OAuth token cache: %s", e)

    def get_token(self) -> str:
        """Returns valid access token, refreshing if within 60s of expiry."""
        # Fast path: cached & valid → no lock contention.
        token = self._token
        if token and token.expires_at - 60 > time.time():
            return token.access_token
        with self._refresh_lock:
            # Double-check inside the lock: a parallel caller may have just
            # refreshed; if so we serve the new token without firing again.
            token = self._token
            if token and token.expires_at - 60 > time.time():
                return token.access_token
            return self._refresh()

    def probe_online(self) -> None:
        """Verify credentials online while preserving a usable token on failure."""
        with self._refresh_lock:
            self._refresh()

    def invalidate(self) -> None:
        """Force refresh on next get_token (call on 401 response)."""
        with self._token_state_lock:
            self._invalidate_generation += 1
            self._token = None
        self._delete_cached_token()

    def _refresh(self) -> str:
        with self._token_state_lock:
            refresh_generation = self._invalidate_generation
        token = self._request_token()
        with self._token_state_lock:
            if refresh_generation != self._invalidate_generation:
                return token.access_token
            self._token = token
        self._save_cached(token)
        with self._token_state_lock:
            stale_after_save = refresh_generation != self._invalidate_generation
            if stale_after_save:
                self._token = None
        if stale_after_save:
            self._delete_cached_token()
        return token.access_token

    def _request_token(self) -> _Token:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                WCL_OAUTH_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
            )
        if resp.status_code != 200:
            error_kind = WCL_ERROR_AUTH
            if resp.status_code == 429:
                error_kind = WCL_ERROR_RATE_LIMITED
            elif resp.status_code >= 500:
                error_kind = WCL_ERROR_SERVER
            raise WCLAuthError(
                f"OAuth failed (HTTP {resp.status_code}): {resp.text[:200]}",
                error_kind=error_kind,
            )
        body = _json_object_response(resp, WCLAuthError, "OAuth response")
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise WCLAuthError(
                "OAuth response missing access_token",
                error_kind=WCL_ERROR_MALFORMED,
            )
        expires_raw = body.get("expires_in", 86400)
        if isinstance(expires_raw, bool):
            raise WCLAuthError(
                "OAuth response has invalid expires_in",
                error_kind=WCL_ERROR_MALFORMED,
            )
        try:
            expires_in = int(expires_raw)
        except (TypeError, ValueError, OverflowError):
            raise WCLAuthError(
                "OAuth response has invalid expires_in",
                error_kind=WCL_ERROR_MALFORMED,
            ) from None
        if expires_in <= 0:
            raise WCLAuthError(
                "OAuth response has invalid expires_in",
                error_kind=WCL_ERROR_MALFORMED,
            )
        try:
            expires_at = time.time() + expires_in
        except OverflowError:
            raise WCLAuthError(
                "OAuth response has invalid expires_in",
                error_kind=WCL_ERROR_MALFORMED,
            ) from None
        if not math.isfinite(expires_at):
            raise WCLAuthError(
                "OAuth response has invalid expires_in",
                error_kind=WCL_ERROR_MALFORMED,
            )
        return _Token(
            access_token=access_token.strip(),
            expires_at=expires_at,
            client_fingerprint=self._client_fingerprint,
        )

    def _delete_cached_token(self) -> None:
        if self._token_path.exists():
            try:
                self._token_path.unlink()
            except OSError:
                pass


class WCLAuthError(Exception):
    def __init__(self, message: str, *, error_kind: str = ""):
        super().__init__(message)
        self.error_kind = error_kind


class WCLApiError(Exception):
    def __init__(self, message: str, *, error_kind: str = ""):
        super().__init__(message)
        self.error_kind = error_kind


def _json_object_response(resp, error_cls: type[Exception], context: str) -> dict:
    try:
        body = resp.json()
    except ValueError as e:
        if error_cls in (WCLApiError, WCLAuthError):
            raise error_cls(
                f"Malformed {context}: invalid JSON",
                error_kind=WCL_ERROR_MALFORMED,
            ) from e
        raise error_cls(f"Malformed {context}: invalid JSON") from e
    if not isinstance(body, dict):
        if error_cls in (WCLApiError, WCLAuthError):
            raise error_cls(
                f"Malformed {context}: expected JSON object",
                error_kind=WCL_ERROR_MALFORMED,
            )
        raise error_cls(f"Malformed {context}: expected JSON object")
    return body


def _safe_nonnegative_finite_float(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0.0:
        return None
    return f


def _rate_limit_info_from_dict(d) -> Optional[RateLimitInfo]:
    if not isinstance(d, dict):
        return None
    limit_per_hour = _safe_nonnegative_finite_float(d.get("limitPerHour"))
    points_spent = _safe_nonnegative_finite_float(d.get("pointsSpentThisHour"))
    reset_in_seconds = _safe_nonnegative_finite_float(d.get("pointsResetIn"))
    if (
        limit_per_hour is None
        or points_spent is None
        or reset_in_seconds is None
    ):
        return None
    return RateLimitInfo(
        limit_per_hour=limit_per_hour,
        points_spent=points_spent,
        reset_in_seconds=reset_in_seconds,
    )


@dataclass(frozen=True)
class _GraphQLErrorInfo:
    message: str
    path: tuple[str, ...] = ()


def _graphql_errors(errors) -> list[_GraphQLErrorInfo]:
    if not errors:
        return []
    raw_errors = errors if isinstance(errors, list) else [errors]
    parsed: list[_GraphQLErrorInfo] = []
    for entry in raw_errors:
        path: tuple[str, ...] = ()
        if isinstance(entry, dict):
            raw_message = entry.get("message")
            if isinstance(raw_message, str) and raw_message.strip():
                message = raw_message.strip()
            else:
                message = "unknown error"
            raw_path = entry.get("path")
            if isinstance(raw_path, list):
                path = tuple(str(part) for part in raw_path)
        elif isinstance(entry, str) and entry.strip():
            message = entry.strip()
        else:
            message = "unknown error"
        parsed.append(_GraphQLErrorInfo(message=message, path=path))
    return parsed


def _is_character_not_found_graphql_error(error: _GraphQLErrorInfo) -> bool:
    low = error.message.lower()
    if "could not find character" in low or "character not found" in low:
        return True
    if low not in {"not found", "could not find"}:
        return False
    path = tuple(part.lower() for part in error.path)
    return bool(path) and path[-1:] == ("character",) and "characterdata" in path


def _ranks_for_graphql_errors(
    errors: list[_GraphQLErrorInfo],
) -> CharacterRanks | None:
    if not errors:
        return None
    non_character_error = next(
        (error for error in errors if not _is_character_not_found_graphql_error(error)),
        None,
    )
    if non_character_error is None:
        return CharacterRanks.empty(not_found=True, error=errors[0].message)
    raise WCLApiError(
        f"GraphQL error: {non_character_error.message}",
        error_kind=WCL_ERROR_GRAPHQL,
    )


def _ranking_alias_payload(char: dict, alias: str) -> dict:
    if alias not in char:
        raise WCLApiError(
            f"Malformed WCL response: {alias} is missing",
            error_kind=WCL_ERROR_MALFORMED,
        )
    enc_data = char.get(alias)
    if enc_data is None:
        raise WCLApiError(
            f"Malformed WCL response: {alias} is null",
            error_kind=WCL_ERROR_MALFORMED,
        )
    if not isinstance(enc_data, dict):
        raise WCLApiError(
            f"Malformed WCL response: {alias} is not an object",
            error_kind=WCL_ERROR_MALFORMED,
        )
    if "ranks" not in enc_data:
        raise WCLApiError(
            f"Malformed WCL response: {alias}.ranks is missing",
            error_kind=WCL_ERROR_MALFORMED,
        )
    if not isinstance(enc_data.get("ranks"), list):
        raise WCLApiError(
            f"Malformed WCL response: {alias}.ranks is not a list",
            error_kind=WCL_ERROR_MALFORMED,
        )
    return enc_data


def _mplus_alias_payload(char: dict, alias: str) -> dict:
    return _ranking_alias_payload(char, alias)


def _raid_zone_alias_payload(char: dict, alias: str) -> dict:
    if alias not in char:
        raise WCLApiError(
            f"Malformed WCL response: {alias} is missing",
            error_kind=WCL_ERROR_MALFORMED,
        )
    zone_data = char.get(alias)
    if zone_data is None:
        raise WCLApiError(
            f"Malformed WCL response: {alias} is null",
            error_kind=WCL_ERROR_MALFORMED,
        )
    if not isinstance(zone_data, dict):
        raise WCLApiError(
            f"Malformed WCL response: {alias} is not an object",
            error_kind=WCL_ERROR_MALFORMED,
        )
    return zone_data


# ───────────────────────────────────────────────────────────────────
# GraphQL


@dataclass(frozen=True)
class WCLConnectionStatus:
    """Secret-free credential/API status snapshot exposed to the UI."""

    state: str = "unknown"
    error_kind: str = ""


@dataclass(frozen=True)
class _WCLAuthValidation:
    auth: WCLAuth
    auth_generation: int
    status_revision: int


class WCLClient:
    """Synchronous WCL GraphQL client with token-aware retry and result aggregation."""

    def __init__(
        self,
        auth: WCLAuth,
        region: str = "EU",
        metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
    ):
        self._auth = auth
        self.metric_preferences = metric_preferences
        # Public attribute: state machine updates this from the VERSION line's
        # region_id so non-EU users don't silently get "Server not found" with
        # the default APSCOUT_REGION=EU. Read fresh on each fetch.
        self.region = region
        self._http = httpx.Client(timeout=15.0)
        # Retry backoff: when WCL rate-limits or temporarily returns 5xx, every
        # subsequent fetch in the block returns immediately instead of
        # hammering the API. WCL's "points" budget is per-hour rolling; 429
        # sleeps longer than transient server errors.
        self._quota_lock = threading.Lock()
        self._rate_limited_until: float = 0.0
        self._server_retry_until: float = 0.0
        self._network_retry_until: float = 0.0
        # Latest API quota snapshot, parsed from rateLimitData on every fetch.
        # Overlay polls this via QTimer to display "spent / limit" in status bar.
        # None until first successful fetch.
        self.last_quota: Optional[RateLimitInfo] = None
        self._quota_snapshot: Optional[_QuotaSnapshot] = None
        self._reserved_quota_points: float = 0.0
        self._auth_generation: int = 0
        self._connection_status = WCLConnectionStatus()
        self._connection_status_revision = 0
        self._closed = False

    def close(self) -> None:
        with self._quota_lock:
            self._closed = True
            self._auth_generation += 1
            self._connection_status_revision += 1
            self._connection_status = WCLConnectionStatus()
        self._http.close()

    @property
    def connection_status(self) -> WCLConnectionStatus:
        with self._quota_lock:
            return self._connection_status

    def begin_auth_validation(self) -> _WCLAuthValidation | None:
        """Snapshot active auth and publish `checking` before worker launch."""
        with self._quota_lock:
            if self._closed:
                return None
            self._connection_status_revision += 1
            validation = _WCLAuthValidation(
                self._auth,
                self._auth_generation,
                self._connection_status_revision,
            )
            self._connection_status = WCLConnectionStatus(state="checking")
            return validation

    def run_auth_validation(self, validation: _WCLAuthValidation) -> None:
        """Run a fresh OAuth probe and ignore stale/reconfigured completion."""
        try:
            validation.auth.probe_online()
        except WCLAuthError as exc:
            error_kind = exc.error_kind
            _log.warning(
                "WCL OAuth validation failed: kind=%s type=%s",
                error_kind or "unknown",
                type(exc).__name__,
            )
            status = WCLConnectionStatus(state="error", error_kind=error_kind)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            _log.warning(
                "WCL OAuth validation failed: kind=%s type=%s",
                WCL_ERROR_NETWORK,
                type(exc).__name__,
            )
            status = WCLConnectionStatus(
                state="error",
                error_kind=WCL_ERROR_NETWORK,
            )
        except Exception as exc:  # noqa: BLE001 — UI status stays category-only
            _log.warning(
                "WCL OAuth validation failed: kind=unknown type=%s",
                type(exc).__name__,
            )
            status = WCLConnectionStatus(state="error")
        else:
            _log.info("WCL OAuth validation: OK")
            status = WCLConnectionStatus(state="oauth_ready")
        self._set_connection_status_if_current(
            validation.auth_generation,
            validation.status_revision,
            status,
        )

    def _set_connection_status_if_current(
        self,
        auth_generation: int,
        status_revision: int,
        status: WCLConnectionStatus,
    ) -> None:
        with self._quota_lock:
            if (
                self._closed
                or auth_generation != self._auth_generation
                or status_revision != self._connection_status_revision
            ):
                return
            self._connection_status_revision += 1
            self._connection_status = status

    def record_api_result(self, *, succeeded: bool, error_kind: str = "") -> None:
        """Record a current-generation network result without raw response data."""
        with self._quota_lock:
            if self._closed:
                return
            self._connection_status_revision += 1
            self._connection_status = WCLConnectionStatus(
                state="api_ready" if succeeded else "error",
                error_kind="" if succeeded else error_kind,
            )

    def mark_active_auth_validated(self) -> None:
        """Publish a successful Settings test for the unchanged active auth."""
        with self._quota_lock:
            if self._closed:
                return
            self._connection_status_revision += 1
            self._connection_status = WCLConnectionStatus(state="oauth_ready")

    def reconfigure_auth(self, auth: WCLAuth, *, validated: bool = False) -> None:
        with self._quota_lock:
            self._auth = auth
            self._auth_generation += 1
            self._connection_status_revision += 1
            self._rate_limited_until = 0.0
            self._server_retry_until = 0.0
            self._network_retry_until = 0.0
            self.last_quota = None
            self._quota_snapshot = None
            self._reserved_quota_points = 0.0
            self._connection_status = WCLConnectionStatus(
                state="oauth_ready" if validated else "unknown"
            )

    def _record_quota_snapshot(
        self,
        quota: RateLimitInfo,
        now: float | None = None,
        auth_generation: int | None = None,
    ) -> None:
        observed_at = time.time() if now is None else now
        with self._quota_lock:
            if (
                auth_generation is not None
                and auth_generation != self._auth_generation
            ):
                return
            self.last_quota = quota
            self._quota_snapshot = _QuotaSnapshot(info=quota, observed_at=observed_at)

    def quota_reset_remaining_seconds(self, now: float | None = None) -> float | None:
        with self._quota_lock:
            snapshot = self._quota_snapshot
        if snapshot is None:
            return None
        current_time = time.time() if now is None else now
        elapsed = max(0.0, current_time - snapshot.observed_at)
        return max(0.0, snapshot.info.reset_in_seconds - elapsed)

    def rate_limit_retry_remaining_seconds(self, now: float | None = None) -> float:
        current_time = time.time() if now is None else now
        with self._quota_lock:
            rate_limited_until = self._rate_limited_until
        return max(0.0, rate_limited_until - current_time)

    def server_retry_remaining_seconds(self, now: float | None = None) -> float:
        current_time = time.time() if now is None else now
        with self._quota_lock:
            server_retry_until = self._server_retry_until
        return max(0.0, server_retry_until - current_time)

    def network_retry_remaining_seconds(self, now: float | None = None) -> float:
        current_time = time.time() if now is None else now
        with self._quota_lock:
            network_retry_until = self._network_retry_until
        return max(0.0, network_retry_until - current_time)

    def quota_guard_retry_remaining_seconds(self, now: float | None = None) -> float:
        return 0.0

    def _estimate_query_quota_points(
        self, metric_preferences: MetricPreferences
    ) -> float:
        if not metric_preferences.any_enabled:
            return 0.0
        points = 1.0
        if metric_preferences.mplus:
            points += float(len(MPLUS_ENCOUNTERS))
        points += float(
            sum(
                (
                    metric_preferences.raid_normal,
                    metric_preferences.raid_heroic,
                    metric_preferences.raid_mythic,
                )
            )
        )
        return points

    def _estimate_raid_boss_detail_quota_points(
        self, metric_preferences: MetricPreferences
    ) -> float:
        enabled_difficulties = sum(
            (
                metric_preferences.raid_normal,
                metric_preferences.raid_heroic,
                metric_preferences.raid_mythic,
            )
        )
        return 1.0 + float(enabled_difficulties * len(CURRENT_RAID_ENCOUNTERS) * 2)

    def _reserve_quota_for_fetch(
        self, points: float, now: float | None = None
    ) -> _QuotaReservation:
        with self._quota_lock:
            reserved = max(0.0, points)
            self._reserved_quota_points += reserved
            return _QuotaReservation(
                points=reserved,
                auth_generation=self._auth_generation,
            )

    def _release_quota_reservation(self, reservation: _QuotaReservation) -> None:
        if reservation.points <= 0:
            return
        with self._quota_lock:
            if reservation.auth_generation != self._auth_generation:
                return
            self._reserved_quota_points = max(
                0.0, self._reserved_quota_points - reservation.points
            )

    def retry_block_remaining_seconds(self, now: float | None = None) -> float:
        return max(
            self.rate_limit_retry_remaining_seconds(now=now),
            self.server_retry_remaining_seconds(now=now),
            self.network_retry_remaining_seconds(now=now),
            self.quota_guard_retry_remaining_seconds(now=now),
        )

    def _active_wcl_retry_block(
        self, now: float
    ) -> tuple[str, str, float] | None:
        with self._quota_lock:
            rate_limited_until = self._rate_limited_until
            server_retry_until = self._server_retry_until
            network_retry_until = self._network_retry_until
        if now < rate_limited_until:
            return WCL_ERROR_RATE_LIMITED, "WCL rate-limited", rate_limited_until
        if now < server_retry_until:
            return WCL_ERROR_SERVER, "WCL server error", server_retry_until
        if now < network_retry_until:
            return WCL_ERROR_NETWORK, "WCL network error", network_retry_until
        return None

    def _set_network_retry_if_current(self, auth_generation: int) -> None:
        self._set_retry_block_if_current(auth_generation, WCL_ERROR_NETWORK)

    def _set_retry_block_if_current(
        self, auth_generation: int, error_kind: str
    ) -> None:
        with self._quota_lock:
            if auth_generation != self._auth_generation:
                return
            if error_kind == WCL_ERROR_RATE_LIMITED:
                self._rate_limited_until = time.time() + WCL_RATE_LIMIT_RETRY_SECONDS
            elif error_kind == WCL_ERROR_SERVER:
                self._server_retry_until = time.time() + WCL_SERVER_RETRY_SECONDS
            elif error_kind == WCL_ERROR_NETWORK:
                self._network_retry_until = time.time() + WCL_NETWORK_RETRY_SECONDS

    def _post_graphql_with_auth_retry(
        self,
        auth: WCLAuth,
        auth_generation: int,
        body: dict[str, object],
    ) -> httpx.Response:
        for attempt in range(2):
            try:
                token = auth.get_token()
            except WCLAuthError as exc:
                self._set_retry_block_if_current(auth_generation, exc.error_kind)
                raise
            except (httpx.TimeoutException, httpx.RequestError):
                self._set_network_retry_if_current(auth_generation)
                raise
            try:
                resp = self._http.post(
                    WCL_API_URL,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except (httpx.TimeoutException, httpx.RequestError):
                self._set_network_retry_if_current(auth_generation)
                raise
            if resp.status_code == 401 and attempt == 0:
                with self._quota_lock:
                    is_current_auth = auth_generation == self._auth_generation
                if is_current_auth:
                    auth.invalidate()
                continue
            if resp.status_code in (401, 403):
                raise WCLApiError(
                    f"Authentication failed (HTTP {resp.status_code})",
                    error_kind=WCL_ERROR_AUTH,
                )
            if resp.status_code == 429:
                self._set_retry_block_if_current(
                    auth_generation, WCL_ERROR_RATE_LIMITED
                )
                raise WCLApiError(
                    "Rate limited (HTTP 429) — cooldown 5min",
                    error_kind=WCL_ERROR_RATE_LIMITED,
                )
            if resp.status_code >= 500:
                self._set_retry_block_if_current(auth_generation, WCL_ERROR_SERVER)
                raise WCLApiError(
                    f"Server error (HTTP {resp.status_code})",
                    error_kind=WCL_ERROR_SERVER,
                )
            if resp.status_code != 200:
                raise WCLApiError(
                    f"Unexpected HTTP {resp.status_code}: {resp.text[:200]}",
                    error_kind=WCL_ERROR_HTTP,
                )
            return resp
        raise WCLApiError("Authentication failed (HTTP 401)", error_kind=WCL_ERROR_AUTH)

    def fetch_character_ranks(
        self,
        name: str,
        server_slug: str,
        spec_id: int = 0,
        role: str = "DAMAGER",
        region: Optional[str] = None,
        metric_preferences: MetricPreferences | None = None,
    ) -> CharacterRanks:
        """One query → raid (3 difficulties) + 8 per-encounter M+ rankings.

        spec_id: WoW retail spec ID. Used to filter encounter runs to that spec
            only — Dsk80 example proved spec critical: as Blood at +15 = 82,
            as Unholy at +15 = 7. spec_id=0 → no filter (rare; mostly debug).
        role: TANK/DAMAGER/HEALER. Determines:
            - Raid metric (dps for tank+damager, hps for healer).
            - M+ encounter metric — only ONE of dps/hps queried per applicant
              to save quota (8 encounter calls instead of 16). DPS-side stays
              None for healers and vice versa.
        region: explicit WCL region ("EU"/"US"/etc). Caller (overlay
            _FetchTask) snapshots self.region once and passes it both here and
            to CharacterCache.get/put — without this parameter the fetch read
            self.region independently and a region update mid-task (VERSION
            snapshot arrives between cache lookup and HTTP send) would query
            new region while the result got cached under the old region key,
            polluting cache + wasting one fetch per character on next session.
            None → fall back to self.region (back-compat for any caller that
            doesn't snapshot)."""
        # Short-circuit during rate-limit cooldown.
        metric_preferences = metric_preferences or self.metric_preferences
        metric_preferences = effective_wcl_preferences_for_spec(
            spec_id,
            metric_preferences,
        )
        if not metric_preferences.any_enabled:
            return CharacterRanks.empty()
        now = time.time()
        with self._quota_lock:
            auth = self._auth
            auth_generation = self._auth_generation
            rate_limited_until = self._rate_limited_until
            server_retry_until = self._server_retry_until
            network_retry_until = self._network_retry_until
        if now < rate_limited_until:
            return CharacterRanks.empty(
                error=f"WCL rate-limited; retrying in {int(rate_limited_until - now)}s",
                error_kind=WCL_ERROR_RATE_LIMITED,
            )
        if now < server_retry_until:
            return CharacterRanks.empty(
                error=f"WCL server error; retrying in {int(server_retry_until - now)}s",
                error_kind=WCL_ERROR_SERVER,
            )
        if now < network_retry_until:
            return CharacterRanks.empty(
                error=f"WCL network error; retrying in {int(network_retry_until - now)}s",
                error_kind=WCL_ERROR_NETWORK,
            )

        # Reserve estimated points so the status row can include in-flight work.
        # This is accounting only; actual WCL 429 responses still drive cooldowns.
        quota_reservation = self._reserve_quota_for_fetch(
            self._estimate_query_quota_points(metric_preferences),
            now=now,
        )

        try:
            # Resolve region once: explicit param wins, else snapshot self.region
            # AT METHOD ENTRY (single read). Without snapshotting, a state-machine
            # versionUpdated firing between this point and the GraphQL POST below
            # would query a different region than the caller passed to cache.get.
            region_used = region if region is not None else self.region
            raid_metric = ROLE_TO_RAID_METRIC.get(role, "dps")
            spec_name = (
                SPEC_ID_TO_WCL_NAME.get(spec_id, "")
                if metric_preferences.mplus
                else ""
            )
            # Unknown / unmapped spec_id: SPEC_ID_TO_WCL_NAME returns "" so the
            # downstream spec filter would silently let all of the applicant's
            # OTHER specs into the result. Log loud — _process_encounter_ranks
            # short-circuits to None (M+ cell shows "—") rather than ship wrong-spec
            # numbers. Trips for unmapped retail spec_ids (future expansions, or
            # garbage values from a corrupted snapshot).
            if metric_preferences.mplus and spec_id != 0 and not spec_name:
                _log.warning(
                    "Unmapped spec_id=%d (no SPEC_ID_TO_WCL_NAME entry) — M+ "
                    "breakdown for %s will be empty to avoid mixing other specs",
                    spec_id,
                    name,
                )
            is_healer = role == "HEALER"

            query = _build_character_ranks_query(role, metric_preferences)
            variables: dict[str, object] = {
                "name": name,
                "serverSlug": server_slug,
                "serverRegion": region_used,
            }
            if metric_preferences.raid_enabled:
                variables["raidZoneID"] = CURRENT_RAID_ZONE_ID
                variables["raidMetric"] = raid_metric
            body = {"query": query, "variables": variables}

            resp = self._post_graphql_with_auth_retry(auth, auth_generation, body)
            data = _json_object_response(resp, WCLApiError, "WCL response")
            graphql_errors = _graphql_errors(data.get("errors"))
            # Update quota snapshot regardless of errors — rateLimitData is at
            # the root, present even on GraphQL-level errors (HTTP 200).
            data_root_obj = data.get("data")
            if not isinstance(data_root_obj, dict):
                graphql_result = _ranks_for_graphql_errors(graphql_errors)
                if graphql_result is not None:
                    return graphql_result
                raise WCLApiError(
                    "Malformed WCL response: data is not an object",
                    error_kind=WCL_ERROR_MALFORMED,
                )
            data_root = data_root_obj
            quota = _rate_limit_info_from_dict(data_root.get("rateLimitData"))
            if quota is not None:
                self._record_quota_snapshot(
                    quota,
                    auth_generation=auth_generation,
                )
            graphql_result = _ranks_for_graphql_errors(graphql_errors)
            if graphql_result is not None:
                return graphql_result
            if "characterData" not in data_root or not isinstance(
                data_root.get("characterData"), dict
            ):
                raise WCLApiError(
                    "Malformed WCL response: characterData is not an object",
                    error_kind=WCL_ERROR_MALFORMED,
                )
            character_data = data_root["characterData"]
            if "character" not in character_data:
                raise WCLApiError(
                    "Malformed WCL response: character key is missing",
                    error_kind=WCL_ERROR_MALFORMED,
                )
            char = character_data.get("character")
            if char is None:
                return CharacterRanks.empty(not_found=True)
            if not isinstance(char, dict):
                raise WCLApiError(
                    "Malformed WCL response: character is not an object",
                    error_kind=WCL_ERROR_MALFORMED,
                )

            # Build per-dungeon breakdown from the 8 aliased encounterRankings.
            # _process_encounter_ranks filters to applicant's spec + highest
            # timed key, then computes best/median/run_count for that subset.
            breakdown: list[DungeonPerf] = []
            if metric_preferences.mplus:
                for alias, _eid, dungeon_name in MPLUS_ENCOUNTERS:
                    perf = _process_encounter_ranks(
                        _mplus_alias_payload(char, alias), spec_name, dungeon_name
                    )
                    if perf is not None:
                        breakdown.append(perf)
            breakdown.sort(key=lambda d: d.name)

            best_avg, median_avg = _compute_mplus_headline(breakdown)

            # Route the breakdown into the role-relevant slot. The other slot
            # stays None / empty — overlay reads whichever is non-None.
            mplus_dps = None if is_healer else best_avg
            mplus_dps_med = None if is_healer else median_avg
            mplus_hps = best_avg if is_healer else None
            mplus_hps_med = median_avg if is_healer else None
            dps_breakdown = [] if is_healer else breakdown
            hps_breakdown = breakdown if is_healer else []

            raid_normal_data = (
                _raid_zone_alias_payload(char, "raidNormal")
                if metric_preferences.raid_normal
                else None
            )
            raid_heroic_data = (
                _raid_zone_alias_payload(char, "raidHeroic")
                if metric_preferences.raid_heroic
                else None
            )
            raid_mythic_data = (
                _raid_zone_alias_payload(char, "raidMythic")
                if metric_preferences.raid_mythic
                else None
            )

            return CharacterRanks(
                raid_normal=_zone_avg(raid_normal_data),
                raid_heroic=_zone_avg(raid_heroic_data),
                raid_mythic=_zone_avg(raid_mythic_data),
                raid_normal_median=_zone_avg(
                    raid_normal_data, "medianPerformanceAverage"
                ),
                raid_heroic_median=_zone_avg(
                    raid_heroic_data, "medianPerformanceAverage"
                ),
                raid_mythic_median=_zone_avg(
                    raid_mythic_data, "medianPerformanceAverage"
                ),
                mplus_dps=mplus_dps,
                mplus_hps=mplus_hps,
                mplus_dps_median=mplus_dps_med,
                mplus_hps_median=mplus_hps_med,
                mplus_dps_breakdown=dps_breakdown,
                mplus_hps_breakdown=hps_breakdown,
                )
            # Unreachable in practice (401 then non-401)
            return CharacterRanks.empty(error="auth retry exhausted")
        finally:
            self._release_quota_reservation(quota_reservation)

    def fetch_character_raid_boss_details(
        self,
        name: str,
        server_slug: str,
        spec_id: int,
        role: str = "DAMAGER",
        region: Optional[str] = None,
        metric_preferences: MetricPreferences | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        metric_preferences = metric_preferences or self.metric_preferences
        metric_preferences = MetricPreferences(
            mplus=False,
            raid_normal=metric_preferences.raid_normal,
            raid_heroic=metric_preferences.raid_heroic,
            raid_mythic=metric_preferences.raid_mythic,
        )
        if not metric_preferences.raid_enabled:
            return {}
        spec_name = SPEC_ID_TO_WCL_NAME.get(spec_id, "")
        if spec_id <= 0 or not spec_name:
            return {}
        now = time.time()
        with self._quota_lock:
            auth = self._auth
            auth_generation = self._auth_generation
        retry_block = self._active_wcl_retry_block(now)
        if retry_block is not None:
            error_kind, error_prefix, retry_until = retry_block
            raise WCLApiError(
                f"{error_prefix}; retrying in {int(retry_until - now)}s",
                error_kind=error_kind,
            )
        reservation = self._reserve_quota_for_fetch(
            self._estimate_raid_boss_detail_quota_points(metric_preferences),
            now=now,
        )
        try:
            body = {
                "query": _build_raid_boss_detail_query(role, metric_preferences),
                "variables": {
                    "name": name,
                    "serverSlug": server_slug,
                    "serverRegion": region if region is not None else self.region,
                    "specName": spec_name,
                },
            }
            resp = self._post_graphql_with_auth_retry(auth, auth_generation, body)
            data = _json_object_response(resp, WCLApiError, "WCL response")
            graphql_errors = _graphql_errors(data.get("errors"))
            data_root = data.get("data")
            if not isinstance(data_root, dict):
                graphql_result = _ranks_for_graphql_errors(graphql_errors)
                if graphql_result is not None and graphql_result.not_found:
                    return {}
                raise WCLApiError(
                    "Malformed WCL response: data is not an object",
                    error_kind=WCL_ERROR_MALFORMED,
                )
            quota = _rate_limit_info_from_dict(data_root.get("rateLimitData"))
            if quota is not None:
                self._record_quota_snapshot(quota, auth_generation=auth_generation)
            graphql_result = _ranks_for_graphql_errors(graphql_errors)
            if graphql_result is not None:
                if graphql_result.not_found:
                    return {}
                if graphql_result.error:
                    raise WCLApiError(
                        graphql_result.error,
                        error_kind=graphql_result.error_kind or WCL_ERROR_GRAPHQL,
                    )
            character_data = data_root.get("characterData")
            if not isinstance(character_data, dict):
                raise WCLApiError(
                    "Malformed WCL response: characterData is not an object",
                    error_kind=WCL_ERROR_MALFORMED,
                )
            char = character_data.get("character")
            if char is None:
                return {}
            if not isinstance(char, dict):
                raise WCLApiError(
                    "Malformed WCL response: character is not an object",
                    error_kind=WCL_ERROR_MALFORMED,
                )
            rows: dict[str, list[dict[str, object]]] = {}
            for difficulty, (_prefix, _wcl_difficulty, pref_name) in (
                _RAID_DETAIL_DIFFICULTIES.items()
            ):
                if not getattr(metric_preferences, pref_name):
                    continue
                _validate_raid_boss_detail_aliases(
                    char, difficulty, CURRENT_RAID_ENCOUNTERS
                )
                parsed = _raid_boss_rows_from_character(
                    char, difficulty, CURRENT_RAID_ENCOUNTERS, spec_name
                )
                if parsed:
                    rows[difficulty] = parsed
            return rows
        finally:
            self._release_quota_reservation(reservation)


def _safe_nonnegative_cache_int(v) -> int:
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, int):
        return v if v >= 0 else 0
    if isinstance(v, str) and v.isdecimal():
        return int(v)
    return 0


def _safe_cache_percent(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0.0 or f > 100.0:
        return None
    return f


def _dict_to_key_bracket_perf(d: dict) -> "KeyBracketPerf":
    if not isinstance(d, dict):
        return KeyBracketPerf(key_level=0, parse_percent=None)
    return KeyBracketPerf(
        key_level=_safe_nonnegative_cache_int(d.get("key_level")),
        parse_percent=_safe_cache_percent(d.get("parse_percent")),
        median_percent=_safe_cache_percent(d.get("median_percent")),
        run_count=_safe_nonnegative_cache_int(d.get("run_count")),
    )


def _dict_to_dungeon_perf(d: dict) -> "DungeonPerf":
    """Reconstructs DungeonPerf from a cache-loaded dict.

    Filters to known fields only — older cached entries (pre-run_count) lack
    the new field, dataclass __init__ would raise on unknown keys if we passed
    them blindly. Defaults from the dataclass apply for missing fields."""
    if not isinstance(d, dict):
        return DungeonPerf(name="?", parse_percent=None)

    # Cache writer emits typed values, but corrupt / hand-edited cache data must
    # not crash row rendering. Keep bad fields local: only that field defaults,
    # while the rest of the cached row stays usable.
    raw_brackets = d.get("brackets") or []
    if not isinstance(raw_brackets, list):
        raw_brackets = []
    brackets = [_dict_to_key_bracket_perf(row) for row in raw_brackets]
    brackets = [
        row
        for row in brackets
        if row.key_level > 0 and row.parse_percent is not None and row.run_count > 0
    ]

    return DungeonPerf(
        name=str(d.get("name") or "?"),
        parse_percent=_safe_cache_percent(d.get("parse_percent")),
        median_percent=_safe_cache_percent(d.get("median_percent")),
        key_level=_safe_nonnegative_cache_int(d.get("key_level")),
        run_count=_safe_nonnegative_cache_int(d.get("run_count")),
        brackets=brackets,
    )


def _project_ranks_to_metric_preferences(
    ranks: CharacterRanks,
    metric_preferences: MetricPreferences,
) -> CharacterRanks:
    projected = ranks
    if not metric_preferences.raid_normal:
        projected = replace(
            projected,
            raid_normal=None,
            raid_normal_median=None,
        )
    if not metric_preferences.raid_heroic:
        projected = replace(
            projected,
            raid_heroic=None,
            raid_heroic_median=None,
        )
    if not metric_preferences.raid_mythic:
        projected = replace(
            projected,
            raid_mythic=None,
            raid_mythic_median=None,
        )
    if not metric_preferences.mplus:
        projected = replace(
            projected,
            mplus_dps=None,
            mplus_hps=None,
            mplus_dps_median=None,
            mplus_hps_median=None,
            mplus_dps_breakdown=[],
            mplus_hps_breakdown=[],
        )
    return projected


def _metric_preference_breadth(metric_preferences: MetricPreferences) -> int:
    return sum(
        (
            metric_preferences.mplus,
            metric_preferences.raid_normal,
            metric_preferences.raid_heroic,
            metric_preferences.raid_mythic,
        )
    )


_ALL_METRIC_PREFERENCE_SCOPES = tuple(
    MetricPreferences(
        mplus=mplus,
        raid_normal=raid_normal,
        raid_heroic=raid_heroic,
        raid_mythic=raid_mythic,
    )
    for mplus in (False, True)
    for raid_normal in (False, True)
    for raid_heroic in (False, True)
    for raid_mythic in (False, True)
)

# A requested scope has at most 15 broader Boolean supersets. Cache reads use
# these exact keys instead of scanning every cached character, which keeps GUI-
# thread lookup cost bounded as the persistent cache grows during a session.
_COVERING_METRIC_SCOPE_KEYS = {
    requested.cache_key(): tuple(
        (stored.cache_key(), _metric_preference_breadth(stored))
        for stored in _ALL_METRIC_PREFERENCE_SCOPES
        if stored != requested and stored.covers(requested)
    )
    for requested in _ALL_METRIC_PREFERENCE_SCOPES
}


def _spec_norm(s: str) -> str:
    """Normalises spec-name strings for comparison (case-insensitive, no spaces).

    WCL returns `spec` field as display form ("Beast Mastery" with space, or
    "Brewmaster" single word) while our SPEC_ID_TO_WCL_NAME mapping mirrors the
    same form. Lowercase + strip spaces both sides → safe match across formats.

    Defensive against non-str inputs — callers all guard via `or ""` but a
    None / int slip past would AttributeError here and crash the whole
    breakdown build for one applicant."""
    if not isinstance(s, str):
        return ""
    return s.lower().replace(" ", "")


def _validate_raid_boss_detail_aliases(
    char: dict,
    difficulty_key: str,
    encounters: list[tuple[str, int, str]] = CURRENT_RAID_ENCOUNTERS,
) -> None:
    alias_info = _RAID_DETAIL_DIFFICULTIES.get(difficulty_key)
    if not alias_info:
        return
    alias_prefix, _wcl_difficulty, _pref_name = alias_info
    for encounter_alias, _encounter_id, _name in encounters:
        base = f"{alias_prefix}_{encounter_alias}"
        _ranking_alias_payload(char, f"{base}_overall")
        _ranking_alias_payload(char, f"{base}_ilvl")


def _raid_boss_rows_from_character(
    char: dict,
    difficulty_key: str,
    encounters: list[tuple[str, int, str]] = CURRENT_RAID_ENCOUNTERS,
    spec_name: str = "",
) -> list[dict[str, object]]:
    alias_info = _RAID_DETAIL_DIFFICULTIES.get(difficulty_key)
    if not alias_info:
        return []
    alias_prefix, _wcl_difficulty, _pref_name = alias_info
    rows: list[dict[str, object]] = []
    for encounter_alias, encounter_id, name in encounters:
        base = f"{alias_prefix}_{encounter_alias}"
        overall = _best_rank_percent(char.get(f"{base}_overall"), spec_name)
        ilvl = _best_rank_percent(char.get(f"{base}_ilvl"), spec_name)
        if overall is None and ilvl is None:
            continue
        rows.append(
            {
                "encounter_id": encounter_id,
                "name": name,
                "overall": overall,
                "ilvl": ilvl,
            }
        )
    return rows


def _best_rank_percent(enc_data: object, spec_name: str = "") -> Optional[float]:
    if not isinstance(enc_data, dict):
        return None
    ranks = enc_data.get("ranks")
    if not isinstance(ranks, list):
        return None
    wanted_spec = _spec_norm(spec_name)
    values: list[float] = []
    for rank in ranks:
        if not isinstance(rank, dict):
            continue
        if wanted_spec:
            rank_spec = rank.get("spec")
            if not isinstance(rank_spec, str) or _spec_norm(rank_spec) != wanted_spec:
                continue
        value = _safe_cache_percent(rank.get("rankPercent"))
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _process_encounter_ranks(
    enc_data: Optional[dict], spec_name: str, display_name: str
) -> Optional["DungeonPerf"]:
    """Reduces one encounterRankings response into a single DungeonPerf entry.

    enc_data: the encounter's `ranks[]` container (the value of one alias in
        the GraphQL response, e.g. `data.characterData.character.mt`).
    spec_name: applicant's current spec display name (e.g. "Brewmaster") —
        runs in OTHER specs are filtered out so a Blood DK applicant doesn't
        pick up their Unholy main's parses.
    display_name: human-friendly dungeon name for tooltip ("Magisters' Terrace").

    Algorithm:
    1. Filter ranks[] to entries matching applicant's spec (norm comparison).
    2. Find max bracketData (= player's highest timed key in this dungeon, IN
       this spec).
    3. Filter again to ranks at that exact key level.
    4. Compute best (max rankPercent), median across that subset.
    5. Return DungeonPerf with run_count = how many runs at top key.

    Returns None if no runs in current spec OR if spec_name is empty (caller
    couldn't resolve the spec_id) — overlay shows "—" rather than falling back
    to wrong-spec data (which would mislead about applicant's abilities in
    their applying role)."""
    if not isinstance(enc_data, dict):
        return None
    ranks = enc_data.get("ranks") or []
    if not isinstance(ranks, list):
        return None

    # Empty spec_name means caller couldn't resolve spec_id (unmapped retail
    # spec, or spec_id=0 debug path). Without a filter, we'd silently mix the
    # applicant's other specs into the result (Blood DK getting Unholy parses,
    # etc.) — fail loud instead. Caller already logged a warning at this point.
    if not spec_name:
        return None

    spec_norm = _spec_norm(spec_name)
    spec_runs: list[dict] = []
    for r in ranks:
        if not isinstance(r, dict):
            continue
        # Spec filter: applicant's CURRENT spec only. WCL's `spec` field on each
        # rank entry = which spec the player was in for that run.
        rspec = r.get("spec") or ""
        if not isinstance(rspec, str):
            continue
        if _spec_norm(rspec) != spec_norm:
            continue
        spec_runs.append(r)

    if not spec_runs:
        return None

    # Group all valid percentiles by key bracket. The old model kept only the
    # highest key, which lost useful target-key evidence (for example +20 grey
    # plus +16 purple when the user is hosting a +16).
    by_key: dict[int, list[float]] = {}
    for r in spec_runs:
        bd = r.get("bracketData")
        if isinstance(bd, bool) or not isinstance(bd, int) or bd <= 0:
            continue
        rp = r.get("rankPercent")
        if isinstance(rp, bool) or not isinstance(rp, (int, float)):
            continue
        pct = float(rp)
        if not math.isfinite(pct) or pct < 0.0 or pct > 100.0:
            continue
        by_key.setdefault(bd, []).append(pct)

    brackets: list[KeyBracketPerf] = []
    for key_level in sorted(by_key):
        percentiles = by_key[key_level]
        sorted_p = sorted(percentiles)
        n = len(sorted_p)
        if n % 2 == 1:
            median = sorted_p[n // 2]
        else:
            median = (sorted_p[n // 2 - 1] + sorted_p[n // 2]) / 2.0
        brackets.append(
            KeyBracketPerf(
                key_level=key_level,
                parse_percent=max(percentiles),
                median_percent=median,
                run_count=n,
            )
        )

    if not brackets:
        return None

    top = brackets[-1]

    return DungeonPerf(
        name=display_name,
        parse_percent=top.parse_percent,
        median_percent=top.median_percent,
        key_level=top.key_level,
        run_count=top.run_count,
        brackets=brackets,
    )


def _compute_mplus_headline(
    breakdown: list["DungeonPerf"],
) -> tuple[Optional[float], Optional[float]]:
    """Aggregates per-dungeon DungeonPerf list into headline (best, median).

    best_avg: mean of per-dungeon best % across ALL dungeons with data —
        represents player's ceiling. Computed even from N=1 dungeons.
    median_avg: mean of per-dungeon median % across ONLY dungeons with N >= 2
        — single-run medians equal best (no info), excluding them gives the
        verified-stable signal. Returns None if NO dungeon has N >= 2.

    Both None if breakdown is empty."""
    if not breakdown:
        return None, None

    bests = [d.parse_percent for d in breakdown if d.parse_percent is not None]
    best_avg = (sum(bests) / len(bests)) if bests else None

    medians = [
        d.median_percent
        for d in breakdown
        if d.median_percent is not None and d.run_count >= 2
    ]
    median_avg = (sum(medians) / len(medians)) if medians else None

    return best_avg, median_avg


def _zone_avg(
    zone_data: Optional[dict], key: str = "bestPerformanceAverage"
) -> Optional[float]:
    """Reads either `bestPerformanceAverage` (default) or `medianPerformanceAverage`.

    For RAIDS: avg of best parse percentile across boss encounters at the given
    difficulty. Standard "avg parse %" displayed everywhere in WCL.

    For M+: with our `specName + metric: points_and_damage/healing` filters,
    this is avg per-DUNGEON best percentile for the applicant's spec. WCL's
    score formula (key_level + DPS factor) ensures "best per dungeon" usually
    corresponds to the applicant's HIGHEST TIMED KEY in that dungeon, so the
    avg approximates "parse % of highest key timed, averaged across 8 dungeons"
    — which is what raid leads inspect when scouting M+ applicants.

    NOTE: this is DIFFERENT from `allStars[0].rankPercent` (= AllStars Key %),
    which is the percentile of the SUM of points across dungeons (a global
    score-based rank). bestPerformanceAverage gives the avg of percentiles;
    AllStars gives the percentile of the sum. They diverge for players with
    uneven dungeon coverage."""
    if not isinstance(zone_data, dict):
        return None
    val = zone_data.get(key)
    if val is None:
        return None
    return _safe_cache_percent(val)


# ───────────────────────────────────────────────────────────────────
# Server-slug derivation
# Realm names from WoW LFG API may be: bare "Charname" (same-realm), or
# "Charname-Realm" (cross-realm). Realm part may be Cyrillic for RU realms.
# WCL ServerSlug: lowercase, hyphens for spaces, no apostrophes.

# Hardcoded RU realm map — verified via WCL search per realm.
RU_REALM_MAP: dict[str, str] = {
    "Соулфлэйер": "soulflayer",
    "Ревущий фьорд": "howling-fjord",
    "Голдрин": "goldrinn",
    "Гордунни": "gordunni",
    "Страж смерти": "deathguard",
    "Ткач снов": "dreamweaver",
    "Подземье": "deepholm",
    "Седогрив": "greymane",
    "Свежеватель душ": "soulflayer",  # alt name
    "Вечная песня": "eversong",
    "Ясеневый лес": "ashenvale",
    "Лазурная стража": "azuregos",
    "Король-лич": "lich-king",
    "Черный шрам": "blackscar",
    "Пиратская бухта": "booty-bay",
    "Корона земли": "thunderhorn",
    "Галакронд": "galakrond",
    "Борейская тундра": "borean-tundra",
    "Разувий": "razuvious",
    "Термоштепсель": "termoplug",
}


def _realm_map_keys(realm: str) -> tuple[str, str]:
    lower = realm.lower()
    compact = "".join(char for char in lower if char.isalnum())
    return lower, compact


_RU_REALM_MAP_LOWER: dict[str, str] = {}
for _realm_name, _realm_slug in RU_REALM_MAP.items():
    for _realm_key in _realm_map_keys(_realm_name):
        _RU_REALM_MAP_LOWER[_realm_key] = _realm_slug


def derive_server_slug(realm_raw: str) -> str:
    """Convert WoW-side realm name to WCL server slug.

    RU map lookup is case-insensitive and also accepts WoW-normalized realm
    names without spaces/dashes. Without that, "Ревущийфьорд" falls through to a
    Cyrillic generic fallback, which is not a valid WCL server slug."""
    realm = realm_raw.strip()
    if not realm:
        return ""
    for key in _realm_map_keys(realm):
        mapped = _RU_REALM_MAP_LOWER.get(key)
        if mapped:
            return mapped
    # Generic fallback: lowercase + replace whitespace/apostrophes with hyphens
    s = realm.lower()
    s = s.replace("'", "").replace("’", "")  # straight + curly apostrophes
    s = "".join(c if c.isalnum() else "-" for c in s)
    s = "-".join(part for part in s.split("-") if part)  # collapse multi-hyphens
    return s


def split_name_realm(raw: str, default_realm: str) -> tuple[str, str]:
    """
    Splits emitted name field into (charname, realm).
    Same-realm applicants come without "-Realm" suffix; default_realm fills in.
    """
    if "-" in raw:
        parts = raw.split("-", 1)
        return parts[0].strip(), parts[1].strip()
    return raw.strip(), default_realm.strip()


def default_realm_from_player(player_full_name: str) -> str:
    """Return the realm suffix from the host character name, if present."""
    return player_full_name.split("-", 1)[-1].strip() if "-" in player_full_name else ""


def applicant_has_explicit_realm(raw: str) -> bool:
    """Return whether an emitted applicant name already carries its realm."""
    return "-" in raw


def _raid_difficulty_keys_for_preferences(
    metric_preferences: MetricPreferences,
) -> tuple[str, ...]:
    keys: list[str] = []
    if metric_preferences.raid_normal:
        keys.append("N")
    if metric_preferences.raid_heroic:
        keys.append("H")
    if metric_preferences.raid_mythic:
        keys.append("M")
    return tuple(keys)


def _sanitize_raid_boss_detail_rows(
    rows: object,
    metric_preferences: MetricPreferences,
) -> dict[str, list[dict[str, object]]]:
    if not isinstance(rows, dict):
        rows = {}
    sanitized: dict[str, list[dict[str, object]]] = {}
    for difficulty in _raid_difficulty_keys_for_preferences(metric_preferences):
        raw_rows = rows.get(difficulty, [])
        if not isinstance(raw_rows, list):
            raw_rows = []
        clean_rows: list[dict[str, object]] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                continue
            encounter_id = _safe_nonnegative_cache_int(raw_row.get("encounter_id"))
            name = raw_row.get("name")
            if encounter_id <= 0 or not isinstance(name, str) or not name.strip():
                continue
            overall = _safe_cache_percent(raw_row.get("overall"))
            ilvl = _safe_cache_percent(raw_row.get("ilvl"))
            if overall is None and ilvl is None:
                continue
            clean_rows.append(
                {
                    "encounter_id": encounter_id,
                    "name": name.strip(),
                    "overall": overall,
                    "ilvl": ilvl,
                }
            )
        sanitized[difficulty] = clean_rows
    return sanitized


# ───────────────────────────────────────────────────────────────────
# Cache (TTL + persistent)


@dataclass
class _CacheEntry:
    fetched_at: float
    ranks: dict | None = None  # asdict(CharacterRanks)
    raid_boss_details: dict | None = None


@dataclass(frozen=True)
class _CacheLookupSnapshot:
    generation: int
    negative_candidate: _CacheEntry | None
    candidates: tuple[_CacheEntry, ...]


@dataclass(frozen=True)
class _CacheSaveSnapshot:
    entries: tuple[tuple[str, _CacheEntry], ...]
    epoch: int
    generation: int


# Bumped when CharacterRanks / DungeonPerf shape or aggregation logic changes —
# old cached entries with stale schema or numbers get auto-discarded on load.
# v2 = per-encounter top-key filtering with run_count (was: zoneRankings best/median).
# v3 = cache key includes role (DPS vs HEALER) — pre-v3 entries were keyed
# without role and would silently serve DPS-shaped data when role flips
# HEALER→DAMAGER on the same character/spec.
# v4 = DungeonPerf carries per-key bracket summaries, not only the top key.
# v5 = spec 1480 maps to Devourer instead of empty placeholder; pre-v5 entries
# for that spec could contain intentionally blank M+ data.
# v6 = enabled raid aliases are strict; pre-v6 entries may contain cached empty
# raid evidence from partial WCL responses.
_CACHE_VERSION = 6


class CharacterCache:
    """Per-character TTL cache, persisted to disk. Thread-safe (QThreadPool fetches)."""

    # 12h default. WCL data updates only when a new log is uploaded; within the
    # same play session/day, re-fetching the same applicant usually spends quota
    # without changing the scouting decision. A Config-provided
    # APSCOUT_CACHE_TTL_SECONDS override is stored per CharacterCache instance.
    TTL_SECONDS = 12 * 60 * 60
    NOT_FOUND_TTL_SECONDS = 30 * 60

    def __init__(
        self,
        cache_dir: Path,
        ttl_seconds: int | None = None,
        *,
        defer_saves: bool = False,
        save_debounce_seconds: float = 1.0,
    ):
        self._path = cache_dir / "character-cache.json"
        self._ttl_seconds = ttl_seconds if ttl_seconds is not None else self.TTL_SECONDS
        self._defer_saves = defer_saves
        self._save_debounce_seconds = max(0.0, save_debounce_seconds)
        self._save_timer: threading.Timer | None = None
        self._dirty = False
        if self._path.exists():
            apply_private_file_mode(self._path)
        self._data: dict[str, _CacheEntry] = self._load()
        # Without this, two workers calling put() in parallel can race: thread A
        # iterating self._data inside json.dumps while thread B inserts a new
        # key via __setitem__ raises RuntimeError("dictionary changed size
        # during iteration"). Not caught by `except OSError` → kills the
        # QRunnable, signal never fires, applicant row stuck on "loading".
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._save_epoch = 0
        self._generation = 0
        self._closing = False
        self._close_done = threading.Event()
        self._close_result: bool | None = None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @staticmethod
    def _key_prefix(
        name: str,
        server_slug: str,
        region: str,
        spec_id: int = 0,
        role: str = "DAMAGER",
    ) -> str:
        # spec_id in key: M+ percentiles are per-spec (encounterRankings filters
        # by spec field); cache ignoring spec would serve stale data when
        # applicant re-applies under a different spec.
        # role in key: M+ query is role-aware (DPS for tank+damager, HPS for
        # healer; only ONE metric block in the response). A Mistweaver who
        # arrives as DAMAGER then re-applies as HEALER on the same listing
        # would otherwise serve stale DPS-shaped data on the second fetch
        # (mplus_hps fields stay None — UI shows "—" though the player
        # has HPS data). Tank and damager share the dps metric so they
        # cache-collide intentionally (single stored value reused across both).
        return f"{region}:{server_slug}:{name.lower()}:{spec_id}:{wcl_metric_role(role)}"

    @staticmethod
    def _not_found_key(name: str, server_slug: str, region: str) -> str:
        return f"nf:{region}:{server_slug}:{name.lower()}"

    @staticmethod
    def _key(
        name: str,
        server_slug: str,
        region: str,
        spec_id: int = 0,
        role: str = "DAMAGER",
        metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
    ) -> str:
        return (
            f"{CharacterCache._key_prefix(name, server_slug, region, spec_id, role)}:"
            f"{metric_preferences.cache_key()}"
        )

    @staticmethod
    def _raid_boss_key_prefix(
        name: str,
        server_slug: str,
        region: str,
        spec_id: int = 0,
        role: str = "DAMAGER",
    ) -> str:
        return (
            "rb:"
            f"{CharacterCache._key_prefix(name, server_slug, region, spec_id, role)}"
        )

    @staticmethod
    def _raid_boss_key(
        name: str,
        server_slug: str,
        region: str,
        spec_id: int = 0,
        role: str = "DAMAGER",
        metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
    ) -> str:
        return (
            f"{CharacterCache._raid_boss_key_prefix(name, server_slug, region, spec_id, role)}:"
            f"{metric_preferences.cache_key()}"
        )

    def _load(self) -> dict[str, _CacheEntry]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        # Schema-version gate: old format = no __version__ key, OR mismatched
        # version. Either way we discard — better one-time refetch (per
        # applicant, ~25 quota) than show wrong percentiles for 10 min.
        if raw.get("__version__") != _CACHE_VERSION:
            return {}
        entries = raw.get("entries") or {}
        if not isinstance(entries, dict):
            return {}
        # Per-entry construction (was: dict-comprehension). One corrupt entry
        # used to TypeError out of the comprehension, caught by the outer
        # try/except, returning {} — wiping the entire cache and forcing a
        # ~25-quota refetch for EVERY applicant on next session. Granular skip
        # keeps the rest of the cache intact.
        result: dict[str, _CacheEntry] = {}
        now = time.time()
        for k, v in entries.items():
            if not isinstance(v, dict):
                continue
            try:
                entry = _CacheEntry(**v)
            except (TypeError, ValueError):
                _log.debug("Discarding corrupt cache entry for key=%s", k)
                continue
            if (
                isinstance(entry.fetched_at, bool)
                or not isinstance(entry.fetched_at, (int, float))
                or not math.isfinite(float(entry.fetched_at))
            ):
                _log.debug("Discarding corrupt cache entry for key=%s", k)
                continue
            if not self._entry_is_fresh(k, entry, now=now):
                continue
            result[k] = entry
        return result

    def _entry_is_fresh(
        self,
        key: str,
        entry: _CacheEntry,
        *,
        now: float,
    ) -> bool:
        age = now - entry.fetched_at
        if age < 0:
            return False
        ttl = (
            self._not_found_ttl_seconds()
            if key.startswith("nf:")
            else self._ttl_seconds
        )
        return age <= ttl

    def _prune_expired_locked(self, now: float) -> bool:
        # Caller must hold self._lock.
        changed = False
        for key, entry in list(self._data.items()):
            if not self._entry_is_fresh(key, entry, now=now):
                self._data.pop(key, None)
                changed = True
        return changed

    def _snapshot_for_save_locked(self) -> _CacheSaveSnapshot:
        # Caller must hold self._lock.
        if self._prune_expired_locked(time.time()):
            self._save_epoch += 1
        # Cache entries are replaced, not mutated in place. The shallow snapshot
        # keeps GUI-thread cache reads from waiting on JSON serialization or disk
        # replacement while preserving a consistent key->entry view for the save.
        return _CacheSaveSnapshot(
            entries=tuple(self._data.items()),
            epoch=self._save_epoch,
            generation=self._generation,
        )

    def _snapshot_is_current_locked(self, snapshot: _CacheSaveSnapshot) -> bool:
        return (
            snapshot.epoch == self._save_epoch
            and snapshot.generation == self._generation
        )

    def _write_snapshot_with_write_lock(self, snapshot: _CacheSaveSnapshot) -> bool:
        # Caller must hold self._write_lock.
        try:
            atomic_write_text(
                self._path,
                json.dumps(
                    {
                        "__version__": _CACHE_VERSION,
                        "entries": {k: asdict(v) for k, v in snapshot.entries},
                    }
                ),
                private=True,
            )
        except OSError:
            return False
        return True

    def _start_save_timer_locked(self) -> None:
        # Caller must hold self._lock.
        if self._closing:
            return
        self._save_timer = threading.Timer(self._save_debounce_seconds, self.flush)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _schedule_save_locked(self) -> bool:
        # Caller must hold self._lock.
        self._dirty = True
        if not self._defer_saves:
            return True
        if self._save_timer is not None and self._save_timer.is_alive():
            return False
        self._start_save_timer_locked()
        return False

    def flush(self) -> bool:
        """Serialize and persist the newest dirty snapshot."""
        timer: threading.Timer | None = None
        snapshot: _CacheSaveSnapshot | None = None
        with self._write_lock:
            with self._lock:
                timer = self._save_timer
                self._save_timer = None
                if not self._dirty:
                    return True
                self._dirty = False
                snapshot = self._snapshot_for_save_locked()
            if (
                timer is not None
                and timer.is_alive()
                and timer is not threading.current_thread()
            ):
                timer.cancel()
            write_succeeded = self._write_snapshot_with_write_lock(snapshot)
            with self._lock:
                if (
                    not write_succeeded
                    and self._snapshot_is_current_locked(snapshot)
                ):
                    self._dirty = True
                if (
                    self._dirty
                    and self._defer_saves
                    and not self._closing
                    and (
                        self._save_timer is None
                        or not self._save_timer.is_alive()
                    )
                ):
                    self._start_save_timer_locked()
            return write_succeeded

    def close(self) -> bool:
        """Reject new mutations and synchronously persist all accepted writes."""
        timer: threading.Timer | None = None
        wait_for_close = False
        with self._lock:
            if self._close_result is not None:
                return self._close_result
            if self._closing:
                wait_for_close = True
            else:
                self._closing = True
                timer = self._save_timer
                self._save_timer = None
        if wait_for_close:
            self._close_done.wait()
            with self._lock:
                return bool(self._close_result)

        result = False
        try:
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
                if timer.is_alive():
                    timer.join()
            result = self.flush()
            if not result:
                # One bounded retry handles a transient failure without leaving
                # another daemon timer alive during interpreter shutdown.
                result = self.flush()
            with self._lock:
                result = result and not self._dirty
        except Exception:  # noqa: BLE001 - terminal persistence boundary
            _log.exception("Unexpected failure while closing character cache")
            result = False
        finally:
            with self._lock:
                self._close_result = result
                self._close_done.set()
        return result

    def clear(self) -> bool:
        """Drop both in-memory and persisted character-rank cache."""
        timer: threading.Timer | None = None
        with self._write_lock:
            with self._lock:
                if self._closing:
                    return False
                timer = self._save_timer
                self._save_timer = None
                self._dirty = False
                self._generation += 1
                self._save_epoch += 1
                self._data.clear()
                snapshot = self._snapshot_for_save_locked()
            if timer is not None and timer.is_alive():
                timer.cancel()
            write_succeeded = True
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError:
                write_succeeded = self._write_snapshot_with_write_lock(snapshot)
            if not write_succeeded:
                with self._lock:
                    if self._snapshot_is_current_locked(snapshot):
                        self._dirty = True
                    if (
                        self._dirty
                        and self._defer_saves
                        and not self._closing
                        and (
                            self._save_timer is None
                            or not self._save_timer.is_alive()
                        )
                    ):
                        self._start_save_timer_locked()
            return write_succeeded

    def _not_found_ttl_seconds(self) -> int:
        return min(self._ttl_seconds, self.NOT_FOUND_TTL_SECONDS)

    def _lookup_snapshot(
        self,
        *,
        prefix: str,
        not_found_key: str,
        metric_preferences: MetricPreferences,
    ) -> _CacheLookupSnapshot:
        now = time.time()
        requested_scope_key = metric_preferences.cache_key()
        exact_key = f"{prefix}:{requested_scope_key}"
        with self._lock:
            generation = self._generation
            negative_candidate = self._data.get(not_found_key)
            if negative_candidate is not None and not self._entry_is_fresh(
                not_found_key,
                negative_candidate,
                now=now,
            ):
                negative_candidate = None

            exact_candidate = self._data.get(exact_key)
            if exact_candidate is not None and not self._entry_is_fresh(
                exact_key,
                exact_candidate,
                now=now,
            ):
                exact_candidate = None

            broader_candidates: list[tuple[float, int, str, _CacheEntry]] = []
            for scope_key, breadth in _COVERING_METRIC_SCOPE_KEYS[
                requested_scope_key
            ]:
                stored_key = f"{prefix}:{scope_key}"
                entry = self._data.get(stored_key)
                if entry is None or not self._entry_is_fresh(
                    stored_key,
                    entry,
                    now=now,
                ):
                    continue
                broader_candidates.append(
                    (-entry.fetched_at, breadth, scope_key, entry)
                )

        broader_candidates.sort()
        candidates = (
            ((exact_candidate,) if exact_candidate is not None else ())
            + tuple(candidate[-1] for candidate in broader_candidates)
        )
        return _CacheLookupSnapshot(
            generation=generation,
            negative_candidate=negative_candidate,
            candidates=candidates,
        )

    def get(
        self,
        name: str,
        server_slug: str,
        region: str,
        spec_id: int = 0,
        role: str = "DAMAGER",
        metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
    ) -> Optional[CharacterRanks]:
        prefix = self._key_prefix(name, server_slug, region, spec_id, role)
        not_found_key = self._not_found_key(name, server_slug, region)
        snapshot = self._lookup_snapshot(
            prefix=prefix,
            not_found_key=not_found_key,
            metric_preferences=metric_preferences,
        )
        if snapshot.negative_candidate is not None:
            ranks = self._ranks_from_entry(snapshot.negative_candidate)
            if ranks is not None and ranks.not_found:
                with self._lock:
                    if snapshot.generation != self._generation:
                        return None
                return CharacterRanks.empty(
                    not_found=True,
                    error=ranks.error,
                    error_kind=ranks.error_kind,
                )
        if not snapshot.candidates:
            return None
        for entry in snapshot.candidates:
            ranks = self._ranks_from_entry(entry)
            if ranks is not None:
                with self._lock:
                    if snapshot.generation != self._generation:
                        return None
                return _project_ranks_to_metric_preferences(ranks, metric_preferences)
        return None

    def get_raid_boss_details(
        self,
        name: str,
        server_slug: str,
        region: str,
        spec_id: int = 0,
        role: str = "DAMAGER",
        metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
    ) -> dict[str, list[dict[str, object]]] | None:
        prefix = self._raid_boss_key_prefix(name, server_slug, region, spec_id, role)
        not_found_key = self._not_found_key(name, server_slug, region)
        snapshot = self._lookup_snapshot(
            prefix=prefix,
            not_found_key=not_found_key,
            metric_preferences=metric_preferences,
        )
        if snapshot.negative_candidate is not None:
            ranks = self._ranks_from_entry(snapshot.negative_candidate)
            if ranks is not None and ranks.not_found:
                with self._lock:
                    if snapshot.generation != self._generation:
                        return None
                return None
        if not snapshot.candidates:
            return None
        for entry in snapshot.candidates:
            rows = self._raid_boss_details_from_entry(entry, metric_preferences)
            if rows is not None:
                with self._lock:
                    if snapshot.generation != self._generation:
                        return None
                return rows
        return None

    def _ranks_from_entry(self, entry: _CacheEntry) -> Optional[CharacterRanks]:
        # Rebuild DungeonPerf list from dicts — asdict flattens dataclass
        # fields recursively, so cached breakdown is list[dict]. Without this
        # rebuild, overlay's `d.name` attribute access would fail on cache hit.
        # Defensive: any corruption (wrong type, unknown CharacterRanks fields)
        # → discard cache entry and return None instead of letting the worker
        # crash on bad data. The fetch path will refill on next request.
        try:
            if not isinstance(entry.ranks, dict):
                return None
            ranks_dict = dict(entry.ranks)
            required_scalar_fields = (
                "raid_normal",
                "raid_heroic",
                "raid_mythic",
                "raid_normal_median",
                "raid_heroic_median",
                "raid_mythic_median",
                "mplus_dps",
                "mplus_hps",
            )
            for fld in required_scalar_fields:
                if fld not in ranks_dict:
                    return None
                ranks_dict[fld] = _safe_cache_percent(ranks_dict.get(fld))
            for fld in (
                "mplus_dps_median",
                "mplus_hps_median",
            ):
                ranks_dict[fld] = _safe_cache_percent(ranks_dict.get(fld))
            for fld in ("mplus_dps_breakdown", "mplus_hps_breakdown"):
                raw = ranks_dict.get(fld) or []
                if not isinstance(raw, list):
                    raw = []
                ranks_dict[fld] = [_dict_to_dungeon_perf(d) for d in raw]
            terminal_scalar_defaults = {
                "not_found": (False, bool),
                "error": ("", str),
                "error_kind": ("", str),
            }
            for fld, (default, expected_type) in terminal_scalar_defaults.items():
                if fld not in ranks_dict:
                    ranks_dict[fld] = default
                elif not isinstance(ranks_dict[fld], expected_type):
                    return None
            return CharacterRanks(**ranks_dict)
        except (TypeError, ValueError):
            return None

    def _raid_boss_details_from_entry(
        self,
        entry: _CacheEntry,
        metric_preferences: MetricPreferences,
    ) -> dict[str, list[dict[str, object]]] | None:
        if not isinstance(entry.raid_boss_details, dict):
            return None
        return _sanitize_raid_boss_detail_rows(
            entry.raid_boss_details,
            metric_preferences,
        )

    def put(
        self,
        name: str,
        server_slug: str,
        region: str,
        spec_id: int,
        ranks: CharacterRanks,
        role: str = "DAMAGER",
        metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        key = self._key(name, server_slug, region, spec_id, role, metric_preferences)
        not_found_key = self._not_found_key(name, server_slug, region)
        with self._lock:
            if self._closing:
                return False
            if (
                expected_generation is not None
                and expected_generation != self._generation
            ):
                return False
            if ranks.not_found:
                identity_prefix = f"{region}:{server_slug}:{name.lower()}:"
                raid_boss_identity_prefix = f"rb:{identity_prefix}"
                for stored_key in list(self._data):
                    if stored_key.startswith(
                        identity_prefix
                    ) or stored_key.startswith(raid_boss_identity_prefix):
                        self._data.pop(stored_key, None)
                self._data[not_found_key] = _CacheEntry(
                    fetched_at=time.time(), ranks=asdict(ranks)
                )
            else:
                self._data.pop(not_found_key, None)
                self._data[key] = _CacheEntry(
                    fetched_at=time.time(), ranks=asdict(ranks)
                )
            self._save_epoch += 1
            flush_now = self._schedule_save_locked()
        if flush_now:
            self.flush()
        return True

    def put_raid_boss_details(
        self,
        name: str,
        server_slug: str,
        region: str,
        spec_id: int,
        rows: dict[str, list[dict[str, object]]],
        role: str = "DAMAGER",
        metric_preferences: MetricPreferences = DEFAULT_METRIC_PREFERENCES,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        key = self._raid_boss_key(
            name,
            server_slug,
            region,
            spec_id,
            role,
            metric_preferences,
        )
        sanitized = _sanitize_raid_boss_detail_rows(rows, metric_preferences)
        with self._lock:
            if self._closing:
                return False
            if (
                expected_generation is not None
                and expected_generation != self._generation
            ):
                return False
            self._data[key] = _CacheEntry(
                fetched_at=time.time(),
                raid_boss_details=sanitized,
            )
            self._save_epoch += 1
            flush_now = self._schedule_save_locked()
        if flush_now:
            self.flush()
        return True
