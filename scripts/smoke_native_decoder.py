"""Exercise an isolated Windows libiconv/ZBar pair without modifying site-packages.

This is a runtime compatibility smoke, not a license or reproducible-build check.
All native calls run in fresh subprocesses; a crashing DLL cannot crash the caller.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import Any
import zlib


ROOT = Path(__file__).resolve().parents[1]
LUA_RENDER = """local ns = {}
assert(loadfile(arg[1]))("ApplicantScout", ns)
local f = assert(io.open(arg[2], "rb"))
local payload = f:read("*a"); f:close()
local ok, matrix = ns.QR.qrcode(payload, 1)
assert(ok, matrix)
for y = 1, #matrix do
    for x = 1, #matrix do io.write(matrix[x][y] > 0 and "1" or "0") end
    io.write("\\n")
end
"""


class SmokeError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_file(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    require(resolved.is_file() and resolved.stat().st_size > 0, f"{label}: expected a nonempty file: {path}")
    return resolved


def frame(body: bytes, version: int, flags: int = 0) -> bytes:
    data = b"APS1" + struct.pack(">BHBB", version, len(body) + 13, flags, 0) + body
    return data + struct.pack(">I", zlib.crc32(data))


def qr_cases() -> list[tuple[str, bytes, bytes, str]]:
    """Wire-shaped fixtures plus the addon's captured v9 golden payload.

    The native decoder must preserve bytes; parser validity is covered separately.
    Raw NUL tests the decoder boundary separately from the addon's hex transport.
    """
    golden = bytes.fromhex((ROOT / "tests/fixtures/aps1_v9_lua_golden.hex").read_text().strip())
    cases: list[tuple[str, bytes, bytes, str]] = []
    for version in (8, 9, 11):
        payload = frame(golden[9:-4], version).hex().encode("ascii")
        cases.append((f"aps-v{version}-hex", payload, payload, "exact"))
    rows = []
    for index in range(24):
        name = f"Игрок{index}-Гордунни".encode("utf-8")
        rows.append(struct.pack(">IBBHHHH", index + 1, 1, 8, 64, 280, 2000 + index, 0)
                    + bytes(8) + bytes([2, len(name)]) + name)
    large = frame(bytes(3) + struct.pack(">H", len(rows)) + b"".join(rows) + bytes(2), 9)
    payload = large.hex().encode("ascii")
    cases.append(("aps-large-cyrillic-hex", payload, payload, "exact"))
    count = (len(large) + 639) // 640
    for index in range(count):
        metadata = struct.pack(">IIHHHI", 17, 23, index, count, len(large),
                               struct.unpack(">I", large[-4:])[0])
        payload = frame(metadata + large[index * 640:(index + 1) * 640], 10).hex().encode("ascii")
        cases.append((f"aps-v10-fragment-{index}", payload, payload, "exact"))
    for name, payload in (("utf8-cyrillic", "Игрок-Гордунни".encode()),
                          ("latin1", "café".encode("latin1")),
                          ("sjis", "日本語".encode("shift_jis"))):
        encoding = {"latin1": "latin1", "sjis": "shift_jis"}.get(name, "utf-8")
        cases.append((name, payload, payload.decode(encoding).encode("utf-8"), "exact"))
    cases.append(("raw-nul-boundary", b"APS1\x00raw-fallback", b"APS1\x00raw-fallback", "exact"))
    return cases


def render_cases(stage: Path, lua: Path, qrencode: Path) -> None:
    from PIL import Image

    script = stage / "render.lua"
    script.write_text(LUA_RENDER, encoding="utf-8")
    records = []
    for name, payload, expected, behavior in qr_cases():
        source = stage / "payload.bin"
        source.write_bytes(payload)
        result = subprocess.run([str(lua), str(script), str(qrencode), str(source)],
                                capture_output=True, text=True, timeout=90, check=False)
        require(result.returncode == 0, f"QR generation failed for {name}: {result.stderr}")
        rows = result.stdout.splitlines()
        require(bool(rows) and all(len(row) == len(rows) and set(row) <= {"0", "1"} for row in rows),
                f"Invalid QR matrix for {name}")
        width = len(rows) + 8
        pixels = bytearray([255] * (width * width))
        for y, row in enumerate(rows):
            for x, cell in enumerate(row):
                pixels[(y + 4) * width + x + 4] = 0 if cell == "1" else 255
        image = Image.frombytes("L", (width, width), bytes(pixels))
        # Integer and fractional module sizes exercise actual scaled captures.
        for scale in (4, 3.5):
            filename = f"{name}-{scale}.png"
            size = round(width * scale)
            image.resize((size, size), Image.Resampling.NEAREST).save(stage / filename)
            records.append({"name": f"{name}-{scale}", "image": filename,
                            "input_hex": payload.hex(), "expected_hex": expected.hex(), "behavior": behavior})
    (stage / "qr-cases.json").write_text(json.dumps(records), encoding="utf-8")


def module_path(handle: int) -> Path:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.GetModuleFileNameW
    function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(handle, buffer, len(buffer))
    require(0 < length < len(buffer), "GetModuleFileNameW failed or truncated its result")
    return Path(buffer.value).resolve(strict=True)


def load_pair(stage: Path) -> tuple[Any, Any]:
    require(os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8, "Native smoke requires Windows x64 Python")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    for name in ("libiconv.dll", "libzbar-64.dll"):
        require(not kernel.GetModuleHandleW(name), f"Unexpected preloaded {name}; isolation was lost")
    iconv = ctypes.CDLL(str(stage / "libiconv.dll"))
    zbar = ctypes.CDLL(str(stage / "libzbar-64.dll"))
    for library, name in ((iconv, "libiconv.dll"), (zbar, "libzbar-64.dll")):
        require(module_path(library._handle) == (stage / name).resolve(), f"Loaded {name} outside isolated directory")
    return iconv, zbar


def conversions(library: Any) -> list[dict[str, Any]]:
    pointer = ctypes.c_void_p
    size = ctypes.c_size_t
    failure = size(-1).value
    library.libiconv_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    library.libiconv_open.restype = pointer
    library.libiconv.argtypes = [pointer, ctypes.POINTER(pointer), ctypes.POINTER(size),
                               ctypes.POINTER(pointer), ctypes.POINTER(size)]
    library.libiconv.restype = size
    library.libiconv_close.argtypes = [pointer]
    library.libiconv_close.restype = ctypes.c_int
    require(ctypes.c_int.in_dll(library, "_libiconv_version").value == 0x010E, "Expected GNU libiconv 1.14 version data")
    library.iconv_canonicalize.argtypes = [ctypes.c_char_p]
    library.iconv_canonicalize.restype = ctypes.c_char_p
    require(library.iconv_canonicalize(b"utf-8") == b"UTF-8", "Canonical encoding name differs")
    require(library.libiconv_open(b"invalid-encoding", b"UTF-8") == pointer(-1).value,
            "Unknown encoding did not reject the descriptor")
    names: list[bytes] = []
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_char_p), pointer)

    def collect_names(count: int, values: Any, _context: Any) -> int:
        names.extend(values[index] for index in range(count))
        return 0

    callback = callback_type(collect_names)
    library.libiconvlist.argtypes = [callback_type, pointer]
    library.libiconvlist.restype = None
    library.libiconvlist(callback, None)
    require(b"UTF-8" in names and b"CP1251" in names, "Encoding enumeration is incomplete")

    def convert(name: str, source: bytes, target: bytes, data: bytes, expected: bytes,
                capacity: int = 256, invalid: bool = False, consumed_before_error: int = 0) -> dict[str, Any]:
        descriptor = library.libiconv_open(target, source)
        require(descriptor not in (None, pointer(-1).value), f"{name}: iconv_open failed")
        try:
            source_buffer = ctypes.create_string_buffer(data)
            target_buffer = ctypes.create_string_buffer(max(capacity, 1))
            source_ptr, target_ptr = pointer(ctypes.addressof(source_buffer)), pointer(ctypes.addressof(target_buffer))
            source_left, target_left = size(len(data)), size(capacity)
            result = library.libiconv(descriptor, ctypes.byref(source_ptr), ctypes.byref(source_left),
                                     ctypes.byref(target_ptr), ctypes.byref(target_left))
            consumed, produced = len(data) - source_left.value, capacity - target_left.value
            output = target_buffer.raw[:produced]
            require(result == (failure if invalid else 0), f"{name}: unexpected conversion return {result}")
            require(output == expected, f"{name}: output differs: {output.hex()}")
            require(consumed == (consumed_before_error if invalid else len(data)), f"{name}: unexpected input consumption")
            require(source_ptr.value == ctypes.addressof(source_buffer) + consumed and
                    target_ptr.value == ctypes.addressof(target_buffer) + produced, f"{name}: pointer updates differ")
            require(library.libiconv(descriptor, None, None, None, None) == 0, f"{name}: reset failed")
            source_ptr.value, target_ptr.value = ctypes.addressof(source_buffer), ctypes.addressof(target_buffer)
            source_left.value, target_left.value = len(data), capacity
            repeated = library.libiconv(descriptor, ctypes.byref(source_ptr), ctypes.byref(source_left),
                                       ctypes.byref(target_ptr), ctypes.byref(target_left))
            require(repeated == result and len(data) - source_left.value == consumed and
                    capacity - target_left.value == produced and target_buffer.raw[:produced] == output,
                    f"{name}: descriptor reuse after reset differs")
            return {"name": name, "output_hex": output.hex(), "result": result,
                    "input_left": source_left.value, "output_left": target_left.value}
        finally:
            require(library.libiconv_close(descriptor) == 0, f"{name}: close failed")

    text = "Игрок café 日本語"
    results = [convert("utf8-identity", b"UTF-8", b"UTF-8", text.encode(), text.encode()),
               convert("utf8-utf16le", b"UTF-8", b"UTF-16LE", text.encode(), text.encode("utf-16le")),
               convert("utf16le-utf8", b"UTF-16LE", b"UTF-8", text.encode("utf-16le"), text.encode()),
               convert("empty", b"UTF-8", b"UTF-8", b"", b"")]
    for name, encoding, text in (("cyrillic", "CP1251", "Игрок-Гордунни"),
                                 ("latin1", "ISO-8859-1", "café"), ("sjis", "SHIFT_JIS", "日本語")):
        results.append(convert(name, encoding.encode(), b"UTF-8", text.encode(encoding), text.encode()))
        results.append(convert(name + "-reverse", b"UTF-8", encoding.encode(), text.encode(), text.encode(encoding)))
    for name, data, capacity in (("invalid-utf8", b"\xff", 64), ("incomplete-utf8", b"\xe2\x82", 64),
                                  ("output-exhausted", b"A", 1), ("zero-output", b"A", 0)):
        results.append(convert(name, b"UTF-8", b"UTF-16LE", data, b"", capacity, True))
    results.append(convert("invalid-after-prefix", b"UTF-8", b"UTF-16LE", b"A\xff", b"A\x00", 64, True, 1))
    results.append(convert("output-after-prefix", b"UTF-8", b"UTF-16LE", b"AB", b"A\x00", 2, True, 1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        repeated = list(pool.map(lambda index: convert(f"thread-{index}", b"CP1251", b"UTF-8",
                                                       "Игрок".encode("cp1251"), "Игрок".encode()), range(32)))
    results.append({"name": "concurrent-descriptors", "passed": len(repeated)})
    results.append({"name": "encoding-enumeration", "names": sorted(value.decode("ascii") for value in names)})
    return results


def child(stage: Path) -> dict[str, Any]:
    iconv, zbar = load_pair(stage)
    converted = conversions(iconv)
    from pyzbar import zbar_library
    # WHY: bind the loader before wrapper.py creates any native function pointers.
    zbar_library.load = lambda: (zbar, [iconv])
    from pyzbar.pyzbar import decode, ZBarSymbol
    from PIL import Image

    decoded = []
    for case in json.loads((stage / "qr-cases.json").read_text(encoding="utf-8")):
        with Image.open(stage / case["image"]) as image:
            values = [item.data.hex() for item in decode(image, symbols=[ZBarSymbol.QRCODE])]
        require(values == [case["expected_hex"]], f"{case['name']}: QR output differs: {values}")
        decoded.append({"name": case["name"], "decoded_hex": values[0], "behavior": case["behavior"]})
    return {"passed": True, "loaded": {name: {"path": str(module_path(library._handle)),
                                               "sha256": digest(module_path(library._handle))}
                                       for name, library in (("libiconv.dll", iconv), ("libzbar-64.dll", zbar))},
            "conversions": converted, "qr": decoded}


def run_smoke(libiconv: Path, zbar: Path, qrencode: Path, lua: Path, *, runs: int = 2) -> dict[str, Any]:
    libiconv, zbar = input_file(libiconv, "libiconv"), input_file(zbar, "ZBar")
    require(libiconv != zbar, "libiconv and ZBar must be different files")
    qrencode, lua = input_file(qrencode, "QR encoder"), input_file(lua, "Lua executable")
    require(1 <= runs <= 5, "runs must be between 1 and 5")
    require(os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8, "Native smoke requires Windows x64 Python")
    expected = {"libiconv.dll": digest(libiconv), "libzbar-64.dll": digest(zbar)}
    with tempfile.TemporaryDirectory(prefix="applicant-scout-native-smoke-") as temporary:
        stage = Path(temporary).resolve()
        shutil.copyfile(libiconv, stage / "libiconv.dll")
        shutil.copyfile(zbar, stage / "libzbar-64.dll")
        require(all(digest(stage / name) == value for name, value in expected.items()), "Staged DLL hash mismatch")
        render_cases(stage, lua, qrencode)
        results = []
        for _ in range(runs):
            process = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--child-stage", str(stage)],
                                     cwd=stage, capture_output=True, text=True, timeout=120, check=False)
            require(process.returncode == 0, f"Native child failed ({process.returncode}): {process.stdout} {process.stderr}")
            result = json.loads(process.stdout)
            require(isinstance(result, dict), "Native child returned a non-object result")
            require(result.get("passed") is True, "Native child did not report success")
            for name, value in expected.items():
                require(result["loaded"][name]["sha256"] == value and
                        Path(result["loaded"][name]["path"]).resolve() == stage / name,
                        f"Native child loaded an unexpected {name}")
            results.append(result)
        require(all(result["conversions"] == results[0]["conversions"] and result["qr"] == results[0]["qr"]
                    for result in results), "Native results differ between fresh processes")
    return {"passed": True, "inputs": {"libiconv": str(libiconv), "zbar": str(zbar)}, "runs": results,
            "limits": ["No private-CRT errno assertion", "QR tests do not exercise the live WoW screenshot path",
                       "Runtime compatibility does not establish source or license completeness"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--libiconv", type=Path)
    parser.add_argument("--zbar", type=Path)
    parser.add_argument("--qrencode", type=Path, default=ROOT.parent / "ApplicantScout-Addon/libs/qrencode.lua")
    parser.add_argument("--lua", type=Path, default=Path(shutil.which("lua5.1") or "lua5.1"))
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--child-stage", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child_stage is None and (args.libiconv is None or args.zbar is None):
        parser.error("--libiconv and --zbar are required")
    try:
        result = child(args.child_stage.resolve(strict=True)) if args.child_stage else run_smoke(
            args.libiconv, args.zbar, args.qrencode, args.lua, runs=args.runs)
    except (OSError, SmokeError, ValueError, KeyError, ImportError, subprocess.SubprocessError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
