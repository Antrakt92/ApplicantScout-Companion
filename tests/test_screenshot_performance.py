"""Differential recovery and bounded pixel-conversion regressions."""
from __future__ import annotations

import weakref

import pytest
from PIL import Image, ImageDraw

from applicant_scout import screenshot as s


# Frozen pre-optimization recovery algorithm: compare actual output pixels,
# rather than comparing the optimized implementation to itself.
def _baseline_normalized_top_left_qr(img: Image.Image) -> Image.Image | None:
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
        width = s._white_edge_extent(gray, horizontal=True)
        height = s._white_edge_extent(gray, horizontal=False)
        if width is None or height is None:
            return None
        side = min(width, height)
        if side < s._QR_RECOVERY_MIN_SIDE_PX or abs(width - height) > max(2, side // 100):
            return None

        with gray.crop((0, 0, side, side)) as square:
            # M3: versions iterate ascending, so the first zero-error
            # (finder + timing) candidate is the global minimum — no later
            # hypothesis can beat (0, version, quiet).
            perfect = False
            for version in s._qr_recovery_candidate_versions(side):
                for quiet in s._QR_RECOVERY_QUIET_ZONES:
                    candidate = s._sample_qr_recovery_candidate(
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
                        if best[0][0] == 0:
                            perfect = True
                            break
                    else:
                        candidate[1].close()
                if perfect:
                    break

        if best is None:
            return None
        (_score, version, source_quiet), sampled = best
        normalized = s._render_normalized_qr(sampled)
        s._log.debug(
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


def _synthetic_recovery_qr(version: int, module_px: int = 6, quiet: int = 4):
    modules = 17 + 4 * version
    total = (modules + 2 * quiet) * module_px
    img = Image.new("L", (total, total), 255)
    draw = ImageDraw.Draw(img)

    def finder(cx: int, cy: int) -> None:
        for y in range(7):
            for x in range(7):
                if x in (0, 6) or y in (0, 6) or (2 <= x <= 4 and 2 <= y <= 4):
                    draw.rectangle(
                        [
                            (cx + x) * module_px,
                            (cy + y) * module_px,
                            (cx + x + 1) * module_px - 1,
                            (cy + y + 1) * module_px - 1,
                        ],
                        fill=0,
                    )

    finder(quiet, quiet)
    finder(quiet + modules - 7, quiet)
    finder(quiet, quiet + modules - 7)
    for index in range(8, modules - 8):
        if index % 2 == 0:
            draw.rectangle(
                [
                    (quiet + index) * module_px,
                    (quiet + 6) * module_px,
                    (quiet + index + 1) * module_px - 1,
                    (quiet + 7) * module_px - 1,
                ],
                fill=0,
            )
            draw.rectangle(
                [
                    (quiet + 6) * module_px,
                    (quiet + index) * module_px,
                    (quiet + 7) * module_px - 1,
                    (quiet + index + 1) * module_px - 1,
                ],
                fill=0,
            )
    return img



@pytest.mark.parametrize("mode", ["RGB", "RGBA", "P", "L"])
@pytest.mark.parametrize("case", ["manual", "white", "too-small", "rectangle", "qr-small", "qr-fractional", "qr-large", "short-edge"])
def test_recovery_matches_frozen_baseline(mode: str, case: str):
    size = (1600, 1000) if case != "short-edge" else (1, 120)
    with Image.new("RGB", size, (20, 50, 90)) as source:
        if case == "white":
            source.paste((255, 255, 255), (0, 0, *size))
        elif case in ("too-small", "rectangle"):
            side = 80 if case == "too-small" else 300
            source.paste((255, 255, 255), (0, 0, side, side + 10))
        elif case.startswith("qr-"):
            version, module_px, quiet = {
                "qr-small": (1, 3, 2),
                "qr-fractional": (10, 6, 3),
                "qr-large": (29, 6, 2),
            }[case]
            with _synthetic_recovery_qr(version, module_px, quiet) as qr:
                if case == "qr-fractional":
                    with qr.resize((361, 361), Image.Resampling.NEAREST) as scaled:
                        source.paste(scaled, (0, 0))
                else:
                    source.paste(qr, (0, 0))
        with source.convert(mode) as image:
            expected = _baseline_normalized_top_left_qr(image)
            actual = s._normalized_top_left_qr(image)
            try:
                assert (actual is None) == (expected is None)
                if expected is not None:
                    assert actual is not None
                    assert actual.mode == expected.mode
                    assert actual.size == expected.size
                    assert actual.tobytes() == expected.tobytes()
                assert image.getpixel((0, 0)) is not None
            finally:
                if expected is not None:
                    expected.close()
                if actual is not None:
                    actual.close()


@pytest.mark.parametrize("recovery", [False, True])
def test_recovery_converts_only_edges_and_candidate_square(monkeypatch, recovery: bool):
    converted_sizes = []
    original = Image.Image.convert

    def convert(image, *args, **kwargs):
        converted_sizes.append(image.size)
        return original(image, *args, **kwargs)

    with Image.new("RGB", (3840, 2160), (20, 50, 90)) as image:
        if recovery:
            with _synthetic_recovery_qr(10) as qr:
                image.paste(qr, (0, 0))
        monkeypatch.setattr(Image.Image, "convert", convert)
        normalized = s._normalized_top_left_qr(image)
        if normalized is not None:
            normalized.close()
    assert converted_sizes[0] == (3840, 2)
    assert sum(w * h for w, h in converted_sizes) < 3840 * 2160 // 20
    if recovery:
        assert converted_sizes == [(3840, 2), (2, 2160), (390, 390)]
    else:
        assert converted_sizes == [(3840, 2)]


@pytest.mark.parametrize("fail_at", [None, 1, 2, 3])
def test_recovery_closes_temporary_images_without_closing_source(monkeypatch, fail_at):
    temporary = []
    original_crop = Image.Image.crop
    original_convert = Image.Image.convert
    calls = 0

    def crop(image, *args, **kwargs):
        result = original_crop(image, *args, **kwargs)
        temporary.append(weakref.ref(result))
        return result

    def convert(image, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise RuntimeError("synthetic conversion failure")
        result = original_convert(image, *args, **kwargs)
        temporary.append(weakref.ref(result))
        return result

    with Image.new("RGB", (1600, 1000), (20, 50, 90)) as image:
        with _synthetic_recovery_qr(10) as qr:
            image.paste(qr, (0, 0))
        monkeypatch.setattr(Image.Image, "crop", crop)
        monkeypatch.setattr(Image.Image, "convert", convert)
        if fail_at is None:
            normalized = s._normalized_top_left_qr(image)
            assert normalized is not None
            normalized.close()
        else:
            with pytest.raises(RuntimeError, match="synthetic conversion failure"):
                s._normalized_top_left_qr(image)
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert all(allocated() is None for allocated in temporary)


@pytest.mark.parametrize("failure", ["sample", "render"])
def test_recovery_releases_temporaries_after_processing_failure(monkeypatch, failure: str):
    temporary = []
    original_convert = Image.Image.convert

    def convert(image, *args, **kwargs):
        result = original_convert(image, *args, **kwargs)
        temporary.append(weakref.ref(result))
        return result

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic processing failure")

    target = "_sample_qr_recovery_candidate" if failure == "sample" else "_render_normalized_qr"
    with Image.new("RGB", (1600, 1000), (20, 50, 90)) as image:
        with _synthetic_recovery_qr(10) as qr:
            image.paste(qr, (0, 0))
        monkeypatch.setattr(Image.Image, "convert", convert)
        monkeypatch.setattr(s, target, fail)
        with pytest.raises(RuntimeError, match="synthetic processing failure"):
            s._normalized_top_left_qr(image)
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert all(allocated() is None for allocated in temporary)


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "P", "L"])
@pytest.mark.parametrize("coordinate", [(0, 0), (1, 0), (0, 1)])
def test_recovery_edge_color_conversion_matches_baseline(mode, coordinate):
    with Image.new("RGB", (700, 550), (20, 50, 90)) as source:
        with _synthetic_recovery_qr(10) as qr:
            source.paste(qr, (0, 0))
        # A high red channel alone must not pass the grayscale white threshold.
        source.putpixel(coordinate, (255, 120, 255))
        with source.convert(mode) as image:
            assert _baseline_normalized_top_left_qr(image) is None
            assert s._normalized_top_left_qr(image) is None
