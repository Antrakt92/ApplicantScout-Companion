from __future__ import annotations

import json
import logging

from applicant_scout import atomic_io
from applicant_scout.state import (
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    LauncherPosition,
    WINDOW_GEOMETRY_LAYOUT_VERSION,
    WindowGeometry,
    load_geometry,
    load_launcher_position,
    save_geometry,
    save_launcher_position,
)


def _write_geometry(tmp_path, data) -> None:
    (tmp_path / "window.json").write_text(json.dumps(data), encoding="utf-8")


def test_load_geometry_defaults_for_missing_file(tmp_path):
    assert load_geometry(tmp_path) == WindowGeometry()


def test_load_geometry_defaults_for_malformed_json(tmp_path):
    (tmp_path / "window.json").write_text("{bad", encoding="utf-8")

    assert load_geometry(tmp_path) == WindowGeometry()


def test_load_geometry_defaults_for_non_dict_json(tmp_path):
    _write_geometry(tmp_path, ["not", "a", "dict"])

    assert load_geometry(tmp_path) == WindowGeometry()


def test_load_geometry_ignores_unknown_keys(tmp_path):
    _write_geometry(
        tmp_path,
        {
            "x": 12,
            "y": 34,
            "w": 700,
            "h": 650,
            "layout_version": WINDOW_GEOMETRY_LAYOUT_VERSION,
            "surprise": "ignored",
        },
    )

    assert load_geometry(tmp_path) == WindowGeometry(
        12,
        34,
        700,
        650,
        WINDOW_GEOMETRY_LAYOUT_VERSION,
    )


def test_load_geometry_coerces_integral_strings(tmp_path):
    _write_geometry(
        tmp_path,
        {
            "x": "-20",
            "y": "45",
            "w": "800",
            "h": "600",
            "layout_version": "4",
        },
    )

    assert load_geometry(tmp_path) == WindowGeometry(-20, 45, 800, 600, 4)


def test_load_geometry_defaults_invalid_scalar_fields_independently(tmp_path):
    _write_geometry(
        tmp_path,
        {
            "x": None,
            "y": {},
            "w": "900",
            "h": [],
            "layout_version": "bad",
        },
    )

    assert load_geometry(tmp_path) == WindowGeometry(
        100,
        100,
        900,
        DEFAULT_WINDOW_HEIGHT,
        WINDOW_GEOMETRY_LAYOUT_VERSION,
    )


def test_load_geometry_rejects_bool_float_and_fractional_strings(tmp_path):
    _write_geometry(
        tmp_path,
        {
            "x": True,
            "y": 2.0,
            "w": "100.5",
            "h": False,
            "layout_version": 3.0,
        },
    )

    assert load_geometry(tmp_path) == WindowGeometry()


def test_load_geometry_defaults_invalid_dimensions(tmp_path):
    _write_geometry(
        tmp_path,
        {
            "x": 10,
            "y": 20,
            "w": 0,
            "h": "-1",
            "layout_version": 2,
        },
    )

    assert load_geometry(tmp_path) == WindowGeometry(
        10,
        20,
        DEFAULT_WINDOW_WIDTH,
        DEFAULT_WINDOW_HEIGHT,
        2,
    )


def test_load_geometry_missing_layout_version_is_legacy_v1(tmp_path):
    _write_geometry(tmp_path, {"x": 1, "y": 2, "w": 3, "h": 4})

    assert load_geometry(tmp_path) == WindowGeometry(1, 2, 3, 4, 1)


def test_save_geometry_round_trips_after_atomic_write(tmp_path):
    geo = WindowGeometry(12, 34, 700, 650, WINDOW_GEOMETRY_LAYOUT_VERSION)

    save_geometry(tmp_path, geo)

    assert load_geometry(tmp_path) == geo


def test_save_geometry_failed_replace_preserves_previous_geometry(
    monkeypatch, caplog, tmp_path
):
    old_geo = WindowGeometry(1, 2, 300, 400, 3)
    save_geometry(tmp_path, old_geo)

    def fail_replace(_src: object, _dst: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)

    with caplog.at_level(logging.WARNING, logger="applicant_scout.state"):
        save_geometry(tmp_path, WindowGeometry(9, 9, 900, 900, 5))

    assert load_geometry(tmp_path) == old_geo
    assert "Failed to save window geometry" in caplog.text
    assert list(tmp_path.glob(".window.json.*.tmp")) == []


def test_launcher_position_round_trips(tmp_path):
    save_launcher_position(tmp_path, LauncherPosition(321, 234))

    assert load_launcher_position(tmp_path) == LauncherPosition(321, 234)


def test_load_launcher_position_rejects_malformed_coordinates(tmp_path):
    (tmp_path / "launcher.json").write_text(
        '{"x": "left", "y": "top"}',
        encoding="utf-8",
    )

    assert load_launcher_position(tmp_path) is None
