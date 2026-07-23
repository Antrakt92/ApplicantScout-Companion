"""Snapshot coalescing, producer isolation, and GUI-apply queue."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from .constants import REGION_ID_TO_WCL
from .screenshot import Snapshot


_SNAPSHOT_AUTHORITY_LISTING = 1 << 0
_SNAPSHOT_AUTHORITY_APPLICANTS = 1 << 1
_SNAPSHOT_AUTHORITY_ROSTER = 1 << 2
_SNAPSHOT_AUTHORITY_VERSION = 1 << 3
_SNAPSHOT_AUTHORITY_LEADER = 1 << 4
_SNAPSHOT_AUTHORITY_LISTING_SEED = 1 << 5


class SignalGate(Protocol):
    def is_current(self, generation: int) -> bool: ...


def snapshot_carries_leader_update(snap: object) -> bool:
    if bool(getattr(snap, "terminal_clear", False)) or not bool(
        getattr(snap, "lfg_unavailable", False)
    ):
        return True
    leader_key = getattr(snap, "leader_key", None)
    key_level = getattr(leader_key, "key_level", None)
    return isinstance(key_level, int) and key_level > 0


def snapshot_authority_mask(snap: object) -> int:
    terminal_clear = bool(getattr(snap, "terminal_clear", False))
    lfg_unavailable = bool(getattr(snap, "lfg_unavailable", False))
    roster_unavailable = bool(getattr(snap, "roster_unavailable", False))
    applicants_unavailable = bool(
        getattr(snap, "applicants_unavailable", False)
    )
    authority = 0
    if terminal_clear or not lfg_unavailable:
        authority |= (
            _SNAPSHOT_AUTHORITY_LISTING
            | _SNAPSHOT_AUTHORITY_LEADER
            | _SNAPSHOT_AUTHORITY_LISTING_SEED
        )
        if terminal_clear or not applicants_unavailable:
            authority |= _SNAPSHOT_AUTHORITY_APPLICANTS
    else:
        # A restricted LFG read can still carry independently useful context.
        if snapshot_carries_leader_update(snap):
            authority |= _SNAPSHOT_AUTHORITY_LEADER
        if getattr(snap, "listing", None) is not None:
            authority |= _SNAPSHOT_AUTHORITY_LISTING_SEED
    if terminal_clear or not roster_unavailable:
        authority |= _SNAPSHOT_AUTHORITY_ROSTER
    if getattr(snap, "version", None) is not None:
        authority |= _SNAPSHOT_AUTHORITY_VERSION
    return authority


def compact_snapshot_segment(snapshots: tuple[object, ...]) -> tuple[object, ...]:
    """Keep the newest observation plus each older snapshot with unique authority."""
    covered = 0
    retained_reversed: list[object] = []
    for snap in reversed(snapshots):
        authority = snapshot_authority_mask(snap)
        if not retained_reversed or authority & ~covered:
            retained_reversed.append(snap)
            covered |= authority
    retained_reversed.reverse()
    return tuple(retained_reversed)


def append_pending_snapshot(
    pending: tuple[object, ...],
    snap: object,
) -> tuple[object, ...]:
    # A clear invalidates every earlier state event, but must itself survive a
    # later partial/full frame so listing-session and waiter cleanup still runs.
    if bool(getattr(snap, "terminal_clear", False)):
        return (snap,)
    if pending and bool(getattr(pending[0], "terminal_clear", False)):
        return (pending[0],) + compact_snapshot_segment(pending[1:] + (snap,))
    return compact_snapshot_segment(pending + (snap,))


def version_producer_identity(version: object) -> tuple[str, str, str | None]:
    player_identity = str(getattr(version, "player_name", "")).strip().casefold()
    player_name, separator, player_realm = player_identity.partition("-")
    region_id = getattr(version, "region_id", None)
    region = REGION_ID_TO_WCL.get(region_id) if isinstance(region_id, int) else None
    return player_name, player_realm if separator else "", region


def producer_identities_conflict(left: object, right: object) -> bool:
    left_name, left_realm, left_region = version_producer_identity(left)
    right_name, right_realm, right_region = version_producer_identity(right)
    return bool(
        left_name
        and right_name
        and (
            left_name != right_name
            or (left_realm and right_realm and left_realm != right_realm)
            or (left_region and right_region and left_region != right_region)
        )
    )


def producer_identity_matches(candidate: object, reference: object) -> bool:
    candidate_name, candidate_realm, candidate_region = version_producer_identity(
        candidate
    )
    reference_name, reference_realm, reference_region = version_producer_identity(
        reference
    )
    return bool(
        candidate_name
        and candidate_name == reference_name
        and (not reference_realm or candidate_realm == reference_realm)
        and (not reference_region or candidate_region == reference_region)
    )


def latest_producer_segment(snapshots: tuple[Snapshot, ...]) -> tuple[Snapshot, ...]:
    versioned = tuple(
        (index, snap.version)
        for index, snap in enumerate(snapshots)
        if snap.version is not None
    )
    if len(versioned) < 2:
        return snapshots
    latest_index, latest_version = versioned[-1]
    start_index = latest_index
    conflict_found = False
    for index, version in reversed(versioned[:-1]):
        if producer_identities_conflict(version, latest_version):
            conflict_found = True
            break
        if producer_identity_matches(version, latest_version):
            start_index = index
    return snapshots[start_index:] if conflict_found else snapshots


def merge_snapshot_segment(snapshots: tuple[Snapshot, ...]) -> Snapshot:
    """Compose final in-memory authority without fabricating a cache snapshot."""
    snapshots = latest_producer_segment(snapshots)
    latest = snapshots[-1]
    listing_source = next(
        (snap for snap in reversed(snapshots) if not snap.lfg_unavailable),
        None,
    )
    applicants_source = None
    if listing_source is not None:
        applicants_source = next(
            (
                snap
                for snap in reversed(snapshots)
                if not snap.lfg_unavailable
                and not snap.applicants_unavailable
                and snap.listing == listing_source.listing
            ),
            None,
        )
    roster_source = next(
        (snap for snap in reversed(snapshots) if not snap.roster_unavailable),
        None,
    )
    version = next(
        (snap.version for snap in reversed(snapshots) if snap.version is not None),
        None,
    )
    leader_source = next(
        (
            snap
            for snap in reversed(snapshots)
            if snapshot_carries_leader_update(snap)
        ),
        None,
    )
    listing_seed_source = None
    if listing_source is None:
        listing_seed_source = next(
            (snap for snap in reversed(snapshots) if snap.listing is not None),
            None,
        )
    return replace(
        latest,
        listing=(
            listing_source.listing
            if listing_source is not None
            else (
                listing_seed_source.listing
                if listing_seed_source is not None
                else None
            )
        ),
        version=version,
        leader_key=(leader_source.leader_key if leader_source is not None else None),
        applicants=(
            list(applicants_source.applicants)
            if applicants_source is not None
            else []
        ),
        roster=list(roster_source.roster) if roster_source is not None else [],
        terminal_clear=False,
        lfg_unavailable=listing_source is None,
        roster_unavailable=roster_source is None,
        applicants_unavailable=applicants_source is None,
    )


def snapshot_application_plan(
    snapshots: tuple[object, ...],
    cache_snapshots: tuple[Snapshot, ...],
) -> tuple[tuple[object, tuple[object, ...]], ...]:
    """Build state-application steps while retaining original cache inputs."""
    typed_snapshots = tuple(
        snap for snap in snapshots if isinstance(snap, Snapshot)
    )
    if len(typed_snapshots) != len(snapshots):
        return tuple((snap, (snap,)) for snap in snapshots)

    steps: list[tuple[object, tuple[object, ...]]] = []
    segment = typed_snapshots
    cache_segment = cache_snapshots
    if segment and segment[0].terminal_clear:
        terminal = segment[0]
        segment = segment[1:]
        cache_terminal: tuple[object, ...] = ()
        if cache_segment and cache_segment[0].terminal_clear:
            cache_terminal = (cache_segment[0],)
            cache_segment = cache_segment[1:]
        planned_terminal = terminal
        if segment and any(snap.version is not None for snap in segment):
            planned_terminal = replace(terminal, version=None)
        steps.append((planned_terminal, cache_terminal))
    if segment:
        steps.append((merge_snapshot_segment(segment), tuple(cache_segment)))
    return tuple(steps)


class SnapshotApplyQueue:
    def __init__(
        self,
        machine: Any,
        window: Any,
        decode_failed_callback: Callable[[str, str], None],
        *,
        signal_gate: SignalGate,
        generation: int,
        live_snapshot_cache_writer: Any | None = None,
        scheduler: Callable[[Callable[[], None]], None],
    ) -> None:
        self._machine = machine
        self._window = window
        self._decode_failed_callback = decode_failed_callback
        self._signal_gate = signal_gate
        self._generation = generation
        self._live_snapshot_cache_writer = live_snapshot_cache_writer
        self._scheduler = scheduler
        self._pending: tuple[str, tuple[object, ...]] | None = None
        self._pending_cache_snapshots: tuple[Snapshot, ...] = ()
        self._flush_pending = False

    def enqueue_snapshot(self, snap: object) -> None:
        pending_snapshots: tuple[object, ...] = ()
        if self._pending is not None and self._pending[0] == "snapshot":
            pending_snapshots = self._pending[1]
        else:
            self._pending_cache_snapshots = ()
        if isinstance(snap, Snapshot):
            if snap.terminal_clear:
                self._pending_cache_snapshots = (snap,)
            else:
                self._pending_cache_snapshots += (snap,)
        self._pending = (
            "snapshot",
            append_pending_snapshot(pending_snapshots, snap),
        )
        self._schedule_flush()

    def enqueue_decode_failed(self, path: str, reason: str) -> None:
        # WHY: a decode failure has no usable state. If a valid snapshot is
        # already waiting for the GUI flush, keep it rather than turning a
        # good-frame-plus-bad-frame burst into no state update.
        if self._pending is not None and self._pending[0] == "snapshot":
            return
        self._pending = ("decode_failed", (path, reason))
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_pending:
            return
        self._flush_pending = True
        self._scheduler(self.flush)

    def flush(self) -> None:
        if self._pending is None:
            self._flush_pending = False
            return
        kind, args = self._pending
        cache_snapshots = self._pending_cache_snapshots
        self._pending = None
        self._pending_cache_snapshots = ()
        self._flush_pending = False
        if not self._signal_gate.is_current(self._generation):
            return
        if kind == "snapshot":
            latest_snap = args[-1]
            getattr(self._window, "note_decode", lambda *_args: None)(latest_snap)
            for snap, step_cache_snapshots in snapshot_application_plan(
                args,
                cache_snapshots,
            ):
                if not self._signal_gate.is_current(self._generation):
                    return
                getattr(self._machine, "apply_snapshot", lambda *_args: None)(snap)
                if not self._signal_gate.is_current(self._generation):
                    return
                if self._live_snapshot_cache_writer is not None:
                    for cache_snap in step_cache_snapshots:
                        if isinstance(cache_snap, Snapshot):
                            self._live_snapshot_cache_writer.submit(cache_snap)
                getattr(
                    self._window,
                    "note_snapshot_applied",
                    lambda *_args: None,
                )(snap)
            return
        path, reason = args
        self._decode_failed_callback(str(path), str(reason))
        getattr(self._window, "note_decode_failed", lambda *_args: None)(
            str(path),
            str(reason),
        )
