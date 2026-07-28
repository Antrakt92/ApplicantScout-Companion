"""Shared normalization and comparison for snapshot producer identities."""

from __future__ import annotations

from typing import TypeAlias

from .constants import REGION_ID_TO_WCL


ProducerIdentity: TypeAlias = tuple[str, str, str | None]


def normalize_producer_identity(
    player_name: object,
    region_id: object,
) -> ProducerIdentity:
    player_identity = str(player_name or "").strip().casefold()
    name, separator, realm = player_identity.partition("-")
    region = REGION_ID_TO_WCL.get(region_id) if isinstance(region_id, int) else None
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
