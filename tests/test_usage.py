from __future__ import annotations

from datetime import date, timedelta
import json
import threading
import time
from uuid import UUID

import httpx
import pytest

from applicant_scout import atomic_io
import applicant_scout.usage as usage


ENDPOINT = "https://usage.example.com/v1/events"


@pytest.fixture(autouse=True)
def fast_private_writes(monkeypatch):
    def write(path, text, *, private):
        assert private is True
        atomic_io.atomic_write_text(path, text, private=False)
    monkeypatch.setattr(usage, "atomic_write_text", write)
    monkeypatch.setattr(usage, "_RETRY_DELAYS", (0.01, 0.02))


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "usage worker did not settle"
        time.sleep(0.005)


class Recorder:
    def __init__(self, status=204):
        self.events = []
        self.status = status

    def __call__(self, endpoint, payload):
        assert endpoint == ENDPOINT
        self.events.append(payload)
        return self.status


def client(tmp_path, sender, **kwargs):
    return usage.UsageClient(
        tmp_path, kwargs.pop("version", "0.16.0"), endpoint=ENDPOINT,
        _sender=sender, **kwargs,
    )


def drain(instance):
    wait_until(lambda: not instance._pending)


def test_no_identity_no_disk_no_replay_before_consent(tmp_path):
    sender = Recorder()
    instance = client(tmp_path, sender)
    assert not instance.consent_enabled
    assert not instance.record("addon_received")
    assert not instance.record("wcl_result")
    assert list(tmp_path.iterdir()) == []
    instance.set_consent(True)
    assert instance.consent_enabled
    drain(instance)
    assert [event["event"] for event in sender.events] == ["consent_started", "version_seen"]
    instance.close()


@pytest.mark.parametrize("saved_consent", [False, True])
def test_unavailable_usage_thread_does_not_abort_startup_or_settings(
    tmp_path, monkeypatch, saved_consent,
):
    if saved_consent:
        (tmp_path / usage.USAGE_STATE_FILENAME).write_text(
            json.dumps({"schema": 1, "consent": True, "install_id": "", "seen": []}),
            encoding="utf-8",
        )

    def cannot_start(_thread):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    sender = Recorder()
    instance = client(tmp_path, sender)
    if not saved_consent:
        instance.set_consent(True)

    assert instance.consent_enabled
    assert not instance.record("addon_received")
    assert not instance._pending
    assert sender.events == []
    instance.close()


def test_payload_allowlist_and_stable_identity_across_restart_update(tmp_path):
    sender = Recorder()
    instance = client(tmp_path, sender, consent=True, _day=lambda: "2026-09-08")
    assert not instance.record("Player-Realm")
    for event in usage.USAGE_EVENTS:
        instance.record(event)
    drain(instance)
    assert len(sender.events) == 5
    first_id = sender.events[0]["install_id"]
    assert UUID(first_id).version == 4
    for event in sender.events:
        assert set(event) == {"schema", "install_id", "version", "event", "day"}
        assert event["install_id"] == first_id
        assert event["schema"] == 1
        assert event["day"] == "2026-09-08"
    instance.close()
    restarted = client(tmp_path, sender, _day=lambda: "2026-09-08")
    assert restarted.consent_enabled
    for event in usage.USAGE_EVENTS:
        restarted.record(event)
    drain(restarted)
    assert len(sender.events) == 5
    restarted.close()
    updated = client(tmp_path, sender, version="0.17.0", _day=lambda: "2026-09-09")
    for event in usage.USAGE_EVENTS:
        updated.record(event)
    drain(updated)
    assert len(sender.events) == 9
    assert all(e["install_id"] == first_id for e in sender.events)
    assert sum(e["event"] == "consent_started" for e in sender.events) == 1
    updated.close()


def test_daily_activity_and_once_per_version(tmp_path):
    sender = Recorder()
    day = ["2026-09-08"]
    instance = client(tmp_path, sender, consent=True, _day=lambda: day[0])
    for day_value in ("2026-09-08", "2026-09-09"):
        day[0] = day_value
        for event in usage.USAGE_EVENTS:
            instance.record(event)
        drain(instance)
    assert len(sender.events) == 8
    assert sum(e["event"] == "version_seen" for e in sender.events) == 1
    instance.close()


def test_revoke_cancels_queued_and_retry_then_reenrolls_fresh_identity(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    sender = Recorder(status=503)

    def blocked(endpoint, payload):
        result = sender(endpoint, payload)
        entered.set()
        assert release.wait(3)
        return result

    instance = client(tmp_path, blocked, consent=True)
    instance.record("consent_started")
    assert entered.wait(3)
    instance.record("addon_received")
    instance.record("wcl_result")
    instance.set_consent(False)
    assert not instance.consent_enabled
    assert not instance.record("setup_completed")
    assert json.loads((tmp_path / "usage.json").read_text()) == {"schema": 1, "consent": False}
    release.set()
    instance.set_consent(True)
    sender.status = 204
    instance.record("consent_started")
    drain(instance)
    assert len(sender.events) == 3
    assert sender.events[0]["install_id"] != sender.events[1]["install_id"]
    assert [e["event"] for e in sender.events] == ["consent_started", "consent_started", "version_seen"]
    instance.close()


@pytest.mark.parametrize("status,expected", [(400, 1), (302, 1), (204, 1), (429, 3), (503, 3)])
def test_bounded_retry_and_only_unacknowledged_events_retry_on_restart(tmp_path, status, expected):
    sender = Recorder(status=status)
    instance = client(tmp_path, sender, consent=True)
    drain(instance)
    sender.events.clear()
    instance.record("addon_received")
    drain(instance)
    assert len(sender.events) == expected
    assert all(e == sender.events[0] for e in sender.events)
    assert not instance.record("addon_received")
    instance.close()
    restarted = client(tmp_path, sender)
    restarted.record("addon_received")
    drain(restarted)
    addon_events = [event for event in sender.events if event["event"] == "addon_received"]
    assert len(addon_events) == expected * (1 if status == 204 else 2)
    restarted.close()


def test_network_exception_is_bounded(tmp_path):
    calls = []

    def failing(endpoint, payload):
        calls.append((endpoint, payload))
        raise httpx.ConnectError("offline")

    instance = client(tmp_path, failing, consent=True)
    instance.record("addon_received")
    drain(instance)
    assert len(calls) == 9
    instance.close()


def test_offline_first_optin_recovers_startup_milestones_on_restart(tmp_path):
    sender = Recorder(status=503)
    instance = client(tmp_path, sender, consent=True)
    drain(instance)
    assert len(sender.events) == 6
    state = json.loads((tmp_path / "usage.json").read_text())
    assert state["install_id"]
    assert state["seen"] == []
    instance.close()
    sender.status = 204
    sender.events.clear()
    restarted = client(tmp_path, sender)
    drain(restarted)
    assert [e["event"] for e in sender.events] == ["consent_started", "version_seen"]
    assert all(e["install_id"] == state["install_id"] for e in sender.events)
    restarted.close()


def test_identity_must_be_durable_before_any_send(tmp_path, monkeypatch):
    original_write = usage.atomic_write_text

    def fail_identity_save(path, text, *, private):
        if json.loads(text).get("install_id"):
            raise OSError("disk full")
        original_write(path, text, private=private)

    monkeypatch.setattr(usage, "atomic_write_text", fail_identity_save)
    sender = Recorder()
    instance = client(tmp_path, sender, consent=True)
    wait_until(lambda: instance._failed)
    assert sender.events == []
    assert not instance.record("addon_received")
    instance.close()


def test_crash_after_ack_retries_same_identity_tuple_for_service_dedup(tmp_path, monkeypatch):
    original_write = usage.atomic_write_text

    def fail_ack_save(path, text, *, private):
        if json.loads(text).get("seen"):
            raise OSError("disk full")
        original_write(path, text, private=private)

    monkeypatch.setattr(usage, "atomic_write_text", fail_ack_save)
    sender = Recorder()
    instance = client(tmp_path, sender, consent=True)
    wait_until(lambda: instance._failed)
    assert len(sender.events) == 1
    accepted = sender.events[0]
    instance.close()
    monkeypatch.setattr(usage, "atomic_write_text", original_write)
    restarted = client(tmp_path, sender)
    drain(restarted)
    assert sender.events[1] == accepted
    assert sender.events[2]["event"] == "version_seen"
    restarted.close()


def test_corrupt_state_never_silently_reenrolls(tmp_path):
    (tmp_path / "usage.json").write_text('{"consent": true, "install_id": "Player-Realm"}')
    sender = Recorder()
    instance = client(tmp_path, sender)
    assert not instance.consent_enabled
    assert not instance.record("addon_received")
    assert sender.events == []
    instance.close()


def test_consent_save_failure_stays_off(tmp_path, monkeypatch):
    def failed_write(*_args, **_kwargs):
        raise OSError("disk full")

    instance = client(tmp_path, Recorder())
    monkeypatch.setattr(usage, "atomic_write_text", failed_write)
    with pytest.raises(usage.UsagePersistenceError, match="Reporting is off"):
        instance.set_consent(True)
    assert not instance.consent_enabled
    assert not instance.record("consent_started")
    instance.close()


def test_acknowledgement_save_failure_stops_further_sends(tmp_path, monkeypatch):
    sender = Recorder()
    instance = client(tmp_path, sender, consent=True)
    drain(instance)
    sender.events.clear()

    def failed_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(usage, "atomic_write_text", failed_write)
    instance.record("addon_received")
    wait_until(lambda: instance._failed)
    assert [e["event"] for e in sender.events] == ["addon_received"]
    assert not instance.record("wcl_result")
    instance.close()


def test_optout_save_failure_still_cancels_session(tmp_path, monkeypatch):
    instance = client(tmp_path, Recorder(), consent=True)

    def failed_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(usage, "atomic_write_text", failed_write)
    with pytest.raises(usage.UsagePersistenceError):
        instance.set_consent(False)
    assert not instance.consent_enabled
    assert not instance.record("addon_received")
    instance.close()


def test_revoke_during_worker_save_cannot_restore_consent(tmp_path, monkeypatch):
    sender = Recorder()
    instance = client(tmp_path, sender, consent=True)
    drain(instance)
    sender.events.clear()
    original_write = usage.atomic_write_text
    entered = threading.Event()
    release = threading.Event()

    def blocked_write(path, text, *, private):
        if "addon_received" in text:
            entered.set()
            assert release.wait(3)
        original_write(path, text, private=private)

    monkeypatch.setattr(usage, "atomic_write_text", blocked_write)
    instance.record("addon_received")
    assert entered.wait(3)
    revoke = threading.Thread(target=lambda: instance.set_consent(False))
    revoke.start()
    wait_until(lambda: not instance.consent_enabled)
    release.set()
    revoke.join(3)
    assert not revoke.is_alive()
    assert json.loads((tmp_path / "usage.json").read_text()) == {"schema": 1, "consent": False}
    assert [e["event"] for e in sender.events] == ["addon_received"]
    instance.close()


def test_maintainer_marker_disables_even_injected_transport(tmp_path):
    (tmp_path / usage.USAGE_TEST_MARKER_FILENAME).touch()
    sender = Recorder()
    instance = client(tmp_path, sender, consent=True)
    assert instance.consent_enabled
    assert not instance.collection_available
    assert not instance.record("addon_received")
    assert sender.events == []
    assert json.loads((tmp_path / "usage.json").read_text())["install_id"] == ""
    instance.close()


@pytest.mark.parametrize("state", [
    "x" * 65537,
    json.dumps({"schema": True, "consent": True}),
    json.dumps({"schema": 1, "consent": "true"}),
    json.dumps({"schema": 1, "consent": True, "install_id": "Player-Realm"}),
    json.dumps({"schema": 1, "consent": True, "seen": ["Player-Realm"]}),
], ids=["oversize", "boolean-schema", "string-consent", "invalid-uuid", "invalid-history"])
def test_untrusted_local_state_fails_closed(tmp_path, state):
    (tmp_path / "usage.json").write_text(state)
    sender = Recorder()
    instance = client(tmp_path, sender)
    assert not instance.consent_enabled
    assert not instance.record("addon_received")
    assert sender.events == []
    instance.close()


def test_record_and_shutdown_do_not_wait_for_network_and_queue_is_bounded(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def blocked(_endpoint, _payload):
        entered.set()
        assert release.wait(3)
        return 204

    day = [date(2026, 9, 8)]
    instance = client(tmp_path, blocked, consent=True, _day=lambda: day[0].isoformat())
    instance.record("consent_started")
    assert entered.wait(3)
    start = time.monotonic()
    accepted = 0
    for _ in range(100):
        day[0] += timedelta(days=1)
        accepted += instance.record("addon_received")
    assert accepted == 31
    assert time.monotonic() - start < 0.5
    start = time.monotonic()
    instance.close()
    assert time.monotonic() - start < 0.5
    assert not instance.record("wcl_result")
    release.set()


@pytest.mark.parametrize("endpoint", [
    "", "http://example.com", "https://user:secret@example.com", "https://example.com?name=x",
    "https://example.com#secret", " https://example.com", "https://example.com:123",
    "https://example.com\n", "https://[bad", "https:///missing",
])
def test_invalid_endpoint_fails_closed(tmp_path, endpoint):
    sender = Recorder()
    instance = usage.UsageClient(tmp_path, "0.16.0", endpoint=endpoint, _sender=sender, consent=True)
    assert not instance.reporting_available
    assert not instance.record("consent_started")
    assert sender.events == []
    instance.close()


@pytest.mark.parametrize("version", [
    "Player-Realm", "0.16.0\n", "0.16.0+hostname", "", "1.2", "01.2.3", "1.02.3", "1.2.03",
])
def test_version_cannot_carry_arbitrary_metadata(tmp_path, version):
    instance = client(tmp_path, Recorder(), consent=True, version=version)
    assert not instance.record("addon_received")
    instance.close()


def test_runtime_source_ci_and_maintainer_exclusion(tmp_path, monkeypatch):
    monkeypatch.setattr(usage.sys, "frozen", False, raising=False)
    instance = usage.UsageClient(tmp_path, "0.16.0", endpoint=ENDPOINT, consent=True)
    assert not instance.record("consent_started")
    instance.close()
    monkeypatch.setattr(usage.sys, "frozen", True)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    for key in ("CI", "GITHUB_ACTIONS", "APSCOUT_USAGE_DISABLED"):
        monkeypatch.setenv(key, "1")
        assert not usage._production_runtime()
        monkeypatch.delenv(key)
    assert usage._production_runtime()
    excluded = client(tmp_path, Recorder(), test_installation=True)
    assert not excluded.record("addon_received")
    excluded.close()


def test_transport_does_not_follow_redirects_or_read_response_body(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example.com/"})

    real_client = httpx.Client

    def fake_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"].connect == 3
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(usage.httpx, "Client", fake_client)
    assert usage._send_event(ENDPOINT, {"schema": 1}) == 302
    assert len(seen) == 1
    assert "authorization" not in seen[0].headers
    assert "cookie" not in seen[0].headers
