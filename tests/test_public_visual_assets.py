from __future__ import annotations

import pytest
from PIL import Image

from scripts.export_public_visual_assets import (
    PUBLIC_VISUAL_EXPORTS,
    PublicVisualAssetError,
    export_public_visual_assets,
)


def _write_source_images(companion_root) -> None:
    visual_root = companion_root / "docs" / "visual"
    visual_root.mkdir(parents=True)
    for export in PUBLIC_VISUAL_EXPORTS:
        image = Image.new("RGBA", (3, 2), (12, 18, 28, 0))
        image.putpixel((0, 0), (255, 80, 20, 255))
        image.putpixel((1, 0), (20, 220, 120, 128))
        image.save(visual_root / export.source_relative.name)


def _write_addon_markers(addon_root) -> None:
    addon_root.mkdir(parents=True)
    (addon_root / "ApplicantScout.toc").write_text("## Interface: 120007\n")
    (addon_root / "ApplicantScout.lua").write_text("-- addon runtime\n")


def test_public_visual_export_mapping_covers_public_addon_assets():
    mapping = {
        export.target_relative.as_posix(): (
            export.source_relative.as_posix(),
            export.image_format,
        )
        for export in PUBLIC_VISUAL_EXPORTS
    }

    assert mapping == {
        "docs/visual/applicantscout-curseforge-mplus-overlay.jpg": (
            "docs/visual/overlay-polish-fixture.png",
            "JPEG",
        ),
        "docs/visual/applicantscout-curseforge-raid-party-overlay.jpg": (
            "docs/visual/overlay-polish-fixture-raid-listing.png",
            "JPEG",
        ),
        "docs/visual/applicantscout-overlay-alpha.png": (
            "docs/visual/overlay-polish-fixture-party-manual-key.png",
            "PNG",
        ),
    }


def test_public_visual_export_writes_jpeg_rgb_and_png_alpha(tmp_path):
    companion_root = tmp_path / "companion"
    addon_root = tmp_path / "addon"
    _write_source_images(companion_root)
    _write_addon_markers(addon_root)

    export_public_visual_assets(
        addon_root,
        companion_root=companion_root,
        check=False,
    )

    jpeg_path = addon_root / "docs" / "visual" / "applicantscout-curseforge-mplus-overlay.jpg"
    png_path = addon_root / "docs" / "visual" / "applicantscout-overlay-alpha.png"
    with Image.open(jpeg_path) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.size == (3, 2)
    with Image.open(png_path) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        assert image.getextrema()[3][0] == 0


def test_public_visual_export_check_detects_stale_targets(tmp_path):
    companion_root = tmp_path / "companion"
    addon_root = tmp_path / "addon"
    _write_source_images(companion_root)
    _write_addon_markers(addon_root)
    export_public_visual_assets(
        addon_root,
        companion_root=companion_root,
        check=False,
    )

    export_public_visual_assets(
        addon_root,
        companion_root=companion_root,
        check=True,
    )

    stale_target = (
        addon_root / "docs" / "visual" / "applicantscout-curseforge-mplus-overlay.jpg"
    )
    stale_target.write_bytes(b"stale")

    with pytest.raises(PublicVisualAssetError, match="stale public visual asset"):
        export_public_visual_assets(
            addon_root,
            companion_root=companion_root,
            check=True,
        )


def test_public_visual_export_rejects_non_addon_root_without_writing(tmp_path):
    companion_root = tmp_path / "companion"
    bad_addon_root = tmp_path / "ApplicantScout-Adddon"
    _write_source_images(companion_root)

    with pytest.raises(PublicVisualAssetError, match="ApplicantScout-Addon checkout"):
        export_public_visual_assets(
            bad_addon_root,
            companion_root=companion_root,
            check=False,
        )

    assert not bad_addon_root.exists()
