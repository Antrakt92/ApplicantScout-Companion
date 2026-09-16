"""Prepare pinned GNU sources for an isolated, non-shipping Windows DLL build.

Only Python's standard library is needed. No compiler or downloaded script is
executed here. The CLI permits fresh output directories under build/native-decoder.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
from urllib.request import urlopen


SOURCE_URL = "https://ftp.gnu.org/pub/gnu/libiconv/libiconv-1.14.tar.gz"
SOURCE_SHA256 = "72b24ded17d687193c3366d0ebe7cde1e6b18f0df8c55438ac95be39e8a30613"
SOURCE_ROOT = "libiconv-1.14"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
SOURCES = ("lib/iconv.c", "libcharset/lib/localcharset.c", "lib/relocatable.c")
EXPORTS = (
    "_libiconv_version @1 DATA", "iconv_canonicalize @2", "libiconv @3",
    "libiconv_close @4", "libiconv_open @5", "libiconv_open_into @6",
    "libiconvctl @7", "libiconvlist @8", "locale_charset @9",
)
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", re.I)


class PreparationError(ValueError):
    """Source identity, archive layout or a template was unexpected."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def candidate_output(requested: Path, repo_root: Path) -> Path:
    repo_root = repo_root.resolve()
    allowed = repo_root / "build" / "native-decoder"
    output = requested if requested.is_absolute() else repo_root / requested
    output = output.absolute()
    resolved = output.resolve()
    # Reject redirected build roots and ancestor junctions, not just '..' escapes.
    if allowed.resolve() != allowed or resolved != output:
        raise PreparationError("output must not traverse links or redirected directories")
    if not resolved.is_relative_to(allowed):
        raise PreparationError("output must be under build/native-decoder")
    if resolved.exists():
        raise PreparationError("output already exists; choose a fresh candidate directory")
    return resolved


def checked_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > 5000 or sum(member.size for member in members) > MAX_SOURCE_BYTES:
        raise PreparationError("archive exceeds source size limits")
    seen: set[str] = set()
    for member in members:
        name = member.name.rstrip("/")
        path = PurePosixPath(name)
        parts = name.split("/")
        if (
            not name or path.is_absolute() or parts[0] != SOURCE_ROOT
            or any(part in ("", ".", "..") for part in parts)
            or any("\\" in part or ":" in part or part.endswith((".", " "))
                   or _RESERVED.fullmatch(part) for part in parts)
            or not (member.isdir() or member.isfile())
        ):
            raise PreparationError(f"unsafe source archive member: {member.name!r}")
        key = name.casefold()
        if key in seen:
            raise PreparationError(f"duplicate source archive path: {member.name!r}")
        seen.add(key)
    files = {member.name.casefold() for member in members if member.isfile()}
    for member in members:
        if any(str(parent).casefold() in files for parent in PurePosixPath(member.name).parents):
            raise PreparationError(f"archive file used as a directory: {member.name!r}")
    return members


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise PreparationError(f"expected exactly one template anchor: {old!r}")
    return text.replace(old, new)


def generated_headers(config_template: str, iconv_template: str,
                      localcharset_template: str) -> dict[str, str]:
    config = replace_once(config_template, "#undef EILSEQ\n", "")
    config = replace_once(config, "#undef ICONV_CONST\n", "#define ICONV_CONST const\n")
    for macro in (
        "ENABLE_EXTRA", "ENABLE_RELOCATABLE", "ENABLE_NLS", "HAVE_WCHAR_T",
        "HAVE_MBSTATE_T", "HAVE_MBRTOWC", "HAVE_WCRTOMB", "HAVE_LANGINFO_CODESET",
    ):
        config = replace_once(config, f"#undef {macro}\n", f"#define {macro} 0\n")
    iconv = replace_once(
        iconv_template, "#ifndef EILSEQ\n#define EILSEQ @EILSEQ@\n#endif",
        '#ifndef EILSEQ\n#error "MSVC errno.h must define EILSEQ"\n#endif',
    )
    substitutions = {
        "@DLL_VARIABLE@": ("", 1), "@ICONV_CONST@": ("const", 1),
        "@USE_MBSTATE_T@": ("0", 2), "@BROKEN_WCHAR_H@": ("0", 1),
        "@HAVE_WCHAR_T@": ("0", 1),
    }
    for token, (value, count) in substitutions.items():
        if iconv.count(token) != count:
            raise PreparationError(f"unexpected substitution count: {token}")
        iconv = iconv.replace(token, value)
    headers = {
        "config.h": config, "iconv.h": iconv,
        "localcharset.h": localcharset_template, "configmake.h": '#define LIBDIR ""\n',
    }
    if any(re.search(r"@[A-Za-z_][A-Za-z_0-9]*@", value) for value in headers.values()):
        raise PreparationError("unresolved generated-header substitution")
    return headers


def prepare(data: bytes, output: Path) -> None:
    if len(data) > MAX_ARCHIVE_BYTES or sha256(data) != SOURCE_SHA256:
        raise PreparationError("GNU source archive SHA-256 mismatch or size limit exceeded")
    if output.exists():
        raise PreparationError("output already exists; refusing to overwrite it")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = checked_members(archive)

        def read_template(relative: str) -> str:
            stream = archive.extractfile(f"{SOURCE_ROOT}/{relative}")
            if stream is None:
                raise PreparationError(f"missing source template: {relative}")
            return stream.read().decode("utf-8")

        headers = generated_headers(
            read_template("config.h.in"), read_template("include/iconv.h.in"),
            read_template("libcharset/include/localcharset.h.in"),
        )
        output.mkdir(parents=True, exist_ok=False)
        source_directory = output / "source"
        source_directory.mkdir()
        for member in members:
            target = source_directory.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise PreparationError(f"unreadable source member: {member.name}")
                with target.open("xb") as destination:
                    destination.write(stream.read())
    artifact = output / "artifact"
    generated = artifact / "generated"
    generated.mkdir(parents=True)
    for name, value in headers.items():
        (generated / name).write_text(value, encoding="utf-8", newline="\n")
    (artifact / "libiconv.def").write_text(
        "LIBRARY libiconv\nEXPORTS\n" + "".join(f"    {entry}\n" for entry in EXPORTS),
        encoding="ascii", newline="\n",
    )
    source = source_directory / SOURCE_ROOT
    licenses = artifact / "licenses" / "libiconv"
    licenses.mkdir(parents=True)
    for name in ("COPYING", "COPYING.LIB"):
        (licenses / name).write_bytes((source / name).read_bytes())
    identity = {
        "schema_version": 1, "purpose": "isolated candidate; not a shipping replacement",
        "source_url": SOURCE_URL, "source_sha256": SOURCE_SHA256,
        "source_version": "1.14", "implementation_patches": [],
        "source_files": {name: sha256((source / name).read_bytes()) for name in SOURCES},
        "generated_files": {
            path.relative_to(artifact).as_posix(): sha256(path.read_bytes())
            for path in sorted(artifact.rglob("*")) if path.is_file()
        },
        "generator_sha256": sha256(Path(__file__).read_bytes()),
        "runtime_acceptance": "not performed by source preparation or compiler success",
    }
    (artifact / "source-identity.json").write_text(
        json.dumps(identity, indent=2) + "\n", encoding="utf-8", newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--archive", type=Path, help="existing pinned GNU archive")
    group.add_argument("--download", action="store_true", help="download the fixed GNU URL")
    parser.add_argument("--output", type=Path, default=Path("build/native-decoder"))
    args = parser.parse_args()
    try:
        output = candidate_output(args.output, Path(__file__).resolve().parents[1])
        if args.download:
            with urlopen(SOURCE_URL, timeout=60) as response:
                if not response.geturl().startswith("https://"):
                    raise PreparationError("source download redirected away from HTTPS")
                data = response.read(MAX_ARCHIVE_BYTES + 1)
        else:
            with args.archive.open("rb") as source_file:
                data = source_file.read(MAX_ARCHIVE_BYTES + 1)
        prepare(data, output)
    except (OSError, ValueError, tarfile.TarError, KeyError) as exc:
        print(f"Source preparation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Prepared GNU libiconv 1.14 candidate sources in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
