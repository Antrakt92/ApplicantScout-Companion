"""Screenshot-folder watcher + QR decoder for ApplicantScout transport.

Replaces the prior custom pixel-marker transport. Addon encodes binary payload
as a QR code via embedded lua-qrcode library, renders it in a frame anchored
TOPLEFT of UIParent, calls Screenshot() — image appears in _retail_/Screenshots/.
We watch that folder, decode QR via pyzbar (battle-tested zbar library —
handles QR Version ≥25 reliably, where opencv's QRCodeDetector empirically
failed for our 30-applicant payloads), parse bytes through the binary format
defined below, and emit a Snapshot via Qt signal.

Wire format mirrors `ApplicantScout.lua::BuildPayload` byte-for-byte:
header "APS1" + version + uint16 length + listing block + version block +
applicant array + CRC32 trailer. Pure binary, big-endian, see addon for spec.
QR is purely a transport layer over those bytes — Reed-Solomon ECC built into
QR handles JPG quantization noise, partial occlusion, and rotation. Current
addon builds prefer legacy hex text for already-fitting payloads and only fall
back to raw byte-mode QR when hex would overflow Version 40.

CRITICAL: only files whose QR successfully decodes AND magic matches "APS1"
are deleted. User's manual screenshots (no QR / unrelated QR / wrong magic)
are preserved.
"""

from __future__ import annotations

import logging
import os
import struct
import threading
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PIL import Image
from PyQt6.QtCore import QObject, pyqtSignal
from pyzbar.pyzbar import ZBarSymbol
from pyzbar.pyzbar import decode as pyzbar_decode
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


_log = logging.getLogger("applicant_scout.screenshot")


# ─── Wire format constants (must match addon's ApplicantScout.lua) ───────────
MAGIC = b"APS1"
# Allow-list of accepted wire versions. v0x01 = single member only (legacy);
# v0x02 = adds 1-byte member_idx between applicant_id and class_id, supports
# multi-member group apps (one block per member, all sharing applicant_id).
# v0x03 = adds listing category_id + difficulty_id.
# v0x04 = adds per-applicant RaiderIO main_score after current score.
# v0x05 = adds compact target-relative RaiderIO completion summary.
# Set, not a min/max range — future versions may be incompatible with v1 but compatible
# with v2; explicit allow-list is the cleanest contract.
WIRE_VERSIONS_SUPPORTED = {0x01, 0x02, 0x03, 0x04, 0x05}

STABLE_SIZE_TIMEOUT = 2.0  # seconds to wait for file size to stabilize
STABLE_SIZE_POLL = 0.05  # poll interval
SUPPORTED_SCREENSHOT_SUFFIXES = frozenset({".jpg", ".tga"})
QR_SCAN_CROP_PX = 720
SLOW_SCREENSHOT_STAGE_LOG_S = 0.75

# Cap startup-cleanup scan at most-recent N WoWScrnShot image files. Backlog scan
# runs on a daemon thread — no startup-latency impact on overlay paint —
# so the cap is now purely a leak-vector ceiling for pathological dev folders.
# 5000 covers years of casual use (~30-80ms per file × 5000 ≈ 4 min thread
# work, daemonised so it doesn't delay shutdown).
_BACKLOG_CLEANUP_LIMIT = 5000


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
class DecodedListing:
    activity_id: int
    key_level: int
    dungeon_name: str
    listing_name: str
    comment: str
    category_id: int = 0
    difficulty_id: int = 0


@dataclass
class DecodedVersion:
    addon_version: str
    game_version: str
    region_id: int  # 1=NA 2=KR 3=EU 4=TW 5=CN
    player_name: str  # "Charname-Realm"


@dataclass
class Snapshot:
    """Result of decoding one screenshot. listing=None means session ended."""

    listing: Optional[DecodedListing]
    version: Optional[DecodedVersion]
    applicants: list[DecodedApplicant] = field(default_factory=list)


@dataclass(frozen=True)
class DecodeResult:
    snapshot: Optional[Snapshot]
    has_marker: bool
    error_reason: Optional[str] = None


# ─── QR detection + payload extraction ──────────────────────────────────────
def _decode_qr_symbols(img: Image.Image) -> list[bytes]:
    try:
        results = pyzbar_decode(img, symbols=[ZBarSymbol.QRCODE])
    except Exception as e:
        _log.debug("pyzbar error: %s", e)
        return []
    return [bytes(r.data) for r in results]


def _has_appscout_symbol(payloads: list[bytes]) -> bool:
    return bool(_collect_appscout_qr_candidates(payloads))


def _decode_qr_symbol_data(image_path: Path) -> list[bytes]:
    """Return raw pyzbar symbol bytes from one screenshot image.

    pyzbar exposes zbar's payload pointer together with an explicit data length,
    so embedded NUL bytes survive intact. That lets us support both transport
    variants the addon may emit:
      * legacy hex text QR (decode via `bytes.fromhex(...)`)
      * raw APS1 byte-mode QR fallback for oversized payloads
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
                    return payloads
                full_payloads = _decode_qr_symbols(img)
                return full_payloads
            return _decode_qr_symbols(img)
    except (OSError, IOError) as e:
        _log.debug("Image.open failed %s: %s", image_path.name, e)
        return []


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


def _try_parse_appscout_payload(raw: bytes) -> tuple[Optional[Snapshot], Optional[str]]:
    """Validate and parse one already-identified APS1 payload candidate."""
    if len(raw) < 9:
        return None, "payload shorter than 9-byte header"

    wire_ver = raw[4]
    if wire_ver not in WIRE_VERSIONS_SUPPORTED:
        return None, f"unsupported wire version 0x{wire_ver:02x}"

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

    try:
        snap = _parse_payload(body[9:], wire_ver)  # skip 9-byte header
    except (IndexError, UnicodeDecodeError, struct.error, ValueError) as e:
        return None, f"parse error: {e}"
    return snap, None


def _parse_payload(buf: bytes, wire_ver: int = 0x01) -> Snapshot:
    """Cursor-based parse of body (already past 9-byte header). Returns Snapshot.
    Raises IndexError if buf truncated (caught by caller as decode failure).

    wire_ver gates block layout:
      * v0x01: legacy single-member applicants.
      * v0x02: adds applicant member_idx.
      * v0x03: adds listing category_id + difficulty_id.
      * v0x04: adds applicant main_score after current score.
      * v0x05: adds compact RaiderIO completion summary after main_score.
    """
    cursor = 0
    listing: Optional[DecodedListing] = None
    version: Optional[DecodedVersion] = None
    applicants: list[DecodedApplicant] = []

    # Listing block
    has_listing = buf[cursor]
    cursor += 1
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
        dn_len = buf[cursor]
        cursor += 1
        dungeon_name = buf[cursor : cursor + dn_len].decode("utf-8", errors="replace")
        cursor += dn_len
        ln_len = buf[cursor]
        cursor += 1
        listing_name = buf[cursor : cursor + ln_len].decode("utf-8", errors="replace")
        cursor += ln_len
        cm_len = buf[cursor]
        cursor += 1
        comment = buf[cursor : cursor + cm_len].decode("utf-8", errors="replace")
        cursor += cm_len
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
    has_version = buf[cursor]
    cursor += 1
    if has_version:
        av_len = buf[cursor]
        cursor += 1
        addon_version = buf[cursor : cursor + av_len].decode("ascii", errors="replace")
        cursor += av_len
        gv_len = buf[cursor]
        cursor += 1
        game_version = buf[cursor : cursor + gv_len].decode("ascii", errors="replace")
        cursor += gv_len
        region_id = buf[cursor]
        cursor += 1
        pn_len = buf[cursor]
        cursor += 1
        player_name = buf[cursor : cursor + pn_len].decode("utf-8", errors="replace")
        cursor += pn_len
        version = DecodedVersion(
            addon_version=addon_version,
            game_version=game_version,
            region_id=region_id,
            player_name=player_name,
        )

    # Applicants array. LFG max is ~70 in practice; sane upper bound 200.
    count = struct.unpack(">H", buf[cursor : cursor + 2])[0]
    cursor += 2
    if count > 200:
        raise ValueError(f"applicant_count {count} exceeds sane limit 200")
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
            rio_profile = buf[cursor] > 0
            cursor += 1
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
        role = buf[cursor]
        cursor += 1
        n_len = buf[cursor]
        cursor += 1
        name = buf[cursor : cursor + n_len].decode("utf-8", errors="replace")
        cursor += n_len
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

    if cursor != len(buf):
        raise ValueError(
            f"trailing or truncated payload bytes: consumed {cursor} of {len(buf)}"
        )

    return Snapshot(listing=listing, version=version, applicants=applicants)


def _decode_screenshot_result(image_path: Path) -> DecodeResult:
    """Decode and parse a screenshot image with diagnostics.

    has_marker=True when the image's QR contained the APS1 magic, REGARDLESS of
    whether the rest of the payload parsed cleanly. snapshot=None with
    has_marker=True means our file but corrupt (truncated write / version
    skew / CRC mismatch) — caller should still delete it; the next snapshot
    in ≤0.5s will succeed.

    Single pyzbar call per image: callers that previously did
    `decode_screenshot(p) ... if not snap and _has_marker(p): unlink()` would
    pay TWO ~30-80ms decodes per failure. The tuple return collapses to one.
    """
    symbol_payloads = _decode_qr_symbol_data(image_path)
    candidates = _collect_appscout_qr_candidates(symbol_payloads)
    if not candidates:
        return DecodeResult(None, False)  # no QR / unrelated QR

    first_error: Optional[str] = None
    for kind, raw in candidates:
        snap, err = _try_parse_appscout_payload(raw)
        if snap is not None:
            wire_ver = raw[4]
            # Diagnostic: confirms which wire version we just parsed. v0x01 =
            # leader-only (legacy); v0x02 = multi-member groups; v0x03 =
            # listing context. If you reload the addon and still see an older
            # wire version, you're likely processing a stale screenshot taken
            # before the addon update.
            _log.info(
                "decoded %s: mode=%s wire=0x%02x apps=%d",
                image_path.name,
                kind,
                wire_ver,
                len(snap.applicants),
            )
            return DecodeResult(snap, True)
        if err is not None:
            if first_error is None:
                first_error = f"{kind}: {err}"
            _log.debug("candidate rejected in %s (%s): %s", image_path.name, kind, err)

    if first_error is not None:
        _log.warning("decode failed in %s: %s", image_path.name, first_error)
    return DecodeResult(None, True, first_error or "parse failed")


def decode_screenshot(image_path: Path) -> tuple[Optional[Snapshot], bool]:
    """Decode and parse a screenshot image. Returns (snapshot, has_marker).

    Compatibility wrapper for callers/tests that only need the historical
    cleanup discriminator. Use _decode_screenshot_result when the caller needs
    a user-visible failure reason.
    """
    result = _decode_screenshot_result(image_path)
    return result.snapshot, result.has_marker


def _has_marker(image_path: Path) -> bool:
    """Cheap "is this our screenshot?" check — used by paths that don't need
    the parsed Snapshot, only the cleanup discriminator. Single pyzbar call.
    """
    return _decode_screenshot_result(image_path).has_marker


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


class _Handler(FileSystemEventHandler):
    """Filters JPG/TGA file events, dedups across on_created/on_modified/on_moved
    and dispatches to callback. Listening to all three event types because:

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
        # Dedup keyed on (path, mtime) to coalesce on_created+on_modified+on_moved
        # pairs for the SAME write while still re-processing a file that was
        # deleted+recreated with the same filename (mtime changes → different key).
        # Critical: WoW Screenshot() filenames have only HH:MM:SS resolution, so
        # rapid shots in the same second collide on filename. If companion deletes
        # shot 1's file and WoW writes shot 2 with the same filename, mtime jumps
        # forward, key changes, dedup correctly admits the second event.
        self._recent_keys: dict[tuple[str, float], float] = {}

    def _should_process(self, path: Path) -> bool:
        if not _is_supported_screenshot_path(path):
            return False
        try:
            mtime = path.stat().st_mtime
        except OSError:
            # File vanished between event and stat (we just unlinked it). Don't
            # try to process — but also don't block future events for this path.
            return False
        key = (str(path), mtime)
        now = time.time()
        # Evict entries older than 3s
        self._recent_keys = {
            k: t for k, t in self._recent_keys.items() if now - t < 3.0
        }
        if key in self._recent_keys:
            return False
        self._recent_keys[key] = now
        return True

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


class ScreenshotWatcher(QObject):
    """Watches Screenshots/ folder via watchdog Observer. On each new JPG/TGA:
    waits for write to complete, decodes QR, emits snapshotReceived(Snapshot)
    on success. Deletes the file if it carries our APS1 marker. Skips delete
    if no marker (preserves user's manual screenshots and unrelated QR codes).

    On startup: scans Screenshots/ for files <60s old and applies the most
    recent valid snapshot — handles 'companion started mid-session' case where
    addon already emitted but we missed the watchdog event."""

    snapshotReceived = pyqtSignal(object)  # Snapshot
    decodeFailed = pyqtSignal(str, str)  # path, reason

    def __init__(self, screenshots_dir: Path, parent=None):
        super().__init__(parent)
        self._dir = screenshots_dir
        self._observer: Optional[Any] = None
        self._backlog_thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()

    def start(self) -> None:
        self._stopped.clear()
        # Ensure folder exists (WoW creates it on first screenshot, but companion
        # may start before WoW ever takes one)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Observer first so any new file arriving during the (slow) backlog
        # scan still gets routed through _on_new_file. The dedup keys in
        # _Handler prevent double-processing if observer + backlog race on
        # the same path.
        observer = Observer()
        observer.schedule(_Handler(self._on_new_file), str(self._dir), recursive=False)
        observer.start()
        self._observer = observer
        _log.info("watching %s", self._dir)
        # Backlog scan on a background thread — for users with hundreds of
        # historical WoWScrnShot JPG/TGA files, the synchronous scan was the
        # dominant startup-latency contributor (~30-80 ms per file × 500 file
        # cap = up to ~30s). Overlay now appears immediately. snapshotReceived
        # is a Qt pyqtSignal — emits cross thread are queued safely to the GUI
        # thread by Qt's signal/slot machinery.
        t = threading.Thread(
            target=self._scan_recent_backlog,
            name="ApplicantScoutBacklogScan",
            daemon=True,
        )
        t.start()
        self._backlog_thread = t

    def stop(self) -> None:
        self._stopped.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        # Backlog thread is daemon=True so process exit doesn't wait for it.
        # We don't .join here: it may be in the middle of a 30-80 ms pyzbar
        # call we can't interrupt cleanly. Daemonised so it dies with us.

    def _emit_snapshot(self, snap: Snapshot) -> None:
        if not self._stopped.is_set():
            self.snapshotReceived.emit(snap)

    def _emit_decode_failed(self, path: Path, reason: str) -> None:
        if not self._stopped.is_set():
            self.decodeFailed.emit(str(path), reason)

    def _scan_recent_backlog(self) -> None:
        """Startup cleanup pass over WoWScrnShot JPG/TGA files. Two jobs:
        (1) emit most recent valid snapshot from last 60s — handles
        'companion started mid-session' case where addon already painted
        applicant data but we missed the watchdog event;
        (2) delete ALL marker-bearing files regardless of age — recovers from
        prior runs where companion crashed/exited before file delete, or where
        decoder failed mid-stream leaving the file untouched.

        Bounded at _BACKLOG_CLEANUP_LIMIT files for pathological cases (user
        with thousands of historical screenshots). User's manual screenshots
        are never touched — APS1 magic check is the discriminator."""
        if self._stopped.is_set():
            return
        now = time.time()
        apply_cutoff = now - 60
        all_files: list[tuple[Path, float]] = []
        for p in _iter_screenshot_candidates(self._dir):
            try:
                all_files.append((p, p.stat().st_mtime))
            except OSError:
                continue
        if not all_files:
            return
        all_files.sort(key=lambda t: t[1], reverse=True)
        if len(all_files) > _BACKLOG_CLEANUP_LIMIT:
            all_files = all_files[:_BACKLOG_CLEANUP_LIMIT]

        apply_closed = False
        deleted = 0
        for p, mtime in all_files:
            if (
                not apply_closed
                and mtime >= apply_cutoff
                and not _wait_for_stable_size(p)
            ):
                _log.info(
                    "backlog: skipping unstable recent screenshot %s",
                    p.name,
                )
                apply_closed = True
                continue
            # Single decode pass. Within apply window AND not yet applied → emit
            # the parsed snapshot. Outside window OR apply closed → still
            # delete-if-marker (cleanup leftover ours from prior crashes). The
            # tuple return collapses what was a two-pyzbar-call sequence into
            # one — meaningful at 500 files × 30-80 ms saved per file.
            try:
                result = _decode_screenshot_result(p)
            except Exception as e:
                _log.warning("backlog decode error %s: %s", p.name, e)
                result = DecodeResult(None, False)
            if (
                result.snapshot is not None
                and not apply_closed
                and mtime >= apply_cutoff
                and not self._stopped.is_set()
            ):
                self._emit_snapshot(result.snapshot)
                _log.info("backlog: applied snapshot from %s", p.name)
                apply_closed = True
            elif result.has_marker and not apply_closed and mtime >= apply_cutoff:
                self._emit_decode_failed(p, result.error_reason or "parse failed")
                _log.warning(
                    "backlog: newest recent ApScout screenshot %s has marker but no "
                    "snapshot; suppressing older startup fallback",
                    p.name,
                )
                apply_closed = True
            if result.has_marker:
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass
        if deleted:
            _log.info("backlog cleanup: deleted %d ApScout screenshots", deleted)

    def _on_new_file(self, path: Path) -> None:
        """Called from watchdog observer thread. Decode + emit + cleanup.

        Cleanup logic (single pyzbar pass via decode_screenshot's tuple return):
        - parse succeeded (snap, marker=True) → delete (ours, applied)
        - parse failed but marker present (None, True) → delete (ours but
          corrupt — truncated write or transient image artifact; next snapshot
          in ≤0.5s will succeed)
        - no marker (None, False) → preserve (user's manual screenshot or
          unrelated QR code)"""
        # INFO log on every screenshot arrival so user can verify watchdog is firing.
        if self._stopped.is_set():
            return
        _log.info("new file: %s", path.name)
        wait_started = time.perf_counter()
        if not _wait_for_stable_size(path):
            wait_elapsed = time.perf_counter() - wait_started
            if wait_elapsed >= SLOW_SCREENSHOT_STAGE_LOG_S:
                _log.info(
                    "screenshot stable wait timed out for %s in %.2fs",
                    path.name,
                    wait_elapsed,
                )
            self._emit_decode_failed(path, "size never stabilized")
            # Even with unstable size the QR may still decode; if ours, drop it.
            if _has_marker(path):
                try:
                    path.unlink()
                except OSError:
                    pass
            return
        wait_elapsed = time.perf_counter() - wait_started
        decode_started = time.perf_counter()
        try:
            result = _decode_screenshot_result(path)
        except Exception as e:
            self._emit_decode_failed(path, repr(e))
            result = DecodeResult(None, False)
        decode_elapsed = time.perf_counter() - decode_started
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
        snap = result.snapshot
        marker = result.has_marker
        if snap is not None:
            self._emit_snapshot(snap)
        if marker:
            if snap is None:
                self._emit_decode_failed(path, result.error_reason or "parse failed")
                _log.warning(
                    "decode returned None for %s — APS1 marker FOUND but parse failed",
                    path.name,
                )
            try:
                path.unlink()
            except OSError as e:
                _log.warning("could not delete %s: %s", path.name, e)
        else:
            _log.info(
                "skip %s — no APS1 marker (manual screenshot, preserved)",
                path.name,
            )


# ─── CLI for standalone testing ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m applicant_scout.screenshot <path-to-screenshot>")
        sys.exit(1)
    result = _decode_screenshot_result(Path(sys.argv[1]))
    if result.snapshot is None:
        if result.has_marker:
            reason = result.error_reason or "parse error / CRC mismatch"
            print(f"DECODE FAILED — APS1 marker found but {reason}")
        else:
            print("DECODE FAILED — no QR / wrong magic")
        sys.exit(2)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
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
