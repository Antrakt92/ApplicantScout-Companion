"""P4 regression tests: SnapshotFlags + _parse_payload block split.

Covers the flags struct path, the legacy boolean-trap kwargs compat wrapper,
and the extracted listing/applicants/roster block parsers. Pure refactor
coverage — byte layouts mirror the addon BuildPayload contract.
"""

from __future__ import annotations

import struct

import pytest

from applicant_scout.screenshot import (
    SnapshotFlags,
    _parse_applicants_block,
    _parse_listing_block,
    _parse_payload,
    _parse_roster_block,
)


def _pack_len_str(raw: bytes) -> bytes:
    return bytes([len(raw)]) + raw


def _listing_bytes(*, wire_ver: int) -> bytes:
    out = bytes([1])  # has_listing
    out += struct.pack(">I", 1234)
    if wire_ver >= 0x03:
        out += struct.pack(">H", 7)
        out += struct.pack(">H", 9)
    out += bytes([15])
    out += _pack_len_str(b"Dungeon")
    out += _pack_len_str(b"Listing")
    out += _pack_len_str(b"Comment")
    return out


def _applicant_bytes(*, wire_ver: int, member_idx: int = 1) -> bytes:
    out = struct.pack(">I", 42)
    if wire_ver >= 0x02:
        out += bytes([member_idx])
    out += bytes([3])
    out += struct.pack(">H", 71)
    out += struct.pack(">H", 600)
    out += struct.pack(">H", 2500)
    if wire_ver >= 0x04:
        out += struct.pack(">H", 2600)
    if wire_ver >= 0x05:
        out += bytes([0, 0, 0, 0, 0, 0, 0, 0])
    out += bytes([1])
    out += _pack_len_str("Player-One".encode("utf-8"))
    return out


def _roster_bytes() -> bytes:
    out = bytes([0, 1, 2, 4])
    out += struct.pack(">H", 72)
    out += struct.pack(">H", 610)
    out += struct.pack(">H", 2550)
    out += struct.pack(">H", 2650)
    out += bytes([0, 0, 0, 0, 0, 0, 0, 0])
    out += bytes([2])
    out += _pack_len_str("Roster-Mate".encode("utf-8"))
    return out


def _v6_body() -> bytes:
    body = _listing_bytes(wire_ver=0x06)
    body += bytes([0])  # has_version=0
    applicant = _applicant_bytes(wire_ver=0x06)
    body += struct.pack(">H", 1) + applicant
    body += struct.pack(">H", 1) + _roster_bytes()
    return body


def test_snapshot_flags_default_to_false():
    flags = SnapshotFlags()
    assert flags == SnapshotFlags(
        terminal_clear=False,
        lfg_unavailable=False,
        roster_unavailable=False,
        applicants_unavailable=False,
    )


def test_flags_path_matches_legacy_kwargs():
    body = _v6_body()
    legacy = _parse_payload(
        body,
        wire_ver=0x06,
        terminal_clear=True,
        lfg_unavailable=True,
        roster_unavailable=True,
        applicants_unavailable=True,
    )
    via_flags = _parse_payload(
        body,
        wire_ver=0x06,
        flags=SnapshotFlags(
            terminal_clear=True,
            lfg_unavailable=True,
            roster_unavailable=True,
            applicants_unavailable=True,
        ),
    )
    assert via_flags == legacy
    assert via_flags.terminal_clear is True
    assert via_flags.lfg_unavailable is True
    assert via_flags.roster_unavailable is True
    assert via_flags.applicants_unavailable is True
    assert via_flags.listing is not None
    assert via_flags.listing.activity_id == 1234
    assert len(via_flags.applicants) == 1
    assert len(via_flags.roster) == 1


def test_flags_win_over_legacy_kwargs():
    body = _v6_body()
    snap = _parse_payload(
        body,
        wire_ver=0x06,
        flags=SnapshotFlags(),
        terminal_clear=True,
        lfg_unavailable=True,
        roster_unavailable=True,
        applicants_unavailable=True,
    )
    assert snap.terminal_clear is False
    assert snap.lfg_unavailable is False
    assert snap.roster_unavailable is False
    assert snap.applicants_unavailable is False


def test_listing_block_parses_v3_fields():
    raw = _listing_bytes(wire_ver=0x03)
    listing, cursor = _parse_listing_block(raw, 0, 0x03)
    assert listing is not None
    assert listing.activity_id == 1234
    assert listing.category_id == 7
    assert listing.difficulty_id == 9
    assert listing.key_level == 15
    assert listing.dungeon_name == "Dungeon"
    assert cursor == len(raw)


def test_listing_block_absent_returns_none():
    listing, cursor = _parse_listing_block(bytes([0]), 0, 0x06)
    assert listing is None
    assert cursor == 1


def test_applicants_block_parses_members():
    raw = struct.pack(">H", 2) + _applicant_bytes(
        wire_ver=0x02, member_idx=1
    ) + _applicant_bytes(wire_ver=0x02, member_idx=2)
    applicants, cursor = _parse_applicants_block(raw, 0, 0x02)
    assert [a.member_idx for a in applicants] == [1, 2]
    assert [a.name for a in applicants] == ["Player-One", "Player-One"]
    assert cursor == len(raw)


def test_applicants_block_v1_implies_leader_member():
    raw = struct.pack(">H", 1) + _applicant_bytes(wire_ver=0x01)
    applicants, _ = _parse_applicants_block(raw, 0, 0x01)
    assert applicants[0].member_idx == 1


def test_roster_block_parses_v6_member():
    raw = struct.pack(">H", 1) + _roster_bytes()
    roster, cursor = _parse_roster_block(raw, 0, 0x06)
    assert len(roster) == 1
    assert roster[0].name == "Roster-Mate"
    assert cursor == len(raw)


def test_roster_block_below_v6_is_empty():
    raw = bytes([9, 9, 9])
    roster, cursor = _parse_roster_block(raw, 1, 0x05)
    assert roster == []
    assert cursor == 1


def test_thin_parse_still_rejects_trailing_bytes():
    with pytest.raises(ValueError, match="trailing or truncated"):
        _parse_payload(_v6_body() + bytes([0]), wire_ver=0x06)
