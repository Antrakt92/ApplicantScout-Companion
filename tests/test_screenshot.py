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
from collections.abc import Callable
from dataclasses import replace
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
    SnapshotSource,
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


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeTimer:
    def __init__(self, delay: float, callback: Callable[[], None]) -> None:
        self.delay = delay
        self.callback = callback
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if self.started and not self.cancelled:
            self.callback()


class _FakeTimerFactory:
    def __init__(self) -> None:
        self.timers: list[_FakeTimer] = []

    def __call__(
        self,
        delay: float,
        callback: Callable[[], None],
    ) -> _FakeTimer:
        timer = _FakeTimer(delay, callback)
        self.timers.append(timer)
        return timer


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


def _large_v9_payload(row_count: int = 24) -> bytes:
    blocks = [
        _build_applicant_block(
            aid=index + 1,
            member_idx=1,
            class_id=8,
            spec_id=64,
            ilvl=280,
            score=2000 + index,
            role=2,
            name=f"Player{index}-Realm",
            version=5,
        )
        for index in range(row_count)
    ]
    return _wrap_payload(_build_body_v7(blocks, []), wire_ver=0x09)


def _wrap_fragments(
    inner: bytes,
    *,
    stream_id: int = 17,
    generation: int = 23,
) -> list[bytes]:
    chunk_size = screenshot_mod.APS1_FRAGMENT_CHUNK_BYTES
    count = (len(inner) + chunk_size - 1) // chunk_size
    assert screenshot_mod.APS1_FRAGMENT_MIN_CHUNKS <= count
    assert count <= screenshot_mod.APS1_FRAGMENT_MAX_CHUNKS
    inner_crc = struct.unpack(">I", inner[-4:])[0]
    frames = []
    for index in range(count):
        chunk = inner[index * chunk_size : (index + 1) * chunk_size]
        metadata = struct.pack(
            ">IIHHHI",
            stream_id,
            generation,
            index,
            count,
            len(inner),
            inner_crc,
        )
        frames.append(_wrap_payload(metadata + chunk, wire_ver=0x0A))
    return frames


def _parse_fragment(raw: bytes) -> screenshot_mod.SnapshotFragment:
    parsed, error = screenshot_mod._try_parse_appscout_candidate(raw)
    assert error is None
    assert isinstance(parsed, screenshot_mod.SnapshotFragment)
    return parsed


def _fragment_with_source(
    fragment: screenshot_mod.SnapshotFragment,
    *,
    path: Path,
    mtime_ns: int,
    size: int = 10,
) -> screenshot_mod.SnapshotFragment:
    return replace(
        fragment,
        source=screenshot_mod.SnapshotSource(
            mtime_ns=mtime_ns,
            file_id=str(path),
            size=size,
        ),
    )


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
    assert 0x09 in WIRE_VERSIONS_SUPPORTED
    assert 0x0A in WIRE_VERSIONS_SUPPORTED
    assert 0x00 not in WIRE_VERSIONS_SUPPORTED  # canary


def test_v10_non_fragment_body_is_rejected():
    raw = _wrap_payload(_build_body([]), wire_ver=0x0A)

    parsed, error = screenshot_mod._try_parse_appscout_candidate(raw)

    assert parsed is None
    assert error == "v10 fragment body is shorter than metadata plus one byte"


def test_v10_fragment_envelope_parses_fixed_chunk_contract():
    inner = _large_v9_payload()
    raw = _wrap_fragments(inner, stream_id=0x01020304, generation=99)[0]

    parsed, error = screenshot_mod._try_parse_appscout_candidate(raw)

    assert error is None
    assert isinstance(parsed, screenshot_mod.SnapshotFragment)
    assert parsed.stream_id == 0x01020304
    assert parsed.generation == 99
    assert parsed.chunk_index == 0
    assert parsed.chunk_count == len(_wrap_fragments(inner))
    assert parsed.inner_total_len == len(inner)
    assert parsed.inner_crc32 == struct.unpack(">I", inner[-4:])[0]
    assert len(parsed.chunk) == screenshot_mod.APS1_FRAGMENT_CHUNK_BYTES


@pytest.mark.parametrize(("flags", "reserved"), [(1, 0), (0, 1), (4, 2)])
def test_v10_fragment_rejects_flags_and_reserved_bytes(flags: int, reserved: int):
    inner = _large_v9_payload()
    body = _wrap_fragments(inner)[0][9:-4]

    parsed, error = screenshot_mod._try_parse_appscout_candidate(
        _wrap_payload(body, wire_ver=0x0A, flags=flags, reserved2=reserved)
    )

    assert parsed is None
    assert error == (
        f"unsupported APS1 v10 reserved bytes 0x{flags:02x} 0x{reserved:02x}"
    )


def test_v10_fragment_rejects_wrong_count_index_and_chunk_length():
    inner = _large_v9_payload()
    frames = _wrap_fragments(inner)
    body = bytearray(frames[0][9:-4])

    body[10:12] = struct.pack(">H", 1)
    parsed, error = screenshot_mod._try_parse_appscout_candidate(
        _wrap_payload(bytes(body), wire_ver=0x0A)
    )
    assert parsed is None
    assert error == "v10 chunk_count 1 outside 2..128"

    body = bytearray(frames[0][9:-4])
    body[8:10] = struct.pack(">H", len(frames))
    parsed, error = screenshot_mod._try_parse_appscout_candidate(
        _wrap_payload(bytes(body), wire_ver=0x0A)
    )
    assert parsed is None
    assert error == f"v10 chunk_index {len(frames)} outside chunk_count {len(frames)}"

    body = frames[0][9:-4][:-1]
    parsed, error = screenshot_mod._try_parse_appscout_candidate(
        _wrap_payload(body, wire_ver=0x0A)
    )
    assert parsed is None
    assert error is not None
    assert "has 639 bytes; expected 640" in error


def test_decode_result_fragment_field_preserves_existing_positional_arguments():
    fragment = _parse_fragment(_wrap_fragments(_large_v9_payload())[0])

    result = screenshot_mod.DecodeResult(None, True, None, False, fragment)

    assert result.snapshot is None
    assert result.has_marker is True
    assert result.error_reason is None
    assert result.decoder_unavailable is False
    assert result.fragment is fragment
    assert result.fragment_candidate is False


@pytest.mark.parametrize("row_count", [200, 201])
def test_applicant_row_bound_is_derived_from_wire_capacity(row_count: int):
    payload = _large_v9_payload(row_count)

    parsed, error = _try_parse_appscout_payload(payload)

    assert error is None
    assert isinstance(parsed, Snapshot)
    assert len(parsed.applicants) == row_count


def test_applicant_row_count_that_cannot_fit_is_rejected_before_row_loop():
    body = bytes([0, 0, 0]) + struct.pack(">H", 201) + struct.pack(">H", 0)

    parsed, error = _try_parse_appscout_payload(
        _wrap_payload(body, wire_ver=0x09)
    )

    assert parsed is None
    assert error is not None
    assert "applicant_count 201 cannot fit" in error


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
    assert snap.roster_unavailable is False


def test_v9_payload_parses_roster_unavailable_flag():
    body = _build_body_v7(
        [
            _build_applicant_block(
                aid=42,
                member_idx=1,
                class_id=8,
                spec_id=64,
                ilvl=281,
                score=2444,
                role=2,
                name="Fresh-Realm",
                version=5,
            )
        ],
        [],
    )
    raw = _wrap_payload(body, wire_ver=0x09, flags=0x04)

    snap, error = _try_parse_appscout_payload(raw)

    assert error is None
    assert snap is not None
    assert snap.terminal_clear is False
    assert snap.lfg_unavailable is False
    assert snap.roster_unavailable is True
    assert [app.name for app in snap.applicants] == ["Fresh-Realm"]


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


def test_decode_result_exposes_v10_fragment_without_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    image_path = tmp_path / "fragment_qr.png"
    _write_blank_image(image_path)
    raw_fragment = _wrap_fragments(_large_v9_payload())[0]
    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=raw_fragment)],
    )

    result = screenshot_mod._decode_screenshot_result(image_path)

    assert result.has_marker is True
    assert result.snapshot is None
    assert isinstance(result.fragment, screenshot_mod.SnapshotFragment)
    assert result.error_reason is None


def test_decode_result_identifies_corrupt_v10_candidate_for_backlog_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    image_path = tmp_path / "corrupt_fragment_qr.png"
    _write_blank_image(image_path)
    raw_fragment = bytearray(_wrap_fragments(_large_v9_payload())[0])
    raw_fragment[-1] ^= 0xFF
    monkeypatch.setattr(
        screenshot_mod,
        "pyzbar_decode",
        lambda img, symbols=None: [SimpleNamespace(data=bytes(raw_fragment))],
    )

    result = screenshot_mod._decode_screenshot_result(image_path)

    assert result.has_marker is True
    assert result.snapshot is None
    assert result.fragment is None
    assert result.fragment_candidate is True
    assert result.error_reason is not None
    assert "CRC mismatch" in result.error_reason


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
    assert handler._should_process(supported)


def test_shared_work_claim_distinguishes_reused_filename_by_precise_stat(
    tmp_path: Path,
):
    path = tmp_path / "WoWScrnShot_0001.jpg"
    path.write_bytes(b"first")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    claims = screenshot_mod._ScreenshotWorkClaims()

    first = claims.try_claim(path)
    assert first is not None
    assert claims.try_claim(path) is None
    first.release()
    assert claims.try_claim(path) is None

    path.write_bytes(b"second-generation")
    os.utime(path, ns=(1_000_000_001, 1_000_000_001))
    second = claims.try_claim(path)
    assert second is not None
    second.release()


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


def test_decode_result_marks_transient_zbar_failure_as_incomplete_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    _write_blank_image(image_path)

    def fail_scan(_image, *, symbols=None):
        raise RuntimeError("temporary zbar failure")

    monkeypatch.setattr(screenshot_mod, "pyzbar_decode", fail_scan)
    monkeypatch.setattr(screenshot_mod, "ZBarSymbol", None)

    result = screenshot_mod._decode_screenshot_result(image_path)

    assert result.snapshot is None
    assert not result.has_marker
    assert result.error_reason == "QR scan failed: temporary zbar failure"


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


def test_fragment_assembler_completes_out_of_order_exactly_once():
    inner = _large_v9_payload()
    fragments = [_parse_fragment(frame) for frame in _wrap_fragments(inner)]
    assembler = screenshot_mod._SnapshotFragmentAssembler()
    completed = []

    for index in reversed(range(len(fragments))):
        path = Path(f"chunk-{index}.jpg")
        outcome = assembler.accept_fragment(
            _fragment_with_source(
                fragments[index],
                path=path,
                mtime_ns=100 + index,
            ),
            path,
        )
        if outcome.snapshot is not None:
            completed.append(outcome.snapshot)

    assert len(completed) == 1
    assert len(completed[0].applicants) == 24
    assert completed[0].source is not None
    assert completed[0].source.mtime_ns == 100 + len(fragments) - 1
    assert {item.path for item in outcome.retired_files} == {
        Path(f"chunk-{index}.jpg") for index in range(len(fragments))
    }

    repeated_path = Path("repeat.jpg")
    repeated = assembler.accept_fragment(
        _fragment_with_source(
            fragments[-1],
            path=repeated_path,
            mtime_ns=500,
        ),
        repeated_path,
    )
    assert repeated.snapshot is None
    assert [item.path for item in repeated.retired_files] == [repeated_path]


def test_fragment_assembler_identical_duplicate_is_idempotent():
    fragments = [_parse_fragment(frame) for frame in _wrap_fragments(_large_v9_payload())]
    assembler = screenshot_mod._SnapshotFragmentAssembler()
    first_path = Path("first.jpg")
    duplicate_path = Path("duplicate.jpg")

    first = assembler.accept_fragment(
        _fragment_with_source(fragments[0], path=first_path, mtime_ns=100),
        first_path,
    )
    duplicate = assembler.accept_fragment(
        _fragment_with_source(fragments[0], path=duplicate_path, mtime_ns=101),
        duplicate_path,
    )
    assert first.snapshot is None
    assert duplicate.snapshot is None
    assert duplicate.error_reason is None

    outcome = duplicate
    for index in range(1, len(fragments)):
        path = Path(f"rest-{index}.jpg")
        outcome = assembler.accept_fragment(
            _fragment_with_source(fragments[index], path=path, mtime_ns=101 + index),
            path,
        )
    assert outcome.snapshot is not None
    assert first_path in {item.path for item in outcome.retired_files}
    assert duplicate_path in {item.path for item in outcome.retired_files}


def test_fragment_assembler_conflicting_duplicate_poisons_generation():
    fragments = [_parse_fragment(frame) for frame in _wrap_fragments(_large_v9_payload())]
    assembler = screenshot_mod._SnapshotFragmentAssembler()
    first_path = Path("first.jpg")
    conflict_path = Path("conflict.jpg")
    assembler.accept_fragment(
        _fragment_with_source(fragments[0], path=first_path, mtime_ns=100),
        first_path,
    )
    conflicting = replace(
        fragments[0],
        chunk=bytes([fragments[0].chunk[0] ^ 0xFF]) + fragments[0].chunk[1:],
    )

    poisoned = assembler.accept_fragment(
        _fragment_with_source(conflicting, path=conflict_path, mtime_ns=101),
        conflict_path,
    )

    assert poisoned.snapshot is None
    assert poisoned.error_reason == "conflicting v10 chunk 0 for one generation"
    assert {item.path for item in poisoned.retired_files} == {
        first_path,
        conflict_path,
    }
    late_path = Path("late.jpg")
    late = assembler.accept_fragment(
        _fragment_with_source(fragments[1], path=late_path, mtime_ns=102),
        late_path,
    )
    assert late.snapshot is None
    assert [item.path for item in late.retired_files] == [late_path]


@pytest.mark.parametrize("nested", [False, True], ids=["inner-crc", "nested-v10"])
def test_fragment_assembler_rejects_invalid_completed_inner_payload(nested: bool):
    logical = _large_v9_payload()
    if nested:
        logical = _wrap_fragments(logical)[0]
        frames = _wrap_fragments(logical, stream_id=18, generation=24)
    else:
        frames = _wrap_fragments(logical)
    fragments = [_parse_fragment(frame) for frame in frames]
    if not nested:
        fragments[-1] = replace(
            fragments[-1],
            chunk=fragments[-1].chunk[:-5]
            + bytes([fragments[-1].chunk[-5] ^ 0xFF])
            + fragments[-1].chunk[-4:],
        )
    assembler = screenshot_mod._SnapshotFragmentAssembler()
    outcome = None
    for index, fragment in enumerate(fragments):
        path = Path(f"invalid-{index}.jpg")
        outcome = assembler.accept_fragment(
            _fragment_with_source(fragment, path=path, mtime_ns=100 + index),
            path,
        )

    assert outcome is not None
    assert outcome.snapshot is None
    assert outcome.error_reason == (
        "nested v10 fragment payload is not allowed"
        if nested
        else "assembled v10 inner CRC mismatch"
    )


def test_fragment_assembler_matches_metadata_crc_to_inner_v9_trailer():
    fragments = [
        replace(_parse_fragment(frame), inner_crc32=0x01020304)
        for frame in _wrap_fragments(_large_v9_payload())
    ]
    assembler = screenshot_mod._SnapshotFragmentAssembler()
    outcome = None
    for index, fragment in enumerate(fragments):
        path = Path(f"wrong-trailer-{index}.jpg")
        outcome = assembler.accept_fragment(
            _fragment_with_source(fragment, path=path, mtime_ns=100 + index),
            path,
        )

    assert outcome is not None
    assert outcome.snapshot is None
    assert outcome.error_reason == "assembled v10 inner CRC trailer mismatch"


def test_fragment_assembler_newer_generation_supersedes_incomplete_old():
    inner = _large_v9_payload()
    old = [_parse_fragment(frame) for frame in _wrap_fragments(inner, generation=10)]
    new = [_parse_fragment(frame) for frame in _wrap_fragments(inner, generation=11)]
    assembler = screenshot_mod._SnapshotFragmentAssembler()
    old_path = Path("old-0.jpg")
    assembler.accept_fragment(
        _fragment_with_source(old[0], path=old_path, mtime_ns=100),
        old_path,
    )

    new_path = Path("new-0.jpg")
    superseding = assembler.accept_fragment(
        _fragment_with_source(new[0], path=new_path, mtime_ns=101),
        new_path,
    )
    assert [item.path for item in superseding.retired_files] == [old_path]

    stale_path = Path("old-1.jpg")
    stale = assembler.accept_fragment(
        _fragment_with_source(old[1], path=stale_path, mtime_ns=102),
        stale_path,
    )
    assert stale.snapshot is None
    assert [item.path for item in stale.retired_files] == [stale_path]

    outcome = superseding
    for index in range(1, len(new)):
        path = Path(f"new-{index}.jpg")
        outcome = assembler.accept_fragment(
            _fragment_with_source(new[index], path=path, mtime_ns=103 + index),
            path,
        )
    assert outcome.snapshot is not None


def test_fragment_assembler_terminal_barrier_blocks_older_late_chunks():
    fragments = [_parse_fragment(frame) for frame in _wrap_fragments(_large_v9_payload())]
    assembler = screenshot_mod._SnapshotFragmentAssembler()
    first_path = Path("fragment.jpg")
    assembler.accept_fragment(
        _fragment_with_source(fragments[0], path=first_path, mtime_ns=200),
        first_path,
    )
    older_terminal = Snapshot(
        listing=None,
        version=None,
        terminal_clear=True,
        source=screenshot_mod.SnapshotSource(150, "old-clear.jpg", 10),
    )
    assert assembler.accept_snapshot(older_terminal).accepted is False

    terminal = replace(
        older_terminal,
        source=screenshot_mod.SnapshotSource(250, "new-clear.jpg", 10),
    )
    barrier = assembler.accept_snapshot(terminal)
    assert barrier.accepted is True
    assert barrier.snapshot is terminal
    assert [item.path for item in barrier.retired_files] == [first_path]

    late_path = Path("late-fragment.jpg")
    late = assembler.accept_fragment(
        _fragment_with_source(fragments[1], path=late_path, mtime_ns=300),
        late_path,
    )
    assert late.snapshot is None
    assert [item.path for item in late.retired_files] == [late_path]


def test_fragment_assembler_timeout_allows_same_generation_repeat_to_rebuild():
    fragments = [_parse_fragment(frame) for frame in _wrap_fragments(_large_v9_payload())]
    assembler = screenshot_mod._SnapshotFragmentAssembler(ttl_seconds=5)
    first_path = Path("first.jpg")
    first = _fragment_with_source(fragments[0], path=first_path, mtime_ns=100)
    assembler.accept_fragment(first, first_path, now=0)

    repeated = assembler.accept_fragment(first, first_path, now=6)
    assert repeated.snapshot is None
    assert repeated.retired_files == ()

    outcome = repeated
    for index in range(1, len(fragments)):
        path = Path(f"chunk-{index}.jpg")
        outcome = assembler.accept_fragment(
            _fragment_with_source(fragments[index], path=path, mtime_ns=100 + index),
            path,
            now=6 + index,
        )
    assert outcome.snapshot is not None


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


def test_watcher_retains_incomplete_fragments_and_emits_one_complete_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fragments = [_parse_fragment(frame) for frame in _wrap_fragments(_large_v9_payload())]
    paths = [tmp_path / f"WoWScrnShot_{index:04d}.jpg" for index in range(len(fragments))]
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    by_path = {
        path: screenshot_mod.DecodeResult(None, True, fragment=fragment)
        for path, fragment in zip(paths, fragments, strict=True)
    }
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda path: by_path[path],
    )

    for path in paths[:-1]:
        path.write_bytes(b"x")
        watcher._on_new_file(path)
        assert path.exists()
        assert snapshots == []
        assert failures == []

    paths[-1].write_bytes(b"x")
    watcher._on_new_file(paths[-1])

    assert len(snapshots) == 1
    assert len(snapshots[0].applicants) == 24
    assert failures == []
    assert not any(path.exists() for path in paths)


def test_watcher_expires_incomplete_fragment_without_another_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fragment = _parse_fragment(_wrap_fragments(_large_v9_payload())[0])
    path = tmp_path / "WoWScrnShot_incomplete.jpg"
    path.write_bytes(b"fragment")
    clock = _FakeClock()
    timers = _FakeTimerFactory()
    watcher = ScreenshotWatcher(
        tmp_path,
        fragment_clock=clock,
        fragment_timer_factory=timers,
    )
    failures: list[tuple[str, str, SnapshotSource | None]] = []
    snapshots: list[Snapshot] = []
    watcher.decodeFailed.connect(
        lambda failed_path, reason, source: failures.append(
            (failed_path, reason, source)
        )
    )
    watcher.snapshotReceived.connect(snapshots.append)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(None, True, fragment=fragment),
    )

    watcher._on_new_file(path)

    assert path.exists()
    assert len(timers.timers) == 1
    assert timers.timers[0].started is True
    assert timers.timers[0].delay == pytest.approx(
        screenshot_mod.APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS
    )

    clock.advance(screenshot_mod.APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS + 0.1)
    timers.timers[0].fire()
    timers.timers[0].fire()

    assert snapshots == []
    assert [(failed_path, reason) for failed_path, reason, _source in failures] == [
        (str(path), "v10 fragment assembly timed out")
    ]
    assert failures[0][2] is not None
    assert not path.exists()


def test_watcher_stop_cancels_fragment_expiry_and_preserves_pending_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fragment = _parse_fragment(_wrap_fragments(_large_v9_payload())[0])
    path = tmp_path / "WoWScrnShot_pending-on-stop.jpg"
    path.write_bytes(b"fragment")
    clock = _FakeClock()
    timers = _FakeTimerFactory()
    watcher = ScreenshotWatcher(
        tmp_path,
        fragment_clock=clock,
        fragment_timer_factory=timers,
    )
    failures: list[tuple[str, str]] = []
    watcher.decodeFailed.connect(
        lambda failed_path, reason: failures.append((failed_path, reason))
    )
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda _path: screenshot_mod.DecodeResult(None, True, fragment=fragment),
    )

    watcher._on_new_file(path)
    watcher.stop()
    clock.advance(screenshot_mod.APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS + 1)
    timers.timers[0].fire()

    assert timers.timers[0].cancelled is True
    assert failures == []
    assert path.exists()


def test_watcher_stop_during_fragment_completion_preserves_all_chunk_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fragments = [_parse_fragment(frame) for frame in _wrap_fragments(_large_v9_payload())]
    paths = [tmp_path / f"WoWScrnShot_{index:04d}.jpg" for index in range(len(fragments))]
    watcher = ScreenshotWatcher(tmp_path)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    by_path = {
        path: screenshot_mod.DecodeResult(None, True, fragment=fragment)
        for path, fragment in zip(paths, fragments, strict=True)
    }
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda path: by_path[path],
    )
    for path in paths[:-1]:
        path.write_bytes(b"x")
        watcher._on_new_file(path)

    def stop_before_emit(_snap: Snapshot) -> bool:
        watcher.stop()
        return False

    monkeypatch.setattr(watcher, "_emit_snapshot", stop_before_emit)
    paths[-1].write_bytes(b"x")
    watcher._on_new_file(paths[-1])

    assert all(path.exists() for path in paths)


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
    failures: list[tuple[str, str, SnapshotSource]] = []
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


def test_backlog_reassembles_newest_fragments_and_never_applies_older_whole(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fragments = [_parse_fragment(frame) for frame in _wrap_fragments(_large_v9_payload())]
    now = 2_000_000_000.0
    fragment_paths = [
        tmp_path / f"WoWScrnShot_fragment-{index}.jpg"
        for index in range(len(fragments))
    ]
    older_whole_path = tmp_path / "WoWScrnShot_older-whole.jpg"
    for index, path in enumerate(fragment_paths):
        path.write_bytes(b"x")
        os.utime(path, (now + index, now + index))
    older_whole_path.write_bytes(b"x")
    os.utime(older_whole_path, (now - 1, now - 1))
    older = Snapshot(listing=None, version=None)
    results = {
        path: screenshot_mod.DecodeResult(None, True, fragment=fragment)
        for path, fragment in zip(fragment_paths, fragments, strict=True)
    }
    results[older_whole_path] = screenshot_mod.DecodeResult(older, True)
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now + len(fragments))
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda path: results[path],
    )

    watcher._scan_recent_backlog()

    assert len(snapshots) == 1
    assert len(snapshots[0].applicants) == 24
    assert failures == []
    assert not any(path.exists() for path in fragment_paths)
    assert not older_whole_path.exists()


def test_backlog_corrupt_newest_marker_defers_failure_for_redundant_fragment_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fragments = [_parse_fragment(frame) for frame in _wrap_fragments(_large_v9_payload())]
    now = 2_000_000_000.0
    corrupt = tmp_path / "WoWScrnShot_newest-corrupt.jpg"
    fragment_paths = [
        tmp_path / f"WoWScrnShot_retry-{index}.jpg"
        for index in range(len(fragments))
    ]
    manual = tmp_path / "WoWScrnShot_manual.jpg"
    corrupt.write_bytes(b"corrupt")
    os.utime(corrupt, (now, now))
    for index, path in enumerate(reversed(fragment_paths), start=1):
        path.write_bytes(b"fragment")
        os.utime(path, (now - index, now - index))
    manual.write_bytes(b"manual")
    os.utime(manual, (now - len(fragment_paths) - 1, now - len(fragment_paths) - 1))
    results = {
        corrupt: screenshot_mod.DecodeResult(
            None,
            True,
            "CRC mismatch",
            fragment_candidate=True,
        ),
        manual: screenshot_mod.DecodeResult(None, False),
    }
    results.update(
        {
            path: screenshot_mod.DecodeResult(None, True, fragment=fragment)
            for path, fragment in zip(fragment_paths, fragments, strict=True)
        }
    )
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda path: results[path],
    )

    watcher._scan_recent_backlog()

    assert len(snapshots) == 1
    assert len(snapshots[0].applicants) == 24
    assert failures == []
    assert not corrupt.exists()
    assert not any(path.exists() for path in fragment_paths)
    assert manual.exists()


def test_backlog_incomplete_newest_generation_suppresses_older_whole_without_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fragment = _parse_fragment(_wrap_fragments(_large_v9_payload())[0])
    now = 2_000_000_000.0
    newest = tmp_path / "WoWScrnShot_newest-fragment.jpg"
    older = tmp_path / "WoWScrnShot_older-whole.jpg"
    oldest_corrupt = tmp_path / "WoWScrnShot_oldest-corrupt.jpg"
    newest.write_bytes(b"fragment")
    older.write_bytes(b"whole")
    oldest_corrupt.write_bytes(b"corrupt")
    os.utime(newest, (now, now))
    os.utime(older, (now - 1, now - 1))
    os.utime(oldest_corrupt, (now - 2, now - 2))
    results = {
        newest: screenshot_mod.DecodeResult(None, True, fragment=fragment),
        older: screenshot_mod.DecodeResult(
            Snapshot(listing=None, version=None),
            True,
        ),
        oldest_corrupt: screenshot_mod.DecodeResult(None, True, "CRC mismatch"),
    }
    watcher = ScreenshotWatcher(tmp_path)
    snapshots: list[Snapshot] = []
    failures: list[tuple[str, str]] = []
    watcher.snapshotReceived.connect(snapshots.append)
    watcher.decodeFailed.connect(lambda path, reason: failures.append((path, reason)))
    monkeypatch.setattr(screenshot_mod.time, "time", lambda: now)
    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(
        screenshot_mod,
        "_decode_screenshot_result",
        lambda path: results[path],
    )

    watcher._scan_recent_backlog()

    assert snapshots == []
    assert failures == []
    assert newest.exists()
    assert not older.exists()
    assert not oldest_corrupt.exists()


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


def test_backlog_persists_manual_fingerprint_across_watcher_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    screenshots = tmp_path / "Screenshots"
    cache_dir = tmp_path / "cache"
    screenshots.mkdir()
    cache_dir.mkdir()
    image_path = screenshots / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"manual")
    os.utime(image_path, (900.0, 900.0))
    calls: list[Path] = []

    def decode(path: Path) -> screenshot_mod.DecodeResult:
        calls.append(path)
        return screenshot_mod.DecodeResult(None, False)

    monkeypatch.setattr(screenshot_mod.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", decode)

    ScreenshotWatcher(screenshots, cache_dir=cache_dir)._scan_recent_backlog()
    assert calls == [image_path]

    monkeypatch.setattr(screenshot_mod, "_MANUAL_INDEX_REGISTRY", {})
    ScreenshotWatcher(screenshots, cache_dir=cache_dir)._scan_recent_backlog()

    assert calls == [image_path]


def test_backlog_does_not_persist_transient_scan_failure_as_manual(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    screenshots = tmp_path / "Screenshots"
    cache_dir = tmp_path / "cache"
    screenshots.mkdir()
    cache_dir.mkdir()
    image_path = screenshots / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"maybe-transport")
    os.utime(image_path, (900.0, 900.0))
    calls: list[Path] = []

    def decode(path: Path) -> screenshot_mod.DecodeResult:
        calls.append(path)
        if len(calls) == 1:
            return screenshot_mod.DecodeResult(None, False, "transient scan failure")
        return screenshot_mod.DecodeResult(None, False)

    monkeypatch.setattr(screenshot_mod.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", decode)

    ScreenshotWatcher(screenshots, cache_dir=cache_dir)._scan_recent_backlog()
    monkeypatch.setattr(screenshot_mod, "_MANUAL_INDEX_REGISTRY", {})
    ScreenshotWatcher(screenshots, cache_dir=cache_dir)._scan_recent_backlog()

    assert calls == [image_path, image_path]


def test_backlog_resumes_beyond_unknown_decode_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    screenshots = tmp_path / "Screenshots"
    cache_dir = tmp_path / "cache"
    screenshots.mkdir()
    cache_dir.mkdir()
    newer_manual = screenshots / "WoWScrnShot_0001.jpg"
    older_manual = screenshots / "WoWScrnShot_0002.jpg"
    oldest_transport = screenshots / "WoWScrnShot_0003.jpg"
    for path, content, mtime in (
        (newer_manual, b"manual-newer", 900.0),
        (older_manual, b"manual-older", 800.0),
        (oldest_transport, b"transport", 700.0),
    ):
        path.write_bytes(content)
        os.utime(path, (mtime, mtime))
    decoded: list[Path] = []

    def decode(path: Path) -> screenshot_mod.DecodeResult:
        decoded.append(path)
        return screenshot_mod.DecodeResult(None, path == oldest_transport)

    monkeypatch.setattr(screenshot_mod.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(screenshot_mod, "_BACKLOG_CLEANUP_LIMIT", 2)
    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", decode)

    ScreenshotWatcher(screenshots, cache_dir=cache_dir)._scan_recent_backlog()
    assert decoded == [newer_manual, older_manual]
    assert oldest_transport.exists()

    monkeypatch.setattr(screenshot_mod, "_MANUAL_INDEX_REGISTRY", {})
    ScreenshotWatcher(screenshots, cache_dir=cache_dir)._scan_recent_backlog()

    assert decoded == [newer_manual, older_manual, oldest_transport]
    assert not oldest_transport.exists()
    assert newer_manual.exists()
    assert older_manual.exists()


def test_observer_and_backlog_share_single_in_flight_decode_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"manual")
    watcher = ScreenshotWatcher(tmp_path)
    decode_entered = screenshot_mod.threading.Event()
    allow_decode = screenshot_mod.threading.Event()
    decoded: list[Path] = []

    def decode(path: Path) -> screenshot_mod.DecodeResult:
        decoded.append(path)
        decode_entered.set()
        assert allow_decode.wait(timeout=2)
        return screenshot_mod.DecodeResult(None, False)

    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", decode)
    observer_thread = screenshot_mod.threading.Thread(
        target=watcher._on_new_file,
        args=(image_path,),
    )
    observer_thread.start()
    assert decode_entered.wait(timeout=2)

    watcher._scan_recent_backlog()
    allow_decode.set()
    observer_thread.join(timeout=2)

    assert not observer_thread.is_alive()
    assert decoded == [image_path]
    assert image_path.exists()


def test_watcher_retries_changed_generation_without_deleting_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    image_path = tmp_path / "WoWScrnShot_0001.jpg"
    image_path.write_bytes(b"old-transport")
    watcher = ScreenshotWatcher(tmp_path)
    decoded_contents: list[bytes] = []
    failures: list[tuple[object, ...]] = []
    watcher.decodeFailed.connect(lambda *args: failures.append(args))

    def decode(path: Path) -> screenshot_mod.DecodeResult:
        decoded_contents.append(path.read_bytes())
        if len(decoded_contents) == 1:
            path.write_bytes(b"replacement-manual-screenshot")
            os.utime(path, ns=(2_000_000_001, 2_000_000_001))
            return screenshot_mod.DecodeResult(None, True, "old generation failed")
        return screenshot_mod.DecodeResult(None, False)

    monkeypatch.setattr(screenshot_mod, "_wait_for_stable_size", lambda _path: True)
    monkeypatch.setattr(screenshot_mod, "_decode_screenshot_result", decode)

    watcher._on_new_file(image_path)

    assert decoded_contents == [
        b"old-transport",
        b"replacement-manual-screenshot",
    ]
    assert failures == []
    assert image_path.read_bytes() == b"replacement-manual-screenshot"
