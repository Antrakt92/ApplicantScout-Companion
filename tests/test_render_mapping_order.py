"""Render mapping keys retain ordering without formatting nested evidence."""

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from itertools import product

import pytest

from applicant_scout.overlay_rows import freeze_render_value, rendered_applicant_key
from applicant_scout.state import Applicant


def _prior_freeze(value):
    if type(value) in {str, int, float, bool, type(None)}:
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return tuple((field.name, _prior_freeze(getattr(value, field.name))) for field in fields(value))
    if isinstance(value, Mapping):
        return tuple(sorted(((_prior_freeze(k), _prior_freeze(v)) for k, v in value.items()), key=repr))
    if isinstance(value, (list, tuple)):
        return tuple(_prior_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_prior_freeze(item) for item in value), key=repr))
    return value


@dataclass(frozen=True)
class _Payload:
    text: str
    values: tuple[int, ...]


@dataclass(frozen=True)
class _DataclassKey:
    label: str


def test_adversarial_string_key_order_matches_prior_freezer():
    alphabet = ["a", "b", "'", '"', "\\", "\n", "\x00", "\u2028", "é", "😀"]
    keys = [""] + ["".join(parts) for length in range(1, 4) for parts in product(alphabet, repeat=length)]
    value = {
        key: {"brackets": [{"key_level": index, "parse_percent": 80}], "payload": _Payload(key, (index,))}
        for index, key in enumerate(keys)
    }
    expected = _prior_freeze(value)
    assert freeze_render_value(value) == expected
    assert freeze_render_value(dict(reversed(list(value.items())))) == expected
    assert freeze_render_value({}) == ()


class _ReprCollisionKey:
    def __repr__(self):
        return "same"


class _StringSubclass(str):
    def __repr__(self):
        return "same"


@pytest.mark.parametrize("key_type", [_ReprCollisionKey, _StringSubclass])
def test_nonexact_string_keys_keep_pair_value_tiebreaker(key_type):
    left = key_type() if key_type is _ReprCollisionKey else key_type("left")
    right = key_type() if key_type is _ReprCollisionKey else key_type("right")
    value = {left: 9, right: 1}
    expected = _prior_freeze(value)
    assert expected[0][0] is right
    assert freeze_render_value(value) == expected


def test_mixed_and_nested_nonstring_keys_match_prior_freezer():
    value = {
        "prefix": [{"long": (1, 2), "short": {"x", "y"}}],
        7: {False: "boolean", None: "none"},
        ("tuple", 3): {_DataclassKey("key"): _Payload("text", (1, 2))},
    }
    assert freeze_render_value(value) == _prior_freeze(value)


def test_nested_in_place_evidence_mutation_changes_applicant_key():
    applicant = Applicant(
        applicant_id="1:1", name="Scout-Realm", cls="WARRIOR", spec_id=71,
        ilvl=480, score=2400, role="DAMAGER",
    )
    applicant.mplus_dps_breakdown = [{"name": "Dungeon", "brackets": [{"key_level": 12, "parse_percent": 80}]}]
    before = rendered_applicant_key(applicant)
    applicant.mplus_dps_breakdown[0]["brackets"][0]["parse_percent"] = 90
    after = rendered_applicant_key(applicant)
    assert before != after
    assert after == tuple(
        (field.name, _prior_freeze(getattr(applicant, field.name)))
        for field in fields(applicant)
        if field.name not in {"raid_boss_parses", "rio_transport_score", "rio_transport_profile", "rio_transport_dungeons"}
    )


def test_string_key_sort_does_not_format_nested_values():
    class ReprProbe:
        calls = 0

        def __repr__(self):
            self.calls += 1
            return "payload"

    probe = ReprProbe()
    value = {"b": [{"brackets": [probe]}], "a": probe}
    expected = _prior_freeze(value)
    assert probe.calls > 0
    probe.calls = 0
    result = freeze_render_value(value)
    assert probe.calls == 0
    assert result == expected
