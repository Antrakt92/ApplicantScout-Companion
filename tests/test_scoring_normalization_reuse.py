"""Per-evaluation dungeon normalization preserves complete scoring results."""

from copy import deepcopy
from dataclasses import replace

import pytest

import applicant_scout.scoring as scoring
from test_scoring import MPLUS_DUNGEONS, _app, _dungeon, _listing


def _evidence(case):
    if case == "dense":
        return [
            _dungeon(name, [(key, 83, 66, 3) for key in range(2, 23)])
            for name in MPLUS_DUNGEONS
        ]
    if case == "aliases":
        return [
            _dungeon(name, [(12, 88, 70, 3)])
            for name in (" SKYREACH ", "Skyreach", "Algeth’ar Academy")
        ]
    if case == "gray":
        return [_dungeon("Skyreach", [(12, 8, 4, 1), (10, 12, 6, 0)])]
    if case == "invalid":
        return [
            None,
            {},
            {"name": 123},
            {"name": " "},
            {
                "name": "Skyreach",
                "brackets": [
                    None,
                    {},
                    {"key_level": -1, "parse_percent": 90},
                    {"key_level": 12, "parse_percent": float("nan")},
                ],
            },
        ]
    return []


def _uncached_dungeon_key(signal):
    key = scoring.normalise_dungeon_name(signal.dungeon_name)
    return key if key and signal.key_level > 0 else None


@pytest.mark.parametrize("case", ["dense", "aliases", "gray", "invalid", "empty"])
@pytest.mark.parametrize("role", ["DAMAGER", "HEALER", "TANK"])
@pytest.mark.parametrize("matching_provenance", [False, True])
def test_complete_candidate_package_and_detail_results_match_uncached_reference(
    monkeypatch, case, role, matching_provenance
):
    listing = _listing(key_level=12)
    evidence = _evidence(case)
    app = _app(
        role=role,
        dps_breakdown=deepcopy(evidence),
        hps_breakdown=deepcopy(evidence),
        rio_profile=True,
        rio_best_key=14,
        rio_best_dungeon_key=12,
        rio_summary_target_key=12,
        rio_summary_activity_id=listing.activity_id if matching_provenance else 999,
        rio_summary_dungeon_name=listing.dungeon_name
        if matching_provenance
        else "Other dungeon",
        rio_dungeons=[{"name": "Skyreach", "key_level": 12, "timed": True}],
    )
    partner = replace(app, applicant_id="1:2", name="Partner-Realm")
    before_inputs = deepcopy((app, partner, listing))

    def results():
        return (
            scoring.candidate_fit(app, listing),
            scoring.package_fit([app, partner], listing),
            scoring.mplus_dungeon_fit_rows(app, listing),
        )

    actual = results()
    monkeypatch.setattr(scoring, "_mplus_wcl_dungeon_key", _uncached_dungeon_key)
    assert results() == actual
    assert (app, partner, listing) == before_inputs


@pytest.mark.parametrize("name", ["Other dungeon", "", " SKYREACH "])
def test_replaced_generated_signal_name_uses_fresh_normalization(name):
    signal = scoring._mplus_wcl_signals(
        _app(dps_breakdown=_evidence("dense")), _listing(key_level=12)
    )[0]
    changed = replace(signal, dungeon_name=name)
    assert scoring._mplus_wcl_dungeon_key(changed) == _uncached_dungeon_key(changed)


def test_generated_signals_reuse_normalization_in_downstream_consumers(monkeypatch):
    calls = []
    original = scoring.normalise_dungeon_name

    def counted(name):
        calls.append(name)
        return original(name)

    monkeypatch.setattr(scoring, "normalise_dungeon_name", counted)
    signals = scoring._mplus_wcl_signals(
        _app(dps_breakdown=_evidence("dense")), _listing(key_level=12)
    )
    assert len(signals) == len(MPLUS_DUNGEONS) * 21
    count_after_generation = len(calls)
    for _ in range(3):
        assert all(scoring._mplus_wcl_dungeon_key(signal) for signal in signals)
        scoring._mplus_representative_wcl_signals(signals, 12)
        scoring._mplus_wcl_dungeon_key_levels(signals, 12)
    assert len(calls) == count_after_generation


def test_mutable_input_rename_is_reflected_in_next_evaluation(monkeypatch):
    listing = _listing(key_level=12)
    evidence = [_dungeon("Skyreach", [(12, 90, 80, 3)])]
    app = _app(score=0, dps_breakdown=evidence)
    before = scoring.candidate_fit(app, listing)
    evidence[0]["name"] = "Pit of Saron"
    after = scoring.candidate_fit(app, listing)
    assert after != before
    assert before.same_dungeon_wcl_best == 90
    assert after.same_dungeon_wcl_best is None
    monkeypatch.setattr(scoring, "_mplus_wcl_dungeon_key", _uncached_dungeon_key)
    assert scoring.candidate_fit(app, listing) == after


def test_legacy_direct_signal_construction_keeps_normalization_fallback():
    signal = scoring._MPlusWCLSignal(" SKYREACH ", 12, 80, 65, 3)
    assert scoring._mplus_wcl_dungeon_key(signal) == scoring.normalise_dungeon_name(
        "Skyreach"
    )
    invalid = scoring._MPlusWCLSignal("Skyreach", 0, 80, 65, 3)
    assert scoring._mplus_wcl_dungeon_key(invalid) is None
