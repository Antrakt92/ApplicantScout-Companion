"""Shared normalization and comparison for snapshot producer identities."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

from .constants import REGION_ID_TO_WCL
from .state import WoWPlayer

if TYPE_CHECKING:
    from .screenshot import DecodedVersion


ProducerIdentity: TypeAlias = tuple[str, str, str | None]
_PLACEHOLDER_TRANSPORT_NAMES = frozenset({"?", "unknown", "unknownobject"})


def is_placeholder_transport_identity(player_name: object) -> bool:
    identity = str(player_name or "").strip()
    if not identity:
        return False
    base = identity.split("-", 1)[0].strip().casefold()
    return base in _PLACEHOLDER_TRANSPORT_NAMES


def resolve_transport_player(old_player: WoWPlayer, version: DecodedVersion) -> WoWPlayer:
    """Resolve omitted producer fields against the immediately preceding player."""
    old_full_name = old_player.full_name.strip()
    incoming_full_name = version.player_name.strip()
    old_identity_is_valid = bool(old_full_name) and not (
        is_placeholder_transport_identity(old_full_name)
    )
    incoming_identity_is_valid = bool(incoming_full_name) and not (
        is_placeholder_transport_identity(incoming_full_name)
    )

    resolved_full_name = incoming_full_name
    resolved_region_id = version.region_id
    old_region_is_valid = REGION_ID_TO_WCL.get(old_player.region_id) is not None
    incoming_region_is_valid = REGION_ID_TO_WCL.get(version.region_id) is not None

    if not incoming_identity_is_valid:
        resolved_full_name = old_full_name if old_identity_is_valid else ""
        if old_identity_is_valid and old_region_is_valid:
            resolved_region_id = old_player.region_id
    elif old_identity_is_valid:
        incoming_name, separator, _incoming_realm = incoming_full_name.partition("-")
        old_name, old_separator, _old_realm = old_full_name.partition("-")
        region_changed = (
            old_region_is_valid
            and incoming_region_is_valid
            and old_player.region_id != version.region_id
        )
        if (
            not separator
            and old_separator
            and incoming_name.casefold() == old_name.casefold()
            and not region_changed
        ):
            resolved_full_name = old_full_name

    if not incoming_region_is_valid and old_region_is_valid:
        resolved_region_id = old_player.region_id

    return WoWPlayer(
        addon_version=version.addon_version,
        game_version=version.game_version,
        region_id=resolved_region_id,
        full_name=resolved_full_name,
    )


def normalize_producer_identity(
    player_name: object,
    region_id: object,
) -> ProducerIdentity:
    player_identity = str(player_name or "").strip().casefold()
    name, separator, realm = player_identity.partition("-")
    region = REGION_ID_TO_WCL.get(region_id) if isinstance(region_id, int) else None
    if is_placeholder_transport_identity(player_identity):
        return "", "", region
    return name, realm if separator else "", region


def producer_identities_conflict(
    left: ProducerIdentity,
    right: ProducerIdentity,
) -> bool:
    left_name, left_realm, left_region = left
    right_name, right_realm, right_region = right
    return bool(
        left_name
        and right_name
        and (
            left_name != right_name
            or (left_realm and right_realm and left_realm != right_realm)
            or (left_region and right_region and left_region != right_region)
        )
    )


def producer_identity_matches(
    candidate: ProducerIdentity,
    reference: ProducerIdentity,
) -> bool:
    candidate_name, candidate_realm, candidate_region = candidate
    reference_name, reference_realm, reference_region = reference
    return bool(
        candidate_name
        and candidate_name == reference_name
        and (not reference_realm or candidate_realm == reference_realm)
        and (not reference_region or candidate_region == reference_region)
    )
