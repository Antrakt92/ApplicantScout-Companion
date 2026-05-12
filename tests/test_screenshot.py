"""Unit tests for screenshot.py wire-format parsers.

Covers v1 backward-compat + v2 multi-member group app support. The v2
addition is a 1-byte member_idx between applicant_id and class_id; bug
this fixes is "Warlock missing from companion when applied as part of a
2-person group" (live user report — see AUDIT.md T2-22).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import applicant_scout.screenshot as screenshot_mod
from applicant_scout.screenshot import (
    DecodedApplicant,
    MAGIC,
    ScreenshotWatcher,
    Snapshot,
    WIRE_VERSIONS_SUPPORTED,
    _Handler,
    _is_supported_screenshot_path,
    _iter_screenshot_candidates,
    _parse_payload,
    decode_screenshot,
)


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
    *,
    version: int,
) -> bytes:
    """Emit one applicant block matching addon's BuildPayload byte layout.

    version=1: legacy 13-byte fixed prefix (no member_idx byte).
    version=2: 14-byte fixed prefix (member_idx u8 between applicant_id +
    class_id).
    version=4: inserts main_score u16 after current score.
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


def _wrap_payload(body: bytes, *, wire_ver: int = 0x04) -> bytes:
    total_len = 9 + len(body) + 4
    framed = MAGIC + bytes([wire_ver]) + struct.pack(">H", total_len) + b"\0\0" + body
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
    assert 0x00 not in WIRE_VERSIONS_SUPPORTED  # canary


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
        "decode_screenshot",
        lambda _path: (Snapshot(listing=None, version=None), True),
    )

    watcher.stop()
    watcher._on_new_file(image_path)

    assert snapshots == []
    assert failures == []
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
        "decode_screenshot",
        lambda _path: (Snapshot(listing=None, version=None), True),
    )

    watcher.stop()
    watcher._scan_recent_backlog()

    assert snapshots == []
    assert image_path.exists()
