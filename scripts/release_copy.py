"""Remove legacy download lists while retaining exact release-copy verification.

Published manifests and assets stay unchanged. Only the recognized historical
download-list projection is accepted in addition to the original manifest body.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys


class ReleaseCopyError(ValueError):
    pass


_VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
_LAST_LEGACY_VERSION = (0, 18, 3)
_HEADINGS = re.compile(r"(?m)^## ([0-9]+\.[0-9]+\.[0-9]+) - [^\r\n]+(?:\r?\n|$)")
_SECTIONS = re.compile(r"(?m)^### (Release Assets|Notes)\r?\n")
_NEXT_HEADING = re.compile(r"(?m)^#{1,3} ")
_BLOCK = re.compile(
    r"### (?P<heading>Release Assets|Notes)(?P<eol>\r?\n)(?P=eol)"
    r"- Requires the ApplicantScout WoW addon `(?P<addon>[0-9]+\.[0-9]+\.[0-9]+)`\.(?P=eol)"
    r"- Installer: `ApplicantScoutCompanionSetup-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\.exe`(?P=eol)"
    r"- Installer checksum: `ApplicantScoutCompanionSetup-(?P=version)\.exe\.sha256`(?P=eol)"
    r"- Portable archive: `ApplicantScoutCompanion-(?P=version)-portable\.zip`"
    r"(?:(?P=eol)- Immutable manifest: `ApplicantScoutCompanion-(?P=version)-release-manifest\.json`)?"
    r"(?:\r?\n){0,2}"
)
_UPDATER_LIST = re.compile(
    r"(?m)^- In-app updates require GitHub Release assets named(?P<eol>\r?\n)"
    r"  `ApplicantScoutCompanionSetup-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\.exe` and(?P=eol)"
    r"  `ApplicantScoutCompanionSetup-(?P=version)\.exe\.sha256`\.(?=\r?$)"
    r"(?P=eol)?(?P=eol)?"
)


def _legacy_version(version: str) -> bool:
    return re.fullmatch(_VERSION, version) is not None and tuple(map(int, version.split("."))) <= _LAST_LEGACY_VERSION


def clean_legacy_asset_lists(text: str) -> str:
    """Remove only complete known asset lists; preserve every other character."""
    headings = list(_HEADINGS.finditer(text))
    removals = []

    def check_version(start: int, version: str) -> None:
        preceding = [heading for heading in headings if heading.start() < start]
        if not preceding or preceding[-1].group(1) != version or not _legacy_version(version):
            raise ReleaseCopyError("Asset list does not match a supported historical release heading")

    for section in _SECTIONS.finditer(text):
        following = _NEXT_HEADING.search(text, section.end())
        end = following.start() if following else len(text)
        block = _BLOCK.fullmatch(text[section.start():end])
        if block is None:
            if section.group(1) == "Release Assets":
                raise ReleaseCopyError("Unrecognized Release Assets section; refusing to remove other text")
            continue
        check_version(section.start(), block.group("version"))
        removals.append((section.start(), end))
    for block in _UPDATER_LIST.finditer(text):
        check_version(block.start(), block.group("version"))
        removals.append(block.span())
    for start, end in sorted(removals, reverse=True):
        text = text[:start] + text[end:]
    return text


def manifest_body(manifest: dict) -> str:
    """Check the original body record before deriving any display projection."""
    try:
        record = manifest["releaseCopy"]["body"]
        encoded = record["contentBase64"]
        raw = base64.b64decode(encoded, validate=True)
        if (record["encoding"] != "utf-8" or type(record["size"]) is not int
                or record["size"] != len(raw) or not raw
                or base64.b64encode(raw).decode("ascii") != encoded
                or hashlib.sha256(raw).hexdigest() != record["sha256"]):
            raise ReleaseCopyError("Manifest body record identity differs")
        body = raw.decode("utf-8")
        if not body.strip() or body.startswith("\ufeff") or "\r" in body or "\x00" in body or not body.endswith("\n"):
            raise ReleaseCopyError("Manifest body is not canonical UTF-8/LF text")
        return body
    except (KeyError, TypeError, UnicodeError, ValueError) as error:
        raise ReleaseCopyError(f"Invalid manifest body: {error}") from error


def published_body_matches(manifest: dict, actual: str) -> bool:
    expected = manifest_body(manifest)
    if actual == expected:
        return True
    tag = manifest.get("tag")
    if (type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 2
            or not isinstance(tag, str) or not tag.startswith("v") or not _legacy_version(tag[1:])):
        return False
    return actual == clean_legacy_asset_lists(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    clean = commands.add_parser("clean")
    clean.add_argument("--input", type=Path, required=True)
    clean.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-body")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--release-json", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "clean":
            original = args.input.read_bytes().decode("utf-8")
            args.output.write_bytes(clean_legacy_asset_lists(original).encode("utf-8"))
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            release = json.loads(args.release_json.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not isinstance(release, dict) or not isinstance(release.get("body"), str):
                raise ReleaseCopyError("Manifest and release must be objects with a release body")
            if not published_body_matches(manifest, release["body"]):
                raise ReleaseCopyError("Published body differs from the authoritative release copy")
    except (OSError, UnicodeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
