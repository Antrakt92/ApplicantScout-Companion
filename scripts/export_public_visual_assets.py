"""Export anonymized companion visual fixtures into public addon media assets."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
from typing import Literal

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JPEG_BACKGROUND = (8, 10, 16)


class PublicVisualAssetError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublicVisualExport:
    source_relative: Path
    target_relative: Path
    image_format: Literal["JPEG", "PNG"]


PUBLIC_VISUAL_EXPORTS: tuple[PublicVisualExport, ...] = (
    PublicVisualExport(
        source_relative=Path("docs/visual/overlay-polish-fixture.png"),
        target_relative=Path(
            "docs/visual/applicantscout-curseforge-mplus-overlay.jpg"
        ),
        image_format="JPEG",
    ),
    PublicVisualExport(
        source_relative=Path("docs/visual/overlay-polish-fixture-raid-listing.png"),
        target_relative=Path(
            "docs/visual/applicantscout-curseforge-raid-party-overlay.jpg"
        ),
        image_format="JPEG",
    ),
    PublicVisualExport(
        source_relative=Path("docs/visual/overlay-polish-fixture-party-manual-key.png"),
        target_relative=Path("docs/visual/applicantscout-overlay-alpha.png"),
        image_format="PNG",
    ),
)


def _assert_addon_root(addon_root: Path) -> None:
    required_markers = (
        addon_root / "ApplicantScout.toc",
        addon_root / "ApplicantScout.lua",
    )
    if not all(path.is_file() for path in required_markers):
        raise PublicVisualAssetError(
            f"Not an ApplicantScout-Addon checkout: {addon_root}"
        )


def _default_addon_root() -> Path:
    sibling = REPO_ROOT.parent / "ApplicantScout-Addon"
    if sibling.is_dir():
        return sibling
    raise PublicVisualAssetError(
        "Could not find ApplicantScout-Addon checkout. Pass --addon-root explicitly."
    )


def _encode_public_visual(source: Path, export: PublicVisualExport) -> bytes:
    if not source.is_file():
        raise PublicVisualAssetError(f"Missing public visual source: {source}")

    with Image.open(source) as image:
        buffer = BytesIO()
        if export.image_format == "JPEG":
            rgba = image.convert("RGBA")
            background = Image.new(
                "RGBA",
                rgba.size,
                (*DEFAULT_JPEG_BACKGROUND, 255),
            )
            background.alpha_composite(rgba)
            background.convert("RGB").save(
                buffer,
                format="JPEG",
                quality=92,
                optimize=True,
            )
            return buffer.getvalue()

        image.convert("RGBA").save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def _write_bytes_atomic(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        os.close(fd)
        fd = -1
        temp_path = Path(temp_name)
        temp_path.write_bytes(payload)
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if fd != -1:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def export_public_visual_assets(
    addon_root: Path,
    *,
    companion_root: Path = REPO_ROOT,
    check: bool,
) -> list[Path]:
    exported: list[Path] = []
    mismatches: list[Path] = []
    _assert_addon_root(addon_root)
    for export in PUBLIC_VISUAL_EXPORTS:
        source = companion_root / export.source_relative
        target = addon_root / export.target_relative
        expected = _encode_public_visual(source, export)
        if check:
            if not target.is_file() or target.read_bytes() != expected:
                mismatches.append(target)
            continue
        _write_bytes_atomic(target, expected)
        exported.append(target)

    if mismatches:
        lines = "\n".join(str(path) for path in mismatches)
        raise PublicVisualAssetError(
            "stale public visual asset; regenerate with "
            "scripts\\export_public_visual_assets.py --addon-root <path>:\n"
            f"{lines}"
        )
    return exported


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export companion overlay fixtures into public addon media assets."
    )
    parser.add_argument(
        "--addon-root",
        type=Path,
        default=None,
        help="Path to the ApplicantScout-Addon checkout.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if public addon media assets are stale instead of writing them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    addon_root = args.addon_root or _default_addon_root()
    try:
        paths = export_public_visual_assets(
            addon_root.resolve(),
            check=args.check,
        )
    except PublicVisualAssetError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.check:
        print("Public visual assets match companion fixtures.")
    else:
        for path in paths:
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
