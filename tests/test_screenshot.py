"""Unit tests for screenshot.py wire-format parsers.

Covers v1 backward-compat + v2 multi-member group app support. The v2
addition is a 1-byte member_idx between applicant_id and class_id, preserving
every member from a grouped application snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import applicant_scout.screenshot as screenshot_mod
from applicant_scout.screenshot import (
    DecodedApplicant,
    DecodedLeaderKey,
    MAGIC,
    ScreenshotWatcher,
    Snapshot,
    WIRE_VERSIONS_SUPPORTED,
    _Handler,
    _is_supported_screenshot_path,
    _iter_screenshot_candidates,
    _parse_payload,
    _try_parse_appscout_payload,
    decode_screenshot,
)


FIXTURES = Path(__file__).parent / "fixtures"
LUA_GOLDEN_STEM = "aps1_v8_lua_golden"
LUA_LEADER_KEY_GOLDEN_STEM = "aps1_v8_lua_leader_key_golden"


def _lua_golden_hex_path(stem: str = LUA_GOLDEN_STEM) -> Path:
    return FIXTURES / f"{stem}.hex"


def _lua_golden_expected_path(stem: str = LUA_GOLDEN_STEM) -> Path:
    return FIXTURES / f"{stem}.expected.json"


def _load_lua_golden_payload(stem: str = LUA_GOLDEN_STEM) -> bytes:
    return bytes.fromhex(_lua_golden_hex_path(stem).read_text(encoding="ascii"))


def _load_lua_golden_expected(stem: str = LUA_GOLDEN_STEM) -> dict:
    return json.loads(_lua_golden_expected_path(stem).read_text(encoding="utf-8"))


# ─── Helpers (mirror addon's _PackLenStr / per-applicant block layout) ──────


def _pack_len_str(b: bytes) -> bytes:
    """Mirror addon's _PackLenStr: u8 length-prefix + UTF-8 bytes."""
    if len(b) > 255:
        raise ValueError("name too long for u8 length prefix")
    return bytes([len(b)]) + b


def _build_applicant_block(
    aid: int,
    class_id: int,
    spec_id: int,
    ilvl: int,
    score: int,
    role: int,
    name: str,
    member_idx: int = 1,
    main_score: int = 0,
    rio_profile: int = 0,
    rio_best_key: int = 0,
    rio_best_dungeon_key: int = 0,
    rio_timed_at_or_above: int = 0,
    rio_timed_at_or_above_minus1: int = 0,
    rio_timed_at_or_above_minus2: int = 0,
    rio_completed_at_or_above_minus1: int = 0,
    rio_dungeon_count: int = 0,
    *,
    version: int,
) -> bytes:
    """Emit one applicant block matching addon's BuildPayload byte layout.

    version=1: legacy 13-byte fixed prefix (no member_idx byte).
    version=2: 14-byte fixed prefix (member_idx u8 between applicant_id +
    class_id).
    version=4: inserts main_score u16 after current score.
    version=5: inserts compact RaiderIO completion summary after main_score.
    """
    out = struct.pack(">I", aid)
    if version >= 2:
        out += bytes([member_idx])
    out += bytes([class_id])
    out += struct.pack(">H", spec_id)
    out += struct.pack(">H", ilvl)
    out += struct.pack(">H", score)
    if version >= 4:
        out += struct.pack(">H", main_score)
    if version >= 5:
        out += bytes(
            [
                rio_profile,
                rio_best_key,
                rio_best_dungeon_key,
                rio_timed_at_or_above,
                rio_timed_at_or_above_minus1,
                rio_timed_at_or_above_minus2,
                rio_completed_at_or_above_minus1,
                rio_dungeon_count,
            ]
        )
    out += bytes([role])
    out += _pack_len_str(name.encode("utf-8"))
    return out


def _build_body(applicants: list[bytes]) -> bytes:
    """Body = has_listing(0) + has_version(0) + applicant_count(u16 BE) + blocks.
    Used for parser-only unit tests (no listing/version blocks)."""
    body = bytes([0, 0])  # has_listing=0, has_version=0
    body += struct.pack(">H", len(applicants))
    for blk in applicants:
        body += blk
    return body


def _build_roster_block(
    *,
    unit_index: int,
    flags: int,
    subgroup: int,
    class_id: int,
    spec_id: int,
    ilvl: int,
    score: int,
    main_score: int,
    role: int,
    name: str,
    rio_profile: int = 0,
    rio_best_key: int = 0,
    rio_best_dungeon_key: int = 0,
    rio_timed_at_or_above: int = 0,
    rio_timed_at_or_above_minus1: int = 0,
    rio_timed_at_or_above_minus2: int = 0,
    rio_completed_at_or_above_minus1: int = 0,
    rio_dungeon_count: int = 0,
) -> bytes:
    return (
        bytes([unit_index, flags, subgroup, class_id])
        + struct.pack(">H", spec_id)
        + struct.pack(">H", ilvl)
        + struct.pack(">H", score)
        + struct.pack(">H", main_score)
        + bytes(
            [
                rio_profile,
                rio_best_key,
                rio_best_dungeon_key,
                rio_timed_at_or_above,
                rio_timed_at_or_above_minus1,
                rio_timed_at_or_above_minus2,
                rio_completed_at_or_above_minus1,
                rio_dungeon_count,
                role,
            ]
        )
        + _pack_len_str(name.encode("utf-8"))
    )


def _build_body_v6(applicants: list[bytes], roster: list[bytes]) -> bytes:
    body = _build_body(applicants)
    body += struct.pack(">H", len(roster))
    for block in roster:
        body += block
    return body


def _build_body_v7(
    applicants: list[bytes],
    roster: list[bytes],
    *,
    leader_key_level: int = 0,
    leader_key_challenge_map_id: int = 0,
    leader_key_player_name: str = "",
) -> bytes:
    body = bytes([0, 0])  # has_listing=0, has_version=0
    if leader_key_level > 0:
        body += bytes([1, leader_key_level])
        body += struct.pack(">H", leader_key_challenge_map_id)
        body += _pack_len_str(leader_key_player_name.encode("utf-8"))
    else:
        body += bytes([0])
    body += struct.pack(">H", len(applicants))
    for block in applicants:
        body += block
    body += struct.pack(">H", len(roster))
    for block in roster:
        body += block
    return body


def _build_listing_body(*, version: int) -> bytes:
    body = bytes([1])
    body += struct.pack(">I", 401)
    if version >= 3:
        body += struct.pack(">H", 2)
        body += struct.pack(">H", 8)
    body += bytes([16])
    body += _pack_len_str(b"Skyreach")
    body += _pack_len_str(b"+16 Skyreach")
    body += _pack_len_str(b"push")
    body += bytes([0])  # has_version=0
    body += struct.pack(">H", 0)
    return body


def _build_v8_listing_body(*, has_listing: int = 1) -> bytes:
    body = bytes([has_listing])
    body += struct.pack(">I", 401)
    body += struct.pack(">H", 2)
    body += struct.pack(">H", 8)
    body += bytes([16])
    body += _pack_len_str(b"Skyreach")
    body += _pack_len_str(b"+16 Skyreach")
    body += _pack_len_str(b"push")
    body += bytes([0, 0])  # has_version=0, has_leader_key=0
    body += struct.pack(">H", 0)  # applicant_count
    body += struct.pack(">H", 0)  # roster_count
    return body


def _build_v8_version_body(*, has_version: int = 1) -> bytes:
    body = bytes([0, has_version])  # has_listing=0
    body += _pack_len_str(b"0.8.2")
    body += _pack_len_str(b"12.0.7")
    body += bytes([3])
    body += _pack_len_str("Player-Realm".encode("utf-8"))
    body += bytes([0])  # has_leader_key=0
    body += struct.pack(">H", 0)  # applicant_count
    body += struct.pack(">H", 0)  # roster_count
    return body


def _build_v8_leader_key_body(*, has_leader_key: int = 1) -> bytes:
    body = bytes([0, 0, has_leader_key])  # no listing/version
    body += bytes([17])
    body += struct.pack(">H", 503)
    body += _pack_len_str("Leader-Realm".encode("utf-8"))
    body += struct.pack(">H", 0)  # applicant_count
    body += struct.pack(">H", 0)  # roster_count
    return body


def _wrap_payload(
    body: bytes,
    *,
    wire_ver: int = 0x04,
    flags: int = 0,
    reserved2: int = 0,
) -> bytes:
    total_len = 9 + len(body) + 4
    framed = (
        MAGIC
        + bytes([wire_ver])
        + struct.pack(">H", total_len)
        + bytes([flags, reserved2])
        + body
    )
    crc = zlib.crc32(framed) & 0xFFFFFFFF
    return framed + struct.pack(">I", crc)


def _write_blank_image(path: Path) -> None:
    Image.new("L", (4, 4), 255).save(path)


# ─── v2: multi-member group (the bug we're fixing) ──────────────────────────


def test_v2_two_member_group_app_parses_two_decoded_applicants():
    """One applicant_id, member_idx 1+2 → two DecodedApplicant entries.
    Block-boundary alignment is the critical assertion: if the new u8 byte
    is misaligned, member 2's class_id would be misread as part of name length."""
    blocks = [
        _build_applicant_block(
            aid=42,
            member_idx=1,
            class_id=1,
            spec_id=71,
            ilvl=480,
            score=2443,
            role=2,
            name="Voodooghost-Twisting Nether",
            version=2,
        ),
        _build_applicant_block(
            aid=42,
            member_idx=2,
            class_id=9,
            spec_id=265,
            ilvl=475,
            score=1850,
            role=2,
            name="Umbranology-Twisting Nether",
            version=2,
        ),
    ]
    snap = _parse_payload(_build_body(blocks), wire_ver=0x02)
    assert len(snap.applicants) == 2
    assert snap.applicants[0].name == "Voodooghost-Twisting Nether"
    assert snap.applicants[0].member_idx == 1
    assert snap.applicants[0].class_id == 1
    assert snap.applicants[1].name == "Umbranology-Twisting Nether"
    assert snap.applicants[1].member_idx == 2
    assert snap.applicants[1].class_id == 9
    # Both share applicant_id (group identity).
    assert snap.applicants[0].applicant_id == snap.applicants[1].applicant_id == 42


def test_v2_solo_applicant_has_member_idx_one():
    blocks = [
        _build_applicant_block(
            aid=99,
            member_idx=1,
            class_id=10,
            spec_id=268,
            ilvl=470,
            score=2200,
            role=0,
            name="Drathmork-Stormrage",
            version=2,
        ),
    ]
    snap = _parse_payload(_build_body(blocks), wire_ver=0x02)
    assert len(snap.applicants) == 1
    assert snap.applicants[0].member_idx == 1


def test_v2_five_member_group_app_max_size():
    """Pin the LFG-max-group case (5 members)."""
    blocks = [
        _build_applicant_block(
            aid=7,
            member_idx=m,
            class_id=m,
            spec_id=100 + m,
            ilvl=470,
            score=2000,
            role=2,
            name=f"Char{m}-Realm",
            version=2,
        )
        for m in range(1, 6)
    ]
    snap = _parse_payload(_build_body(blocks), wire_ver=0x02)
    assert len(snap.applicants) == 5
    assert [a.member_idx for a in snap.applicants] == [1, 2, 3, 4, 5]


def test_v2_mixed_solo_and_group_in_one_snapshot():
    """Realistic scenario: 3 applicants, mix of solo + 2-person + 3-person."""
    blocks: list[bytes] = []
    # solo
    blocks.append(
        _build_applicant_block(
            aid=1,
            member_idx=1,
            class_id=8,
            spec_id=63,
            ilvl=470,
            score=2000,
            role=2,
            name="Solo-Realm",
            version=2,
        )
    )
    # 2-person
    blocks.append(
        _build_applicant_block(
            aid=2,
            member_idx=1,
            class_id=1,
            spec_id=71,
            ilvl=475,
            score=2100,
            role=2,
            name="GroupA1-Realm",
            version=2,
        )
    )
    blocks.append(
        _build_applicant_block(
            aid=2,
            member_idx=2,
            class_id=9,
            spec_id=265,
            ilvl=470,
            score=1900,
            role=2,
            name="GroupA2-Realm",
            version=2,
        )
    )
    # 3-person
    for m in range(1, 4):
        blocks.append(
            _build_applicant_block(
                aid=3,
                member_idx=m,
                class_id=m,
                spec_id=100,
                ilvl=480,
                score=2200,
                role=2,
                name=f"GroupB{m}-Realm",
                version=2,
            )
        )
    snap = _parse_payload(_build_body(blocks), wire_ver=0x02)
    assert len(snap.applicants) == 6
    by_aid: dict[int, list[DecodedApplicant]] = {}
    for a in snap.applicants:
        by_aid.setdefault(a.applicant_id, []).append(a)
    assert len(by_aid[1]) == 1
    assert len(by_aid[2]) == 2
    assert len(by_aid[3]) == 3


# ─── v1 backward compatibility ──────────────────────────────────────────────


def test_v1_payload_back_compat_member_idx_defaults_to_one():
    """Old companion / old screenshots must still parse. member_idx in
    DecodedApplicant defaults to 1 when wire_ver=1."""
    blocks = [
        _build_applicant_block(
            aid=42,
            class_id=1,
            spec_id=71,
            ilvl=480,
            score=2443,
            role=2,
            name="Solo-Realm",
            version=1,  # no member_idx in block
        ),
    ]
    snap = _parse_payload(_build_body(blocks), wire_ver=0x01)
    assert len(snap.applicants) == 1
    assert snap.applicants[0].member_idx == 1
    assert snap.applicants[0].name == "Solo-Realm"
    assert snap.applicants[0].class_id == 1
    assert snap.applicants[0].spec_id == 71


def test_v1_payload_back_compat_default_wire_ver():
    """_parse_payload defaults wire_ver=0x01 for callers that haven't been
    updated. Keeps test_screenshot import surface stable for legacy fixtures."""
    blocks = [
        _build_applicant_block(
            aid=1,
            class_id=4,
            spec_id=259,
            ilvl=460,
            score=1800,
            role=2,
            name="X-Y",
            version=1,
        ),
    ]
    snap = _parse_payload(_build_body(blocks))  # default wire_ver
    assert len(snap.applicants) == 1
    assert snap.applicants[0].member_idx == 1


# ─── Wire-version allow-list ────────────────────────────────────────────────


def test_wire_versions_supported_pin():
    """Sentinel: pinning the WIRE_VERSIONS_SUPPORTED set contents.

    Catches accidental relaxation of the allow-list (e.g., refactor adding
    blanket 0x00..0xFF acceptance) and accidental tightening (e.g., dropping
    0x01 back-compat)."""
    assert 0x01 in WIRE_VERSIONS_SUPPORTED
    assert 0x02 in WIRE_VERSIONS_SUPPORTED
    assert 0x03 in WIRE_VERSIONS_SUPPORTED
    assert 0x04 in WIRE_VERSIONS_SUPPORTED
    assert 0x05 in WIRE_VERSIONS_SUPPORTED
    assert 0x06 in WIRE_VERSIONS_SUPPORTED
    assert 0x07 in WIRE_VERSIONS_SUPPORTED
    assert 0x08 in WIRE_VERSIONS_SUPPORTED
    assert 0x00 not in WIRE_VERSIONS_SUPPORTED  # canary


def test_v9_payload_is_rejected_instead_of_parsed_as_known_version():
    raw = _wrap_payload(_build_body([]), wire_ver=0x09)

    snap, error = _try_parse_appscout_payload(raw)

    assert snap is None
    assert error == "unsupported wire version 0x09"


def test_v8_payload_parses_lfg_unavailable_flag():
    body = _build_body_v7([], [])
    raw = _wrap_payload(body, wire_ver=0x08)

    snap, error = _try_parse_appscout_payload(raw)

    assert error is None
    assert snap is not None
    assert snap.terminal_clear is False
    assert snap.lfg_unavailable is False

    raw = _wrap_payload(body, wire_ver=0x08, flags=0x02)

    snap, error = _try_parse_appscout_payload(raw)

    assert error is None
    assert snap is not None
    assert snap.terminal_clear is False
    assert snap.lfg_unavailable is True


def test_v8_payload_parses_terminal_clear_flag():
    raw = _wrap_payload(_build_body_v7([], []), wire_ver=0x08, flags=0x01)

    snap, error = _try_parse_appscout_payload(raw)

    assert error is None
    assert snap is not None
    assert snap.terminal_clear is True
    assert snap.lfg_unavailable is False


def test_v8_payload_rejects_unknown_or_conflicting_flags():
    snap, error = _try_parse_appscout_payload(
        _wrap_payload(_build_body_v7([], []), wire_ver=0x08, flags=0x04)
    )
    assert snap is None
    assert error == "unsupported APS1 v8 flags 0x04"

    snap, error = _try_parse_appscout_payload(
        _wrap_payload(_build_body_v7([], []), wire_ver=0x08, flags=0x03)
    )
    assert snap is None
    assert error == "terminal and LFG-unavailable flags are mutually exclusive"

    snap, error = _try_parse_appscout_payload(
        _wrap_payload(_build_body_v7([], []), wire_ver=0x08, reserved2=1)
    )
    assert snap is None
    assert error == "unsupported APS1 v8 reserved byte 0x01"


def test_pre_v8_reserved_bytes_do_not_become_v8_flags():
    snap, error = _try_parse_appscout_payload(
        _wrap_payload(_build_body_v7([], []), wire_ver=0x07, flags=0x02)
    )

    assert snap is None
    assert error == "unsupported APS1 pre-v8 reserved bytes 0x02 0x00"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (_build_v8_listing_body(has_listing=2), "has_listing"),
        (_build_v8_version_body(has_version=2), "has_version"),
        (_build_v8_leader_key_body(has_leader_key=2), "has_leader_key"),
    ],
)
def test_crc_valid_payload_rejects_noncanonical_presence_byte(
    body: bytes,
    field: str,
):
    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x08))

    assert snap is None
    assert error is not None
    assert f"{field} must be 0 or 1, got 2" in error


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            _build_body_v7(
                [
                    _build_applicant_block(
                        42,
                        1,
                        71,
                        480,
                        2000,
                        2,
                        "Applicant-Realm",
                        rio_profile=2,
                        version=5,
                    )
                ],
                [],
            ),
            "applicant.rio_profile",
        ),
        (
            _build_body_v7(
                [],
                [
                    _build_roster_block(
                        unit_index=1,
                        flags=1,
                        subgroup=1,
                        class_id=1,
                        spec_id=71,
                        ilvl=480,
                        score=2000,
                        main_score=2100,
                        role=2,
                        name="Roster-Realm",
                        rio_profile=2,
                    )
                ],
            ),
            "roster.rio_profile",
        ),
    ],
)
def test_crc_valid_payload_rejects_noncanonical_rio_profile_byte(
    body: bytes,
    field: str,
):
    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x08))

    assert snap is None
    assert error is not None
    assert f"{field} must be 0 or 1, got 2" in error


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            _build_body_v7(
                [
                    _build_applicant_block(
                        42,
                        1,
                        71,
                        480,
                        2000,
                        4,
                        "Applicant-Realm",
                        version=5,
                    )
                ],
                [],
            ),
            "applicant.role",
        ),
        (
            _build_body_v7(
                [],
                [
                    _build_roster_block(
                        unit_index=1,
                        flags=1,
                        subgroup=1,
                        class_id=1,
                        spec_id=71,
                        ilvl=480,
                        score=2000,
                        main_score=2100,
                        role=4,
                        name="Roster-Realm",
                    )
                ],
            ),
            "roster.role",
        ),
    ],
)
def test_crc_valid_payload_rejects_role_byte_outside_wire_enum(
    body: bytes,
    field: str,
):
    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x08))

    assert snap is None
    assert error is not None
    assert f"{field} must be one of 0, 1, 2, 3, got 4" in error


def test_crc_valid_payload_accepts_unknown_role_byte_three():
    body = _build_body_v7(
        [
            _build_applicant_block(
                42,
                1,
                71,
                480,
                2000,
                3,
                "Applicant-Realm",
                version=5,
            )
        ],
        [
            _build_roster_block(
                unit_index=1,
                flags=1,
                subgroup=1,
                class_id=1,
                spec_id=71,
                ilvl=480,
                score=2000,
                main_score=2100,
                role=3,
                name="Roster-Realm",
            )
        ],
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x08))

    assert error is None
    assert snap is not None
    assert snap.applicants[0].role == 3
    assert snap.roster[0].role == 3


def test_v6_roster_block_parses_current_party_members():
    body = _build_body_v6(
        [],
        [
            _build_roster_block(
                unit_index=0,
                flags=1,
                subgroup=1,
                class_id=10,
                spec_id=270,
                ilvl=712,
                score=3301,
                main_score=3400,
                role=1,
                name="Healmonk-TwistingNether",
                rio_profile=1,
                rio_best_key=16,
                rio_best_dungeon_key=15,
                rio_timed_at_or_above=2,
                rio_timed_at_or_above_minus1=5,
                rio_timed_at_or_above_minus2=8,
                rio_completed_at_or_above_minus1=6,
                rio_dungeon_count=8,
            )
        ],
    )

    snap = _parse_payload(body, wire_ver=0x06)

    assert snap.applicants == []
    assert len(snap.roster) == 1
    member = snap.roster[0]
    assert member.name == "Healmonk-TwistingNether"
    assert member.is_self
    assert member.unit_index == 0
    assert member.subgroup == 1
    assert member.class_id == 10
    assert member.spec_id == 270
    assert member.ilvl == 712
    assert member.score == 3301
    assert member.main_score == 3400
    assert member.rio_best_key == 16
    assert member.role == 1


def test_v6_roster_block_accepts_full_raid_size():
    roster = [
        _build_roster_block(
            unit_index=i,
            flags=2,
            subgroup=((i - 1) // 5) + 1,
            class_id=(i % 13) + 1,
            spec_id=250 + i,
            ilvl=700 + i,
            score=2500 + i,
            main_score=2600 + i,
            role=2,
            name=f"Raider{i}-Realm",
        )
        for i in range(1, 41)
    ]

    snap = _parse_payload(_build_body_v6([], roster), wire_ver=0x06)

    assert len(snap.roster) == 40
    assert snap.roster[0].name == "Raider1-Realm"
    assert snap.roster[-1].name == "Raider40-Realm"
    assert snap.roster[-1].subgroup == 8


def test_v5_snapshots_default_to_empty_roster():
    body = _build_body(
        [
            _build_applicant_block(
                42,
                1,
                71,
                480,
                2000,
                2,
                "Applicant-Realm",
                version=5,
            )
        ]
    )

    snap = _parse_payload(body, wire_ver=0x05)

    assert len(snap.applicants) == 1
    assert snap.roster == []


def test_v6_payload_crc_accepts_roster_block():
    body = _build_body_v6(
        [],
        [
            _build_roster_block(
                unit_index=1,
                flags=2,
                subgroup=1,
                class_id=1,
                spec_id=71,
                ilvl=701,
                score=3000,
                main_score=0,
                role=2,
                name="Warrior-Realm",
            )
        ],
    )
    header = MAGIC + bytes([0x06]) + b"\0\0\0\0"
    total_len = len(header) + len(body) + 4
    payload_without_crc = header[:5] + struct.pack(">H", total_len) + header[7:] + body
    payload = payload_without_crc + struct.pack(
        ">I", zlib.crc32(payload_without_crc) & 0xFFFFFFFF
    )

    snap, err = _try_parse_appscout_payload(payload)

    assert err is None
    assert snap is not None
    assert [m.name for m in snap.roster] == ["Warrior-Realm"]


def test_v7_payload_crc_accepts_leader_key_block():
    body = _build_body_v7(
        [],
        [],
        leader_key_level=17,
        leader_key_challenge_map_id=503,
        leader_key_player_name="Leader-Realm",
    )

    snap, err = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x07))

    assert err is None
    assert snap is not None
    assert snap.leader_key == DecodedLeaderKey(
        key_level=17,
        challenge_map_id=503,
        player_name="Leader-Realm",
    )


def test_crc_valid_payload_with_duplicate_applicant_composite_key_is_rejected():
    body = _build_body(
        [
            _build_applicant_block(
                42, 1, 71, 480, 2000, 2, "Tank-Realm", member_idx=1, version=2
            ),
            _build_applicant_block(
                42, 8, 267, 481, 2100, 2, "Warlock-Realm", member_idx=1, version=2
            ),
        ]
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x02))

    assert snap is None
    assert error is not None
    assert "duplicate applicant identity 42:1" in error


def test_crc_valid_v1_payload_with_duplicate_applicant_id_is_rejected():
    body = _build_body(
        [
            _build_applicant_block(42, 1, 71, 480, 2000, 2, "Tank-Realm", version=1),
            _build_applicant_block(
                42, 8, 267, 481, 2100, 2, "Warlock-Realm", version=1
            ),
        ]
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x01))

    assert snap is None
    assert error is not None
    assert "duplicate applicant identity 42:1" in error


def test_duplicate_validation_allows_same_applicant_id_with_distinct_member_idx():
    body = _build_body(
        [
            _build_applicant_block(
                42, 1, 71, 480, 2000, 2, "Tank-Realm", member_idx=1, version=2
            ),
            _build_applicant_block(
                42, 8, 267, 481, 2100, 2, "Warlock-Realm", member_idx=2, version=2
            ),
        ]
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x02))

    assert error is None
    assert snap is not None
    assert [(a.applicant_id, a.member_idx) for a in snap.applicants] == [(42, 1), (42, 2)]


def test_crc_valid_payload_skips_placeholder_applicants_before_duplicate_validation():
    body = _build_body(
        [
            _build_applicant_block(
                42, 1, 71, 480, 2000, 2, "Unknown-Realm", member_idx=1, version=2
            ),
            _build_applicant_block(
                42, 8, 267, 481, 2100, 2, " unknown-realm ", member_idx=1, version=2
            ),
            _build_applicant_block(
                99, 8, 63, 482, 2200, 2, "Solo-Realm", member_idx=1, version=2
            ),
        ]
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x02))

    assert error is None
    assert snap is not None
    assert [applicant.name for applicant in snap.applicants] == ["Solo-Realm"]


def test_crc_valid_payload_preserves_non_placeholder_unknown_prefix_applicant():
    body = _build_body(
        [
            _build_applicant_block(
                42, 1, 71, 480, 2000, 2, "Unknownhero-Realm", member_idx=1, version=2
            )
        ]
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x02))

    assert error is None
    assert snap is not None
    assert [applicant.name for applicant in snap.applicants] == ["Unknownhero-Realm"]


def test_crc_valid_payload_with_blank_applicant_name_is_rejected():
    body = _build_body(
        [
            _build_applicant_block(
                42, 1, 71, 480, 2000, 2, "  ", member_idx=1, version=2
            )
        ]
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x02))

    assert snap is None
    assert error is not None
    assert "blank applicant identity 42:1" in error


def test_crc_valid_payload_with_invalid_applicant_member_index_is_rejected():
    body = _build_body(
        [
            _build_applicant_block(
                42, 1, 71, 480, 2000, 2, "Tank-Realm", member_idx=0, version=2
            )
        ]
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x02))

    assert snap is None
    assert error is not None
    assert "invalid applicant member_idx 42:0" in error


def test_crc_valid_payload_rejects_invalid_placeholder_applicant_member_index():
    body = _build_body(
        [
            _build_applicant_block(
                42, 1, 71, 480, 2000, 2, "Unknown-Realm", member_idx=0, version=2
            )
        ]
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x02))

    assert snap is None
    assert error is not None
    assert "invalid applicant member_idx 42:0" in error


def test_crc_valid_payload_with_duplicate_roster_identity_is_rejected():
    body = _build_body_v6(
        [],
        [
            _build_roster_block(
                unit_index=1,
                flags=2,
                subgroup=1,
                class_id=1,
                spec_id=71,
                ilvl=701,
                score=3000,
                main_score=0,
                role=2,
                name="Warrior-Realm",
            ),
            _build_roster_block(
                unit_index=2,
                flags=2,
                subgroup=1,
                class_id=2,
                spec_id=72,
                ilvl=702,
                score=3100,
                main_score=0,
                role=2,
                name=" warrior-realm ",
            ),
        ],
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x06))

    assert snap is None
    assert error is not None
    assert "duplicate roster identity warrior-realm" in error


def test_crc_valid_payload_skips_placeholder_roster_identities_before_duplicate_validation():
    body = _build_body_v6(
        [],
        [
            _build_roster_block(
                unit_index=1,
                flags=2,
                subgroup=1,
                class_id=1,
                spec_id=71,
                ilvl=701,
                score=3000,
                main_score=0,
                role=2,
                name="Unknown-Realm",
            ),
            _build_roster_block(
                unit_index=2,
                flags=2,
                subgroup=1,
                class_id=2,
                spec_id=72,
                ilvl=702,
                score=3100,
                main_score=0,
                role=2,
                name=" unknown-realm ",
            ),
            _build_roster_block(
                unit_index=3,
                flags=2,
                subgroup=1,
                class_id=5,
                spec_id=256,
                ilvl=703,
                score=3200,
                main_score=0,
                role=1,
                name="Host-Realm",
            ),
        ],
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x06))

    assert error is None
    assert snap is not None
    assert [member.name for member in snap.roster] == ["Host-Realm"]


def test_crc_valid_payload_skips_unqualified_placeholder_roster_identity():
    body = _build_body_v6(
        [],
        [
            _build_roster_block(
                unit_index=1,
                flags=2,
                subgroup=1,
                class_id=1,
                spec_id=71,
                ilvl=701,
                score=3000,
                main_score=0,
                role=2,
                name="?",
            ),
            _build_roster_block(
                unit_index=2,
                flags=2,
                subgroup=1,
                class_id=2,
                spec_id=72,
                ilvl=702,
                score=3100,
                main_score=0,
                role=2,
                name="UNKNOWNOBJECT",
            ),
            _build_roster_block(
                unit_index=3,
                flags=2,
                subgroup=1,
                class_id=5,
                spec_id=256,
                ilvl=703,
                score=3200,
                main_score=0,
                role=1,
                name="Host-Realm",
            ),
        ],
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x06))

    assert error is None
    assert snap is not None
    assert [member.name for member in snap.roster] == ["Host-Realm"]


def test_crc_valid_payload_preserves_non_placeholder_unknown_prefix_roster_identity():
    body = _build_body_v6(
        [],
        [
            _build_roster_block(
                unit_index=1,
                flags=2,
                subgroup=1,
                class_id=1,
                spec_id=71,
                ilvl=701,
                score=3000,
                main_score=0,
                role=2,
                name="Unknownhero-Realm",
            )
        ],
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x06))

    assert error is None
    assert snap is not None
    assert [member.name for member in snap.roster] == ["Unknownhero-Realm"]


def test_crc_valid_payload_with_blank_roster_name_is_rejected():
    body = _build_body_v6(
        [],
        [
            _build_roster_block(
                unit_index=1,
                flags=2,
                subgroup=1,
                class_id=1,
                spec_id=71,
                ilvl=701,
                score=3000,
                main_score=0,
                role=2,
                name="  ",
            )
        ],
    )

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x06))

    assert snap is None
    assert error is not None
    assert "blank roster identity" in error


def test_crc_valid_payload_with_trailing_body_bytes_is_rejected():
    raw = _wrap_payload(_build_body([]) + b"extra")

    snap, error = _try_parse_appscout_payload(raw)

    assert snap is None
    assert error is not None
    assert "trailing or truncated payload bytes" in error


def test_crc_valid_payload_with_trailing_decoded_bytes_is_rejected():
    raw = _wrap_payload(_build_body([])) + b"extra"

    snap, error = _try_parse_appscout_payload(raw)

    assert snap is None
    assert error is not None
    assert "trailing decoded bytes" in error


def test_crc_valid_payload_with_overlong_final_name_is_rejected_with_field_reason():
    block = (
        struct.pack(">I", 42)
        + bytes([1, 1])
        + struct.pack(">H", 71)
        + struct.pack(">H", 480)
        + struct.pack(">H", 2443)
        + struct.pack(">H", 3468)
        + bytes([2, 10])
        + b"A"
    )
    raw = _wrap_payload(_build_body([block]), wire_ver=0x04)

    snap, error = _try_parse_appscout_payload(raw)

    assert snap is None
    assert error is not None
    assert "applicant.name" in error
    assert "exceeds remaining payload bytes" in error


def test_crc_valid_payload_rejects_invalid_utf8_applicant_name():
    block = (
        struct.pack(">I", 42)
        + bytes([1, 1])
        + struct.pack(">H", 71)
        + struct.pack(">H", 480)
        + struct.pack(">H", 2443)
        + struct.pack(">H", 3468)
        + bytes([2])
        + _pack_len_str(b"\xff")
    )

    snap, error = _try_parse_appscout_payload(
        _wrap_payload(_build_body([block]), wire_ver=0x04)
    )

    assert snap is None
    assert error is not None
    assert "applicant.name" in error
    assert "invalid utf-8" in error


def test_crc_valid_payload_rejects_invalid_utf8_listing_text():
    body = bytes([1])
    body += struct.pack(">I", 401)
    body += struct.pack(">H", 2)
    body += struct.pack(">H", 8)
    body += bytes([16])
    body += _pack_len_str(b"\xff")
    body += _pack_len_str(b"+16 Skyreach")
    body += _pack_len_str(b"push")
    body += bytes([0])
    body += struct.pack(">H", 0)

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x03))

    assert snap is None
    assert error is not None
    assert "listing.dungeon_name" in error
    assert "invalid utf-8" in error


def test_crc_valid_payload_rejects_invalid_ascii_version_text():
    body = bytes([0, 1])
    body += _pack_len_str(b"\xff")
    body += _pack_len_str(b"12.0.5")
    body += bytes([3])
    body += _pack_len_str(b"Player-Realm")
    body += struct.pack(">H", 0)

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x02))

    assert snap is None
    assert error is not None
    assert "version.addon_version" in error
    assert "invalid ascii" in error


def test_crc_valid_payload_rejects_invalid_utf8_roster_name():
    roster = (
        bytes([1, 2, 1, 1])
        + struct.pack(">H", 71)
        + struct.pack(">H", 701)
        + struct.pack(">H", 3000)
        + struct.pack(">H", 0)
        + bytes([0, 0, 0, 0, 0, 0, 0, 0, 2])
        + _pack_len_str(b"\xff")
    )

    snap, error = _try_parse_appscout_payload(
        _wrap_payload(_build_body_v6([], [roster]), wire_ver=0x06)
    )

    assert snap is None
    assert error is not None
    assert "roster.name" in error
    assert "invalid utf-8" in error


def test_crc_valid_payload_accepts_non_ascii_text_fields():
    applicant = _build_applicant_block(
        42,
        1,
        71,
        480,
        2443,
        2,
        "Игрок-Ревущийфьорд",
        member_idx=1,
        main_score=3468,
        version=6,
    )
    roster = _build_roster_block(
        unit_index=1,
        flags=2,
        subgroup=1,
        class_id=1,
        spec_id=71,
        ilvl=701,
        score=3000,
        main_score=0,
        role=2,
        name="Élite-Ravencrest",
    )
    body = bytes([1])
    body += struct.pack(">I", 401)
    body += struct.pack(">H", 2)
    body += struct.pack(">H", 8)
    body += bytes([16])
    body += _pack_len_str("Ключ".encode("utf-8"))
    body += _pack_len_str("+16 Академия".encode("utf-8"))
    body += _pack_len_str("пуш".encode("utf-8"))
    body += bytes([1])
    body += _pack_len_str(b"1.2.3")
    body += _pack_len_str(b"12.0.5")
    body += bytes([3])
    body += _pack_len_str("Хост-Гордунни".encode("utf-8"))
    body += struct.pack(">H", 1)
    body += applicant
    body += struct.pack(">H", 1)
    body += roster

    snap, error = _try_parse_appscout_payload(_wrap_payload(body, wire_ver=0x06))

    assert error is None
    assert snap is not None
    assert snap.listing is not None
    assert snap.listing.dungeon_name == "Ключ"
    assert snap.version is not None
    assert snap.version.player_name == "Хост-Гордунни"
    assert snap.applicants[0].name == "Игрок-Ревущийфьорд"
    assert snap.roster[0].name == "Élite-Ravencrest"


def test_v4_applicant_block_parses_current_and_main_score():
    """v4 adds main_score after current score; role/name alignment must hold."""
    blocks = [
        _build_applicant_block(
            aid=42,
            member_idx=1,
            class_id=1,
            spec_id=71,
            ilvl=480,
            score=2443,
            main_score=3468,
            role=2,
            name="Altwar-Twisting Nether",
            version=4,
        ),
    ]

    snap = _parse_payload(_build_body(blocks), wire_ver=0x04)

    assert len(snap.applicants) == 1
    applicant = snap.applicants[0]
    assert applicant.score == 2443
    assert applicant.main_score == 3468
    assert applicant.role == 2
    assert applicant.name == "Altwar-Twisting Nether"


def test_v5_applicant_block_parses_rio_completion_summary():
    """v5 adds target-relative RaiderIO completion bytes after main_score."""
    blocks = [
        _build_applicant_block(
            aid=7,
            member_idx=1,
            class_id=8,
            spec_id=63,
            ilvl=488,
            score=3321,
            main_score=3550,
            rio_profile=1,
            rio_best_key=17,
            rio_best_dungeon_key=15,
            rio_timed_at_or_above=1,
            rio_timed_at_or_above_minus1=8,
            rio_timed_at_or_above_minus2=8,
            rio_completed_at_or_above_minus1=8,
            rio_dungeon_count=8,
            role=2,
            name="Rio-Realm",
            version=5,
        ),
    ]

    snap = _parse_payload(_build_body(blocks), wire_ver=0x05)
    applicant = snap.applicants[0]

    assert applicant.main_score == 3550
    assert applicant.rio_profile is True
    assert applicant.rio_best_key == 17
    assert applicant.rio_best_dungeon_key == 15
    assert applicant.rio_timed_at_or_above == 1
    assert applicant.rio_timed_at_or_above_minus1 == 8
    assert applicant.rio_timed_at_or_above_minus2 == 8
    assert applicant.rio_completed_at_or_above_minus1 == 8
    assert applicant.rio_dungeon_count == 8
    assert applicant.role == 2
    assert applicant.name == "Rio-Realm"


def test_v3_payload_back_compat_main_score_defaults_to_zero():
    blocks = [
        _build_applicant_block(
            aid=99,
            member_idx=1,
            class_id=10,
            spec_id=268,
            ilvl=470,
            score=2200,
            role=0,
            name="Oldwire-Realm",
            version=3,
        ),
    ]

    snap = _parse_payload(_build_body(blocks), wire_ver=0x03)

    assert len(snap.applicants) == 1
    assert snap.applicants[0].score == 2200
    assert snap.applicants[0].main_score == 0
    assert snap.applicants[0].rio_profile is False
    assert snap.applicants[0].rio_best_key == 0


def test_v2_listing_defaults_context_fields_to_zero():
    snap = _parse_payload(_build_listing_body(version=2), wire_ver=0x02)

    assert snap.listing is not None
    assert snap.listing.activity_id == 401
    assert snap.listing.key_level == 16
    assert snap.listing.category_id == 0
    assert snap.listing.difficulty_id == 0


def test_v3_listing_parses_category_and_difficulty():
    snap = _parse_payload(_build_listing_body(version=3), wire_ver=0x03)

    assert snap.listing is not None
    assert snap.listing.activity_id == 401
    assert snap.listing.category_id == 2
    assert snap.listing.difficulty_id == 8
    assert snap.listing.key_level == 16


# ─── Screenshot decode boundary ──────────────────────────────────────────────


def test_decode_screenshot_accepts_raw_byte_qr_with_embedded_nul(
    monkeypatch, tmp_path: Path
):
    image_path = tmp_path / "raw_qr.png"
    _write_blank_image(image_path)
    raw_payload = _wrap_payload(_build_body([]))

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=raw_payload)],
    )

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert snap.applicants == []


@pytest.mark.parametrize(
    "fixture_stem",
    [LUA_GOLDEN_STEM, LUA_LEADER_KEY_GOLDEN_STEM],
    ids=["base", "leader-key"],
)
def test_decode_screenshot_accepts_lua_generated_aps1_v8_golden(
    monkeypatch, tmp_path: Path, fixture_stem: str
):
    image_path = tmp_path / "lua_golden_qr.png"
    _write_blank_image(image_path)
    raw_payload = _load_lua_golden_payload(fixture_stem)
    expected = _load_lua_golden_expected(fixture_stem)

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=raw_payload)],
    )

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert snap.listing is not None
    assert snap.version is not None
    assert snap.listing.__dict__ == expected["listing"]
    assert snap.version.__dict__ == expected["version"]
    assert (
        snap.leader_key.__dict__ if snap.leader_key is not None else None
    ) == expected.get("leader_key")
    assert [a.__dict__ for a in snap.applicants] == expected["applicants"]
    assert [m.__dict__ for m in snap.roster] == expected["roster"]


def test_decode_screenshot_accepts_legacy_hex_qr(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "hex_qr.png"
    _write_blank_image(image_path)
    raw_payload = _wrap_payload(_build_body([]))
    hex_payload = raw_payload.hex().upper().encode("ascii")

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=hex_payload)],
    )

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert snap.applicants == []


def test_decode_log_includes_roster_count(monkeypatch, tmp_path: Path, caplog):
    image_path = tmp_path / "hex_qr.png"
    _write_blank_image(image_path)
    raw_payload = _wrap_payload(
        _build_body_v6(
            [],
            [
                _build_roster_block(
                    unit_index=1,
                    flags=1,
                    subgroup=1,
                    class_id=10,
                    spec_id=269,
                    ilvl=700,
                    score=3200,
                    main_score=0,
                    role=2,
                    name="Host-Realm",
                ),
                _build_roster_block(
                    unit_index=2,
                    flags=0,
                    subgroup=1,
                    class_id=2,
                    spec_id=65,
                    ilvl=701,
                    score=3100,
                    main_score=0,
                    role=1,
                    name="Healer-Realm",
                ),
            ],
        ),
        wire_ver=0x06,
    )
    hex_payload = raw_payload.hex().upper().encode("ascii")
    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=hex_payload)],
    )
    caplog.set_level(logging.INFO, logger="applicant_scout.screenshot")

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert len(snap.roster) == 2
    assert "applicant_rows=0 roster=2" in caplog.text


def test_decode_screenshot_accepts_lua_generated_golden_as_legacy_hex(
    monkeypatch, tmp_path: Path
):
    image_path = tmp_path / "lua_golden_hex_qr.png"
    _write_blank_image(image_path)
    raw_payload = _load_lua_golden_payload()
    hex_payload = raw_payload.hex().upper().encode("ascii")
    expected = _load_lua_golden_expected()

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=hex_payload)],
    )

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert snap.listing is not None
    assert snap.listing.__dict__ == expected["listing"]
    assert [(a.applicant_id, a.member_idx) for a in snap.applicants] == [
        (a["applicant_id"], a["member_idx"]) for a in expected["applicants"]
    ]


def test_decode_screenshot_uses_top_left_crop_before_full_image(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "large_qr.jpg"
    Image.new("RGB", (1280, 720), "white").save(image_path)
    raw_payload = _wrap_payload(_build_body([]))
    calls: list[tuple[int, int]] = []

    def fake_decode(img, symbols=None):
        calls.append(img.size)
        return [SimpleNamespace(data=raw_payload)]

    monkeypatch.setattr(screenshot_mod, "pyzbar_decode", fake_decode)

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert calls == [(screenshot_mod.QR_SCAN_CROP_PX, 720)]


def test_decode_screenshot_falls_back_to_full_image_when_crop_has_no_appscout_qr(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "moved_qr.jpg"
    Image.new("RGB", (1280, 720), "white").save(image_path)
    raw_payload = _wrap_payload(_build_body([]))
    calls: list[tuple[int, int]] = []

    def fake_decode(img, symbols=None):
        calls.append(img.size)
        if len(calls) == 1:
            return [SimpleNamespace(data=b"https://example.invalid")]
        return [SimpleNamespace(data=raw_payload)]

    monkeypatch.setattr(screenshot_mod, "pyzbar_decode", fake_decode)

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert calls == [(screenshot_mod.QR_SCAN_CROP_PX, 720), (1280, 720)]


def test_decode_screenshot_prefers_valid_raw_candidate_over_legacy_hex(
    monkeypatch, tmp_path: Path
):
    image_path = tmp_path / "mixed_qr.png"
    _write_blank_image(image_path)
    raw_payload = _wrap_payload(_build_body([]))
    legacy_payload = _wrap_payload(
        _build_body(
            [
                _build_applicant_block(
                    aid=9,
                    class_id=8,
                    spec_id=63,
                    ilvl=470,
                    score=2000,
                    role=2,
                    name="Hex-Win",
                    version=1,
                )
            ]
        ),
        wire_ver=0x01,
    )
    legacy_hex = legacy_payload.hex().upper().encode("ascii")

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [
            SimpleNamespace(data=b"https://example.invalid"),
            SimpleNamespace(data=legacy_hex),
            SimpleNamespace(data=raw_payload),
        ],
    )

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert snap.applicants == []


def test_decode_screenshot_prefers_raw_lua_golden_over_hex_candidate(
    monkeypatch, tmp_path: Path
):
    image_path = tmp_path / "lua_golden_mixed_qr.png"
    _write_blank_image(image_path)
    raw_payload = _load_lua_golden_payload()
    legacy_payload = _wrap_payload(
        _build_body(
            [
                _build_applicant_block(
                    aid=9,
                    class_id=8,
                    spec_id=63,
                    ilvl=470,
                    score=2000,
                    role=2,
                    name="Hex-Win",
                    version=1,
                )
            ]
        ),
        wire_ver=0x01,
    )
    legacy_hex = legacy_payload.hex().upper().encode("ascii")

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [
            SimpleNamespace(data=legacy_hex),
            SimpleNamespace(data=raw_payload),
        ],
    )

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert snap.version is not None
    assert snap.version.addon_version == _load_lua_golden_expected()["version"][
        "addon_version"
    ]


def test_decode_screenshot_falls_through_corrupt_raw_to_valid_hex(
    monkeypatch, tmp_path: Path
):
    image_path = tmp_path / "corrupt_then_hex.png"
    _write_blank_image(image_path)
    corrupt_raw = bytearray(_wrap_payload(_build_body([])))
    corrupt_raw[-1] ^= 0xFF
    legacy_payload = _wrap_payload(
        _build_body(
            [
                _build_applicant_block(
                    aid=11,
                    class_id=1,
                    spec_id=71,
                    ilvl=480,
                    score=2443,
                    role=2,
                    name="Fallback-Realm",
                    version=1,
                )
            ]
        ),
        wire_ver=0x01,
    )
    legacy_hex = legacy_payload.hex().upper().encode("ascii")

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [
            SimpleNamespace(data=bytes(corrupt_raw)),
            SimpleNamespace(data=legacy_hex),
        ],
    )

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert [a.name for a in snap.applicants] == ["Fallback-Realm"]


def test_decode_screenshot_falls_back_to_full_image_when_crop_has_corrupt_appscout_qr(
    monkeypatch, tmp_path: Path
):
    image_path = tmp_path / "crop_corrupt_full_valid.png"
    Image.new("L", (1000, 1000), 255).save(image_path)
    corrupt_crop = bytearray(_wrap_payload(_build_body([])))
    corrupt_crop[-1] ^= 0xFF
    full_payload = _wrap_payload(
        _build_body(
            [
                _build_applicant_block(
                    aid=11,
                    class_id=1,
                    spec_id=71,
                    ilvl=480,
                    score=2443,
                    role=2,
                    name="Full-Realm",
                    version=1,
                )
            ]
        ),
        wire_ver=0x01,
    )
    seen_sizes: list[tuple[int, int]] = []

    def fake_decode(img, symbols=None):
        seen_sizes.append(img.size)
        if img.size == (screenshot_mod.QR_SCAN_CROP_PX, screenshot_mod.QR_SCAN_CROP_PX):
            return [SimpleNamespace(data=bytes(corrupt_crop))]
        return [SimpleNamespace(data=full_payload)]

    monkeypatch.setattr(screenshot_mod, "pyzbar_decode", fake_decode)

    snap, marker = decode_screenshot(image_path)

    assert marker is True
    assert snap is not None
    assert [a.name for a in snap.applicants] == ["Full-Realm"]
    assert seen_sizes == [
        (screenshot_mod.QR_SCAN_CROP_PX, screenshot_mod.QR_SCAN_CROP_PX),
        (1000, 1000),
    ]


def test_decode_screenshot_marks_corrupt_appscout_payload_as_ours(
    monkeypatch, tmp_path: Path
):
    image_path = tmp_path / "corrupt_raw_only.png"
    _write_blank_image(image_path)
    corrupt_raw = bytearray(_wrap_payload(_build_body([])))
    corrupt_raw[-1] ^= 0xFF

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=bytes(corrupt_raw))],
    )

    snap, marker = decode_screenshot(image_path)

    assert snap is None
    assert marker is True


def test_decode_result_records_corrupt_appscout_reason(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "corrupt_raw_only.png"
    _write_blank_image(image_path)
    corrupt_raw = bytearray(_wrap_payload(_build_body([])))
    corrupt_raw[-1] ^= 0xFF

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=bytes(corrupt_raw))],
    )

    result = screenshot_mod._decode_screenshot_result(image_path)

    assert result.snapshot is None
    assert result.has_marker is True
    assert result.error_reason is not None
    assert "CRC mismatch" in result.error_reason


def test_decode_result_reports_duplicate_identity_as_marker_parse_failure(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "duplicate_identity.png"
    _write_blank_image(image_path)
    body = _build_body(
        [
            _build_applicant_block(
                42, 1, 71, 480, 2000, 2, "Tank-Realm", member_idx=1, version=2
            ),
            _build_applicant_block(
                42, 8, 267, 481, 2100, 2, "Warlock-Realm", member_idx=1, version=2
            ),
        ]
    )

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [
            SimpleNamespace(data=_wrap_payload(body, wire_ver=0x02))
        ],
    )

    result = screenshot_mod._decode_screenshot_result(image_path)

    assert result.snapshot is None
    assert result.has_marker is True
    assert result.error_reason is not None
    assert "duplicate applicant identity 42:1" in result.error_reason


def test_decode_result_records_unexpected_appscout_candidate_exception_as_marker_failure(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "unexpected_parser_error.png"
    _write_blank_image(image_path)
    raw_payload = _wrap_payload(_build_body([]))
    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=raw_payload)],
    )

    def raise_unexpected(_raw: bytes):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(screenshot_mod, "_try_parse_appscout_payload", raise_unexpected)

    result = screenshot_mod._decode_screenshot_result(image_path)

    assert result.snapshot is None
    assert result.has_marker is True
    assert result.error_reason is not None
    assert "unexpected parser error: RuntimeError: parser exploded" in result.error_reason


def test_decode_result_continues_after_unexpected_candidate_exception_to_valid_candidate(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "unexpected_then_valid.png"
    _write_blank_image(image_path)
    broken_raw = _wrap_payload(_build_body([]))
    valid_payload = _wrap_payload(
        _build_body(
            [
                _build_applicant_block(
                    aid=11,
                    class_id=1,
                    spec_id=71,
                    ilvl=480,
                    score=2443,
                    role=2,
                    name="Fallback-Realm",
                    version=1,
                )
            ]
        ),
        wire_ver=0x01,
    )
    valid_hex = valid_payload.hex().upper().encode("ascii")
    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [
            SimpleNamespace(data=broken_raw),
            SimpleNamespace(data=valid_hex),
        ],
    )
    original_parse = screenshot_mod._try_parse_appscout_payload

    def parse_or_raise(raw: bytes):
        if raw == broken_raw:
            raise RuntimeError("parser exploded")
        return original_parse(raw)

    monkeypatch.setattr(screenshot_mod, "_try_parse_appscout_payload", parse_or_raise)

    result = screenshot_mod._decode_screenshot_result(image_path)

    assert result.has_marker is True
    assert result.snapshot is not None
    assert [applicant.name for applicant in result.snapshot.applicants] == [
        "Fallback-Realm"
    ]


def test_decode_screenshot_ignores_foreign_qr_without_marker(
    monkeypatch, tmp_path: Path
):
    image_path = tmp_path / "foreign_qr.png"
    _write_blank_image(image_path)

    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=b"https://example.invalid")],
    )

    snap, marker = decode_screenshot(image_path)

    assert snap is None
    assert marker is False


# ─── Screenshot candidate filtering ─────────────────────────────────────────


def test_supported_screenshot_suffixes_accept_jpg_and_tga_case_insensitive():
    assert _is_supported_screenshot_path(Path("WoWScrnShot_1.jpg"))
    assert _is_supported_screenshot_path(Path("WoWScrnShot_1.JPG"))
    assert _is_supported_screenshot_path(Path("WoWScrnShot_1.tga"))
    assert _is_supported_screenshot_path(Path("WoWScrnShot_1.TGA"))
    assert not _is_supported_screenshot_path(Path("WoWScrnShot_1.png"))


def test_iter_screenshot_candidates_filters_prefix_and_suffix(tmp_path: Path):
    for name in (
        "WoWScrnShot_0001.jpg",
        "WoWScrnShot_0002.JPG",
        "WoWScrnShot_0003.tga",
        "WoWScrnShot_0004.TGA",
        "WoWScrnShot_0005.png",
        "Manual_0006.jpg",
    ):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "WoWScrnShot_dir.tga").mkdir()

    got = {p.name for p in _iter_screenshot_candidates(tmp_path)}

    assert got == {
        "WoWScrnShot_0001.jpg",
        "WoWScrnShot_0002.JPG",
        "WoWScrnShot_0003.tga",
        "WoWScrnShot_0004.TGA",
    }


def test_handler_should_process_uses_supported_suffix_policy(tmp_path: Path):
    seen: list[Path] = []
    handler = _Handler(seen.append)

    unsupported = tmp_path / "WoWScrnShot_0001.png"
    unsupported.write_bytes(b"x")
    assert not handler._should_process(unsupported)

    supported = tmp_path / "WoWScrnShot_0001.tga"
    supported.write_bytes(b"x")
    assert handler._should_process(supported)
    assert not handler._should_process(supported)


def test_handler_should_process_distinguishes_reused_filename_by_precise_stat():
    class FakePath:
        suffix = ".jpg"

        def __init__(self, *, mtime: float, mtime_ns: int, size: int) -> None:
            self._stat = SimpleNamespace(
                st_mtime=mtime,
                st_mtime_ns=mtime_ns,
                st_size=size,
            )

        def __str__(self) -> str:
            return "C:/World of Warcraft/_retail_/Screenshots/WoWScrnShot_0001.jpg"

        def stat(self) -> SimpleNamespace:
            return self._stat

    handler = _Handler(lambda _path: None)

    assert handler._should_process(FakePath(mtime=1000.0, mtime_ns=1000, size=10))
    assert not handler._should_process(
        FakePath(mtime=1000.0, mtime_ns=1000, size=10)
    )
    assert handler._should_process(FakePath(mtime=1000.0, mtime_ns=1001, size=10))
    assert handler._should_process(FakePath(mtime=1000.0, mtime_ns=1001, size=12))


def test_decode_qr_symbols_surfaces_lazy_import_failure(monkeypatch: pytest.MonkeyPatch):
    screenshot_mod.pyzbar_decode = None
    screenshot_mod.ZBarSymbol = None
    original_import = __import__

    def fail_pyzbar_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pyzbar.pyzbar":
            raise ImportError("zbar missing")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fail_pyzbar_import)

    with pytest.raises(screenshot_mod.QRDecoderUnavailable, match="zbar missing"):
        screenshot_mod._decode_qr_symbols(Image.new("RGB", (1, 1)))


def test_cleanup_dry_run_reports_marker_files_without_deleting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    marker = tmp_path / "WoWScrnShot_0001.jpg"
    manual = tmp_path / "WoWScrnShot_0002.jpg"
    _write_blank_image(marker)
    _write_blank_image(manual)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)

    def fake_decode(path: Path) -> screenshot_mod.DecodeResult:
        if path == marker:
            return screenshot_mod.DecodeResult(None, True, "CRC mismatch")
        return screenshot_mod.DecodeResult(None, False)

    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", fake_decode)

    summary = screenshot_mod.cleanup_appscout_screenshots(tmp_path, delete=False)

    assert summary.scanned == 2
    assert summary.markers_found == 1
    assert summary.deleted == 0
    assert summary.preserved == 2
    assert marker.exists()
    assert manual.exists()


def test_cleanup_delete_removes_marker_and_preserves_manual_screenshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    marker = tmp_path / "WoWScrnShot_0001.jpg"
    manual = tmp_path / "WoWScrnShot_0002.jpg"
    _write_blank_image(marker)
    _write_blank_image(manual)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda path: screenshot_mod.DecodeResult(None, path == marker),
    )

    summary = screenshot_mod.cleanup_appscout_screenshots(tmp_path, delete=True)

    assert summary.markers_found == 1
    assert summary.deleted == 1
    assert summary.preserved == 1
    assert not marker.exists()
    assert manual.exists()


def test_cleanup_delete_removes_parse_failed_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    _write_blank_image(image_path)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(None, True, "CRC mismatch"),
    )

    summary = screenshot_mod.cleanup_appscout_screenshots(tmp_path, delete=True)

    assert summary.markers_found == 1
    assert summary.deleted == 1
    assert summary.decode_errors == 0
    assert not image_path.exists()


def test_cleanup_preserves_unreadable_file_when_ownership_not_proven(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    _write_blank_image(image_path)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)

    def raise_decode(_path: Path) -> screenshot_mod.DecodeResult:
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", raise_decode)

    summary = screenshot_mod.cleanup_appscout_screenshots(tmp_path, delete=True)

    assert summary.decode_errors == 1
    assert summary.deleted == 0
    assert summary.preserved == 1
    assert image_path.exists()


def test_cleanup_skips_unstable_candidate_without_decoding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    _write_blank_image(image_path)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: False)

    def fail_if_decoded(_path: Path) -> screenshot_mod.DecodeResult:
        raise AssertionError("unstable files must not be decoded or deleted")

    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", fail_if_decoded)

    summary = screenshot_mod.cleanup_appscout_screenshots(tmp_path, delete=True)

    assert summary.unstable == 1
    assert summary.preserved == 1
    assert image_path.exists()


def test_cleanup_limit_scans_newest_candidates_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    older = tmp_path / "WoWScrnShot_0001.jpg"
    newer = tmp_path / "WoWScrnShot_0002.jpg"
    _write_blank_image(older)
    _write_blank_image(newer)
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    seen: list[str] = []
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)

    def fake_decode(path: Path) -> screenshot_mod.DecodeResult:
        seen.append(path.name)
        return screenshot_mod.DecodeResult(None, True)

    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", fake_decode)

    summary = screenshot_mod.cleanup_appscout_screenshots(tmp_path, limit=1)

    assert seen == ["WoWScrnShot_0002.jpg"]
    assert summary.scanned == 1
    assert summary.limited is True


def test_screenshot_module_cli_preserves_legacy_single_file_decode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    _write_blank_image(image_path)
    snapshot = Snapshot(listing=None, version=None)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(snapshot, True),
    )

    assert screenshot_mod._main([str(image_path)]) == 0

    captured = capsys.readouterr()
    assert "DECODED OK:" in captured.out
    assert "applicants (0)" in captured.out


def test_screenshot_module_cli_cleanup_is_dry_run_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    _write_blank_image(image_path)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(None, True),
    )

    assert screenshot_mod._main(["cleanup", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert "dry run" in captured.out
    assert "--delete" in captured.out
    assert image_path.exists()


def test_screenshot_module_cli_with_explicit_argv_does_not_configure_root_logging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    _write_blank_image(image_path)
    snapshot = Snapshot(listing=None, version=None)
    calls: list[str] = []
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(snapshot, True),
    )
    monkeypatch.setattr(logging, "basicConfig", lambda **_kwargs: calls.append("basic"))

    assert screenshot_mod._main([str(image_path)]) == 0
    assert calls == []


def test_watcher_start_cleans_observer_when_observer_start_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    class FakeObserver:
        def __init__(self) -> None:
            self.scheduled: list[tuple[object, str, bool]] = []
            self.stopped = False
            self.joined: list[float | None] = []
            self.alive = False

        def schedule(self, handler: object, path: str, *, recursive: bool) -> None:
            self.scheduled.append((handler, path, recursive))

        def start(self) -> None:
            self.alive = True
            raise RuntimeError("observer start failed")

        def stop(self) -> None:
            self.stopped = True
            self.alive = False

        def join(self, timeout: float | None = None) -> None:
            self.joined.append(timeout)

        def is_alive(self) -> bool:
            return self.alive

    observer = FakeObserver()
    monkeypatch.setattr(screenshot_mod, "Observer", lambda: observer)
    watcher = ScreenshotWatcher(tmp_path)

    with pytest.raises(RuntimeError, match="observer start failed"):
        watcher.start()

    assert observer.scheduled
    assert observer.stopped
    assert observer.joined == [2]
    assert watcher._observer is None


def test_watcher_start_cleans_observer_when_backlog_thread_start_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    class FakeObserver:
        def __init__(self) -> None:
            self.stopped = False
            self.joined: list[float | None] = []
            self.alive = False

        def schedule(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            self.alive = True

        def stop(self) -> None:
            self.stopped = True
            self.alive = False

        def join(self, timeout: float | None = None) -> None:
            self.joined.append(timeout)

        def is_alive(self) -> bool:
            return self.alive

    class FailingThread:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread start failed")

    observer = FakeObserver()
    monkeypatch.setattr(screenshot_mod, "Observer", lambda: observer)
    monkeypatch.setattr(screenshot_mod.threading, "Thread", FailingThread)
    watcher = ScreenshotWatcher(tmp_path)

    with pytest.raises(RuntimeError, match="thread start failed"):
        watcher.start()

    assert observer.stopped
    assert observer.joined == [2]
    assert watcher._observer is None
    assert watcher._backlog_thread is None


def test_watcher_stop_suppresses_direct_file_signals(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"x")
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(
            Snapshot(listing=None, version=None), True
        ),
    )

    watcher.stop()
    watcher._on_new_file(image_path)

    assert snapshots == []
    assert failures == []
    assert image_path.exists()


def test_watcher_emits_decode_failed_for_marker_parse_failure(
    monkeypatch, tmp_path: Path
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"x")
    watcher = ScreenshotWatcher(tmp_path)
    failures: list[tuple[str, str]] = []
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(None, True),
    )

    watcher._on_new_file(image_path)

    assert failures == [(str(image_path), "parse failed")]
    assert not image_path.exists()


def test_watcher_stable_timeout_preserves_manual_screenshot_without_failure(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"manual")
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: False)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(None, False),
    )

    watcher._on_new_file(image_path)

    assert snapshots == []
    assert failures == []
    assert image_path.exists()


def test_watcher_preserves_manual_screenshot_when_decode_raises(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"manual")
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)

    def raise_decode(_path: Path):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", raise_decode)

    watcher._on_new_file(image_path)

    assert snapshots == []
    assert failures == []
    assert image_path.exists()


def test_watcher_stable_timeout_preserves_manual_screenshot_when_decode_raises(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"manual")
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: False)

    def raise_decode(_path: Path):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", raise_decode)

    watcher._on_new_file(image_path)

    assert snapshots == []
    assert failures == []
    assert image_path.exists()


def test_watcher_stable_timeout_emits_marker_snapshot_and_deletes_transport(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"transport")
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: False)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(snapshot, True),
    )

    watcher._on_new_file(image_path)

    assert snapshots == [snapshot]
    assert failures == []
    assert not image_path.exists()


def test_watcher_stamps_live_snapshot_source_from_file_stat(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    content = b"transport"
    image_path.write_bytes(content)
    os.utime(image_path, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    expected_mtime_ns = image_path.stat().st_mtime_ns
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    watcher.snapshotReceived.connect(snapshots.append)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(snapshot, True),
    )

    watcher._on_new_file(image_path)

    assert snapshots == [snapshot]
    source = snapshots[0].source
    assert source is not None
    assert source.mtime_ns == expected_mtime_ns
    assert source.file_id == str(image_path)
    assert source.size == len(content)


def test_watcher_stable_timeout_emits_marker_parse_failure_and_deletes_transport(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"transport")
    watcher = ScreenshotWatcher(tmp_path)
    failures: list[tuple[str, str]] = []
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: False)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(None, True, "CRC mismatch"),
    )

    watcher._on_new_file(image_path)

    assert failures == [(str(image_path), "CRC mismatch")]
    assert not image_path.exists()


def test_watcher_decode_failure_includes_snapshot_source(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    content = b"transport"
    image_path.write_bytes(content)
    os.utime(image_path, ns=(1_700_000_001_123_456_789, 1_700_000_001_123_456_789))
    expected_mtime_ns = image_path.stat().st_mtime_ns
    watcher = ScreenshotWatcher(tmp_path)
    failures: list[tuple[str, str, object]] = []
    watcher.decodeFailed.connect(
        lambda path, reason, source: failures.append((path, reason, source))
    )
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(None, True, "CRC mismatch"),
    )

    watcher._on_new_file(image_path)

    assert len(failures) == 1
    path, reason, source = failures[0]
    assert path == str(image_path)
    assert reason == "CRC mismatch"
    assert source is not None
    assert source.mtime_ns == expected_mtime_ns
    assert source.file_id == str(image_path)
    assert source.size == len(content)


def test_watcher_surfaces_decoder_unavailable_without_deleting_screenshot(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    content = b"maybe-transport"
    image_path.write_bytes(content)
    watcher = ScreenshotWatcher(tmp_path)
    failures: list[tuple[str, str, object]] = []
    watcher.decodeFailed.connect(
        lambda path, reason, source: failures.append((path, reason, source))
    )
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(
            None,
            False,
            "QR decoder unavailable: zbar missing",
            decoder_unavailable=True,
        ),
    )

    watcher._on_new_file(image_path)

    assert len(failures) == 1
    path, reason, source = failures[0]
    assert path == str(image_path)
    assert reason == "QR decoder unavailable: zbar missing"
    assert source is not None
    assert image_path.exists()


def test_watcher_stop_mid_new_file_does_not_delete_unemitted_marker_snapshot(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"transport")
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))

    def stop_during_stable_wait(_path: Path) -> bool:
        watcher.stop()
        return True

    monkeypatch.setattr(
        screenshot_mod,
        "_wait_for_stable_size",
        stop_during_stable_wait,
    )
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(snapshot, True),
    )

    watcher._on_new_file(image_path)

    assert snapshots == []
    assert failures == []
    assert image_path.exists()


def test_watcher_stop_during_snapshot_emit_preserves_marker_file(
    monkeypatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"transport")
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    watcher.snapshotReceived.connect(snapshots.append)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(snapshot, True),
    )

    def stop_before_emit(_snap: Snapshot) -> bool:
        watcher.stop()
        return False

    monkeypatch.setattr(watcher, "_emit_snapshot", stop_before_emit)

    watcher._on_new_file(image_path)

    assert snapshots == []
    assert image_path.exists()


def test_backlog_does_not_apply_older_snapshot_after_newest_marker_decode_failure(
    monkeypatch,
    tmp_path: Path,
):
    now = 1_000.0
    newest = tmp_path / "WoWScrnShot_0001.jpg"
    older = tmp_path / "WoWScrnShot_9999.jpg"
    newest.write_bytes(b"newest")
    older.write_bytes(b"older")
    os.utime(newest, (now, now))
    os.utime(older, (now - 5.0, now - 5.0))
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda path: screenshot_mod.DecodeResult(None, True, "CRC mismatch")
        if path == newest
        else screenshot_mod.DecodeResult(snapshot, True),
    )

    watcher._scan_recent_backlog()

    assert snapshots == []
    assert failures == [(str(newest), "CRC mismatch")]
    assert not newest.exists()
    assert not older.exists()


def test_backlog_marker_parser_exception_suppresses_older_snapshot(
    monkeypatch,
    tmp_path: Path,
):
    now = 1_000.0
    newest = tmp_path / "WoWScrnShot_0001.jpg"
    older = tmp_path / "WoWScrnShot_9999.jpg"
    _write_blank_image(newest)
    _write_blank_image(older)
    os.utime(newest, (now, now))
    os.utime(older, (now - 5.0, now - 5.0))
    newest_payload = _wrap_payload(_build_body([]))
    older_payload = _wrap_payload(
        _build_body(
            [
                _build_applicant_block(
                    aid=11,
                    class_id=1,
                    spec_id=71,
                    ilvl=480,
                    score=2443,
                    role=2,
                    name="Older-Realm",
                    version=1,
                )
            ]
        ),
        wire_ver=0x01,
    )
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)

    def fake_decode(img, symbols=None):
        filename = Path(getattr(img, "filename", "")).name
        payload = newest_payload if filename == newest.name else older_payload
        return [SimpleNamespace(data=payload)]

    monkeypatch.setattr(screenshot_mod, "pyzbar_decode", fake_decode)
    original_parse = screenshot_mod._try_parse_appscout_payload

    def parse_or_raise(raw: bytes):
        if raw == newest_payload:
            raise RuntimeError("parser exploded")
        return original_parse(raw)

    monkeypatch.setattr(screenshot_mod, "_try_parse_appscout_payload", parse_or_raise)

    watcher._scan_recent_backlog()

    assert snapshots == []
    assert len(failures) == 1
    assert failures[0][0] == str(newest)
    assert "unexpected parser error: RuntimeError: parser exploded" in failures[0][1]
    assert not newest.exists()
    assert not older.exists()


def test_backlog_can_apply_older_snapshot_when_newest_file_has_no_marker(
    monkeypatch,
    tmp_path: Path,
):
    now = 1_000.0
    manual = tmp_path / "WoWScrnShot_0001.jpg"
    older = tmp_path / "WoWScrnShot_9999.jpg"
    manual.write_bytes(b"manual")
    older.write_bytes(b"older")
    os.utime(manual, (now, now))
    os.utime(older, (now - 5.0, now - 5.0))
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda path: screenshot_mod.DecodeResult(None, False)
        if path == manual
        else screenshot_mod.DecodeResult(snapshot, True),
    )

    watcher._scan_recent_backlog()

    assert snapshots == [snapshot]
    assert failures == []
    assert manual.exists()
    assert not older.exists()


def test_backlog_stamps_snapshot_source_from_candidate_stat(
    monkeypatch,
    tmp_path: Path,
):
    now = 1_000.0
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    content = b"transport"
    image_path.write_bytes(content)
    os.utime(image_path, ns=(1_700_000_002_123_456_789, 1_700_000_002_123_456_789))
    expected_mtime_ns = image_path.stat().st_mtime_ns
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    watcher.snapshotReceived.connect(snapshots.append)
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(snapshot, True),
    )

    watcher._scan_recent_backlog()

    assert snapshots == [snapshot]
    source = snapshots[0].source
    assert source is not None
    assert source.mtime_ns == expected_mtime_ns
    assert source.file_id == str(image_path)
    assert source.size == len(content)


def test_backlog_skips_unstable_recent_file_without_decoding_or_deleting(
    monkeypatch,
    tmp_path: Path,
):
    now = 1_000.0
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"partial")
    os.utime(image_path, (now, now))
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: False)
    decoded_paths: list[Path] = []

    def fail_if_decoded(_path: Path) -> screenshot_mod.DecodeResult:
        decoded_paths.append(_path)
        raise AssertionError("backlog decoded an unstable startup screenshot")

    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", fail_if_decoded)

    watcher._scan_recent_backlog()

    assert decoded_paths == []
    assert snapshots == []
    assert failures == []
    assert image_path.exists()


def test_backlog_unstable_newest_manual_does_not_delete_older_valid_snapshot_without_applying(
    monkeypatch,
    tmp_path: Path,
):
    now = 1_000.0
    newest = tmp_path / "WoWScrnShot_0001.jpg"
    older = tmp_path / "WoWScrnShot_9999.jpg"
    newest.write_bytes(b"manual-still-writing")
    older.write_bytes(b"transport")
    os.utime(newest, (now, now))
    os.utime(older, (now - 5.0, now - 5.0))
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        screenshot_mod,
        "_wait_for_stable_size",
        lambda path: path != newest,
    )
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda path: screenshot_mod.DecodeResult(snapshot, True)
        if path == older
        else screenshot_mod.DecodeResult(None, False),
    )

    watcher._scan_recent_backlog()

    assert snapshots == [snapshot]
    assert failures == []
    assert newest.exists()
    assert not older.exists()


def test_backlog_stop_mid_scan_preserves_unapplied_marker_files(
    monkeypatch,
    tmp_path: Path,
):
    now = 1_000.0
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"transport")
    os.utime(image_path, (now, now))
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)

    def stop_during_decode(_path: Path) -> screenshot_mod.DecodeResult:
        watcher.stop()
        return screenshot_mod.DecodeResult(snapshot, True)

    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", stop_during_decode)

    watcher._scan_recent_backlog()

    assert snapshots == []
    assert failures == []
    assert image_path.exists()


def test_backlog_stop_during_snapshot_emit_preserves_marker_file(
    monkeypatch,
    tmp_path: Path,
):
    now = 1_000.0
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"transport")
    os.utime(image_path, (now, now))
    snapshot = Snapshot(listing=None, version=None)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    watcher.snapshotReceived.connect(snapshots.append)
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(snapshot, True),
    )

    def stop_before_emit(_snap: Snapshot) -> bool:
        watcher.stop()
        return False

    monkeypatch.setattr(watcher, "_emit_snapshot", stop_before_emit)

    watcher._scan_recent_backlog()

    assert snapshots == []
    assert image_path.exists()


def test_watcher_stop_suppresses_backlog_signals(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"x")
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    watcher.snapshotReceived.connect(snapshots.append)
    monkeypatch.setattr(
        screenshot_mod,
        "_iter_screenshot_candidates",
        lambda _path: [image_path],
    )
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(
            Snapshot(listing=None, version=None), True
        ),
    )

    watcher.stop()
    watcher._scan_recent_backlog()

    assert snapshots == []
    assert image_path.exists()
