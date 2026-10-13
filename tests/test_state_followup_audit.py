"""Evidence eligibility must survive state projection and privacy transitions."""

from dataclasses import replace

import pytest

from applicant_scout.__main__ import StateMachine
from applicant_scout.metric_preferences import MetricPreferences
from applicant_scout.screenshot import DecodedApplicant, DecodedListing, Snapshot
from applicant_scout.state import Applicant, AppState


ALL = MetricPreferences(mplus=True, raid_normal=True, raid_heroic=True, raid_mythic=True)
BLOCKED = ["restricted", "error", "empty", "unavailable", "disabled"]


def _app():
    return Applicant("1:1", "Scout-Ravencrest", "WARRIOR", 71, 700, 3000, "DAMAGER",
                     fetch_status="ready", wcl_metric_preferences=ALL,
                     raid_heroic=99, raid_heroic_median=98, raid_mythic=70,
                     raid_evidence={"H": {"boss_count": 1, "total_bosses": 9}},
                     mplus_dps=99, mplus_dps_median=98,
                     mplus_dps_breakdown=[{"name": "Pit of Saron", "parse_percent": 99}],
                     raid_boss_parses={"H": [{"overall": 99}], "M": [{"overall": 70}]},
                     raid_boss_availability={"H": "available", "M": "available"},
                     rio_raid_progress={"H": {"killed": 1, "total": 9}})


@pytest.mark.parametrize("status", BLOCKED)
def test_state_projection_retracts_blocked_scalars_without_erasing_independent_detail(status):
    app = _app()
    app.wcl_availability = {"raid_heroic": status, "raid_mythic": "available", "mplus": status}
    app.project_wcl_data_to_preferences(ALL)
    assert app.raid_heroic is None
    assert app.raid_heroic_median is None
    assert "H" not in app.raid_evidence
    assert app.mplus_dps is None
    assert app.mplus_dps_median is None
    assert app.mplus_dps_breakdown == []
    assert app.raid_mythic == 70
    assert app.raid_boss_parses["H"] == [{"overall": 99}]
    assert app.rio_raid_progress["H"]["killed"] == 1


@pytest.mark.parametrize("status", BLOCKED)
def test_state_projection_retracts_independently_blocked_manual_rows(status):
    app = _app()
    app.raid_boss_availability["H"] = status
    app.project_wcl_data_to_preferences(ALL)
    assert "H" not in app.raid_boss_parses
    assert app.raid_boss_parses["M"] == [{"overall": 70}]
    assert app.raid_heroic == 99
    assert app.rio_raid_progress["H"]["killed"] == 1


def test_private_proof_retracts_old_detail_but_fresh_manual_proof_can_restore_access():
    app = _app()
    assert app.retract_private_raid_details({"raid_heroic": "restricted", "raid_mythic": "available"})
    assert "H" not in app.raid_boss_parses
    assert app.raid_boss_availability["H"] == "restricted"
    assert app.raid_boss_parses["M"] == [{"overall": 70}]
    assert app.rio_raid_progress["H"]["killed"] == 1
    app.wcl_availability = {"raid_heroic": "restricted"}
    app.raid_boss_parses["H"] = [{"overall": 24}]
    app.raid_boss_availability["H"] = "available"
    app.project_wcl_data_to_preferences(ALL)
    assert app.raid_boss_parses["H"] == [{"overall": 24}]
    app.project_wcl_data_to_preferences(replace(ALL, raid_heroic=False))
    assert "H" not in app.raid_boss_parses


@pytest.mark.parametrize("class_id", [0, 1])
def test_reused_application_slot_cannot_transfer_missing_class_spec_or_gear_to_new_character(class_id):
    state = AppState()
    machine = StateMachine(state)
    listing = DecodedListing(401, 14, "Pit of Saron", "+14", "")
    first = DecodedApplicant(1, 1, 71, 700, 3000, 2, "Scout-Ravencrest")
    machine.apply_snapshot(Snapshot(listing=listing, version=None, applicants=[first]))
    previous = state.applicants["1:1"]
    previous.fetch_status = "ready"
    previous.raid_heroic = 99

    changed = replace(first, name="Other-Ravencrest", class_id=class_id, spec_id=0, ilvl=0)
    machine.apply_snapshot(Snapshot(listing=listing, version=None, applicants=[changed]))
    current = state.applicants["1:1"]
    assert current.name == "Other-Ravencrest"
    assert current.cls == ("WARRIOR" if class_id else "?")
    assert current.spec_id == 0 and current.ilvl == 0
    assert current.raid_heroic is None and current.fetch_status == "pending"


def test_explicit_class_change_cannot_inherit_old_unknown_gear():
    state = AppState()
    machine = StateMachine(state)
    listing = DecodedListing(401, 14, "Pit of Saron", "+14", "")
    first = DecodedApplicant(1, 1, 71, 700, 3000, 2, "Scout-Ravencrest")
    machine.apply_snapshot(Snapshot(listing=listing, version=None, applicants=[first]))
    previous = state.applicants["1:1"]
    previous.fetch_status = "ready"
    previous.raid_heroic = 99

    machine.apply_snapshot(Snapshot(
        listing=listing, version=None,
        applicants=[replace(first, class_id=5, spec_id=0, ilvl=0)],
    ))
    current = state.applicants["1:1"]
    assert current.cls == "PRIEST" and current.spec_id == 0
    assert current.ilvl == 0
    assert current.raid_heroic is None and current.fetch_status == "pending"


def test_older_aggregate_privacy_preserves_newer_manual_scope_only():
    app = _app()
    app.raid_boss_publication_epochs = {"H": 6, "M": 4}
    assert app.retract_private_raid_details(
        {"raid_heroic": "restricted", "raid_mythic": "restricted"},
        expected_publication_epoch=5,
    )
    assert app.raid_boss_parses["H"] == [{"overall": 99}]
    assert app.raid_boss_availability["H"] == "available"
    assert app.raid_boss_publication_epochs == {"H": 6}
    assert "M" not in app.raid_boss_parses
    assert app.raid_boss_availability["M"] == "restricted"


def test_transport_copy_preserves_newer_manual_receipt_and_reset_clears_it():
    source = _app()
    source.raid_boss_publication_epochs = {"H": 6}
    target = _app()
    StateMachine._copy_wcl_data(source, target)
    assert not target.retract_private_raid_details(
        {"raid_heroic": "restricted"}, expected_publication_epoch=5,
    )
    assert target.raid_boss_parses["H"] == [{"overall": 99}]
    assert target.raid_boss_publication_epochs == {"H": 6}
    target.clear_wcl_data()
    assert target.raid_boss_publication_epochs == {}
    assert source.raid_boss_publication_epochs == {"H": 6}


def test_disabled_detail_scope_drops_receipt_but_private_detail_keeps_its_newer_proof():
    app = _app()
    app.raid_boss_publication_epochs = {"H": 6, "M": 7}
    app.raid_boss_availability["M"] = "restricted"
    app.project_wcl_data_to_preferences(replace(ALL, raid_heroic=False))
    assert app.raid_boss_publication_epochs == {"M": 7}
    assert app.raid_boss_availability["M"] == "restricted"
    assert "M" not in app.raid_boss_parses
