"""Fail-closed provenance and architecture checks for the frozen Windows app."""

from __future__ import annotations

import argparse
import ast
import os
import re
import stat
import struct
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

AMD64_MACHINE = 0x8664
PE_EXTENSIONS = frozenset({".dll", ".exe", ".pyd"})
FORBIDDEN_FROZEN_MODULE_ROOTS = frozenset(
    {
        "_pytest",
        "_pyinstaller_hooks_contrib",
        "altgraph",
        "build",
        "colorama",
        "iniconfig",
        "nodeenv",
        "numpy",
        "packaging",
        "pefile",
        "pip",
        "pluggy",
        "pygments",
        "pyinstaller",
        "pyproject_hooks",
        "pyright",
        "pytest",
        "pytestqt",
        "ruff",
        "setuptools",
        "wheel",
        "win32ctypes",
    }
)
ALLOWED_APPLICATION_IMPORT_WARNINGS = frozenset({"collections.abc"})
PACKAGED_ROOT_EXTRAS = frozenset(
    {
        ".apscout-payload-version",
        "LICENSE",
        "RELEASE_NOTES.md",
        "THIRD-PARTY-NOTICES.md",
    }
)
PACKAGED_LICENSE_SUFFIXES = frozenset({"", ".html", ".md", ".rst", ".txt"})


class FrozenRuntimeVerificationError(RuntimeError):
    """Raised when a frozen artifact cannot be proven safe to package."""


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _absolute(path: Path) -> Path:
    """Normalize a path without dereferencing a junction or symlink."""
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_filesystem_redirection(path: Path) -> bool:
    """Reject every link/junction/reparse point before traversing a payload."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise FrozenRuntimeVerificationError(
            f"Could not inspect frozen payload path {path}: {exc}"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def verify_no_payload_redirection(app_dir: Path, *, trusted_root: Path) -> Path:
    """Reject a redirected payload root or any redirected in-repo ancestor."""
    app_dir = _absolute(app_dir)
    trusted_root = _absolute(trusted_root)
    if not _is_within(app_dir, trusted_root):
        raise FrozenRuntimeVerificationError(
            f"Frozen payload path is outside the trusted build root: {app_dir}"
        )

    current = trusted_root
    candidates = [current]
    for part in app_dir.relative_to(trusted_root).parts:
        current = current / part
        candidates.append(current)
    for candidate in candidates:
        if _is_filesystem_redirection(candidate):
            raise FrozenRuntimeVerificationError(
                f"Frozen payload path is a filesystem redirection: {candidate}"
            )
    return app_dir


def _walk_payload_paths(root: Path) -> Iterable[Path]:
    """Walk without following filesystem redirections on Windows or POSIX."""
    if _is_filesystem_redirection(root):
        raise FrozenRuntimeVerificationError(
            f"Frozen payload path is a filesystem redirection: {root}"
        )
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise FrozenRuntimeVerificationError(
                f"Could not enumerate frozen payload path {directory}: {exc}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            if _is_filesystem_redirection(path):
                raise FrozenRuntimeVerificationError(
                    f"Frozen payload path is a filesystem redirection: {path}"
                )
            yield path
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise FrozenRuntimeVerificationError(
                    f"Could not inspect frozen payload path {path}: {exc}"
                ) from exc
            if is_directory:
                pending.append(path)


def _absolute_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if os.path.isabs(value):
            yield value
        return
    if isinstance(value, dict):
        for item in value.items():
            yield from _absolute_strings(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _absolute_strings(item)


def _read_toc(path: Path) -> Any:
    try:
        contents = path.read_text(encoding="utf-8")
        return ast.literal_eval(contents)
    except (OSError, SyntaxError, ValueError) as exc:
        raise FrozenRuntimeVerificationError(
            f"Could not parse PyInstaller provenance file {path}: {exc}"
        ) from exc


def verify_toc_provenance(
    toc_paths: Iterable[Path],
    *,
    allowed_roots: Iterable[Path],
    allowed_files: Iterable[Path] = (),
    forbidden_roots: Iterable[Path] = (),
) -> None:
    roots = tuple(_resolved(root) for root in allowed_roots)
    files = frozenset(_resolved(path) for path in allowed_files)
    forbidden = tuple(_resolved(root) for root in forbidden_roots)
    if not roots:
        raise FrozenRuntimeVerificationError(
            "No allowed provenance roots were supplied."
        )

    for toc_path in toc_paths:
        toc = _read_toc(toc_path)
        origins = tuple(_absolute_strings(toc))
        if not origins:
            raise FrozenRuntimeVerificationError(
                f"PyInstaller provenance file contains no absolute origins: {toc_path}"
            )
        for raw_origin in origins:
            origin = _resolved(Path(raw_origin))
            if any(_is_within(origin, root) for root in forbidden):
                raise FrozenRuntimeVerificationError(
                    f"Frozen input came from a forbidden site-packages origin: {raw_origin}"
                )
            if origin not in files and not any(
                _is_within(origin, root) for root in roots
            ):
                raise FrozenRuntimeVerificationError(
                    f"Frozen input came from an untrusted origin: {raw_origin}"
                )


def verify_collect_membership(
    app_dir: Path,
    collect_toc_path: Path,
    *,
    allowed_extra_files: Iterable[str] = (),
) -> None:
    toc = _read_toc(collect_toc_path)
    if not isinstance(toc, tuple) or len(toc) != 1 or not isinstance(toc[0], list):
        raise FrozenRuntimeVerificationError(
            f"Unexpected PyInstaller COLLECT structure: {collect_toc_path}"
        )
    expected: set[str] = set()
    for row in toc[0]:
        if not isinstance(row, tuple) or len(row) != 3:
            raise FrozenRuntimeVerificationError(
                f"Malformed PyInstaller COLLECT row: {row!r}"
            )
        destination, _origin, kind = row
        if not isinstance(destination, str) or not isinstance(kind, str):
            raise FrozenRuntimeVerificationError(
                f"Malformed PyInstaller COLLECT row: {row!r}"
            )
        relative = Path(destination.replace("\\", "/")).as_posix()
        expected.add(relative if kind == "EXECUTABLE" else f"_internal/{relative}")

    actual = {
        path.relative_to(app_dir).as_posix()
        for path in _walk_payload_paths(app_dir)
        if path.is_file()
    }
    missing = sorted(expected - actual)
    allowed_extras = {Path(path).as_posix() for path in allowed_extra_files}
    unexpected = sorted(actual - expected - allowed_extras)
    if missing or unexpected:
        detail: list[str] = []
        if missing:
            detail.append("missing: " + ", ".join(missing[:10]))
        if unexpected:
            detail.append("unexpected: " + ", ".join(unexpected[:10]))
        raise FrozenRuntimeVerificationError(
            "Frozen payload does not match COLLECT provenance ("
            + "; ".join(detail)
            + ")"
        )


def verify_packaged_extras(app_dir: Path) -> set[str]:
    """Return the exact non-PyInstaller payload after validating its safe shape."""
    extras: set[str] = set()
    for relative in PACKAGED_ROOT_EXTRAS:
        path = app_dir / relative
        if (
            _is_filesystem_redirection(path)
            or not path.is_file()
            or path.stat().st_size <= 0
        ):
            raise FrozenRuntimeVerificationError(
                f"Missing or unsafe packaged root artifact: {relative}"
            )
        extras.add(relative)

    marker = (app_dir / ".apscout-payload-version").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"\d+\.\d+\.\d+", marker) is None:
        raise FrozenRuntimeVerificationError(
            "Packaged payload version marker is not an exact stable version."
        )

    licenses_dir = app_dir / "licenses"
    if _is_filesystem_redirection(licenses_dir) or not licenses_dir.is_dir():
        raise FrozenRuntimeVerificationError("Packaged license directory is missing.")
    license_count = 0
    for path in _walk_payload_paths(licenses_dir):
        if not path.is_file():
            continue
        relative = path.relative_to(app_dir).as_posix()
        name = path.name.casefold()
        suffix = path.suffix.casefold()
        if (
            not any(token in name for token in ("license", "copying", "notice"))
            or suffix not in PACKAGED_LICENSE_SUFFIXES
            or path.stat().st_size <= 0
        ):
            raise FrozenRuntimeVerificationError(
                f"Unexpected packaged license artifact: {relative}"
            )
        extras.add(relative)
        license_count += 1
    if license_count == 0:
        raise FrozenRuntimeVerificationError("Packaged license directory is empty.")
    return extras


def pe_machine(path: Path) -> int:
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise FrozenRuntimeVerificationError(f"Not a PE image: {path}")
            stream.seek(0x3C)
            offset_bytes = stream.read(4)
            if len(offset_bytes) != 4:
                raise FrozenRuntimeVerificationError(f"Truncated PE header: {path}")
            pe_offset = struct.unpack("<I", offset_bytes)[0]
            stream.seek(pe_offset)
            if stream.read(4) != b"PE\0\0":
                raise FrozenRuntimeVerificationError(f"Invalid PE signature: {path}")
            machine_bytes = stream.read(2)
            if len(machine_bytes) != 2:
                raise FrozenRuntimeVerificationError(
                    f"Truncated PE machine field: {path}"
                )
            return struct.unpack("<H", machine_bytes)[0]
    except OSError as exc:
        raise FrozenRuntimeVerificationError(
            f"Could not inspect PE image {path}: {exc}"
        ) from exc


def verify_amd64_payload(app_dir: Path, *, producer_python: Path) -> None:
    if struct.calcsize("P") != 8:
        raise FrozenRuntimeVerificationError("Release builder Python is not 64-bit.")
    if _resolved(Path(sys.executable)) != _resolved(producer_python):
        raise FrozenRuntimeVerificationError(
            f"Verifier ran under unexpected Python: {sys.executable}"
        )

    native_files = [producer_python]
    native_files.extend(
        path
        for path in _walk_payload_paths(app_dir)
        if path.is_file() and path.suffix.casefold() in PE_EXTENSIONS
    )
    if len(native_files) == 1:
        raise FrozenRuntimeVerificationError(
            f"Frozen payload has no native files: {app_dir}"
        )
    for path in native_files:
        machine = pe_machine(path)
        if machine != AMD64_MACHINE:
            raise FrozenRuntimeVerificationError(
                f"Expected AMD64 PE image, found 0x{machine:04x}: {path}"
            )


def verify_no_bundled_icu(app_dir: Path) -> None:
    bundled = sorted(
        path.relative_to(app_dir).as_posix()
        for path in _walk_payload_paths(app_dir)
        if path.is_file()
        and path.name.casefold().startswith("icu")
        and path.suffix.casefold() == ".dll"
    )
    if bundled:
        raise FrozenRuntimeVerificationError(
            "Frozen runtime contains unsupported bundled ICU DLLs: "
            + ", ".join(bundled)
        )


def _module_names(value: Any) -> Iterable[str]:
    if (
        isinstance(value, tuple)
        and len(value) == 3
        and isinstance(value[0], str)
        and value[2] in {"EXTENSION", "PYMODULE", "PYSOURCE"}
    ):
        yield value[0]
        return
    if isinstance(value, dict):
        for item in value.items():
            yield from _module_names(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _module_names(item)


def verify_no_forbidden_modules(*toc_paths: Path) -> None:
    """Reject optional build/test packages that the runtime never imports."""
    forbidden: set[str] = set()
    for toc_path in toc_paths:
        names = tuple(_module_names(_read_toc(toc_path)))
        if not names:
            raise FrozenRuntimeVerificationError(
                f"PyInstaller module provenance contains no module rows: {toc_path}"
            )
        for module_name in names:
            module_root = module_name.partition(".")[0].casefold()
            if module_root in FORBIDDEN_FROZEN_MODULE_ROOTS:
                forbidden.add(module_root)
    if forbidden:
        raise FrozenRuntimeVerificationError(
            "Frozen runtime contains build/test-only modules: "
            + ", ".join(sorted(forbidden))
        )


def verify_application_import_warnings(warn_path: Path) -> None:
    """Fail on new missing modules attributed to application top-level imports."""
    try:
        lines = warn_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FrozenRuntimeVerificationError(
            f"Could not read PyInstaller warning report {warn_path}: {exc}"
        ) from exc

    unexpected: set[str] = set()
    pattern = re.compile(r"^missing module named (?:'([^']+)'|(\S+)) - imported by ")
    for line in lines:
        if "applicant_scout." not in line or "(top-level)" not in line:
            continue
        match = pattern.match(line)
        if match is None:
            raise FrozenRuntimeVerificationError(
                f"Malformed application import warning: {line}"
            )
        module_name = match.group(1) or match.group(2)
        if module_name not in ALLOWED_APPLICATION_IMPORT_WARNINGS:
            unexpected.add(module_name)
    if unexpected:
        raise FrozenRuntimeVerificationError(
            "PyInstaller reports missing application imports: "
            + ", ".join(sorted(unexpected))
        )


def verify_frozen_runtime(
    *,
    repo_root: Path,
    app_dir: Path,
    work_dir: Path,
    base_prefix: Path,
    windows_dir: Path,
    producer_python: Path,
    packaged_layout: bool = False,
) -> None:
    repo_root = _absolute(repo_root)
    app_dir = verify_no_payload_redirection(app_dir, trusted_root=repo_root)
    work_dir = _resolved(work_dir)
    toc_dir = work_dir / "ApplicantScout"
    analysis_toc = toc_dir / "Analysis-00.toc"
    collect_toc = toc_dir / "COLLECT-00.toc"
    pyz_toc = toc_dir / "PYZ-00.toc"
    warn_path = toc_dir / "warn-ApplicantScout.txt"
    toc_paths = (analysis_toc, collect_toc)
    for required in (app_dir, *toc_paths, pyz_toc, warn_path, producer_python):
        if not required.exists():
            raise FrozenRuntimeVerificationError(
                f"Missing frozen build input: {required}"
            )

    verify_toc_provenance(
        toc_paths,
        allowed_roots=(
            repo_root / "src",
            repo_root / "packaging" / "pyinstaller",
            repo_root / ".venv",
            base_prefix,
            windows_dir / "System32",
        ),
        allowed_files=(
            toc_dir / "ApplicantScout.exe",
            toc_dir / "base_library.zip",
        ),
        forbidden_roots=(
            base_prefix / "Lib" / "site-packages",
            base_prefix / "lib" / "site-packages",
        ),
    )
    allowed_extra_files = verify_packaged_extras(app_dir) if packaged_layout else ()
    verify_collect_membership(
        app_dir,
        collect_toc,
        allowed_extra_files=allowed_extra_files,
    )
    verify_no_forbidden_modules(analysis_toc, pyz_toc)
    verify_application_import_warnings(warn_path)
    verify_no_bundled_icu(app_dir)
    verify_amd64_payload(app_dir, producer_python=producer_python)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--base-prefix", type=Path, required=True)
    parser.add_argument("--windows-dir", type=Path, required=True)
    parser.add_argument("--producer-python", type=Path, required=True)
    parser.add_argument("--packaged-layout", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        verify_frozen_runtime(
            repo_root=args.repo_root,
            app_dir=args.app_dir,
            work_dir=args.work_dir,
            base_prefix=args.base_prefix,
            windows_dir=args.windows_dir,
            producer_python=args.producer_python,
            packaged_layout=args.packaged_layout,
        )
    except FrozenRuntimeVerificationError as exc:
        print(f"Frozen runtime verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
