"""Screenshot-folder watcher + QR decoder for ApplicantScout transport.

Replaces the prior custom pixel-marker transport. Addon encodes binary payload
as a QR code via embedded lua-qrcode library, renders it in a frame anchored
TOPLEFT of UIParent, calls Screenshot() — image appears in _retail_/Screenshots/.
We watch that folder, decode QR via pyzbar (battle-tested zbar library —
handles dense QR versions reliably when module boundaries are pixel-aligned,
where opencv's QRCodeDetector empirically failed for large applicant payloads),
parse bytes through the binary format below, and emit a Snapshot via Qt signal.

Complete logical snapshots mirror `ApplicantScout.lua::BuildPayload`
byte-for-byte: header "APS1" + version + uint16 length + flags/reserved bytes,
then listing block + version block + applicant array + CRC32 trailer. Oversized
snapshots use bounded APS1 v10 fragment envelopes and are emitted only after
the original logical payload has been reassembled and validated. Pure binary,
big-endian, see addon for spec.
QR is purely a transport layer over those bytes — Reed-Solomon ECC built into
QR handles JPG quantization noise, partial occlusion, and rotation. Current
Addon builds use legacy-compatible hex text for normal payloads and bounded
hex fragment frames for oversized payloads.

CRITICAL: only files whose QR successfully decodes AND magic matches "APS1"
are deleted. User's manual screenshots (no QR / unrelated QR / wrong magic)
are preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import struct
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional, cast

from PIL import Image
from PyQt6.QtCore import QObject, pyqtSignal
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .atomic_io import atomic_write_text
from .producer_identity import is_placeholder_transport_identity


_log = logging.getLogger("applicant_scout.screenshot")
pyzbar_decode = None
ZBarSymbol = None


class QRDecoderUnavailable(RuntimeError):
    """Raised when the native zbar/pyzbar decoder cannot be imported."""


class QRScanFailed(RuntimeError):
    """Raised when an available decoder cannot complete this image scan."""


# ─── Wire format constants (must match addon's ApplicantScout.lua) ───────────
MAGIC = b"APS1"
# Allow-list of accepted wire versions. v0x01 = single member only (legacy);
# v0x02 = adds 1-byte member_idx between applicant_id and class_id, supports
# multi-member group apps (one block per member, all sharing applicant_id).
# v0x03 = adds listing category_id + difficulty_id.
# v0x04 = adds per-applicant RaiderIO main_score after current score.
# v0x05 = adds compact target-relative RaiderIO completion summary.
# v0x06 = adds current group roster.
# v0x07 = adds current group leader keystone context.
# v0x08 = adds terminal/LFG-unavailable partial flags.
# v0x09 = adds roster-unavailable partial flag for QR-overflow fallback.
# v0x0A = bounded transport fragment containing one slice of a complete logical
# payload. Fragments are assembled before any Snapshot reaches application code.
# v0x0B = v9 body plus an applicant-surface-unavailable flag. Producers retain
# v9 for ordinary frames so mixed-version rollout fails closed only on the new
# applicant-partial generations.
# Set, not a min/max range — future versions may be incompatible with v1 but compatible
# with v2; explicit allow-list is the cleanest contract.
WIRE_VERSIONS_SUPPORTED = {
    0x01,
    0x02,
    0x03,
    0x04,
    0x05,
    0x06,
    0x07,
    0x08,
    0x09,
    0x0A,
    0x0B,
}
APS1_FLAG_TERMINAL_CLEAR = 0x01
APS1_FLAG_LFG_UNAVAILABLE = 0x02
APS1_FLAG_ROSTER_UNAVAILABLE = 0x04
APS1_FLAG_APPLICANTS_UNAVAILABLE = 0x08
APS1_KNOWN_V8_FLAGS = APS1_FLAG_TERMINAL_CLEAR | APS1_FLAG_LFG_UNAVAILABLE
APS1_KNOWN_V9_FLAGS = APS1_KNOWN_V8_FLAGS | APS1_FLAG_ROSTER_UNAVAILABLE
APS1_KNOWN_V11_FLAGS = APS1_KNOWN_V9_FLAGS | APS1_FLAG_APPLICANTS_UNAVAILABLE
APS1_FRAGMENT_VERSION = 0x0A
APS1_FRAGMENT_CHUNK_BYTES = 640
APS1_FRAGMENT_MIN_CHUNKS = 2
APS1_FRAGMENT_MAX_CHUNKS = 128
APS1_FRAGMENT_METADATA_BYTES = 18
APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS = 300.0

STABLE_SIZE_TIMEOUT = 2.0  # seconds to wait for file size to stabilize
STABLE_SIZE_POLL = 0.05  # poll interval
SUPPORTED_SCREENSHOT_SUFFIXES = frozenset({".jpg", ".tga"})
QR_SCAN_CROP_PX = 720
SLOW_SCREENSHOT_STAGE_LOG_S = 0.75
_QR_RECOVERY_WHITE_THRESHOLD = 220
_QR_RECOVERY_MIN_SIDE_PX = 84
_QR_MIN_VERSION = 1
_QR_MAX_VERSION = 40
_QR_MODULES_BASE = 17
_QR_MODULES_PER_VERSION = 4
_QR_FINDER_MODULES = 7
_QR_TIMING_PATTERN_MARGIN = 8
_QR_STANDARD_QUIET_MODULES = 4
_QR_RECOVERY_RENDER_MODULE_PX = 4
_QR_RECOVERY_QUIET_ZONES = (2, 3, _QR_STANDARD_QUIET_MODULES)
_QR_RECOVERY_MAX_FINDER_ERRORS = 6
_QR_RECOVERY_MAX_TIMING_ERRORS = 4
_QR_RECOVERY_EDGE_NONWHITE_RUN = 2
_QR_RECOVERY_BLACK_THRESHOLD = 128
_QR_RECOVERY_MIN_MODULE_PX = 2.0
_QR_RECOVERY_MAX_MODULE_PX = 10.0

# Cap each startup-cleanup pass at the most-recent N unknown screenshots.
# Manual fingerprints persist, so later starts advance through older files without
# letting one pathological folder consume minutes of CPU beside WoW.
_BACKLOG_CLEANUP_LIMIT = 500
# Recent frames retain the larger envelope above because a fresh fragmented
# snapshot may need many files to reassemble. Historical files are cleanup-only:
# keep their native QR/JPG work to a small slice per launch so starting the
# companion beside WoW cannot spend tens of seconds decoding stale captures.
_BACKLOG_HISTORICAL_CLEANUP_LIMIT = 4
_BACKLOG_INCOMPLETE_SCAN_LIMIT = 4
_RECENT_WORK_KEY_TTL_SECONDS = 3.0
_GENERATION_RETRY_DELAY_SECONDS = 0.05
_INCOMPLETE_SCAN_RETRY_DELAY_SECONDS = 0.35
_TRANSIENT_SCAN_RETRY_DELAY_SECONDS = 0.15
_TRANSIENT_SCAN_MAX_RETRIES = 2
# Bump when no-marker classification changes so older fingerprints are
# reconsidered exactly once by the new decoder rather than hidden forever.
_MANUAL_INDEX_VERSION = 2
_MANUAL_INDEX_FILE_PREFIX = f"screenshot-manual-index-v{_MANUAL_INDEX_VERSION}"


# ─── Decoded data model ─────────────────────────────────────────────────────
@dataclass
class DecodedApplicant:
    applicant_id: int
    class_id: int  # 1-13 retail WoW classID, 0 if unknown
    spec_id: int
    ilvl: int
    score: int
    role: int  # 0=tank, 1=healer, 2=damager, 3=unknown
    name: str  # utf-8, "Charname-Realm"
    main_score: int = 0
    rio_profile: bool = False
    rio_best_key: int = 0
    rio_best_dungeon_key: int = 0
    rio_timed_at_or_above: int = 0
    rio_timed_at_or_above_minus1: int = 0
    rio_timed_at_or_above_minus2: int = 0
    rio_completed_at_or_above_minus1: int = 0
    rio_dungeon_count: int = 0
    rio_dungeons: list[dict] = field(default_factory=list)
    # 1-based, matches WoW API's GetApplicantMemberInfo(id, m). For wire v0x01
    # payloads (single-member-only) this defaults to 1 — back-compat keeps the
    # composite-id construction `f"{applicant_id}:{member_idx}"` valid for
    # legacy snapshots/screenshots without the addon needing the v2 emit path.
    member_idx: int = 1


@dataclass
class DecodedRosterMember:
    unit_index: int
    flags: int
    subgroup: int
    class_id: int
    spec_id: int
    ilvl: int
    score: int
    main_score: int
    rio_profile: bool = False
    rio_best_key: int = 0
    rio_best_dungeon_key: int = 0
    rio_timed_at_or_above: int = 0
    rio_timed_at_or_above_minus1: int = 0
    rio_timed_at_or_above_minus2: int = 0
    rio_completed_at_or_above_minus1: int = 0
    rio_dungeon_count: int = 0
    role: int = 3
    name: str = ""
    rio_dungeons: list[dict] = field(default_factory=list)

    @property
    def is_self(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def is_raid_member(self) -> bool:
        return bool(self.flags & 0x02)


@dataclass
class DecodedListing:
    activity_id: int
    key_level: int
    dungeon_name: str
    listing_name: str
    comment: str
    category_id: int = 0
    difficulty_id: int = 0


@dataclass
class DecodedLeaderKey:
    key_level: int
    challenge_map_id: int = 0
    player_name: str = ""


@dataclass
class DecodedVersion:
    addon_version: str
    game_version: str
    region_id: int  # 1=NA 2=KR 3=EU 4=TW 5=CN
    player_name: str  # "Charname-Realm"


@dataclass(frozen=True)
class SnapshotSource:
    mtime_ns: int
    file_id: str
    size: int


def _unlink_if_source_matches(
    path: Path,
    expected: SnapshotSource | None,
) -> bool:
    """Delete only the file generation that was inspected by the caller."""
    try:
        if expected is not None:
            current = path.stat()
            if (
                current.st_mtime_ns != expected.mtime_ns
                or current.st_size != expected.size
            ):
                return False
        path.unlink()
    except FileNotFoundError:
        return True
    return True


@dataclass
class Snapshot:
    """Result of decoding one screenshot."""

    listing: Optional[DecodedListing]
    version: Optional[DecodedVersion]
    leader_key: Optional[DecodedLeaderKey] = None
    applicants: list[DecodedApplicant] = field(default_factory=list)
    roster: list[DecodedRosterMember] = field(default_factory=list)
    terminal_clear: bool = False
    lfg_unavailable: bool = False
    roster_unavailable: bool = False
    applicants_unavailable: bool = False
    source: SnapshotSource | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class SnapshotFragment:
    """One bounded v10 slice of a complete logical APS1 snapshot."""

    stream_id: int
    generation: int
    chunk_index: int
    chunk_count: int
    inner_total_len: int
    inner_crc32: int
    chunk: bytes = field(repr=False)
    source: SnapshotSource | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class DecodeResult:
    snapshot: Optional[Snapshot]
    has_marker: bool
    error_reason: Optional[str] = None
    decoder_unavailable: bool = False
    fragment: Optional[SnapshotFragment] = None
    fragment_candidate: bool = False
    transport_suspected: bool = False
    scan_incomplete: bool = False


@dataclass
class _QRScanDiagnostics:
    transport_suspected: bool = False


@dataclass(frozen=True)
class ScreenshotCleanupSummary:
    scanned: int
    markers_found: int
    deleted: int
    preserved: int
    unstable: int
    scan_errors: int
    decode_errors: int
    delete_failed: int
    limited: bool


# ─── QR detection + payload extraction ──────────────────────────────────────
def _decode_qr_symbols(img: Image.Image) -> list[bytes]:
    try:
        global pyzbar_decode
        global ZBarSymbol
        # WHY: pyzbar loads the native zbar wrapper and can cost about a second
        # on Windows. Keep it off the companion startup path; screenshot decode
        # runs from the watcher/backlog worker after the UI can paint.
        decoder = pyzbar_decode
        symbol_type = ZBarSymbol
        if decoder is None:
            try:
                from pyzbar.pyzbar import ZBarSymbol as imported_symbol_type
                from pyzbar.pyzbar import decode as imported_decoder
            except Exception as exc:  # noqa: BLE001
                raise QRDecoderUnavailable(f"QR decoder unavailable: {exc}") from exc

            decoder = imported_decoder
            symbol_type = imported_symbol_type
            pyzbar_decode = imported_decoder
            ZBarSymbol = imported_symbol_type

        symbols = [symbol_type.QRCODE] if symbol_type is not None else None
        results = decoder(img, symbols=symbols)
    except QRDecoderUnavailable:
        raise
    except Exception as e:
        _log.debug("pyzbar error: %s", e)
        raise QRScanFailed(f"QR scan failed: {e}") from e
    return [bytes(r.data) for r in results]


def _has_appscout_symbol(payloads: list[bytes]) -> bool:
    return bool(_collect_appscout_qr_candidates(payloads))


def _white_edge_extent(img: Image.Image, *, horizontal: bool) -> int | None:
    """Measure a white square flush with the image's top-left corner."""
    major = img.width if horizontal else img.height
    cross = min(2, img.height if horizontal else img.width)
    if major < _QR_RECOVERY_MIN_SIDE_PX or cross <= 0:
        return None

    def edge_is_white(index: int) -> bool:
        for offset in range(cross):
            point = (index, offset) if horizontal else (offset, index)
            if cast(int, img.getpixel(point)) < _QR_RECOVERY_WHITE_THRESHOLD:
                return False
        return True

    if not edge_is_white(0):
        return None
    nonwhite_run = 0
    for index in range(1, major):
        if edge_is_white(index):
            nonwhite_run = 0
            continue
        nonwhite_run += 1
        if nonwhite_run >= _QR_RECOVERY_EDGE_NONWHITE_RUN:
            return index - 1
    return major


def _finder_pattern_errors(
    pixels: Any,
    *,
    row: int,
    column: int,
) -> int:
    errors = 0
    last = _QR_FINDER_MODULES - 1
    for y in range(_QR_FINDER_MODULES):
        for x in range(_QR_FINDER_MODULES):
            expected_dark = (
                y in (0, last) or x in (0, last) or (2 <= y <= 4 and 2 <= x <= 4)
            )
            actual_dark = (
                cast(int, pixels[column + x, row + y]) < _QR_RECOVERY_BLACK_THRESHOLD
            )
            errors += actual_dark != expected_dark
    return errors


def _timing_pattern_errors(pixels: Any, modules: int) -> int:
    errors = 0
    timing_index = _QR_FINDER_MODULES - 1
    for index in range(
        _QR_TIMING_PATTERN_MARGIN,
        modules - _QR_TIMING_PATTERN_MARGIN,
    ):
        expected_dark = index % 2 == 0
        errors += (
            cast(int, pixels[index, timing_index]) < _QR_RECOVERY_BLACK_THRESHOLD
        ) != expected_dark
        errors += (
            cast(int, pixels[timing_index, index]) < _QR_RECOVERY_BLACK_THRESHOLD
        ) != expected_dark
    return errors


def _sample_qr_recovery_candidate(
    square: Image.Image,
    *,
    side: int,
    version: int,
    quiet: int,
) -> tuple[tuple[int, int, int], Image.Image] | None:
    modules = _QR_MODULES_BASE + _QR_MODULES_PER_VERSION * version
    total = modules + 2 * quiet
    module_px = side / total
    if not _QR_RECOVERY_MIN_MODULE_PX <= module_px <= _QR_RECOVERY_MAX_MODULE_PX:
        return None

    with square.resize(
        (total, total),
        resample=Image.Resampling.NEAREST,
    ) as sampled_total:
        sampled = sampled_total.crop((quiet, quiet, quiet + modules, quiet + modules))

    accepted = False
    try:
        pixels = sampled.load()
        assert pixels is not None
        finder_errors = _finder_pattern_errors(pixels, row=0, column=0)
        if finder_errors > _QR_RECOVERY_MAX_FINDER_ERRORS:
            return None
        finder_errors += _finder_pattern_errors(
            pixels,
            row=0,
            column=modules - _QR_FINDER_MODULES,
        )
        if finder_errors > _QR_RECOVERY_MAX_FINDER_ERRORS:
            return None
        finder_errors += _finder_pattern_errors(
            pixels,
            row=modules - _QR_FINDER_MODULES,
            column=0,
        )
        if finder_errors > _QR_RECOVERY_MAX_FINDER_ERRORS:
            return None
        timing_errors = _timing_pattern_errors(pixels, modules)
        if timing_errors > _QR_RECOVERY_MAX_TIMING_ERRORS:
            return None
        accepted = True
        return (finder_errors + timing_errors, version, quiet), sampled
    finally:
        if not accepted:
            sampled.close()


def _render_normalized_qr(sampled: Image.Image) -> Image.Image:
    modules = sampled.width
    render_px = _QR_RECOVERY_RENDER_MODULE_PX
    quiet_px = _QR_STANDARD_QUIET_MODULES * render_px
    total_modules = modules + 2 * _QR_STANDARD_QUIET_MODULES
    normalized = Image.new(
        "L",
        (total_modules * render_px, total_modules * render_px),
        255,
    )
    with sampled.resize(
        (modules * render_px, modules * render_px),
        resample=Image.Resampling.NEAREST,
    ) as rendered_modules:
        normalized.paste(rendered_modules, (quiet_px, quiet_px))
    return normalized


def _normalized_top_left_qr(img: Image.Image) -> Image.Image | None:
    """Recover a QR whose fractional UI scale blurred module boundaries.

    ApplicantScout's historical renderer placed the QR flush with TOPLEFT and
    used UI units as if they were physical pixels. At fractional effective
    scales that produces alternating module widths which zbar may reject even
    though module centres remain intact. Recognise the three finder patterns
    plus both timing rows, then rebuild an integer-pixel QR for one final zbar
    pass. This is only candidate recovery; APS1 parsing and CRC remain the
    ownership boundary used by callers.
    """
    gray = img.convert("L")
    best: tuple[tuple[int, int, int], Image.Image] | None = None
    try:
        width = _white_edge_extent(gray, horizontal=True)
        height = _white_edge_extent(gray, horizontal=False)
        if width is None or height is None:
            return None
        side = min(width, height)
        if side < _QR_RECOVERY_MIN_SIDE_PX or abs(width - height) > max(2, side // 100):
            return None

        with gray.crop((0, 0, side, side)) as square:
            for version in range(_QR_MIN_VERSION, _QR_MAX_VERSION + 1):
                for quiet in _QR_RECOVERY_QUIET_ZONES:
                    candidate = _sample_qr_recovery_candidate(
                        square,
                        side=side,
                        version=version,
                        quiet=quiet,
                    )
                    if candidate is None:
                        continue
                    if best is None or candidate[0] < best[0]:
                        if best is not None:
                            best[1].close()
                        best = candidate
                    else:
                        candidate[1].close()

        if best is None:
            return None
        (_score, version, source_quiet), sampled = best
        normalized = _render_normalized_qr(sampled)
        _log.debug(
            "normalised top-left QR candidate: version=%d source_quiet=%d side=%d",
            version,
            source_quiet,
            side,
        )
        return normalized
    finally:
        if best is not None:
            best[1].close()
        gray.close()


def _recover_top_left_qr_symbols(img: Image.Image) -> tuple[list[bytes], bool]:
    normalized = _normalized_top_left_qr(img)
    if normalized is None:
        return [], False
    with normalized:
        payloads = _decode_qr_symbols(normalized)
    # Recovery is heuristic candidate discovery. Do not let geometry plus the
    # four-byte magic broaden the deletion boundary: only return a symbol after
    # the complete APS1 parser (including CRC and fragment validation) accepts
    # it. Normal zbar scans retain their historical marker-failure behaviour.
    accepted: list[bytes] = []
    appscout_candidate_found = False
    for symbol_payload in payloads:
        candidates = _collect_appscout_qr_candidates([symbol_payload])
        appscout_candidate_found = appscout_candidate_found or bool(candidates)
        for _kind, raw in candidates:
            try:
                parsed, _error = _try_parse_appscout_candidate(raw)
            except Exception:  # noqa: BLE001 - preserve uncertain screenshots
                parsed = None
            if isinstance(parsed, (Snapshot, SnapshotFragment)):
                accepted.append(symbol_payload)
                break
    return accepted, appscout_candidate_found


def _recover_top_left_qr_symbols_with_diagnostics(
    img: Image.Image,
    diagnostics: _QRScanDiagnostics | None,
) -> list[bytes]:
    recovered_payloads, suspected = _recover_top_left_qr_symbols(img)
    if diagnostics is not None and suspected:
        diagnostics.transport_suspected = True
    return recovered_payloads


def _iter_qr_symbol_data_batches(
    image_path: Path,
    diagnostics: _QRScanDiagnostics | None = None,
) -> Iterator[list[bytes]]:
    """Yield raw pyzbar symbol batches from one screenshot image.

    pyzbar exposes zbar's payload pointer together with an explicit data length,
    so embedded NUL bytes survive intact. That lets us support both transport
    variants the addon may emit:
      * legacy hex text QR (decode via `bytes.fromhex(...)`)
      * raw APS1 byte-mode QR fallback for oversized payloads

    The top-left crop is yielded first for normal transport performance. If the
    crop contains an ApplicantScout marker but its payload later fails parsing,
    callers can continue to the full-image batch and recover from a stale/corrupt
    QR in the crop while a valid moved/debug QR exists elsewhere in the screenshot.
    """
    # Context-managed Image.open: PIL keeps a lazy file handle until pixel
    # access. If pyzbar_decode raises before the bitmap is materialised
    # (corrupt image, library bug), bare `Image.open(...)` leaks the handle —
    # subsequent `path.unlink()` in `_on_new_file` then PermissionErrors on
    # Windows (file in use). With-block guarantees release on every exit path.
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            crop_width = min(QR_SCAN_CROP_PX, width)
            crop_height = min(QR_SCAN_CROP_PX, height)
            if crop_width < width or crop_height < height:
                # WHY: ApplicantScout keeps the transport QR at TOPLEFT during
                # normal sessions. Scanning a 720px crop avoids a full-screen
                # zbar pass on 1440p/4K screenshots; fallback preserves manual
                # /apscout qrmove positions and future non-default layouts.
                with img.crop((0, 0, crop_width, crop_height)) as cropped:
                    payloads = _decode_qr_symbols(cropped)
                if _has_appscout_symbol(payloads):
                    yield payloads
                    full_payloads = _decode_qr_symbols(img)
                    if full_payloads:
                        yield full_payloads
                    return
                recovered_payloads = _recover_top_left_qr_symbols_with_diagnostics(
                    img,
                    diagnostics,
                )
                if recovered_payloads:
                    yield recovered_payloads
                    return
                full_payloads = _decode_qr_symbols(img)
                if full_payloads:
                    yield full_payloads
                return
            payloads = _decode_qr_symbols(img)
            if payloads:
                yield payloads
            if not _has_appscout_symbol(payloads):
                recovered_payloads = _recover_top_left_qr_symbols_with_diagnostics(
                    img,
                    diagnostics,
                )
                if recovered_payloads:
                    yield recovered_payloads
    except (OSError, IOError) as e:
        _log.debug("Image.open failed %s: %s", image_path.name, e)
        raise QRScanFailed(f"could not read screenshot image: {e}") from e


def _decode_legacy_hex_qr(data: bytes) -> Optional[bytes]:
    try:
        decoded = bytes.fromhex(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    return decoded if decoded.startswith(MAGIC) else None


def _collect_appscout_qr_candidates(
    symbol_payloads: list[bytes],
) -> list[tuple[str, bytes]]:
    """Return ordered ApplicantScout payload candidates from one QR scan.

    WHY preserve this order: legacy companions only understand hex, so the
    addon keeps hex as its normal path and uses raw byte-mode only for
    oversize-overflow escape. New companions should still prefer raw APS1
    payloads when both appear in one image, but they must keep legacy hex
    support for backlog screenshots and mixed-version rollouts.
    """
    raw_candidates: list[tuple[str, bytes]] = []
    hex_candidates: list[tuple[str, bytes]] = []
    for data in symbol_payloads:
        if data.startswith(MAGIC):
            raw_candidates.append(("raw", data))
            continue
        decoded = _decode_legacy_hex_qr(data)
        if decoded is not None:
            hex_candidates.append(("hex", decoded))
    return raw_candidates + hex_candidates


def _try_parse_appscout_candidate(
    raw: bytes,
) -> tuple[Snapshot | SnapshotFragment | None, Optional[str]]:
    """Validate and parse one already-identified APS1 payload candidate."""
    if len(raw) < 9:
        return None, "payload shorter than 9-byte header"

    wire_ver = raw[4]
    if wire_ver not in WIRE_VERSIONS_SUPPORTED:
        return None, f"unsupported wire version 0x{wire_ver:02x}"
    flags = raw[7]
    reserved2 = raw[8]
    if wire_ver == APS1_FRAGMENT_VERSION:
        if flags or reserved2:
            return (
                None,
                f"unsupported APS1 v10 reserved bytes 0x{flags:02x} 0x{reserved2:02x}",
            )
    elif wire_ver >= 0x08:
        known_flags = (
            APS1_KNOWN_V11_FLAGS
            if wire_ver == 0x0B
            else APS1_KNOWN_V9_FLAGS
            if wire_ver >= 0x09
            else APS1_KNOWN_V8_FLAGS
        )
        unknown_flags = flags & ~known_flags
        if unknown_flags:
            return None, f"unsupported APS1 v{wire_ver} flags 0x{unknown_flags:02x}"
        if flags & APS1_FLAG_TERMINAL_CLEAR and flags & APS1_FLAG_LFG_UNAVAILABLE:
            return None, "terminal and LFG-unavailable flags are mutually exclusive"
        if flags & APS1_FLAG_TERMINAL_CLEAR and flags & (
            APS1_FLAG_ROSTER_UNAVAILABLE | APS1_FLAG_APPLICANTS_UNAVAILABLE
        ):
            return (
                None,
                "terminal and partial-unavailable flags are mutually exclusive",
            )
        if reserved2:
            return None, f"unsupported APS1 v{wire_ver} reserved byte 0x{reserved2:02x}"
    elif flags or reserved2:
        return (
            None,
            f"unsupported APS1 pre-v8 reserved bytes 0x{flags:02x} 0x{reserved2:02x}",
        )

    total_len = struct.unpack(">H", raw[5:7])[0]
    # Sanity: 13 = minimum valid body (9 header + 1 has_listing=0 + 1
    # has_version=0 + 2 applicant_count=0 + 4 CRC trailer).
    if total_len < 13 or total_len > len(raw):
        return None, f"invalid total_len {total_len} for {len(raw)} decoded bytes"
    if total_len != len(raw):
        return (
            None,
            f"trailing decoded bytes: total_len {total_len} for {len(raw)} decoded bytes",
        )

    payload = raw[:total_len]
    body = payload[:-4]
    expected_crc = struct.unpack(">I", payload[-4:])[0]
    actual_crc = zlib.crc32(body) & 0xFFFFFFFF
    if expected_crc != actual_crc:
        return (
            None,
            f"CRC mismatch expected {expected_crc:08x} actual {actual_crc:08x}",
        )

    if wire_ver == APS1_FRAGMENT_VERSION:
        fragment_body = body[9:]
        if len(fragment_body) < APS1_FRAGMENT_METADATA_BYTES + 1:
            return None, "v10 fragment body is shorter than metadata plus one byte"
        (
            stream_id,
            generation,
            chunk_index,
            chunk_count,
            inner_total_len,
            inner_crc32,
        ) = struct.unpack(">IIHHHI", fragment_body[:APS1_FRAGMENT_METADATA_BYTES])
        chunk = fragment_body[APS1_FRAGMENT_METADATA_BYTES:]
        if not APS1_FRAGMENT_MIN_CHUNKS <= chunk_count <= APS1_FRAGMENT_MAX_CHUNKS:
            return (
                None,
                f"v10 chunk_count {chunk_count} outside "
                f"{APS1_FRAGMENT_MIN_CHUNKS}..{APS1_FRAGMENT_MAX_CHUNKS}",
            )
        if chunk_index >= chunk_count:
            return (
                None,
                f"v10 chunk_index {chunk_index} outside chunk_count {chunk_count}",
            )
        if not 13 <= inner_total_len <= 0xFFFF:
            return None, f"v10 invalid inner_total_len {inner_total_len}"
        expected_count = (
            inner_total_len + APS1_FRAGMENT_CHUNK_BYTES - 1
        ) // APS1_FRAGMENT_CHUNK_BYTES
        if chunk_count != expected_count:
            return (
                None,
                f"v10 chunk_count {chunk_count} does not match inner_total_len "
                f"{inner_total_len} ({expected_count} expected)",
            )
        expected_chunk_len = APS1_FRAGMENT_CHUNK_BYTES
        if chunk_index == chunk_count - 1:
            expected_chunk_len = inner_total_len - (
                APS1_FRAGMENT_CHUNK_BYTES * (chunk_count - 1)
            )
        if len(chunk) != expected_chunk_len:
            return (
                None,
                f"v10 chunk {chunk_index}/{chunk_count} has {len(chunk)} bytes; "
                f"expected {expected_chunk_len}",
            )
        return (
            SnapshotFragment(
                stream_id=stream_id,
                generation=generation,
                chunk_index=chunk_index,
                chunk_count=chunk_count,
                inner_total_len=inner_total_len,
                inner_crc32=inner_crc32,
                chunk=chunk,
            ),
            None,
        )

    try:
        snap = _parse_payload(
            body[9:],
            wire_ver,
            terminal_clear=bool(flags & APS1_FLAG_TERMINAL_CLEAR),
            lfg_unavailable=bool(flags & APS1_FLAG_LFG_UNAVAILABLE),
            roster_unavailable=bool(flags & APS1_FLAG_ROSTER_UNAVAILABLE),
            applicants_unavailable=bool(flags & APS1_FLAG_APPLICANTS_UNAVAILABLE),
        )  # skip 9-byte header
        snap = validate_snapshot_for_application(snap)
    except (IndexError, UnicodeDecodeError, struct.error, ValueError) as e:
        return None, f"parse error: {e}"
    return snap, None


def _try_parse_appscout_payload(raw: bytes) -> tuple[Optional[Snapshot], Optional[str]]:
    """Parse one complete logical snapshot while preserving the legacy API."""
    parsed, error = _try_parse_appscout_candidate(raw)
    if isinstance(parsed, SnapshotFragment):
        return None, "v10 fragment requires watcher assembly"
    return parsed, error


def validate_snapshot_for_application(snap: Snapshot) -> Snapshot:
    _validate_snapshot_applicant_shapes(snap)
    snap = _without_placeholder_transport_identities(snap)
    _validate_snapshot_unique_identities(snap)
    return snap


def _without_placeholder_transport_identities(snap: Snapshot) -> Snapshot:
    applicants = [
        applicant
        for applicant in snap.applicants
        if not is_placeholder_transport_identity(applicant.name)
    ]
    roster = [
        member
        for member in snap.roster
        if not is_placeholder_transport_identity(member.name)
    ]
    applicants_unavailable = snap.applicants_unavailable or len(applicants) != len(
        snap.applicants
    )
    roster_unavailable = snap.roster_unavailable or len(roster) != len(snap.roster)
    normalized_applicants = [] if applicants_unavailable else applicants
    normalized_roster = [] if roster_unavailable else roster
    if (
        normalized_applicants == snap.applicants
        and normalized_roster == snap.roster
        and applicants_unavailable == snap.applicants_unavailable
        and roster_unavailable == snap.roster_unavailable
    ):
        return snap
    return replace(
        snap,
        applicants=normalized_applicants,
        roster=normalized_roster,
        applicants_unavailable=applicants_unavailable,
        roster_unavailable=roster_unavailable,
    )


def _validate_snapshot_applicant_shapes(snap: Snapshot) -> None:
    for applicant in snap.applicants:
        if not 1 <= applicant.member_idx <= 5:
            raise ValueError(
                f"invalid applicant member_idx {applicant.applicant_id}:"
                f"{applicant.member_idx}"
            )
        if not applicant.name.strip():
            raise ValueError(
                f"blank applicant identity {applicant.applicant_id}:"
                f"{applicant.member_idx}"
            )


def _validate_snapshot_unique_identities(snap: Snapshot) -> None:
    seen_applicants: set[tuple[int, int]] = set()
    for applicant in snap.applicants:
        identity = (applicant.applicant_id, applicant.member_idx)
        if identity in seen_applicants:
            raise ValueError(
                f"duplicate applicant identity {applicant.applicant_id}:"
                f"{applicant.member_idx}"
            )
        seen_applicants.add(identity)

    seen_roster: set[str] = set()
    for member in snap.roster:
        identity = member.name.strip().lower()
        if not identity:
            raise ValueError("blank roster identity")
        if identity in seen_roster:
            raise ValueError(f"duplicate roster identity {identity}")
        seen_roster.add(identity)


def _read_len_str(
    buf: bytes,
    cursor: int,
    *,
    encoding: str,
    field: str,
) -> tuple[str, int]:
    if cursor >= len(buf):
        raise ValueError(f"{field} length byte missing")
    length = buf[cursor]
    cursor += 1
    end = cursor + length
    if end > len(buf):
        raise ValueError(f"{field} length {length} exceeds remaining payload bytes")
    raw = buf[cursor:end]
    try:
        return raw.decode(encoding), end
    except UnicodeDecodeError as exc:
        raise ValueError(f"{field} contains invalid {encoding}") from exc


def _read_wire_bool(buf: bytes, cursor: int, *, field: str) -> tuple[bool, int]:
    value = buf[cursor]
    cursor += 1
    if value not in (0, 1):
        raise ValueError(f"{field} must be 0 or 1, got {value}")
    return value == 1, cursor


def _read_wire_role(buf: bytes, cursor: int, *, field: str) -> tuple[int, int]:
    value = buf[cursor]
    cursor += 1
    if value not in (0, 1, 2, 3):
        raise ValueError(f"{field} must be one of 0, 1, 2, 3, got {value}")
    return value, cursor


def _minimum_applicant_record_size(wire_ver: int) -> int:
    # Includes the one-byte length prefix for an empty name. The later
    # application validator rejects blank identities; this is only a structural
    # bound that prevents impossible loop counts before cursor parsing.
    size = 4 + 1 + 2 + 2 + 2 + 1 + 1
    if wire_ver >= 0x02:
        size += 1
    if wire_ver >= 0x04:
        size += 2
    if wire_ver >= 0x05:
        size += 8
    return size


def _parse_payload(
    buf: bytes,
    wire_ver: int = 0x01,
    *,
    terminal_clear: bool = False,
    lfg_unavailable: bool = False,
    roster_unavailable: bool = False,
    applicants_unavailable: bool = False,
) -> Snapshot:
    """Cursor-based parse of body (already past 9-byte header). Returns Snapshot.
    Raises IndexError if buf truncated (caught by caller as decode failure).

    wire_ver gates block layout:
      * v0x01: legacy single-member applicants.
      * v0x02: adds applicant member_idx.
      * v0x03: adds listing category_id + difficulty_id.
      * v0x04: adds applicant main_score after current score.
      * v0x05: adds compact RaiderIO completion summary after main_score.
      * v0x06: adds current party/raid roster after applicants.
      * v0x07: adds optional leader keystone context after version block.
      * v0x08: adds header flags for terminal clear and partial LFG snapshots.
      * v0x09: adds a header flag for snapshots that omitted the roster block.
      * v0x0B: adds an applicant-surface flag while retaining the v9 body.
    """
    cursor = 0
    listing: Optional[DecodedListing] = None
    version: Optional[DecodedVersion] = None
    leader_key: Optional[DecodedLeaderKey] = None
    applicants: list[DecodedApplicant] = []

    # Listing block
    has_listing, cursor = _read_wire_bool(buf, cursor, field="has_listing")
    if has_listing:
        activity_id = struct.unpack(">I", buf[cursor : cursor + 4])[0]
        cursor += 4
        category_id = 0
        difficulty_id = 0
        if wire_ver >= 0x03:
            category_id = struct.unpack(">H", buf[cursor : cursor + 2])[0]
            cursor += 2
            difficulty_id = struct.unpack(">H", buf[cursor : cursor + 2])[0]
            cursor += 2
        key_level = buf[cursor]
        cursor += 1
        dungeon_name, cursor = _read_len_str(
            buf, cursor, encoding="utf-8", field="listing.dungeon_name"
        )
        listing_name, cursor = _read_len_str(
            buf, cursor, encoding="utf-8", field="listing.listing_name"
        )
        comment, cursor = _read_len_str(
            buf, cursor, encoding="utf-8", field="listing.comment"
        )
        listing = DecodedListing(
            activity_id=activity_id,
            key_level=key_level,
            dungeon_name=dungeon_name,
            listing_name=listing_name,
            comment=comment,
            category_id=category_id,
            difficulty_id=difficulty_id,
        )

    # Version block
    has_version, cursor = _read_wire_bool(buf, cursor, field="has_version")
    if has_version:
        addon_version, cursor = _read_len_str(
            buf, cursor, encoding="ascii", field="version.addon_version"
        )
        game_version, cursor = _read_len_str(
            buf, cursor, encoding="ascii", field="version.game_version"
        )
        region_id = buf[cursor]
        cursor += 1
        player_name, cursor = _read_len_str(
            buf, cursor, encoding="utf-8", field="version.player_name"
        )
        version = DecodedVersion(
            addon_version=addon_version,
            game_version=game_version,
            region_id=region_id,
            player_name=player_name,
        )

    if wire_ver >= 0x07:
        has_leader_key, cursor = _read_wire_bool(
            buf,
            cursor,
            field="has_leader_key",
        )
        if has_leader_key:
            key_level = buf[cursor]
            cursor += 1
            challenge_map_id = struct.unpack(">H", buf[cursor : cursor + 2])[0]
            cursor += 2
            player_name, cursor = _read_len_str(
                buf, cursor, encoding="utf-8", field="leader_key.player_name"
            )
            leader_key = DecodedLeaderKey(
                key_level=key_level,
                challenge_map_id=challenge_map_id,
                player_name=player_name,
            )

    # Applicants array. Bound the count by what can structurally fit in this
    # uint16-sized payload rather than a stale product assumption. Grouped
    # applications can legitimately exceed 200 member rows.
    count = struct.unpack(">H", buf[cursor : cursor + 2])[0]
    cursor += 2
    minimum_tail = 2 if wire_ver >= 0x06 else 0
    remaining_for_applicants = len(buf) - cursor - minimum_tail
    max_structural_count = max(
        0,
        remaining_for_applicants // _minimum_applicant_record_size(wire_ver),
    )
    if count > max_structural_count:
        raise ValueError(
            f"applicant_count {count} cannot fit in {remaining_for_applicants} bytes"
        )
    for _ in range(count):
        aid = struct.unpack(">I", buf[cursor : cursor + 4])[0]
        cursor += 4
        # v0x02 inserts a 1-byte member_idx between applicant_id and class_id.
        # v0x01 has no such byte — implicit member_idx=1 (the leader).
        if wire_ver >= 0x02:
            member_idx = buf[cursor]
            cursor += 1
        else:
            member_idx = 1
        class_id = buf[cursor]
        cursor += 1
        spec_id = struct.unpack(">H", buf[cursor : cursor + 2])[0]
        cursor += 2
        ilvl = struct.unpack(">H", buf[cursor : cursor + 2])[0]
        cursor += 2
        score = struct.unpack(">H", buf[cursor : cursor + 2])[0]
        cursor += 2
        if wire_ver >= 0x04:
            main_score = struct.unpack(">H", buf[cursor : cursor + 2])[0]
            cursor += 2
        else:
            main_score = 0
        if wire_ver >= 0x05:
            rio_profile, cursor = _read_wire_bool(
                buf,
                cursor,
                field="applicant.rio_profile",
            )
            rio_best_key = buf[cursor]
            cursor += 1
            rio_best_dungeon_key = buf[cursor]
            cursor += 1
            rio_timed_at_or_above = buf[cursor]
            cursor += 1
            rio_timed_at_or_above_minus1 = buf[cursor]
            cursor += 1
            rio_timed_at_or_above_minus2 = buf[cursor]
            cursor += 1
            rio_completed_at_or_above_minus1 = buf[cursor]
            cursor += 1
            rio_dungeon_count = buf[cursor]
            cursor += 1
        else:
            rio_profile = False
            rio_best_key = 0
            rio_best_dungeon_key = 0
            rio_timed_at_or_above = 0
            rio_timed_at_or_above_minus1 = 0
            rio_timed_at_or_above_minus2 = 0
            rio_completed_at_or_above_minus1 = 0
            rio_dungeon_count = 0
        role, cursor = _read_wire_role(buf, cursor, field="applicant.role")
        name, cursor = _read_len_str(
            buf, cursor, encoding="utf-8", field="applicant.name"
        )
        applicants.append(
            DecodedApplicant(
                applicant_id=aid,
                class_id=class_id,
                spec_id=spec_id,
                ilvl=ilvl,
                score=score,
                role=role,
                name=name,
                main_score=main_score,
                rio_profile=rio_profile,
                rio_best_key=rio_best_key,
                rio_best_dungeon_key=rio_best_dungeon_key,
                rio_timed_at_or_above=rio_timed_at_or_above,
                rio_timed_at_or_above_minus1=rio_timed_at_or_above_minus1,
                rio_timed_at_or_above_minus2=rio_timed_at_or_above_minus2,
                rio_completed_at_or_above_minus1=rio_completed_at_or_above_minus1,
                rio_dungeon_count=rio_dungeon_count,
                rio_dungeons=[],
                member_idx=member_idx,
            )
        )

    roster: list[DecodedRosterMember] = []
    if wire_ver >= 0x06:
        roster_count = struct.unpack(">H", buf[cursor : cursor + 2])[0]
        cursor += 2
        if roster_count > 40:
            raise ValueError(f"roster_count {roster_count} exceeds sane limit 40")
        for _ in range(roster_count):
            unit_index = buf[cursor]
            cursor += 1
            flags = buf[cursor]
            cursor += 1
            subgroup = buf[cursor]
            cursor += 1
            class_id = buf[cursor]
            cursor += 1
            spec_id = struct.unpack(">H", buf[cursor : cursor + 2])[0]
            cursor += 2
            ilvl = struct.unpack(">H", buf[cursor : cursor + 2])[0]
            cursor += 2
            score = struct.unpack(">H", buf[cursor : cursor + 2])[0]
            cursor += 2
            main_score = struct.unpack(">H", buf[cursor : cursor + 2])[0]
            cursor += 2
            rio_profile, cursor = _read_wire_bool(
                buf,
                cursor,
                field="roster.rio_profile",
            )
            rio_best_key = buf[cursor]
            cursor += 1
            rio_best_dungeon_key = buf[cursor]
            cursor += 1
            rio_timed_at_or_above = buf[cursor]
            cursor += 1
            rio_timed_at_or_above_minus1 = buf[cursor]
            cursor += 1
            rio_timed_at_or_above_minus2 = buf[cursor]
            cursor += 1
            rio_completed_at_or_above_minus1 = buf[cursor]
            cursor += 1
            rio_dungeon_count = buf[cursor]
            cursor += 1
            role, cursor = _read_wire_role(buf, cursor, field="roster.role")
            name, cursor = _read_len_str(
                buf, cursor, encoding="utf-8", field="roster.name"
            )
            roster.append(
                DecodedRosterMember(
                    unit_index=unit_index,
                    flags=flags,
                    subgroup=subgroup,
                    class_id=class_id,
                    spec_id=spec_id,
                    ilvl=ilvl,
                    score=score,
                    main_score=main_score,
                    rio_profile=rio_profile,
                    rio_best_key=rio_best_key,
                    rio_best_dungeon_key=rio_best_dungeon_key,
                    rio_timed_at_or_above=rio_timed_at_or_above,
                    rio_timed_at_or_above_minus1=rio_timed_at_or_above_minus1,
                    rio_timed_at_or_above_minus2=rio_timed_at_or_above_minus2,
                    rio_completed_at_or_above_minus1=rio_completed_at_or_above_minus1,
                    rio_dungeon_count=rio_dungeon_count,
                    role=role,
                    name=name,
                    rio_dungeons=[],
                )
            )

    if cursor != len(buf):
        raise ValueError(
            f"trailing or truncated payload bytes: consumed {cursor} of {len(buf)}"
        )

    return Snapshot(
        listing=listing,
        version=version,
        leader_key=leader_key,
        applicants=applicants,
        roster=roster,
        terminal_clear=terminal_clear,
        lfg_unavailable=lfg_unavailable,
        roster_unavailable=roster_unavailable,
        applicants_unavailable=applicants_unavailable,
    )


def _decode_screenshot_result(image_path: Path) -> DecodeResult:
    """Decode and parse a screenshot image with diagnostics.

    has_marker=True when the image's QR contained the APS1 magic, REGARDLESS of
    whether the rest of the payload parsed cleanly. snapshot=None with
    has_marker=True means our file but corrupt (truncated write / version
    skew / CRC mismatch) — caller should still delete it; the next snapshot
    in ≤0.5s will succeed.

    Normal pixel-aligned transport stops after the top-left crop. Full-screen
    and normalized scans run only as fallbacks when that fast path has no valid
    ApplicantScout candidate.
    """
    first_error: Optional[str] = None
    has_marker = False
    has_fragment_candidate = False
    try:
        diagnostics = _QRScanDiagnostics()
        batches = _iter_qr_symbol_data_batches(image_path, diagnostics)
        for symbol_payloads in batches:
            candidates = _collect_appscout_qr_candidates(symbol_payloads)
            if not candidates:
                continue
            has_marker = True
            for kind, raw in candidates:
                is_fragment_candidate = len(raw) > 4 and raw[4] == APS1_FRAGMENT_VERSION
                has_fragment_candidate = has_fragment_candidate or is_fragment_candidate
                try:
                    if is_fragment_candidate:
                        parsed, err = _try_parse_appscout_candidate(raw)
                    else:
                        parsed, err = _try_parse_appscout_payload(raw)
                except Exception as exc:  # noqa: BLE001
                    err = (
                        f"unexpected parser error: {type(exc).__name__}: "
                        f"{str(exc)[:200]}"
                    )
                    if first_error is None:
                        first_error = f"{kind}: {err}"
                    _log.exception(
                        "candidate parser error in %s (%s)", image_path.name, kind
                    )
                    continue
                if isinstance(parsed, SnapshotFragment):
                    _log.info(
                        "decoded %s: mode=%s wire=0x%02x fragment=%d/%d generation=%d",
                        image_path.name,
                        kind,
                        raw[4],
                        parsed.chunk_index + 1,
                        parsed.chunk_count,
                        parsed.generation,
                    )
                    return DecodeResult(None, True, fragment=parsed)
                if isinstance(parsed, Snapshot):
                    wire_ver = raw[4]
                    # Diagnostic: confirms which wire version we just parsed.
                    # v0x01 = leader-only (legacy); v0x02 = multi-member groups;
                    # v0x03 = listing context. If you reload the addon and still
                    # see an older wire version, you're likely processing a stale
                    # screenshot taken before the addon update.
                    _log.info(
                        "decoded %s: mode=%s wire=0x%02x applicant_rows=%d roster=%d",
                        image_path.name,
                        kind,
                        wire_ver,
                        len(parsed.applicants),
                        len(parsed.roster),
                    )
                    return DecodeResult(parsed, True)
                if err is not None:
                    if first_error is None:
                        first_error = f"{kind}: {err}"
                    _log.debug(
                        "candidate rejected in %s (%s): %s",
                        image_path.name,
                        kind,
                        err,
                    )
    except QRDecoderUnavailable as exc:
        reason = str(exc) or "QR decoder unavailable"
        _log.warning("%s", reason)
        return DecodeResult(
            None,
            False,
            reason,
            decoder_unavailable=True,
        )
    except QRScanFailed as exc:
        reason = str(exc) or "QR scan failed"
        _log.warning("could not scan %s: %s", image_path.name, reason)
        # QRScanFailed means zbar was present but could not finish this image.
        # It is not evidence that the generation has no APS1 marker, so keep it
        # eligible for the watcher's delayed retry just like an exception that
        # escapes the image/native boundary.
        return DecodeResult(None, False, reason, scan_incomplete=True)

    if not has_marker:
        return DecodeResult(
            None,
            False,
            transport_suspected=diagnostics.transport_suspected,
        )

    if first_error is not None:
        _log.warning("decode failed in %s: %s", image_path.name, first_error)
    return DecodeResult(
        None,
        True,
        first_error or "parse failed",
        fragment_candidate=has_fragment_candidate,
    )


# ─── File watcher ────────────────────────────────────────────────────────────
def _wait_for_stable_size(path: Path, timeout: float = STABLE_SIZE_TIMEOUT) -> bool:
    """Watchdog on_created fires BEFORE write completes. Poll size until
    it stops changing (= write done). Returns True on stable size, False on
    timeout."""
    last_size = -1
    elapsed = 0.0
    while elapsed < timeout:
        try:
            sz = path.stat().st_size
        except OSError:
            time.sleep(STABLE_SIZE_POLL)
            elapsed += STABLE_SIZE_POLL
            continue
        if sz == last_size and sz > 0:
            return True
        last_size = sz
        time.sleep(STABLE_SIZE_POLL)
        elapsed += STABLE_SIZE_POLL
    return False


def _is_supported_screenshot_path(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SCREENSHOT_SUFFIXES


def _iter_screenshot_candidates(directory: Path) -> Iterator[Path]:
    for path in directory.glob("WoWScrnShot_*"):
        if path.is_file() and _is_supported_screenshot_path(path):
            yield path


def cleanup_appscout_screenshots(
    directory: Path,
    *,
    delete: bool = False,
    limit: int | None = None,
) -> ScreenshotCleanupSummary:
    """Find ApplicantScout-owned screenshots and optionally remove them.

    This is an explicit support/privacy cleanup path. It deliberately does not
    reuse ScreenshotWatcher backlog logic because the watcher emits snapshots,
    has startup recency rules, and is capped for background work.
    """
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")
    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(f"Screenshots folder does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Screenshots path is not a folder: {directory}")

    candidates: list[tuple[Path, os.stat_result]] = []
    scan_errors = 0
    for path in _iter_screenshot_candidates(directory):
        try:
            candidates.append((path, path.stat()))
        except OSError as exc:
            scan_errors += 1
            _log.warning("could not stat screenshot candidate %s: %s", path.name, exc)

    candidates.sort(key=lambda t: t[1].st_mtime_ns, reverse=True)
    limited = limit is not None and len(candidates) > limit
    if limit is not None:
        candidates = candidates[:limit]

    scanned = 0
    markers_found = 0
    deleted = 0
    preserved = 0
    unstable = 0
    decode_errors = 0
    delete_failed = 0

    for path, _stat_result in candidates:
        scanned += 1
        if not _wait_for_stable_size(path):
            unstable += 1
            preserved += 1
            continue
        try:
            decoded_stat = path.stat()
            result = _decode_screenshot_result(path)
        except Exception as exc:  # noqa: BLE001
            decode_errors += 1
            preserved += 1
            _log.warning(
                "cleanup decode error before APS1 ownership for %s: %s",
                path.name,
                exc,
                exc_info=True,
            )
            continue

        if result.decoder_unavailable:
            decode_errors += 1
            preserved += 1
            continue

        if result.error_reason is not None and not result.has_marker:
            decode_errors += 1
            preserved += 1
            continue

        if not result.has_marker:
            preserved += 1
            continue

        markers_found += 1
        if not delete:
            preserved += 1
            continue

        decoded_source = SnapshotSource(
            mtime_ns=decoded_stat.st_mtime_ns,
            file_id=str(path),
            size=decoded_stat.st_size,
        )
        try:
            if _unlink_if_source_matches(path, decoded_source):
                deleted += 1
            else:
                preserved += 1
                _log.info(
                    "cleanup preserved replacement screenshot: %s",
                    path.name,
                )
        except OSError as exc:
            delete_failed += 1
            preserved += 1
            _log.warning("cleanup could not delete %s: %s", path.name, exc)

    return ScreenshotCleanupSummary(
        scanned=scanned,
        markers_found=markers_found,
        deleted=deleted,
        preserved=preserved,
        unstable=unstable,
        scan_errors=scan_errors,
        decode_errors=decode_errors,
        delete_failed=delete_failed,
        limited=limited,
    )


def format_screenshot_cleanup_summary(
    summary: ScreenshotCleanupSummary,
    *,
    delete: bool,
) -> str:
    mode = "removed" if delete else "dry run"
    lines = [
        f"ApplicantScout screenshot cleanup {mode}: scanned {summary.scanned} "
        f"candidate(s), found {summary.markers_found} ApplicantScout marker file(s), "
        f"removed {summary.deleted}, preserved {summary.preserved}."
    ]
    if not delete and summary.markers_found:
        lines.append("Pass --delete to remove the marker-bearing screenshots.")
    if summary.limited:
        lines.append("Scan was limited to the newest requested candidate count.")
    if summary.unstable:
        lines.append(f"Preserved {summary.unstable} unstable file(s).")
    if summary.scan_errors or summary.decode_errors or summary.delete_failed:
        lines.append(
            "Errors: "
            f"scan={summary.scan_errors}, decode={summary.decode_errors}, "
            f"delete={summary.delete_failed}."
        )
    return "\n".join(lines)


def screenshot_cleanup_exit_code(summary: ScreenshotCleanupSummary) -> int:
    return (
        1
        if (summary.scan_errors or summary.decode_errors or summary.delete_failed)
        else 0
    )


@dataclass(frozen=True)
class _ScreenshotWorkKey:
    path: str
    mtime_ns: int
    size: int


def _normalized_work_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _work_key_from_stat(path: Path, stat_result: os.stat_result) -> _ScreenshotWorkKey:
    return _ScreenshotWorkKey(
        path=_normalized_work_path(path),
        mtime_ns=int(
            getattr(
                stat_result,
                "st_mtime_ns",
                int(float(stat_result.st_mtime) * 1_000_000_000),
            )
        ),
        size=int(stat_result.st_size),
    )


class _ScreenshotWorkClaim:
    def __init__(
        self,
        owner: _ScreenshotWorkClaims,
        path: Path,
        key: _ScreenshotWorkKey,
        stat_result: os.stat_result,
    ) -> None:
        self._owner = owner
        self.path = path
        self.path_key = key.path
        self.key = key
        self.stat_result = stat_result
        self._seen_keys = {key}
        self._released = False
        self._release_keys_override: set[_ScreenshotWorkKey] | None = None
        self.retry_requested = False

    def refresh(self) -> os.stat_result | None:
        try:
            stat_result = self.path.stat()
        except OSError:
            return None
        key = _work_key_from_stat(self.path, stat_result)
        self.key = key
        self.stat_result = stat_result
        self._seen_keys.add(key)
        return stat_result

    def request_retry_for_changed_generation(
        self,
        decoded_key: _ScreenshotWorkKey,
    ) -> None:
        self.retry_requested = True
        # The new generation has not been processed. Do not put its key in the
        # recent set, or the bounded retry below would suppress the very work
        # needed to replace the stale decode result.
        self._release_keys_override = {decoded_key}

    def mark_processed_generation(self, decoded_key: _ScreenshotWorkKey) -> None:
        # Only the generation actually decoded may enter the recent-work cache.
        # A same-path replacement arriving during signal dispatch remains new work.
        self._release_keys_override = {decoded_key}

    def release(self) -> None:
        if self._released:
            return
        if self._release_keys_override is None:
            self.refresh()
        elif not self.retry_requested:
            current_stat = self.refresh()
            if current_stat is not None and self.key not in self._release_keys_override:
                self.retry_requested = True
        self._released = True
        self._owner._release(
            self.path_key,
            self._release_keys_override or self._seen_keys,
        )


class _ScreenshotWorkClaims:
    """One in-process arbitration point for watchdog and startup work."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_paths: set[str] = set()
        self._recent_keys: dict[_ScreenshotWorkKey, float] = {}

    def try_claim(self, path: Path) -> _ScreenshotWorkClaim | None:
        if not _is_supported_screenshot_path(path):
            return None
        try:
            stat_result = path.stat()
        except OSError:
            return None
        key = _work_key_from_stat(path, stat_result)
        now = time.monotonic()
        with self._lock:
            self._recent_keys = {
                recent_key: seen_at
                for recent_key, seen_at in self._recent_keys.items()
                if now - seen_at < _RECENT_WORK_KEY_TTL_SECONDS
            }
            if key.path in self._active_paths or key in self._recent_keys:
                return None
            self._active_paths.add(key.path)
        return _ScreenshotWorkClaim(self, path, key, stat_result)

    def _release(
        self,
        path_key: str,
        seen_keys: set[_ScreenshotWorkKey],
    ) -> None:
        now = time.monotonic()
        with self._lock:
            self._active_paths.discard(path_key)
            for key in seen_keys:
                self._recent_keys[key] = now

    def forget_recent_generation(self, key: _ScreenshotWorkKey) -> None:
        """Allow one explicitly scheduled retry of an unchanged generation."""
        with self._lock:
            self._recent_keys.pop(key, None)


def _manual_index_path(cache_dir: Path, screenshots_dir: Path) -> Path:
    directory_key = _normalized_work_path(screenshots_dir).encode(
        "utf-8",
        errors="surrogatepass",
    )
    digest = hashlib.sha256(directory_key).hexdigest()[:16]
    return Path(cache_dir) / f"{_MANUAL_INDEX_FILE_PREFIX}-{digest}.json"


class _ManualScreenshotIndex:
    """Persistent fingerprints for files not decoded as APS1 by this revision."""

    def __init__(self, state_path: Path | None) -> None:
        self._state_path = state_path
        self._lock = threading.Lock()
        self._loaded = False
        self._keys: set[_ScreenshotWorkKey] = set()
        self._deferred_keys: set[_ScreenshotWorkKey] = set()
        self._deferred_cursor: _ScreenshotWorkKey | None = None
        self._dirty = False

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._state_path is None:
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _log.warning("could not load screenshot manual index: %s", exc)
            return
        if not isinstance(raw, dict) or raw.get("version") != _MANUAL_INDEX_VERSION:
            return
        entries = raw.get("manual")
        if not isinstance(entries, list):
            return
        deferred_entries = raw.get("deferred", [])
        if not isinstance(deferred_entries, list):
            deferred_entries = []
        deferred_cursor = raw.get("deferred_cursor")
        for source_entries, target in (
            (entries, self._keys),
            (deferred_entries, self._deferred_keys),
        ):
            for entry in source_entries:
                if (
                    not isinstance(entry, list)
                    or len(entry) != 3
                    or not isinstance(entry[0], str)
                    or not isinstance(entry[1], int)
                    or not isinstance(entry[2], int)
                    or entry[1] < 0
                    or entry[2] < 0
                ):
                    continue
                target.add(_ScreenshotWorkKey(entry[0], entry[1], entry[2]))
        if (
            isinstance(deferred_cursor, list)
            and len(deferred_cursor) == 3
            and isinstance(deferred_cursor[0], str)
            and isinstance(deferred_cursor[1], int)
            and isinstance(deferred_cursor[2], int)
            and deferred_cursor[1] >= 0
            and deferred_cursor[2] >= 0
        ):
            self._deferred_cursor = _ScreenshotWorkKey(
                deferred_cursor[0],
                deferred_cursor[1],
                deferred_cursor[2],
            )

    def snapshot(self) -> set[_ScreenshotWorkKey]:
        with self._lock:
            self._load_locked()
            return self._keys | self._deferred_keys

    def deferred_snapshot(self) -> set[_ScreenshotWorkKey]:
        with self._lock:
            self._load_locked()
            return set(self._deferred_keys)

    def contains(self, key: _ScreenshotWorkKey) -> bool:
        with self._lock:
            self._load_locked()
            return key in self._keys

    def select_retry_window(
        self,
        ordered_keys: list[_ScreenshotWorkKey],
        *,
        limit: int,
    ) -> set[_ScreenshotWorkKey]:
        """Rotate bounded retries without changing newest-to-oldest authority."""
        with self._lock:
            self._load_locked()
            eligible = list(ordered_keys)
            if not eligible or limit <= 0:
                return set()
            start = 0
            if self._deferred_cursor in eligible:
                start = (eligible.index(self._deferred_cursor) + 1) % len(eligible)
            count = min(limit, len(eligible))
            selected = [
                eligible[(start + offset) % len(eligible)] for offset in range(count)
            ]
            if self._deferred_cursor != selected[-1]:
                self._deferred_cursor = selected[-1]
                self._dirty = True
            return set(selected)

    def note_manual(self, key: _ScreenshotWorkKey, *, flush: bool) -> None:
        with self._lock:
            self._load_locked()
            if key not in self._keys:
                self._keys.add(key)
                self._dirty = True
            if key in self._deferred_keys:
                self._deferred_keys.discard(key)
                self._dirty = True
            if flush:
                self._flush_locked()

    def note_deferred(self, key: _ScreenshotWorkKey, *, flush: bool) -> None:
        with self._lock:
            self._load_locked()
            if key not in self._deferred_keys:
                self._deferred_keys.add(key)
                self._dirty = True
            if flush:
                self._flush_locked()

    def forget_deferred(self, key: _ScreenshotWorkKey, *, flush: bool) -> None:
        with self._lock:
            self._load_locked()
            if key in self._deferred_keys:
                self._deferred_keys.discard(key)
                self._dirty = True
            if flush:
                self._flush_locked()

    def prune_missing(
        self,
        baseline: set[_ScreenshotWorkKey],
        current: set[_ScreenshotWorkKey],
    ) -> None:
        with self._lock:
            self._load_locked()
            stale = baseline - current
            if stale:
                self._keys.difference_update(stale)
                self._deferred_keys.difference_update(stale)
                if self._deferred_cursor in stale:
                    self._deferred_cursor = None
                self._dirty = True

    def flush(self) -> None:
        with self._lock:
            self._load_locked()
            self._flush_locked()

    def reset(self) -> None:
        with self._lock:
            self._keys.clear()
            self._deferred_keys.clear()
            self._deferred_cursor = None
            self._loaded = True
            self._dirty = False
            if self._state_path is None:
                return
            try:
                self._state_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.warning("could not clear screenshot manual index: %s", exc)

    def _flush_locked(self) -> None:
        if not self._dirty or self._state_path is None:
            return
        entries = [
            [key.path, key.mtime_ns, key.size]
            for key in sorted(
                self._keys,
                key=lambda item: (item.path, item.mtime_ns, item.size),
            )
        ]
        deferred_entries = [
            [key.path, key.mtime_ns, key.size]
            for key in sorted(
                self._deferred_keys,
                key=lambda item: (item.path, item.mtime_ns, item.size),
            )
        ]
        payload = json.dumps(
            {
                "version": _MANUAL_INDEX_VERSION,
                "manual": entries,
                "deferred": deferred_entries,
                "deferred_cursor": (
                    [
                        self._deferred_cursor.path,
                        self._deferred_cursor.mtime_ns,
                        self._deferred_cursor.size,
                    ]
                    if self._deferred_cursor is not None
                    else None
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            atomic_write_text(self._state_path, payload, private=True)
        except Exception as exc:  # noqa: BLE001 - best-effort cache state
            _log.warning("could not save screenshot manual index: %s", exc)
            return
        self._dirty = False


_MANUAL_INDEX_REGISTRY_LOCK = threading.Lock()
_MANUAL_INDEX_REGISTRY: dict[str, _ManualScreenshotIndex] = {}


def _manual_index_for(
    screenshots_dir: Path,
    cache_dir: Path | None,
) -> _ManualScreenshotIndex:
    if cache_dir is None:
        return _ManualScreenshotIndex(None)
    state_path = _manual_index_path(cache_dir, screenshots_dir)
    registry_key = _normalized_work_path(state_path)
    with _MANUAL_INDEX_REGISTRY_LOCK:
        index = _MANUAL_INDEX_REGISTRY.get(registry_key)
        if index is None:
            index = _ManualScreenshotIndex(state_path)
            _MANUAL_INDEX_REGISTRY[registry_key] = index
        return index


def clear_screenshot_manual_indexes(cache_dir: Path) -> None:
    cache_key = _normalized_work_path(cache_dir)
    with _MANUAL_INDEX_REGISTRY_LOCK:
        indexes = [
            index
            for index in _MANUAL_INDEX_REGISTRY.values()
            if index._state_path is not None
            and _normalized_work_path(index._state_path.parent) == cache_key
        ]
    for index in indexes:
        index.reset()


class _Handler(FileSystemEventHandler):
    """Filters JPG/TGA file events and dispatches all relevant paths.

    ScreenshotWatcher owns deduplication so observer and backlog work share the
    same claim. Listening to all three event types because:

    - on_created fires when a new file appears (typical WoW Screenshot() path)
    - on_modified fires if WoW writes via fwrite-without-create-flag, or if two
      shots in the same second overwrite the same filename
    - on_moved fires if WoW writes to a .tmp then atomically renames (some
      versions of Windows + some antivirus products force atomic-rename pattern)

    Without on_modified+on_moved, subsequent screenshots in the same second OR
    via tmp-rename pattern silently disappear from the pipeline."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def _should_process(self, path: Path) -> bool:
        if not _is_supported_screenshot_path(path):
            return False
        try:
            return path.is_file()
        except OSError:
            return False

    @staticmethod
    def _path_from_event(value: str | bytes) -> Path:
        return Path(os.fsdecode(value))

    def _handle_path_event(self, event, path_value: str | bytes) -> None:
        if event.is_directory:
            return
        path = self._path_from_event(path_value)
        if self._should_process(path):
            self._callback(path)

    def on_created(self, event):
        self._handle_path_event(event, event.src_path)

    def on_modified(self, event):
        self._handle_path_event(event, event.src_path)

    def on_moved(self, event):
        # For move/rename, the destination is the final filename we care about.
        dest = getattr(event, "dest_path", None) or event.src_path
        self._handle_path_event(event, dest)


@dataclass(frozen=True)
class _FragmentAssemblyOutcome:
    snapshot: Snapshot | None = None
    retired_files: tuple["_RetainedFragmentFile", ...] = ()
    error_reason: str | None = None
    accepted: bool = False
    retry_after: float | None = None
    diagnostic_path: Path | None = None
    diagnostic_source: SnapshotSource | None = None


@dataclass(frozen=True)
class _RetainedFragmentFile:
    path: Path
    source: SnapshotSource | None


@dataclass
class _PendingFragmentAssembly:
    chunks: dict[int, bytes]
    files: dict[Path, SnapshotSource | None]
    newest_source: SnapshotSource | None
    newest_source_key: tuple[int, str, int] | None
    last_seen: float


def _snapshot_source_order_key(
    source: SnapshotSource | None,
) -> tuple[int, str, int] | None:
    if source is None:
        return None
    return source.mtime_ns, source.file_id, source.size


class _SnapshotFragmentAssembler:
    """Thread-safe authority boundary between transport chunks and Snapshots."""

    def __init__(self, *, ttl_seconds: float = APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._stream_id: int | None = None
        self._generation: int | None = None
        self._signature: tuple[int, int, int] | None = None
        self._completed = False
        self._poisoned = False
        self._frontier_source_key: tuple[int, str, int] | None = None
        self._pending: _PendingFragmentAssembly | None = None
        self._barred_stream_id: int | None = None
        self._barred_generation: int | None = None

    @staticmethod
    def _sorted_files(
        files: dict[Path, SnapshotSource | None],
    ) -> tuple[_RetainedFragmentFile, ...]:
        return tuple(
            _RetainedFragmentFile(path, files[path])
            for path in sorted(files, key=lambda item: str(item))
        )

    def _retire_pending_locked(self) -> tuple[_RetainedFragmentFile, ...]:
        if self._pending is None:
            return ()
        files = self._sorted_files(self._pending.files)
        self._pending = None
        return files

    def _expire_locked(self, now: float) -> tuple[_RetainedFragmentFile, ...]:
        pending = self._pending
        if pending is None or now - pending.last_seen <= self._ttl_seconds:
            return ()
        return self._retire_pending_locked()

    def expire(self, *, now: float | None = None) -> _FragmentAssemblyOutcome:
        """Expire pending bytes without requiring another screenshot arrival."""
        current_time = time.monotonic() if now is None else now
        with self._lock:
            pending = self._pending
            if pending is None:
                return _FragmentAssemblyOutcome()
            remaining = self._ttl_seconds - (current_time - pending.last_seen)
            if remaining > 0:
                return _FragmentAssemblyOutcome(retry_after=remaining)
            diagnostic_source = pending.newest_source
            if diagnostic_source is not None:
                diagnostic_path = Path(diagnostic_source.file_id)
            elif pending.files:
                diagnostic_path = next(iter(pending.files))
            else:
                diagnostic_path = None
            retired = self._retire_pending_locked()
            return _FragmentAssemblyOutcome(
                retired_files=retired,
                error_reason="v10 fragment assembly timed out",
                diagnostic_path=diagnostic_path,
                diagnostic_source=diagnostic_source,
            )

    @staticmethod
    def _is_newer_source(
        candidate: tuple[int, str, int] | None,
        frontier: tuple[int, str, int] | None,
    ) -> bool:
        if frontier is None:
            return True
        return candidate is not None and candidate > frontier

    def accept_snapshot(
        self,
        snap: Snapshot,
        *,
        now: float | None = None,
    ) -> _FragmentAssemblyOutcome:
        """Apply a whole-frame barrier and reject snapshots behind chunk work."""
        current_time = time.monotonic() if now is None else now
        source_key = _snapshot_source_order_key(snap.source)
        with self._lock:
            retired = list(self._expire_locked(current_time))
            if not self._is_newer_source(source_key, self._frontier_source_key):
                return _FragmentAssemblyOutcome(retired_files=tuple(retired))
            retired.extend(self._retire_pending_locked())
            if self._stream_id is not None and self._generation is not None:
                self._barred_stream_id = self._stream_id
                self._barred_generation = self._generation
            self._stream_id = None
            self._generation = None
            self._signature = None
            self._completed = False
            self._poisoned = False
            self._frontier_source_key = source_key
            return _FragmentAssemblyOutcome(
                snapshot=snap,
                retired_files=tuple(retired),
                accepted=True,
            )

    def accept_fragment(
        self,
        fragment: SnapshotFragment,
        path: Path,
        *,
        now: float | None = None,
    ) -> _FragmentAssemblyOutcome:
        current_time = time.monotonic() if now is None else now
        source_key = _snapshot_source_order_key(fragment.source)
        signature = (
            fragment.chunk_count,
            fragment.inner_total_len,
            fragment.inner_crc32,
        )
        with self._lock:
            retired = list(self._expire_locked(current_time))
            if (
                fragment.stream_id == self._barred_stream_id
                and self._barred_generation is not None
                and fragment.generation <= self._barred_generation
            ):
                retired.append(_RetainedFragmentFile(path, fragment.source))
                return _FragmentAssemblyOutcome(retired_files=tuple(retired))
            same_stream = fragment.stream_id == self._stream_id
            if not same_stream:
                if not self._is_newer_source(source_key, self._frontier_source_key):
                    retired.append(_RetainedFragmentFile(path, fragment.source))
                    return _FragmentAssemblyOutcome(retired_files=tuple(retired))
                retired.extend(self._retire_pending_locked())
                self._stream_id = fragment.stream_id
                self._generation = fragment.generation
                self._signature = signature
                self._completed = False
                self._poisoned = False
            else:
                assert self._generation is not None
                if fragment.generation < self._generation:
                    retired.append(_RetainedFragmentFile(path, fragment.source))
                    return _FragmentAssemblyOutcome(retired_files=tuple(retired))
                if fragment.generation > self._generation:
                    retired.extend(self._retire_pending_locked())
                    self._generation = fragment.generation
                    self._signature = signature
                    self._completed = False
                    self._poisoned = False
                elif self._completed or self._poisoned:
                    retired.append(_RetainedFragmentFile(path, fragment.source))
                    return _FragmentAssemblyOutcome(retired_files=tuple(retired))
                elif signature != self._signature:
                    retired.extend(self._retire_pending_locked())
                    retired.append(_RetainedFragmentFile(path, fragment.source))
                    self._poisoned = True
                    return _FragmentAssemblyOutcome(
                        retired_files=tuple(retired),
                        error_reason="conflicting v10 metadata for one generation",
                    )

            if source_key is not None and (
                self._frontier_source_key is None
                or source_key > self._frontier_source_key
            ):
                self._frontier_source_key = source_key

            if self._pending is None:
                self._pending = _PendingFragmentAssembly(
                    chunks={},
                    files={},
                    newest_source=fragment.source,
                    newest_source_key=source_key,
                    last_seen=current_time,
                )
            pending = self._pending
            pending.files[path] = fragment.source
            retired = [
                item
                for item in retired
                if not (item.path == path and item.source == fragment.source)
            ]
            pending.last_seen = current_time
            if source_key is not None and (
                pending.newest_source_key is None
                or source_key > pending.newest_source_key
            ):
                pending.newest_source = fragment.source
                pending.newest_source_key = source_key

            prior = pending.chunks.get(fragment.chunk_index)
            if prior is not None and prior != fragment.chunk:
                retired.extend(self._retire_pending_locked())
                self._poisoned = True
                return _FragmentAssemblyOutcome(
                    retired_files=tuple(retired),
                    error_reason=(
                        f"conflicting v10 chunk {fragment.chunk_index} "
                        "for one generation"
                    ),
                )
            pending.chunks[fragment.chunk_index] = fragment.chunk
            if len(pending.chunks) < fragment.chunk_count:
                return _FragmentAssemblyOutcome(
                    retired_files=tuple(retired),
                    accepted=True,
                )

            inner = b"".join(
                pending.chunks[index] for index in range(fragment.chunk_count)
            )
            parsed: Snapshot | SnapshotFragment | None = None
            if len(inner) != fragment.inner_total_len:
                error = (
                    f"assembled v10 payload has {len(inner)} bytes; "
                    f"expected {fragment.inner_total_len}"
                )
            elif struct.unpack(">I", inner[-4:])[0] != fragment.inner_crc32:
                error = "assembled v10 inner CRC trailer mismatch"
            elif zlib.crc32(inner[:-4]) & 0xFFFFFFFF != fragment.inner_crc32:
                error = "assembled v10 inner CRC mismatch"
            else:
                parsed, error = _try_parse_appscout_candidate(inner)
                if isinstance(parsed, SnapshotFragment):
                    parsed = None
                    error = "nested v10 fragment payload is not allowed"
                elif parsed is not None:
                    parsed = replace(parsed, source=pending.newest_source)

            retired.extend(self._retire_pending_locked())
            if error is not None or not isinstance(parsed, Snapshot):
                self._poisoned = True
                return _FragmentAssemblyOutcome(
                    retired_files=tuple(retired),
                    error_reason=error
                    or "assembled v10 payload did not contain a snapshot",
                )
            self._completed = True
            return _FragmentAssemblyOutcome(
                snapshot=parsed,
                retired_files=tuple(retired),
                accepted=True,
            )


class ScreenshotWatcher(QObject):
    """Watches Screenshots/ folder via watchdog Observer. On each new JPG/TGA:
    waits for write to complete, decodes QR, emits snapshotReceived(Snapshot)
    on success. Deletes the file if it carries our APS1 marker. Skips delete
    if no marker (preserves manual, unrelated, and uncertain transport images).

    On startup: applies the most recent valid snapshot from the last 60 seconds
    and may reassemble incomplete v10 fragment sets retained for up to five
    minutes. This handles starting the companion after capture began without
    allowing an older logical snapshot to become fresh again."""

    snapshotReceived = pyqtSignal(object)  # Snapshot
    decodeFailed = pyqtSignal(str, str, object)  # path, reason, SnapshotSource | None

    def __init__(
        self,
        screenshots_dir: Path,
        parent=None,
        *,
        cache_dir: Path | None = None,
        fragment_clock: Callable[[], float] | None = None,
        fragment_timer_factory: Callable[[float, Callable[[], None]], Any]
        | None = None,
        generation_retry_timer_factory: Callable[[float, Callable[[], None]], Any]
        | None = None,
    ):
        super().__init__(parent)
        self._dir = screenshots_dir
        self._observer: Optional[Any] = None
        self._backlog_thread: Optional[threading.Thread] = None
        self._backlog_rescan_requested = False
        self._observer_lock = threading.RLock()
        self._supervisor_thread: Optional[threading.Thread] = None
        self._observer_restart_count = 0
        self._stopped = threading.Event()
        self._work_claims = _ScreenshotWorkClaims()
        self._manual_index = _manual_index_for(screenshots_dir, cache_dir)
        self._fragment_assembler = _SnapshotFragmentAssembler()
        self._fragment_clock = fragment_clock or time.monotonic
        self._fragment_timer_factory = fragment_timer_factory or threading.Timer
        self._fragment_expiry_lock = threading.Lock()
        self._fragment_expiry_timer: Any | None = None
        self._fragment_expiry_token = 0
        self._fragment_expiry_identity: tuple[int, int] | None = None
        self._fragment_degraded_reported = False
        self._generation_retry_timer_factory = (
            generation_retry_timer_factory or threading.Timer
        )
        self._generation_retry_lock = threading.Lock()
        self._generation_retry_timers: dict[str, Any] = {}
        self._incomplete_retry_lock = threading.Lock()
        self._incomplete_retry_timers: dict[
            str,
            tuple[_ScreenshotWorkKey, Any],
        ] = {}

    @staticmethod
    def _observer_is_healthy(observer: Any | None) -> bool:
        if observer is None:
            return False
        try:
            is_alive = getattr(observer, "is_alive", None)
            if callable(is_alive) and not is_alive():
                return False
            emitters = getattr(observer, "emitters", None)
            if emitters is not None:
                emitters = tuple(emitters)
                if not emitters:
                    return False
                for emitter in emitters:
                    emitter_is_alive = getattr(emitter, "is_alive", None)
                    if callable(emitter_is_alive) and not emitter_is_alive():
                        return False
        except Exception:  # noqa: BLE001 - broken watchdog state is unhealthy
            return False
        return True

    @staticmethod
    def _stop_observer(observer: Any | None) -> None:
        if observer is None:
            return
        try:
            is_alive = getattr(observer, "is_alive", None)
            was_alive = not callable(is_alive) or is_alive()
        except Exception:  # noqa: BLE001 - attempt a bounded join when uncertain
            was_alive = True
        try:
            observer.stop()
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            _log.debug("observer cleanup stop failed: %s", exc)
        try:
            if was_alive:
                observer.join(timeout=2)
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            _log.debug("observer cleanup join failed: %s", exc)

    def _start_observer_locked(self) -> None:
        # Ensure folder exists (WoW creates it on first screenshot, but companion
        # may start before WoW ever takes one)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Observer first so any new file arriving during the backlog scan still
        # gets routed through _on_new_file. Both paths share _work_claims, so
        # only one may decode a given file generation.
        observer = Observer()
        try:
            observer.schedule(
                _Handler(self._on_new_file),
                str(self._dir),
                recursive=False,
            )
            observer.start()
            self._observer = observer
            _log.info("watching %s", self._dir)
            # Backlog scan on a background thread — for users with hundreds of
            # historical WoWScrnShot JPG/TGA files, the synchronous scan was the
            # dominant startup-latency contributor (~30-80 ms per file × 500 file
            # cap = up to ~30s). Overlay now appears immediately. snapshotReceived
            # is a Qt pyqtSignal — emits cross thread are queued safely to the GUI
            # thread by Qt's signal/slot machinery.
            self._request_backlog_scan_locked()
        except Exception:
            self._observer = None
            self._backlog_thread = None
            self._backlog_rescan_requested = False
            self._stop_observer(observer)
            raise

    def _request_backlog_scan_locked(self) -> None:
        self._backlog_rescan_requested = True
        current = self._backlog_thread
        if current is not None and current.is_alive():
            return
        worker = threading.Thread(
            target=self._run_backlog_scans,
            name="ApplicantScoutBacklogScan",
            daemon=True,
        )
        self._backlog_thread = worker
        try:
            worker.start()
        except Exception:
            self._backlog_thread = None
            self._backlog_rescan_requested = False
            raise

    def _request_backlog_rescan_after_deferred_resolution(self) -> None:
        with self._observer_lock:
            if self._stopped.is_set():
                return
            # Direct cleanup/tests can scan without starting the live watcher.
            # Only a live observer or an existing backlog worker owns follow-up
            # scans; otherwise a classified manual file would unexpectedly
            # create a background thread from a synchronous helper call.
            if self._observer is None and self._backlog_thread is None:
                return
            self._request_backlog_scan_locked()

    def _run_backlog_scans(self) -> None:
        while True:
            with self._observer_lock:
                if self._stopped.is_set():
                    self._backlog_rescan_requested = False
                    self._backlog_thread = None
                    return
                self._backlog_rescan_requested = False
            try:
                self._scan_recent_backlog()
            except Exception:  # noqa: BLE001 - a later restart may request a retry
                _log.exception("screenshot backlog scan failed")
            with self._observer_lock:
                if self._stopped.is_set() or not self._backlog_rescan_requested:
                    self._backlog_rescan_requested = False
                    self._backlog_thread = None
                    return

    def _supervise_observer(self) -> None:
        while not self._stopped.wait(5.0):
            try:
                self.ensure_running()
            except Exception:  # noqa: BLE001 - retry on the next supervisor tick
                _log.exception("could not restart failed screenshot observer")

    def start(self) -> None:
        with self._observer_lock:
            self._stopped.clear()
            try:
                self._start_observer_locked()
                supervisor = threading.Thread(
                    target=self._supervise_observer,
                    name="ApplicantScoutScreenshotSupervisor",
                    daemon=True,
                )
                supervisor.start()
                self._supervisor_thread = supervisor
            except Exception:
                self._stopped.set()
                observer = self._observer
                self._observer = None
                self._supervisor_thread = None
                self._stop_observer(observer)
                raise

    def ensure_running(self) -> bool:
        """Restart a dead watchdog dispatcher/emitter and rescan recent files."""
        with self._observer_lock:
            if self._stopped.is_set():
                return False
            if self._observer_is_healthy(self._observer):
                return True
            failed_observer = self._observer
            self._observer = None
            self._stop_observer(failed_observer)
            self._start_observer_locked()
            self._observer_restart_count += 1
            _log.warning(
                "restarted failed screenshot observer (restart %d)",
                self._observer_restart_count,
            )
            return True

    def request_stop(self) -> None:
        with self._fragment_expiry_lock:
            self._stopped.set()
            self._fragment_expiry_token += 1
            timer = self._fragment_expiry_timer
            self._fragment_expiry_timer = None
        with self._generation_retry_lock:
            generation_retry_timers = tuple(self._generation_retry_timers.values())
            self._generation_retry_timers.clear()
        with self._incomplete_retry_lock:
            incomplete_retry_timers = tuple(
                timer for _key, timer in self._incomplete_retry_timers.values()
            )
            self._incomplete_retry_timers.clear()
        if timer is not None:
            timer.cancel()
        for generation_retry_timer in generation_retry_timers:
            generation_retry_timer.cancel()
        for incomplete_retry_timer in incomplete_retry_timers:
            incomplete_retry_timer.cancel()

    def stop(self) -> None:
        self.request_stop()
        with self._observer_lock:
            observer = self._observer
            self._observer = None
            supervisor = self._supervisor_thread
            self._supervisor_thread = None
            self._backlog_rescan_requested = False
        self._stop_observer(observer)
        if supervisor is not None and supervisor is not threading.current_thread():
            supervisor.join(timeout=2)
        self._manual_index.flush()
        # Backlog thread is daemon=True so process exit doesn't wait for it.
        # We don't .join here: it may be in the middle of a 30-80 ms pyzbar
        # call we can't interrupt cleanly. Daemonised so it dies with us.

    @staticmethod
    def _set_timer_daemon(timer: Any) -> None:
        try:
            timer.daemon = True
        except (AttributeError, RuntimeError):
            pass

    def _start_fragment_timer(self, timer: Any) -> None:
        self._set_timer_daemon(timer)
        try:
            timer.start()
        except Exception as exc:  # noqa: BLE001
            with self._fragment_expiry_lock:
                if self._fragment_expiry_timer is timer:
                    self._fragment_expiry_timer = None
                    self._fragment_expiry_token += 1
            _log.warning("could not start fragment expiry timer: %s", exc)

    def _make_fragment_timer_locked(
        self,
        identity: tuple[int, int],
        delay: float,
    ) -> Any:
        self._fragment_expiry_token += 1
        token = self._fragment_expiry_token
        timer = self._fragment_timer_factory(
            max(0.001, delay),
            lambda: self._on_fragment_expiry(token, identity),
        )
        self._fragment_expiry_timer = timer
        return timer

    def _arm_fragment_expiry(self, fragment: SnapshotFragment) -> None:
        identity = fragment.stream_id, fragment.generation
        with self._fragment_expiry_lock:
            if self._stopped.is_set():
                return
            old_timer = self._fragment_expiry_timer
            if identity != self._fragment_expiry_identity:
                self._fragment_degraded_reported = False
            self._fragment_expiry_identity = identity
            timer = self._make_fragment_timer_locked(
                identity,
                APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS,
            )
        if old_timer is not None:
            old_timer.cancel()
        self._start_fragment_timer(timer)

    def _cancel_fragment_expiry(self) -> None:
        with self._fragment_expiry_lock:
            self._fragment_expiry_token += 1
            timer = self._fragment_expiry_timer
            self._fragment_expiry_timer = None
            self._fragment_expiry_identity = None
            self._fragment_degraded_reported = False
        if timer is not None:
            timer.cancel()

    def _note_fragment_degraded_failure(self) -> None:
        with self._fragment_expiry_lock:
            if self._fragment_expiry_identity is not None:
                self._fragment_degraded_reported = True

    def _on_fragment_expiry(
        self,
        token: int,
        identity: tuple[int, int],
    ) -> None:
        retry_timer: Any | None = None
        failure: tuple[Path, str, SnapshotSource | None] | None = None
        with self._fragment_expiry_lock:
            if (
                self._stopped.is_set()
                or token != self._fragment_expiry_token
                or identity != self._fragment_expiry_identity
            ):
                return
            self._fragment_expiry_timer = None
            outcome = self._fragment_assembler.expire(now=self._fragment_clock())
            if outcome.retry_after is not None:
                retry_timer = self._make_fragment_timer_locked(
                    identity,
                    outcome.retry_after,
                )
            else:
                self._delete_retired_fragment_files(outcome.retired_files)
                if (
                    outcome.error_reason is not None
                    and not self._fragment_degraded_reported
                ):
                    self._fragment_degraded_reported = True
                    failure = (
                        outcome.diagnostic_path or self._dir,
                        outcome.error_reason,
                        outcome.diagnostic_source,
                    )
                self._fragment_expiry_identity = None
        if retry_timer is not None:
            self._start_fragment_timer(retry_timer)
        if failure is not None:
            self._emit_decode_failed(*failure)

    def _emit_snapshot(self, snap: Snapshot) -> bool:
        if self._stopped.is_set():
            return False
        self.snapshotReceived.emit(snap)
        return not self._stopped.is_set()

    def _emit_decode_failed(
        self,
        path: Path,
        reason: str,
        source: SnapshotSource | None = None,
    ) -> bool:
        if self._stopped.is_set():
            return False
        self.decodeFailed.emit(str(path), reason, source)
        return not self._stopped.is_set()

    @staticmethod
    def _source_from_stat(path: Path, stat_result: os.stat_result) -> SnapshotSource:
        return SnapshotSource(
            mtime_ns=stat_result.st_mtime_ns,
            file_id=str(path),
            size=stat_result.st_size,
        )

    @staticmethod
    def _snapshot_with_source(
        snap: Snapshot,
        source: SnapshotSource | None,
    ) -> Snapshot:
        if source is None:
            return snap
        return replace(snap, source=source)

    @staticmethod
    def _fragment_with_source(
        fragment: SnapshotFragment,
        source: SnapshotSource | None,
    ) -> SnapshotFragment:
        if source is None:
            return fragment
        return replace(fragment, source=source)

    @staticmethod
    def _delete_retired_fragment_files(
        files: tuple[_RetainedFragmentFile, ...],
    ) -> int:
        deleted = 0
        for retained in files:
            path = retained.path
            try:
                if _unlink_if_source_matches(path, retained.source):
                    deleted += 1
            except OSError as exc:
                _log.warning("could not delete retired fragment %s: %s", path.name, exc)
        return deleted

    def _scan_recent_backlog(self) -> None:
        """Restore recent state, then advance bounded historical cleanup.

        No-marker screenshots are fingerprinted for the current decoder
        revision. Recovered APS1 candidates that fail full validation remain
        retryable; a decoder revision bump reconsiders all other no-marker
        fingerprints exactly once.
        """
        if self._stopped.is_set():
            return
        now = time.time()
        apply_cutoff = now - 60
        fragment_cutoff = now - APS1_FRAGMENT_ASSEMBLY_TTL_SECONDS
        baseline_manual_keys = self._manual_index.snapshot()
        all_files: list[tuple[Path, os.stat_result]] = []
        for p in _iter_screenshot_candidates(self._dir):
            try:
                all_files.append((p, p.stat()))
            except OSError:
                continue
        current_keys = {
            _work_key_from_stat(path, stat_result) for path, stat_result in all_files
        }
        self._manual_index.prune_missing(baseline_manual_keys, current_keys)
        # Pruning can remove a persisted deferred authority barrier whose file
        # was rotated or deleted while the companion was stopped. All retry and
        # manual decisions in this pass must use the post-prune state, or the
        # stale key makes every remaining candidate look older than an unknown
        # screenshot that no longer exists.
        current_manual_keys = self._manual_index.snapshot()
        current_deferred_keys = self._manual_index.deferred_snapshot()
        try:
            if not all_files:
                return
            all_files.sort(key=lambda item: item[1].st_mtime_ns, reverse=True)
            recent = [item for item in all_files if item[1].st_mtime >= fragment_cutoff]
            historical = [
                item for item in all_files if item[1].st_mtime < fragment_cutoff
            ]
            manual_only_keys = current_manual_keys - current_deferred_keys
            ordered_retry_candidates = [
                _work_key_from_stat(path, stat_result)
                for path, stat_result in all_files
                if _work_key_from_stat(path, stat_result) not in manual_only_keys
            ]
            retry_window_active = bool(current_deferred_keys)
            selected_deferred = (
                self._manual_index.select_retry_window(
                    ordered_retry_candidates,
                    limit=_BACKLOG_INCOMPLETE_SCAN_LIMIT,
                )
                if retry_window_active
                else set()
            )
            remaining = _BACKLOG_CLEANUP_LIMIT
            apply_closed = False
            authority_blocked = False
            deleted = 0
            remaining, apply_closed, authority_blocked, phase_deleted, stop_scan = (
                self._scan_backlog_phase(
                    recent,
                    recent=True,
                    remaining=remaining,
                    apply_closed=apply_closed,
                    authority_blocked=authority_blocked,
                    retry_window_active=retry_window_active,
                    selected_deferred_keys=selected_deferred,
                    snapshot_apply_cutoff_ns=int(apply_cutoff * 1_000_000_000),
                )
            )
            deleted += phase_deleted
            if not stop_scan and remaining > 0 and not self._stopped.is_set():
                historical_remaining = min(
                    remaining,
                    _BACKLOG_HISTORICAL_CLEANUP_LIMIT,
                )
                (
                    _historical_remaining,
                    apply_closed,
                    authority_blocked,
                    phase_deleted,
                    _stop_scan,
                ) = self._scan_backlog_phase(
                    historical,
                    recent=False,
                    remaining=historical_remaining,
                    apply_closed=apply_closed,
                    authority_blocked=authority_blocked,
                    retry_window_active=retry_window_active,
                    selected_deferred_keys=selected_deferred,
                    snapshot_apply_cutoff_ns=None,
                )
                deleted += phase_deleted
            if deleted:
                _log.info(
                    "backlog cleanup: deleted %d ApScout screenshots",
                    deleted,
                )
        finally:
            self._manual_index.flush()

    def _scan_backlog_phase(
        self,
        candidates: list[tuple[Path, os.stat_result]],
        *,
        recent: bool,
        remaining: int,
        apply_closed: bool,
        authority_blocked: bool,
        retry_window_active: bool,
        selected_deferred_keys: set[_ScreenshotWorkKey],
        snapshot_apply_cutoff_ns: int | None,
    ) -> tuple[int, bool, bool, int, bool]:
        deleted = 0
        fragment_frontier_active = False
        incomplete_scans = 0
        deferred_failure: tuple[Path, str, SnapshotSource] | None = None
        for path, _candidate_stat in candidates:
            if self._stopped.is_set() or remaining <= 0:
                break
            if recent and not _wait_for_stable_size(path):
                _log.info(
                    "backlog: skipping unstable recent screenshot %s",
                    path.name,
                )
                continue
            claim = self._work_claims.try_claim(path)
            if claim is None:
                continue
            stop_scan = False
            retry_owned_generation: _ScreenshotWorkKey | None = None
            try:
                if self._manual_index.contains(claim.key):
                    continue
                if retry_window_active and claim.key not in selected_deferred_keys:
                    # Retry fairness must never change snapshot authority. A
                    # skipped newer generation remains unresolved, so preserve
                    # every older owned screenshot until a rotated pass classifies it.
                    authority_blocked = True
                    continue
                remaining -= 1
                decoded_key = claim.key
                decoded = self._decode_claim_generation(path, claim, decoded_key)
                if decoded is None:
                    if self._stopped.is_set():
                        return remaining, apply_closed, authority_blocked, deleted, True
                    continue
                result, decode_succeeded = decoded
                if self._stopped.is_set():
                    return remaining, apply_closed, authority_blocked, deleted, True
                generation_current = self._finalize_decode_result(
                    claim,
                    decoded_key,
                    result,
                    decode_succeeded=decode_succeeded,
                    flush=False,
                )
                source = self._source_from_stat(path, claim.stat_result)
                delete_current_marker = result.fragment is None
                if not generation_current:
                    pass
                elif result.decoder_unavailable:
                    if (
                        recent
                        and not apply_closed
                        and not authority_blocked
                        and not fragment_frontier_active
                    ):
                        if not self._emit_decode_failed(
                            path,
                            result.error_reason or "QR decoder unavailable",
                            source,
                        ):
                            return (
                                remaining,
                                apply_closed,
                                authority_blocked,
                                deleted,
                                True,
                            )
                        apply_closed = True
                    stop_scan = True
                elif result.scan_incomplete:
                    # A native/image exception does not establish ownership.
                    # Persist a separate deferred fingerprint (not a manual
                    # classification) so later starts advance through the
                    # backlog. Bound new failures per pass to avoid minutes of
                    # retry waits when the decoder itself is unavailable.
                    self._manual_index.note_deferred(decoded_key, flush=False)
                    if recent:
                        # A current listing discovered during startup should not
                        # require a companion restart after a transient decoder
                        # failure. Historical cleanup stays restart-bounded.
                        self._schedule_incomplete_scan_retry(path, source)
                    incomplete_scans += 1
                    authority_blocked = True
                    stop_scan = incomplete_scans >= _BACKLOG_INCOMPLETE_SCAN_LIMIT
                elif result.error_reason is not None and not result.has_marker:
                    if (
                        recent
                        and not apply_closed
                        and not authority_blocked
                        and not fragment_frontier_active
                    ):
                        if not self._emit_decode_failed(
                            path,
                            result.error_reason,
                            source,
                        ):
                            return (
                                remaining,
                                apply_closed,
                                authority_blocked,
                                deleted,
                                True,
                            )
                        apply_closed = True
                elif result.fragment is not None:
                    if authority_blocked:
                        delete_current_marker = False
                    elif not recent or apply_closed:
                        delete_current_marker = True
                    else:
                        outcome = self._fragment_assembler.accept_fragment(
                            self._fragment_with_source(result.fragment, source),
                            path,
                            now=self._fragment_clock(),
                        )
                        if outcome.error_reason is not None:
                            fragment_frontier_active = False
                            self._cancel_fragment_expiry()
                            if not self._emit_decode_failed(
                                path,
                                outcome.error_reason,
                                source,
                            ):
                                return (
                                    remaining,
                                    apply_closed,
                                    authority_blocked,
                                    deleted,
                                    True,
                                )
                            deleted += self._delete_retired_fragment_files(
                                outcome.retired_files
                            )
                            apply_closed = True
                        elif outcome.snapshot is not None:
                            fragment_frontier_active = False
                            self._cancel_fragment_expiry()
                            assembled_source = outcome.snapshot.source
                            is_fresh = (
                                snapshot_apply_cutoff_ns is not None
                                and assembled_source is not None
                                and assembled_source.mtime_ns
                                >= snapshot_apply_cutoff_ns
                            )
                            if is_fresh:
                                if not self._emit_snapshot(outcome.snapshot):
                                    return (
                                        remaining,
                                        apply_closed,
                                        authority_blocked,
                                        deleted,
                                        True,
                                    )
                                deleted += self._delete_retired_fragment_files(
                                    outcome.retired_files
                                )
                                _log.info(
                                    "backlog: applied assembled snapshot ending at %s",
                                    path.name,
                                )
                                apply_closed = True
                            else:
                                deleted += self._delete_retired_fragment_files(
                                    outcome.retired_files
                                )
                        else:
                            deleted += self._delete_retired_fragment_files(
                                outcome.retired_files
                            )
                            if outcome.accepted:
                                fragment_frontier_active = True
                                self._arm_fragment_expiry(result.fragment)
                elif result.snapshot is not None:
                    if (
                        deferred_failure is None
                        and not fragment_frontier_active
                        and not authority_blocked
                    ):
                        whole = self._snapshot_with_source(result.snapshot, source)
                        outcome = self._fragment_assembler.accept_snapshot(
                            whole,
                            now=self._fragment_clock(),
                        )
                        if outcome.accepted:
                            self._cancel_fragment_expiry()
                        deleted += self._delete_retired_fragment_files(
                            outcome.retired_files
                        )
                        is_fresh = (
                            recent
                            and snapshot_apply_cutoff_ns is not None
                            and source.mtime_ns >= snapshot_apply_cutoff_ns
                        )
                        if outcome.accepted and is_fresh and not apply_closed:
                            if not self._emit_snapshot(whole):
                                return (
                                    remaining,
                                    apply_closed,
                                    authority_blocked,
                                    deleted,
                                    True,
                                )
                            _log.info("backlog: applied snapshot from %s", path.name)
                            apply_closed = True
                elif (
                    result.has_marker
                    and recent
                    and not apply_closed
                    and not authority_blocked
                    and not fragment_frontier_active
                ):
                    if result.fragment_candidate:
                        if deferred_failure is None:
                            deferred_failure = (
                                path,
                                result.error_reason or "parse failed",
                                source,
                            )
                        _log.warning(
                            "backlog: newest recent ApScout v10 screenshot %s is "
                            "invalid; deferring failure while checking fragment retries",
                            path.name,
                        )
                    else:
                        if not self._emit_decode_failed(
                            path,
                            result.error_reason or "parse failed",
                            source,
                        ):
                            return (
                                remaining,
                                apply_closed,
                                authority_blocked,
                                deleted,
                                True,
                            )
                        apply_closed = True
                if (
                    generation_current
                    and result.has_marker
                    and delete_current_marker
                    and not authority_blocked
                    and (apply_closed or not fragment_frontier_active)
                ):
                    if self._stopped.is_set():
                        return remaining, apply_closed, authority_blocked, deleted, True
                    try:
                        if _unlink_if_source_matches(path, source):
                            deleted += 1
                        else:
                            _log.info(
                                "backlog preserved replacement screenshot: %s",
                                path.name,
                            )
                    except OSError as exc:
                        _log.warning(
                            "backlog could not delete %s: %s",
                            path.name,
                            exc,
                        )
                if (
                    generation_current
                    and authority_blocked
                    and not result.scan_incomplete
                    and (result.has_marker or result.transport_suspected)
                ):
                    # This owned generation was intentionally preserved behind
                    # a newer unresolved authority barrier. It must not enter
                    # the recent-work dedupe cache, or the coalesced rescan that
                    # follows barrier resolution cannot apply it in-process.
                    retry_owned_generation = decoded_key
            finally:
                claim.release()
            if retry_owned_generation is not None:
                self._work_claims.forget_recent_generation(retry_owned_generation)
            if claim.retry_requested and not self._stopped.is_set():
                self._on_new_file(path)
            if stop_scan:
                return remaining, apply_closed, authority_blocked, deleted, True
        if (
            deferred_failure is not None
            and not apply_closed
            and not authority_blocked
            and not self._stopped.is_set()
        ):
            failed_path, reason, source = deferred_failure
            if fragment_frontier_active:
                self._note_fragment_degraded_failure()
            if not self._emit_decode_failed(failed_path, reason, source):
                return remaining, apply_closed, authority_blocked, deleted, True
            apply_closed = True
        return remaining, apply_closed, authority_blocked, deleted, False

    def _decode_claim_generation(
        self,
        path: Path,
        claim: _ScreenshotWorkClaim,
        decoded_key: _ScreenshotWorkKey,
    ) -> tuple[DecodeResult, bool] | None:
        decode_succeeded = False
        try:
            result = _decode_screenshot_result(path)
            decode_succeeded = True
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "decode error before APS1 ownership for %s: %r",
                path.name,
                exc,
                exc_info=True,
            )
            # Treat an unexpected native/image exception like every other
            # transient scan failure. Returning a clean no-marker result here
            # permanently marked this exact file generation as processed.
            result = DecodeResult(
                None,
                False,
                "temporary screenshot decode failure",
                scan_incomplete=True,
            )
        retries = 0
        while (
            result.error_reason is not None
            and not result.has_marker
            and not result.decoder_unavailable
            and retries < _TRANSIENT_SCAN_MAX_RETRIES
        ):
            if self._stopped.wait(_TRANSIENT_SCAN_RETRY_DELAY_SECONDS):
                return None
            current_stat = claim.refresh()
            if current_stat is None:
                return None
            if claim.key != decoded_key:
                break
            retries += 1
            try:
                result = _decode_screenshot_result(path)
                decode_succeeded = True
            except Exception as exc:  # noqa: BLE001
                decode_succeeded = False
                _log.debug(
                    "transient screenshot retry failed for %s: %r",
                    path.name,
                    exc,
                    exc_info=True,
                )
                result = DecodeResult(
                    None,
                    False,
                    "temporary screenshot decode failure",
                    scan_incomplete=True,
                )
        if not decode_succeeded:
            # Repeated unexpected exceptions still do not prove that a manual
            # screenshot belongs to ApplicantScout. Preserve it silently after
            # the bounded retry, while leaving the generation unclassified.
            result = DecodeResult(None, False, scan_incomplete=True)
        return result, decode_succeeded

    def _finalize_decode_result(
        self,
        claim: _ScreenshotWorkClaim,
        decoded_key: _ScreenshotWorkKey,
        result: DecodeResult,
        *,
        decode_succeeded: bool,
        flush: bool,
    ) -> bool:
        current_stat = claim.refresh()
        if current_stat is not None and claim.key != decoded_key:
            _log.info(
                "screenshot changed during decode; retrying current generation: %s",
                claim.path.name,
            )
            claim.request_retry_for_changed_generation(decoded_key)
            return False
        was_deferred = decoded_key in self._manual_index.deferred_snapshot()
        if (
            decode_succeeded
            and current_stat is not None
            and not result.has_marker
            and not result.decoder_unavailable
            and result.error_reason is None
            and not result.transport_suspected
        ):
            self._manual_index.note_manual(decoded_key, flush=flush)
        elif not result.scan_incomplete:
            # A later process/live event may successfully classify a generation
            # that an earlier backlog pass deferred. Remove the cursor entry so
            # it cannot become a permanent blacklist.
            self._manual_index.forget_deferred(decoded_key, flush=flush)
        claim.mark_processed_generation(decoded_key)
        if was_deferred and not result.scan_incomplete:
            # A deferred newest screenshot is an authority barrier for every
            # older candidate. Once a later retry classifies that generation,
            # immediately resume the coalesced backlog so an older valid APS
            # snapshot cannot remain stuck until another filesystem event.
            self._request_backlog_rescan_after_deferred_resolution()
        return True

    def _on_new_file(self, path: Path) -> None:
        try:
            self._on_new_file_guarded(path)
        except Exception:  # noqa: BLE001 - never kill watchdog's dispatcher thread
            _log.exception(
                "screenshot observer callback failed for %s; watcher remains active",
                path.name,
            )

    def _on_new_file_guarded(
        self,
        path: Path,
        *,
        allow_incomplete_retry: bool = True,
    ) -> None:
        for _attempt in range(2):
            claim = self._work_claims.try_claim(path)
            if claim is None:
                return
            try:
                self._process_new_file(
                    path,
                    claim,
                    allow_incomplete_retry=allow_incomplete_retry,
                )
            finally:
                claim.release()
            if not claim.retry_requested:
                return
        self._schedule_generation_retry(path)

    def _schedule_generation_retry(self, path: Path) -> None:
        path_key = _normalized_work_path(path)
        with self._generation_retry_lock:
            if self._stopped.is_set() or path_key in self._generation_retry_timers:
                return
            timer_ref: list[Any] = []

            def retry() -> None:
                if timer_ref:
                    self._run_generation_retry(path, path_key, timer_ref[0])

            timer = self._generation_retry_timer_factory(
                _GENERATION_RETRY_DELAY_SECONDS,
                retry,
            )
            timer_ref.append(timer)
            self._generation_retry_timers[path_key] = timer
        self._set_timer_daemon(timer)
        try:
            timer.start()
        except Exception as exc:  # noqa: BLE001 - observer remains usable
            with self._generation_retry_lock:
                if self._generation_retry_timers.get(path_key) is timer:
                    self._generation_retry_timers.pop(path_key, None)
            _log.warning("could not start screenshot generation retry timer: %s", exc)

    def _run_generation_retry(self, path: Path, path_key: str, timer: Any) -> None:
        with self._generation_retry_lock:
            if self._generation_retry_timers.get(path_key) is not timer:
                return
            self._generation_retry_timers.pop(path_key, None)
        if not self._stopped.is_set():
            self._on_new_file(path)

    def _schedule_incomplete_scan_retry(
        self,
        path: Path,
        source: SnapshotSource,
    ) -> None:
        path_key = _normalized_work_path(path)
        expected_key = _ScreenshotWorkKey(path_key, source.mtime_ns, source.size)
        with self._incomplete_retry_lock:
            if self._stopped.is_set():
                return
            previous = self._incomplete_retry_timers.get(path_key)
            if previous is not None and previous[0] == expected_key:
                return
            timer_ref: list[Any] = []

            def retry() -> None:
                if timer_ref:
                    self._run_incomplete_scan_retry(
                        path,
                        path_key,
                        expected_key,
                        timer_ref[0],
                    )

            timer = self._generation_retry_timer_factory(
                _INCOMPLETE_SCAN_RETRY_DELAY_SECONDS,
                retry,
            )
            timer_ref.append(timer)
            self._incomplete_retry_timers[path_key] = expected_key, timer
        if previous is not None:
            previous[1].cancel()
        self._set_timer_daemon(timer)
        try:
            timer.start()
        except Exception as exc:  # noqa: BLE001 - observer remains usable
            with self._incomplete_retry_lock:
                if self._incomplete_retry_timers.get(path_key) == (
                    expected_key,
                    timer,
                ):
                    self._incomplete_retry_timers.pop(path_key, None)
            _log.warning("could not start incomplete screenshot retry timer: %s", exc)

    def _run_incomplete_scan_retry(
        self,
        path: Path,
        path_key: str,
        expected_key: _ScreenshotWorkKey,
        timer: Any,
    ) -> None:
        with self._incomplete_retry_lock:
            if self._incomplete_retry_timers.get(path_key) != (
                expected_key,
                timer,
            ):
                return
            self._incomplete_retry_timers.pop(path_key, None)
        if self._stopped.is_set():
            return
        try:
            current_key = _work_key_from_stat(path, path.stat())
        except OSError:
            return
        if current_key != expected_key:
            return
        self._work_claims.forget_recent_generation(expected_key)
        self._on_new_file_guarded(path, allow_incomplete_retry=False)

    def _dispatch_live_decode_result(
        self,
        path: Path,
        result: DecodeResult,
        source: SnapshotSource,
        *,
        marker_failure_reason: str,
        allow_incomplete_retry: bool,
    ) -> None:
        if result.scan_incomplete:
            if allow_incomplete_retry:
                _log.info(
                    "deferring one final incomplete screenshot scan for %s",
                    path.name,
                )
                self._schedule_incomplete_scan_retry(path, source)
            else:
                _log.info(
                    "preserving %s after final incomplete screenshot scan",
                    path.name,
                )
            return
        if result.decoder_unavailable:
            self._emit_decode_failed(
                path,
                result.error_reason or "QR decoder unavailable",
                source,
            )
            return

        if result.error_reason is not None and not result.has_marker:
            self._emit_decode_failed(path, result.error_reason, source)
            return

        if result.fragment is not None:
            outcome = self._fragment_assembler.accept_fragment(
                self._fragment_with_source(result.fragment, source),
                path,
                now=self._fragment_clock(),
            )
            if outcome.error_reason is not None:
                self._cancel_fragment_expiry()
                if not self._emit_decode_failed(path, outcome.error_reason, source):
                    return
                self._delete_retired_fragment_files(outcome.retired_files)
            elif outcome.snapshot is not None:
                self._cancel_fragment_expiry()
                if not self._emit_snapshot(outcome.snapshot):
                    return
                self._delete_retired_fragment_files(outcome.retired_files)
            else:
                self._delete_retired_fragment_files(outcome.retired_files)
                if outcome.accepted:
                    self._arm_fragment_expiry(result.fragment)
            # Valid incomplete fragments intentionally remain on disk and emit
            # no GUI signal. Completion/supersession/poison/TTL retires them.
            return

        snap = result.snapshot
        if snap is not None:
            whole = self._snapshot_with_source(snap, source)
            outcome = self._fragment_assembler.accept_snapshot(
                whole,
                now=self._fragment_clock(),
            )
            if outcome.accepted:
                self._cancel_fragment_expiry()
            self._delete_retired_fragment_files(outcome.retired_files)
            if outcome.accepted and not self._emit_snapshot(whole):
                return

        if not result.has_marker:
            if result.transport_suspected:
                _log.warning(
                    "skip %s — recovered top-left APS1 candidate was not "
                    "valid APS1 (preserved and left retryable)",
                    path.name,
                )
            else:
                _log.info(
                    "skip %s — no decodable APS1 marker (manual screenshot, preserved)",
                    path.name,
                )
            return
        if snap is None:
            if not self._emit_decode_failed(
                path,
                result.error_reason or marker_failure_reason,
                source,
            ):
                return
            _log.warning(
                "decode returned None for %s — APS1 marker FOUND but parse failed",
                path.name,
            )
        if self._stopped.is_set():
            return
        try:
            if not _unlink_if_source_matches(path, source):
                _log.info(
                    "preserved replacement screenshot after decode: %s",
                    path.name,
                )
        except OSError as exc:
            _log.warning("could not delete %s: %s", path.name, exc)

    def _decode_current_claim(
        self,
        path: Path,
        claim: _ScreenshotWorkClaim,
    ) -> tuple[DecodeResult, SnapshotSource, float] | None:
        decode_started = time.perf_counter()
        if claim.refresh() is None or self._manual_index.contains(claim.key):
            return None
        decoded_key = claim.key
        source = self._source_from_stat(path, claim.stat_result)
        decoded = self._decode_claim_generation(path, claim, decoded_key)
        if decoded is None:
            return None
        result, decode_succeeded = decoded
        if self._stopped.is_set():
            return None
        if not self._finalize_decode_result(
            claim,
            decoded_key,
            result,
            decode_succeeded=decode_succeeded,
            flush=True,
        ):
            return None
        return result, source, time.perf_counter() - decode_started

    def _process_new_file(
        self,
        path: Path,
        claim: _ScreenshotWorkClaim,
        *,
        allow_incomplete_retry: bool = True,
    ) -> None:
        """Called from watchdog observer thread. Decode + emit + cleanup.

        Cleanup logic (single pyzbar pass via decode_screenshot's tuple return):
        - parse succeeded (snap, marker=True) → delete (ours, applied)
        - parse failed but marker present (None, True) → delete (ours but
          corrupt — truncated write or transient image artifact; next snapshot
          in ≤0.5s will succeed)
        - no marker (None, False) → preserve (manual, unrelated QR, or an
          unreadable transport candidate)"""
        # INFO log on every screenshot arrival so user can verify watchdog is firing.
        if self._stopped.is_set():
            return
        if self._manual_index.contains(claim.key):
            return
        _log.info("new file: %s", path.name)
        wait_started = time.perf_counter()
        if not _wait_for_stable_size(path):
            if self._stopped.is_set():
                return
            wait_elapsed = time.perf_counter() - wait_started
            if wait_elapsed >= SLOW_SCREENSHOT_STAGE_LOG_S:
                _log.info(
                    "screenshot stable wait timed out for %s in %.2fs",
                    path.name,
                    wait_elapsed,
                )
            # Manual screenshots can be large/slow too. Only surface a health
            # failure when the timed-out file is actually an ApScout transport
            # image; unrelated screenshots must stay silent and preserved.
            decoded = self._decode_current_claim(path, claim)
            if decoded is None:
                return
            result, source, _decode_elapsed = decoded
            self._dispatch_live_decode_result(
                path,
                result,
                source,
                marker_failure_reason="size never stabilized",
                allow_incomplete_retry=allow_incomplete_retry,
            )
            return
        wait_elapsed = time.perf_counter() - wait_started
        decoded = self._decode_current_claim(path, claim)
        if decoded is None:
            return
        result, source, decode_elapsed = decoded
        if (
            wait_elapsed >= SLOW_SCREENSHOT_STAGE_LOG_S
            or decode_elapsed >= SLOW_SCREENSHOT_STAGE_LOG_S
        ):
            _log.info(
                "screenshot processed %s: stable_wait=%.2fs decode=%.2fs marker=%s",
                path.name,
                wait_elapsed,
                decode_elapsed,
                result.has_marker,
            )
        self._dispatch_live_decode_result(
            path,
            result,
            source,
            marker_failure_reason="parse failed",
            allow_incomplete_retry=allow_incomplete_retry,
        )


def positive_int_arg(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def system_exit_code(code: object) -> int:
    return code if isinstance(code, int) else 1


def _decode_file_cli(path: Path) -> int:
    result = _decode_screenshot_result(path)
    if result.snapshot is None:
        if result.decoder_unavailable:
            reason = result.error_reason or "QR decoder unavailable"
            print(f"DECODE FAILED — {reason}")
        elif result.has_marker:
            reason = result.error_reason or "parse error / CRC mismatch"
            print(f"DECODE FAILED — APS1 marker found but {reason}")
        else:
            print("DECODE FAILED — no QR / wrong magic")
        return 2
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    snap = result.snapshot
    print("DECODED OK:")
    print(f"  listing: {snap.listing}")
    print(f"  version: {snap.version}")
    print(f"  applicants ({len(snap.applicants)}):")
    for a in snap.applicants:
        print(
            f"    id={a.applicant_id} m={a.member_idx} cls={a.class_id} spec={a.spec_id} "
            f"ilvl={a.ilvl} score={a.score} main={a.main_score} "
            f"role={a.role} name={a.name!r}"
        )
    return 0


def _cleanup_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m applicant_scout.screenshot cleanup"
    )
    parser.add_argument("screenshots_dir")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--limit", type=positive_int_arg)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return system_exit_code(exc.code)
    try:
        summary = cleanup_appscout_screenshots(
            Path(args.screenshots_dir),
            delete=args.delete,
            limit=args.limit,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(format_screenshot_cleanup_summary(summary, delete=args.delete))
    return screenshot_cleanup_exit_code(summary)


def _main(argv: list[str] | None = None) -> int:
    if argv is None:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else list(argv)
    if not args:
        print("usage: python -m applicant_scout.screenshot <path-to-screenshot>")
        print(
            "       python -m applicant_scout.screenshot cleanup "
            "<ScreenshotsDir> [--delete] [--limit N]"
        )
        return 1
    if args[0] == "cleanup":
        return _cleanup_cli(args[1:])
    if len(args) != 1:
        print("usage: python -m applicant_scout.screenshot <path-to-screenshot>")
        return 1
    return _decode_file_cli(Path(args[0]))


# ─── CLI for standalone testing ─────────────────────────────────────────────
if __name__ == "__main__":
    raise SystemExit(_main())
