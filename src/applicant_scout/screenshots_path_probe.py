"""Lightweight isolated validation for a configured WoW Screenshots path."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


SCREENSHOTS_PATH_PROBE_ARG = "--internal-screenshots-path-probe"


def _looks_like_wow_retail_root(retail_root: Path) -> bool:
    file_markers = (retail_root / "Wow.exe",)
    dir_markers = (
        retail_root / "Interface",
        retail_root / "Interface" / "AddOns",
        retail_root / "WTF",
    )
    return any(marker.is_file() for marker in file_markers) or any(
        marker.is_dir() for marker in dir_markers
    )


def screenshots_path_health_warning(path: Path) -> str | None:
    """Return a non-fatal warning for paths that look unlike WoW screenshots."""
    path = Path(path)
    problems: list[str] = []
    if path.name.lower() != "screenshots":
        problems.append("folder is not named Screenshots")

    retail_root = path.parent if path.parent.name.lower() == "_retail_" else None
    if retail_root is None:
        problems.append(r"path is not directly under a _retail_ folder")
    elif not retail_root.exists():
        problems.append(r"_retail_ folder does not exist")
    elif not _looks_like_wow_retail_root(retail_root):
        problems.append(r"_retail_ folder has no WoW install markers")

    if not problems:
        return None
    return "Screenshots folder warning: " + "; ".join(problems) + "."


def screenshots_path_probe_result_path(token: str) -> Path:
    if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
        raise ValueError("invalid path probe token")
    return (
        Path(tempfile.gettempdir())
        / f"applicant-scout-path-probe-{token}.json"
    )


def run_screenshots_path_probe_command(raw_path: str, token: str) -> int:
    try:
        result_path = screenshots_path_probe_result_path(token)
    except ValueError:
        return 2
    try:
        candidate = Path(raw_path)
        warning = (
            "Screenshots path points to a file, not a folder."
            if candidate.is_file()
            else screenshots_path_health_warning(candidate)
        )
    except Exception as exc:  # noqa: BLE001 - isolated filesystem boundary
        warning = f"Screenshots folder warning: could not check path: {exc}"
    try:
        result_path.write_text(
            json.dumps({"warning": warning}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        return 3
    return 0


def dispatch_screenshots_path_probe(args: list[str]) -> int | None:
    """Run the internal probe when requested, before importing the GUI runtime."""
    if not args or args[0] != SCREENSHOTS_PATH_PROBE_ARG:
        return None
    if len(args) != 3:
        return 2
    return run_screenshots_path_probe_command(args[1], args[2])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    exit_code = dispatch_screenshots_path_probe(args)
    return 2 if exit_code is None else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
