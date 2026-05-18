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

from .atomic_io import atomic_write_text
from .constants import (
    CURRENT_RAID_ZONE_ID,
    MPLUS_ENCOUNTERS,
    ROLE_TO_RAID_METRIC,
    SPEC_ID_TO_WCL_NAME,
)
from .metric_preferences import DEFAULT_METRIC_PREFERENCES, MetricPreferences


_log = logging.getLogger("applicant_scout.wcl")


WCL_OAUTH_URL = "https://www.warcraftlogs.com/oauth/token"
WCL_API_URL = "https://www.warcraftlogs.com/api/v2/client"


# When this fraction of the rolling-hour budget is consumed, fetch_character_ranks
# refuses new requests and returns a "quota guard" error. Acts as a soft stop
# that protects the remaining budget for any genuinely-needed fetch (e.g. a
# fresh applicant whose data we don't yet have) instead of burning it on
# refetches that would push the queue into a hard 429 → 5-min global cooldown
# (which kills the whole scout session). 0.85 leaves a 15% reserve, which
# covers ~30 applicants of headroom at typical 14-points-per-fetch cost.
QUOTA_GUARD_RATIO = 0.85

WCL_ERROR_QUOTA_GUARD = "quota_guard"
WCL_ERROR_RATE_LIMITED = "rate_limited"
WCL_ERROR_AUTH = "auth"
WCL_ERROR_SERVER = "server"
WCL_ERROR_MALFORMED = "malformed"
WCL_ERROR_GRAPHQL = "graphql"
WCL_ERROR_NETWORK = "network"
WCL_ERROR_HTTP = "http"
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
        self._token: Optional[_Token] = self._load_cached()
        # Two QRunnable workers can hit get_token() simultaneously for the
        # first request after token expiry. Without this lock both would call
        # _refresh — wasted HTTP roundtrip + theoretical race on token.json
        # write. Lock keeps the second caller waiting for the first refresh
        # to finish, then returns the same fresh token.
        self._refresh_lock = threading.Lock()

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

    def _save_cached(self) -> None:
        if self._token is None:
            return
        # Failure is non-fatal — the fresh token is still in memory and will
        # serve the next get_token call. Only side effect of a missed save is
        # one extra OAuth refresh on next process start (~1s). Better than
        # propagating OSError up through fetch_character_ranks and surfacing
        # "Permission denied: token.json" to the user as a WCL fetch error.
        try:
            atomic_write_text(
                self._token_path,
                json.dumps(asdict(self._token)),
                private=True,
            )
        except OSError as e:
            _log.warning("Could not persist OAuth token cache: %s", e)

    def get_token(self) -> str:
        """Returns valid access token, refreshing if within 60s of expiry."""
        # Fast path: cached & valid → no lock contention.
        if self._token and self._token.expires_at - 60 > time.time():
            return self._token.access_token
        with self._refresh_lock:
            # Double-check inside the lock: a parallel caller may have just
            # refreshed; if so we serve the new token without firing again.
            if self._token and self._token.expires_at - 60 > time.time():
                return self._token.access_token
            return self._refresh()

    def invalidate(self) -> None:
        """Force refresh on next get_token (call on 401 response)."""
        self._token = None
        if self._token_path.exists():
            try:
                self._token_path.unlink()
            except OSError:
                pass

    def _refresh(self) -> str:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                WCL_OAUTH_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
            )
        if resp.status_code != 200:
            raise WCLAuthError(
                f"OAuth failed (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        body = _json_object_response(resp, WCLAuthError, "OAuth response")
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise WCLAuthError("OAuth response missing access_token")
        expires_raw = body.get("expires_in", 86400)
        if isinstance(expires_raw, bool):
            raise WCLAuthError("OAuth response has invalid expires_in")
        try:
            expires_in = int(expires_raw)
        except (TypeError, ValueError, OverflowError):
            raise WCLAuthError("OAuth response has invalid expires_in") from None
        if expires_in <= 0:
            raise WCLAuthError("OAuth response has invalid expires_in")
        self._token = _Token(
            access_token=access_token.strip(),
            expires_at=time.time() + expires_in,
            client_fingerprint=self._client_fingerprint,
        )
        self._save_cached()
        return self._token.access_token


class WCLAuthError(Exception):
    pass


class WCLApiError(Exception):
    def __init__(self, message: str, *, error_kind: str = ""):
        super().__init__(message)
        self.error_kind = error_kind


def _json_object_response(resp, error_cls: type[Exception], context: str) -> dict:
    try:
        body = resp.json()
    except ValueError as e:
        if error_cls is WCLApiError:
            raise error_cls(
                f"Malformed {context}: invalid JSON",
                error_kind=WCL_ERROR_MALFORMED,
            ) from e
        raise error_cls(f"Malformed {context}: invalid JSON") from e
    if not isinstance(body, dict):
        if error_cls is WCLApiError:
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


def _graphql_error_messages(errors) -> list[str]:
    if not errors:
        return []
    raw_errors = errors if isinstance(errors, list) else [errors]
    messages: list[str] = []
    for entry in raw_errors:
        if isinstance(entry, dict):
            raw_message = entry.get("message")
            if isinstance(raw_message, str) and raw_message.strip():
                messages.append(raw_message.strip())
            else:
                messages.append("unknown error")
        elif isinstance(entry, str) and entry.strip():
            messages.append(entry.strip())
        else:
            messages.append("unknown error")
    return messages


def _ranks_for_graphql_error_messages(messages: list[str]) -> CharacterRanks | None:
    if not messages:
        return None
    msg = messages[0]
    low = msg.lower()
    if "not found" in low or "could not find" in low:
        return CharacterRanks.empty(not_found=True, error=msg)
    raise WCLApiError(
        f"GraphQL error: {msg}",
        error_kind=WCL_ERROR_GRAPHQL,
    )


# ───────────────────────────────────────────────────────────────────
# GraphQL


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

    def close(self) -> None:
        self._http.close()

    def reconfigure_auth(self, auth: WCLAuth) -> None:
        with self._quota_lock:
            self._auth = auth
            self._auth_generation += 1
            self._rate_limited_until = 0.0
            self._server_retry_until = 0.0
            self._network_retry_until = 0.0
            self.last_quota = None
            self._quota_snapshot = None
            self._reserved_quota_points = 0.0

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
        with self._quota_lock:
            quota = self.last_quota
            reserved = self._reserved_quota_points
        if quota is None or quota.limit_per_hour <= 0:
            return 0.0
        ratio = (quota.points_spent + reserved) / quota.limit_per_hour
        if ratio < QUOTA_GUARD_RATIO:
            return 0.0
        return self.quota_reset_remaining_seconds(now=now) or 0.0

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

    def _quota_guard_error_locked(
        self, now: float
    ) -> CharacterRanks | None:
        quota = self.last_quota
        snapshot = self._quota_snapshot
        if quota is None or quota.limit_per_hour <= 0 or snapshot is None:
            return None
        elapsed = max(0.0, now - snapshot.observed_at)
        remaining = max(0.0, quota.reset_in_seconds - elapsed)
        if remaining <= 0:
            return None
        usage = quota.points_spent + self._reserved_quota_points
        ratio = usage / quota.limit_per_hour
        if ratio < QUOTA_GUARD_RATIO:
            return None
        return CharacterRanks.empty(
            error=f"WCL quota guard {int(ratio * 100)}% used — pausing"
            f" fetches; resets in {int(remaining / 60)}m",
            error_kind=WCL_ERROR_QUOTA_GUARD,
        )

    def _reserve_quota_for_fetch(
        self, points: float, now: float | None = None
    ) -> CharacterRanks | _QuotaReservation:
        current_time = time.time() if now is None else now
        with self._quota_lock:
            guard_error = self._quota_guard_error_locked(current_time)
            if guard_error is not None:
                return guard_error
            reserved = max(0.0, points)
            self._reserved_quota_points += reserved
            return _QuotaReservation(points=reserved)

    def _release_quota_reservation(self, reservation: _QuotaReservation) -> None:
        if reservation.points <= 0:
            return
        with self._quota_lock:
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

        # Quota-aware gate: when we've used >=QUOTA_GUARD_RATIO of the rolling-
        # hour budget, refuse new fetches and surface a guard error instead of
        # racing toward a hard 429 (which triggers a 5-min full cooldown that
        # blocks ALL applicants). The budget recovers gradually as the rolling
        # window slides, so the gate auto-lifts within minutes once usage falls
        # below the threshold. Only kicks in if we have a real quota snapshot
        # (limit_per_hour > 0) — first fetch of a session always passes.
        quota_reservation = self._reserve_quota_for_fetch(
            self._estimate_query_quota_points(metric_preferences),
            now=now,
        )
        if isinstance(quota_reservation, CharacterRanks):
            return quota_reservation

        try:
            # Resolve region once: explicit param wins, else snapshot self.region
            # AT METHOD ENTRY (single read). Without snapshotting, a state-machine
            # versionUpdated firing between this point and the GraphQL POST below
            # would query a different region than the caller passed to cache.get.
            region_used = region if region is not None else self.region
            raid_metric = ROLE_TO_RAID_METRIC.get(role, "dps")
            spec_name = SPEC_ID_TO_WCL_NAME.get(spec_id, "")
            # Unknown / unmapped spec_id: SPEC_ID_TO_WCL_NAME returns "" so the
            # downstream spec filter would silently let all of the applicant's
            # OTHER specs into the result. Log loud — _process_encounter_ranks
            # short-circuits to None (M+ cell shows "—") rather than ship wrong-spec
            # numbers. Trips for unmapped retail spec_ids (future expansions, or
            # garbage values from a corrupted snapshot).
            if spec_id != 0 and not spec_name:
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

            for attempt in range(2):
                token = auth.get_token()
                try:
                    resp = self._http.post(
                        WCL_API_URL,
                        json=body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                except (httpx.TimeoutException, httpx.RequestError):
                    with self._quota_lock:
                        if auth_generation == self._auth_generation:
                            self._network_retry_until = (
                                time.time() + WCL_NETWORK_RETRY_SECONDS
                            )
                    raise
                if resp.status_code == 401 and attempt == 0:
                    auth.invalidate()
                    continue
                if resp.status_code in (401, 403):
                    raise WCLApiError(
                        f"Authentication failed (HTTP {resp.status_code})",
                        error_kind=WCL_ERROR_AUTH,
                    )
                if resp.status_code == 429:
                    with self._quota_lock:
                        if auth_generation == self._auth_generation:
                            self._rate_limited_until = time.time() + 300
                    raise WCLApiError(
                        "Rate limited (HTTP 429) — cooldown 5min",
                        error_kind=WCL_ERROR_RATE_LIMITED,
                    )
                if resp.status_code >= 500:
                    with self._quota_lock:
                        if auth_generation == self._auth_generation:
                            self._server_retry_until = time.time() + WCL_SERVER_RETRY_SECONDS
                    raise WCLApiError(
                        f"Server error (HTTP {resp.status_code})",
                        error_kind=WCL_ERROR_SERVER,
                    )
                if resp.status_code != 200:
                    raise WCLApiError(
                        f"Unexpected HTTP {resp.status_code}: {resp.text[:200]}",
                        error_kind=WCL_ERROR_HTTP,
                    )
                break

            data = _json_object_response(resp, WCLApiError, "WCL response")
            messages = _graphql_error_messages(data.get("errors"))
            # Update quota snapshot regardless of errors — rateLimitData is at
            # the root, present even on GraphQL-level errors (HTTP 200).
            data_root_obj = data.get("data")
            if not isinstance(data_root_obj, dict):
                graphql_result = _ranks_for_graphql_error_messages(messages)
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
            graphql_result = _ranks_for_graphql_error_messages(messages)
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
                        char.get(alias), spec_name, dungeon_name
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

            return CharacterRanks(
                raid_normal=_zone_avg(char.get("raidNormal"))
                if metric_preferences.raid_normal
                else None,
                raid_heroic=_zone_avg(char.get("raidHeroic"))
                if metric_preferences.raid_heroic
                else None,
                raid_mythic=_zone_avg(char.get("raidMythic"))
                if metric_preferences.raid_mythic
                else None,
                raid_normal_median=_zone_avg(
                    char.get("raidNormal"), "medianPerformanceAverage"
                )
                if metric_preferences.raid_normal
                else None,
                raid_heroic_median=_zone_avg(
                    char.get("raidHeroic"), "medianPerformanceAverage"
                )
                if metric_preferences.raid_heroic
                else None,
                raid_mythic_median=_zone_avg(
                    char.get("raidMythic"), "medianPerformanceAverage"
                )
                if metric_preferences.raid_mythic
                else None,
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


# ───────────────────────────────────────────────────────────────────
# Cache (TTL + persistent)


@dataclass
class _CacheEntry:
    fetched_at: float
    ranks: dict  # asdict(CharacterRanks)


# Bumped when CharacterRanks / DungeonPerf shape or aggregation logic changes —
# old cached entries with stale schema or numbers get auto-discarded on load.
# v2 = per-encounter top-key filtering with run_count (was: zoneRankings best/median).
# v3 = cache key includes role (DPS vs HEALER) — pre-v3 entries were keyed
# without role and would silently serve DPS-shaped data when role flips
# HEALER→DAMAGER on the same character/spec.
# v4 = DungeonPerf carries per-key bracket summaries, not only the top key.
# v5 = spec 1480 maps to Devourer instead of empty placeholder; pre-v5 entries
# for that spec could contain intentionally blank M+ data.
_CACHE_VERSION = 5


class CharacterCache:
    """Per-character TTL cache, persisted to disk. Thread-safe (QThreadPool fetches)."""

    # 12h default. WCL data updates only when a new log is uploaded; within the
    # same play session/day, re-fetching the same applicant usually spends quota
    # without changing the scouting decision. A Config-provided
    # APSCOUT_CACHE_TTL_SECONDS override is stored per CharacterCache instance.
    TTL_SECONDS = 12 * 60 * 60

    def __init__(self, cache_dir: Path, ttl_seconds: int | None = None):
        self._path = cache_dir / "character-cache.json"
        self._ttl_seconds = ttl_seconds if ttl_seconds is not None else self.TTL_SECONDS
        self._data: dict[str, _CacheEntry] = self._load()
        # Without this, two workers calling put() in parallel can race: thread A
        # iterating self._data inside json.dumps while thread B inserts a new
        # key via __setitem__ raises RuntimeError("dictionary changed size
        # during iteration"). Not caught by `except OSError` → kills the
        # QRunnable, signal never fires, applicant row stuck on "loading".
        self._lock = threading.Lock()
        self._generation = 0

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

    def _load(self) -> dict[str, _CacheEntry]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
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
            result[k] = entry
        return result

    def _save_locked(self) -> None:
        # Caller must hold self._lock.
        try:
            atomic_write_text(
                self._path,
                json.dumps(
                    {
                        "__version__": _CACHE_VERSION,
                        "entries": {k: asdict(v) for k, v in self._data.items()},
                    }
                ),
            )
        except OSError:
            pass

    def clear(self) -> None:
        """Drop both in-memory and persisted character-rank cache."""
        with self._lock:
            self._generation += 1
            self._data.clear()
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError:
                self._save_locked()

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
        key = f"{prefix}:{metric_preferences.cache_key()}"
        now = time.time()
        with self._lock:
            candidates: list[tuple[int, float, int, _CacheEntry]] = []
            exact_entry = self._data.get(key)
            if exact_entry is not None and now - exact_entry.fetched_at <= self._ttl_seconds:
                candidates.append((0, -exact_entry.fetched_at, -1, exact_entry))
            for index, (stored_key, entry) in enumerate(self._data.items()):
                if stored_key == key or not stored_key.startswith(f"{prefix}:"):
                    continue
                stored_raw = stored_key[len(prefix) + 1 :]
                stored_preferences = MetricPreferences.from_cache_key(stored_raw)
                if stored_preferences is None:
                    continue
                if not stored_preferences.covers(metric_preferences):
                    continue
                if now - entry.fetched_at > self._ttl_seconds:
                    continue
                breadth = _metric_preference_breadth(stored_preferences)
                candidates.append((breadth, -entry.fetched_at, index, entry))
        if not candidates:
            return None
        candidates.sort()
        for _breadth, _fetched_at, _index, entry in candidates:
            ranks = self._ranks_from_entry(entry)
            if ranks is not None:
                return _project_ranks_to_metric_preferences(ranks, metric_preferences)
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
            return CharacterRanks(**ranks_dict)
        except (TypeError, ValueError):
            return None

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
        with self._lock:
            if (
                expected_generation is not None
                and expected_generation != self._generation
            ):
                return False
            self._data[key] = _CacheEntry(fetched_at=time.time(), ranks=asdict(ranks))
            self._save_locked()
        return True
