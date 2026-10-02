"""Corrupt transport numbers are discarded before cached snapshots reach Qt."""

from dataclasses import replace
import json

import pytest

from applicant_scout.__main__ import StateMachine
from applicant_scout.compatibility import MINIMUM_ADDON_VERSION
from applicant_scout.live_snapshot_cache import (
    live_snapshot_cache_path,
    load_live_snapshot,
    save_live_snapshot,
)
from applicant_scout.state import AppState
from support.snapshot_builders import _live_snapshot
from test_overlay_fetch_identity import _window


_UINT8_RIO_FIELDS = (
    "rio_best_key", "rio_best_dungeon_key", "rio_timed_at_or_above",
    "rio_timed_at_or_above_minus1", "rio_timed_at_or_above_minus2",
    "rio_completed_at_or_above_minus1", "rio_dungeon_count",
)
_FIELDS = [
    ("listing", "activity_id", 0, 0xFFFFFFFF),
    ("listing", "key_level", 0, 0xFF),
    ("listing", "category_id", 0, 0xFFFF),
    ("listing", "difficulty_id", 0, 0xFFFF),
    ("leader_key", "key_level", 0, 0xFF),
    ("leader_key", "challenge_map_id", 0, 0xFFFF),
    ("version", "region_id", 0, 0xFF),
    ("applicants", "applicant_id", 0, 0xFFFFFFFF),
    ("applicants", "member_idx", 1, 5),
    *[
        (surface, field, 0, 0xFFFF)
        for surface in ("applicants", "roster")
        for field in ("spec_id", "ilvl", "score", "main_score")
    ],
    *[
        (surface, field, 0, 0xFF)
        for surface in ("applicants", "roster")
        for field in ("class_id", *_UINT8_RIO_FIELDS)
    ],
    *[(surface, "role", 0, 3) for surface in ("applicants", "roster")],
    *[( "roster", field, 0, 0xFF) for field in ("unit_index", "flags", "subgroup")],
]


def _cached_payload(tmp_path):
    assert save_live_snapshot(tmp_path, _live_snapshot(), now=100)
    path = live_snapshot_cache_path(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["snapshot"]["leader_key"] = {
        "key_level": 14, "challenge_map_id": 375, "player_name": "Host-Realm",
    }
    return path, data


def _row(data, surface):
    value = data["snapshot"][surface]
    return value[0] if surface in ("applicants", "roster") else value


@pytest.mark.parametrize("surface,field,minimum,maximum", _FIELDS)
@pytest.mark.parametrize("boundary", ["below", "above", "minimum", "maximum"])
def test_cached_core_integer_domains(tmp_path, surface, field, minimum, maximum, boundary):
    path, data = _cached_payload(tmp_path)
    value = {
        "below": minimum - 1, "above": maximum + 1,
        "minimum": minimum, "maximum": maximum,
    }[boundary]
    _row(data, surface)[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")

    restored = load_live_snapshot(tmp_path, now=101)

    if boundary in ("below", "above"):
        assert restored is None
        assert not path.exists()
    else:
        assert restored is not None
        actual = getattr(restored.snapshot, surface)
        actual = actual[0] if surface in ("applicants", "roster") else actual
        assert getattr(actual, field) == value
        assert path.exists()


@pytest.mark.parametrize(
    "surface,field",
    [("listing", "key_level"), ("listing", "activity_id"),
     ("applicants", "score"), ("roster", "role")],
)
@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_cached_core_numbers_remain_strict_integers(tmp_path, surface, field, value):
    path, data = _cached_payload(tmp_path)
    _row(data, surface)[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")

    assert load_live_snapshot(tmp_path, now=101) is None
    assert not path.exists()


@pytest.mark.parametrize("field", ["score", "key_level"])
def test_corrupt_cache_is_discarded_and_fresh_snapshot_recovers_table(qtbot, tmp_path, field):
    path, data = _cached_payload(tmp_path)
    data["snapshot"]["version"]["addon_version"] = MINIMUM_ADDON_VERSION
    if field == "key_level":
        data["snapshot"]["listing"]["key_level"] = 10**400
    else:
        applicant = data["snapshot"]["applicants"][0]
        applicant["score"] = 10**400
        applicant["rio_profile"] = False
        applicant["rio_dungeons"] = []
        for key, value in list(applicant.items()):
            if key.startswith("rio_") and type(value) is int:
                applicant[key] = 0
    partner = dict(data["snapshot"]["applicants"][0])
    partner.update(member_idx=2, name="Partner-Realm", score=3000)
    data["snapshot"]["applicants"].append(partner)
    path.write_text(json.dumps(data), encoding="utf-8")

    assert load_live_snapshot(tmp_path, now=101) is None
    assert not path.exists()
    state = AppState()
    machine = StateMachine(state)
    window, client = _window(qtbot, tmp_path, state)
    try:
        window._select_tab_state("applicants")
        window._refresh_table()
        assert window._table.rowCount() == 0

        fresh = _live_snapshot()
        assert fresh.version is not None
        fresh = replace(
            fresh,
            version=replace(fresh.version, addon_version=MINIMUM_ADDON_VERSION),
            applicants=[
                fresh.applicants[0],
                replace(fresh.applicants[0], member_idx=2, name="Partner-Realm"),
            ],
        )
        assert save_live_snapshot(tmp_path, fresh, now=102)
        restored = load_live_snapshot(tmp_path, now=103)
        assert restored is not None
        machine.apply_snapshot(restored.snapshot)
        window._refresh_table()
        assert len(state.applicants) == 2
        assert window._table.rowCount() == 2
    finally:
        window.shutdown_fetches()
        window.close()
        client.close()


def test_cached_dungeon_extension_preserves_finite_float_values(tmp_path):
    path, data = _cached_payload(tmp_path)
    rows = [{"name": "Theater of Pain", "key_level": 14, "score": 123.5}]
    data["snapshot"]["applicants"][0]["rio_dungeons"] = rows
    path.write_text(json.dumps(data), encoding="utf-8")

    restored = load_live_snapshot(tmp_path, now=101)

    assert restored is not None
    assert restored.snapshot.applicants[0].rio_dungeons == rows

