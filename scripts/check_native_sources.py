"""Validate pinned native-binary/source evidence without downloading anything.

Ordinary mode permits explicitly incomplete research. --require-complete gates
publication on source records and build instructions; --installed also
checks the local distribution versions and binary bytes. This checks recorded
evidence, not reproducibility or legal compliance.

An explicit build_input plus installed_sha256 records a reviewed replacement:
the repository build input must always match sha256, while --installed checks
the original wheel bytes against installed_sha256. No automatic substitutions.
Optional source local_path records also require a matching retained archive.

--payload-root points at the frozen application's _internal directory. It checks
every recorded binary and native-file coverage within PySide6, shiboken6 and pyzbar only;
this is not an inventory of every dependency in the application.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_HASH = re.compile(r"[a-fA-F0-9]{64}")
# Reviewed Microsoft redistributables present in the Qt Windows payload. Keep
# exact paths: a broad runtime-name wildcard could hide a newly bundled library.
_PAYLOAD_RUNTIME_EXCLUSIONS = frozenset(
    "pyside6/" + name
    for name in (
        "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
        "vcruntime140.dll", "vcruntime140_1.dll",
    )
) | frozenset(
    "shiboken6/" + name
    for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
)
_PAYLOAD_SCOPES = frozenset({"pyside6", "shiboken6", "pyzbar"})


class NativeSourceError(ValueError):
    """A record or its installed binary identity could not be validated."""


class IncompleteSourceError(NativeSourceError):
    """Valid research records are not yet complete enough for publication."""


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def read_constraints(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.fullmatch(line)
        if match is None:
            raise NativeSourceError(f"constraints line {number}: expected an exact package==version pin")
        package, version = match.groups()
        package = normalized_name(package)
        if package in pins:
            raise NativeSourceError(f"constraints line {number}: duplicate package")
        pins[package] = version
    if not pins:
        raise NativeSourceError("constraints contain no exact pins")
    return pins


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise NativeSourceError(f"{label}: expected nonempty text without surrounding whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise NativeSourceError(f"{label}: control characters are not allowed")
    return value


def _relative_path(value: object, label: str) -> str:
    path = _text(value, label)
    if "\\" in path or re.search(r'[<>:"|?*]', path):
        raise NativeSourceError(f"{label}: expected a relative POSIX file path")
    if any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in path.split("/")):
        raise NativeSourceError(f"{label}: absolute, ambiguous, or traversing paths are forbidden")
    return path


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise NativeSourceError(f"{label}: expected a SHA-256 hex digest")
    return value.lower()


def _source_url(value: object) -> str:
    url = _text(value, "source URL")
    if any(char.isspace() for char in url) or "\\" in url:
        raise NativeSourceError("source URL: whitespace and backslashes are forbidden")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or port == 0):
            raise ValueError("invalid source URL")
    except ValueError as exc:
        raise NativeSourceError("source URL: expected HTTPS without credentials or fragments") from exc
    return parsed._replace(netloc=parsed.netloc.lower()).geturl()


def _contained(root: Path, path: Path, label: str) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise NativeSourceError(f"{label}: resolved path escapes its root")
    return resolved


def _check_repository_file(root: Path, value: object, digest: str, label: str) -> None:
    relative = _relative_path(value, label)
    path = _contained(root, root / relative, label)
    if not path.is_file() or path.stat().st_size == 0:
        raise NativeSourceError(f"{label}: must be an existing nonempty file: {relative}")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise NativeSourceError(f"{label} SHA-256 differs: {relative}")


def _check_installed(package: str, version: str, binaries: list[tuple[str, str]]) -> None:
    try:
        actual_version = metadata.version(package)
        distribution = metadata.distribution(package)
    except metadata.PackageNotFoundError as exc:
        raise NativeSourceError(f"{package}: installed distribution is missing") from exc
    if actual_version != version:
        raise NativeSourceError(f"{package}: installed version differs from the release pin")
    root = Path(str(distribution.locate_file(""))).resolve()
    for relative, digest in binaries:
        path = _contained(root, Path(str(distribution.locate_file(relative))), "installed binary")
        if not path.is_file():
            raise NativeSourceError(f"{package}: installed binary is missing: {relative}")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise NativeSourceError(f"{package}: installed binary SHA-256 differs: {relative}")


def _check_payload(root: Path, records: list[tuple[str, str, list[tuple[str, str]]]]) -> None:
    root = root.resolve()
    if not root.is_dir():
        raise NativeSourceError("payload root must be an existing _internal directory")
    expected: dict[str, str] = {}
    for _package, _version, binaries in records:
        for relative, digest in binaries:
            key = relative.casefold()
            if key in expected:
                raise NativeSourceError(f"duplicate payload binary path across packages: {relative}")
            expected[key] = digest
            path = _contained(root, root / relative, "payload binary")
            if not path.is_file():
                raise NativeSourceError(f"payload binary is missing: {relative}")
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest:
                raise NativeSourceError(f"payload binary SHA-256 differs: {relative}")

    # Inspect directories explicitly: rglob can silently skip symlink/junction
    # directories, allowing an unrecorded native subtree to evade coverage.
    pending = [path for path in root.iterdir() if path.name.casefold() in _PAYLOAD_SCOPES]
    seen: set[str] = set()
    while pending:
        path = pending.pop()
        relative = path.relative_to(root).as_posix()
        _contained(root, path, "payload entry")
        if path.is_symlink() or path.is_junction():
            raise NativeSourceError(f"payload aliases are not allowed in native scopes: {relative}")
        if path.is_dir():
            pending.extend(path.iterdir())
            continue
        if path.suffix.lower() not in {".dll", ".pyd"}:
            continue
        if not path.is_file():
            raise NativeSourceError(f"payload native entry is not a file: {relative}")
        key = relative.casefold()
        if key in seen:
            raise NativeSourceError(f"duplicate native payload path: {relative}")
        seen.add(key)
        if key not in expected and key not in _PAYLOAD_RUNTIME_EXCLUSIONS:
            raise NativeSourceError(f"unrecorded native payload binary: {relative}")


def validate_manifest(
    data: object, constraints: dict[str, str], *, repo_root: Path,
    installed: bool = False, require_complete: bool = False,
    payload_root: Path | None = None,
) -> tuple[str, ...]:
    """Return incomplete component names, or raise for invalid/incomplete input.

    Package names in constraints must be normalized as returned by read_constraints.
    Optional checks never weaken validation of records that are already present.
    """
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise NativeSourceError("manifest must declare schema_version 1")
    components = data.get("components")
    if not isinstance(components, list) or not components:
        raise NativeSourceError("components must be a nonempty list")
    names: set[str] = set()
    binary_keys: set[tuple[str, str]] = set()
    incomplete: list[str] = []
    incomplete_reasons: list[str] = []
    installed_records: list[tuple[str, str, list[tuple[str, str]]]] = []
    payload_records: list[tuple[str, str, list[tuple[str, str]]]] = []
    for record in components:
        if not isinstance(record, dict):
            raise NativeSourceError("component must be an object")
        name = _text(record.get("name"), "component name")
        if name.casefold() in names:
            raise NativeSourceError("duplicate component name")
        names.add(name.casefold())
        package = _text(record.get("package"), "package")
        if _PACKAGE.fullmatch(package) is None:
            raise NativeSourceError("invalid package name")
        package = normalized_name(package)
        version = _text(record.get("version"), "version")
        if constraints.get(package) != version:
            raise NativeSourceError(f"{name}: package version does not match exact release constraints")
        binary_records = record.get("binaries")
        if not isinstance(binary_records, list) or not binary_records:
            raise NativeSourceError(f"{name}: binaries must be a nonempty list")
        binaries: list[tuple[str, str]] = []
        installed_binaries: list[tuple[str, str]] = []
        for binary in binary_records:
            if not isinstance(binary, dict):
                raise NativeSourceError(f"{name}: binary must be an object")
            path = _relative_path(binary.get("path"), "binary path")
            digest = _hash(binary.get("sha256"), "binary hash")
            installed_digest = digest
            if ("build_input" in binary) != ("installed_sha256" in binary):
                raise NativeSourceError(f"{name}: build_input and installed_sha256 must be declared together")
            if "build_input" in binary:
                installed_digest = _hash(binary["installed_sha256"], "installed binary hash")
                _check_repository_file(repo_root, binary["build_input"], digest, f"{name}: build input")
            key = package, path.casefold()
            if key in binary_keys:
                raise NativeSourceError(f"{name}: duplicate binary path")
            binary_keys.add(key)
            binaries.append((path, digest))
            installed_binaries.append((path, installed_digest))
        source_records = record.get("sources")
        if not isinstance(source_records, list):
            raise NativeSourceError(f"{name}: sources must be a list")
        urls: set[str] = set()
        for source in source_records:
            if not isinstance(source, dict):
                raise NativeSourceError(f"{name}: source must be an object")
            url = _source_url(source.get("url"))
            source_digest = _hash(source.get("sha256"), "source hash")
            if "local_path" in source:
                _check_repository_file(repo_root, source["local_path"], source_digest, f"{name}: local source archive")
            if url in urls:
                raise NativeSourceError(f"{name}: duplicate source URL")
            urls.add(url)
        unresolved = record.get("unresolved")
        if not isinstance(unresolved, list):
            raise NativeSourceError(f"{name}: unresolved must be a list")
        for issue in unresolved:
            _text(issue, "unresolved item")
        instructions = record.get("build_instructions")
        has_instructions = False
        if instructions is not None:
            relative = _relative_path(instructions, "build instructions")
            path = _contained(repo_root, repo_root / relative, "build instructions")
            try:
                has_instructions = path.is_file() and bool(path.read_text(encoding="utf-8").strip())
            except (OSError, UnicodeError) as exc:
                raise NativeSourceError(f"{name}: build instructions cannot be read as UTF-8") from exc
        if unresolved or not urls or not has_instructions:
            incomplete.append(name)
            reasons = list(unresolved)
            if not urls:
                reasons.append("no source archives recorded")
            if not has_instructions:
                reasons.append("build instructions are missing or empty")
            incomplete_reasons.append(name + ": " + "; ".join(reasons))
        installed_records.append((package, version, installed_binaries))
        payload_records.append((package, version, binaries))
    # All declared build inputs are mandatory even without the optional environment checks.
    if installed:
        for package, version, binaries in installed_records:
            _check_installed(package, version, binaries)
    if payload_root is not None:
        _check_payload(payload_root, payload_records)
    if incomplete and require_complete:
        raise IncompleteSourceError("native source evidence is incomplete: " + " | ".join(incomplete_reasons))
    return tuple(incomplete)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NativeSourceError("manifest JSON contains duplicate keys")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "packaging/native-source-provenance.json")
    parser.add_argument("--constraints", type=Path, default=REPO_ROOT / "constraints-release.txt")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--payload-root", type=Path, help="frozen application's _internal directory")
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
        incomplete = validate_manifest(
            data, read_constraints(args.constraints), repo_root=args.repo_root,
            installed=args.installed, require_complete=args.require_complete,
            payload_root=args.payload_root,
        )
    except IncompleteSourceError as exc:
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return 1
    except NativeSourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, RecursionError):
        print("ERROR: native source evidence or local files could not be read", file=sys.stderr)
        return 2
    print(f"Native source evidence validated; {len(incomplete)} incomplete component(s).")
    for name in incomplete:
        print(f"INCOMPLETE: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
