"""An explicit installer choice grants consent only before saved preferences."""
from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from applicant_scout import atomic_io, usage


CHOICE_FILENAME = "usage-installer-choice"


@pytest.fixture(autouse=True)
def isolated_private_writes(monkeypatch):
    def write(path, text, *, private, publication_guard=None):
        assert private is True
        atomic_io.atomic_write_text(
            path, text, private=False, publication_guard=publication_guard,
        )

    monkeypatch.setattr(usage, "atomic_write_text", write)


def _client(path, events, **kwargs):
    def sender(_endpoint, payload):
        events.append(payload)
        return 204

    return usage.UsageClient(path, "0.16.0", endpoint="https://usage.example.com/v1/events", _sender=sender, **kwargs)


@pytest.mark.parametrize("marker", [b"opt-in\n", b"opt-in\r\n"])
def test_installer_optin_persists_before_first_event(tmp_path, marker):
    (tmp_path / CHOICE_FILENAME).write_bytes(marker)
    events = []
    sent = threading.Event()

    def sender(_endpoint, payload):
        state = json.loads((tmp_path / usage.USAGE_STATE_FILENAME).read_text())
        events.append((payload, state["consent"]))
        sent.set()
        return 204

    instance = usage.UsageClient(tmp_path, "0.16.0", endpoint="https://usage.example.com/v1/events", _sender=sender)
    try:
        assert instance.consent_enabled
        assert sent.wait(timeout=2)
        assert events
        assert all(persisted is True for _event, persisted in events)
    finally:
        instance.close()


@pytest.mark.parametrize("marker", [
    b"opt-out\n", b"", b"opt-in", b" opt-in\n", b"opt-in \n",
    b"OPT-IN\n", b"opt-in\nextra", b"\xef\xbb\xbfopt-in\n", b"\xff",
    b"opt-in\n" + b"x" * 65536,
], ids=["optout", "empty", "no-newline", "leading-space", "trailing-space", "uppercase", "extra", "bom", "invalid-byte", "oversized"])
def test_nonaffirmative_or_malformed_choice_stays_off(tmp_path, marker):
    (tmp_path / CHOICE_FILENAME).write_bytes(marker)
    events = []
    instance = _client(tmp_path, events)
    try:
        assert not instance.consent_enabled
        assert not instance.record("addon_received")
        assert events == []
        assert json.loads((tmp_path / usage.USAGE_STATE_FILENAME).read_text())["consent"] is False
    finally:
        instance.close()


@pytest.mark.parametrize("saved", [True, False])
def test_saved_choice_wins_over_conflicting_installer_marker(tmp_path, saved):
    (tmp_path / CHOICE_FILENAME).write_bytes(b"opt-out\n" if saved else b"opt-in\n")
    (tmp_path / usage.USAGE_STATE_FILENAME).write_text(json.dumps({"schema": 1, "consent": saved}))
    events = []
    instance = _client(tmp_path, events)
    try:
        assert instance.consent_enabled is saved
        if not saved:
            assert events == []
    finally:
        instance.close()


@pytest.mark.parametrize("state", [b'{"schema": 1}', b"malformed", b'{"schema":1,"consent":null}'])
def test_existing_unknown_or_corrupt_state_cannot_take_installer_optin(tmp_path, state):
    (tmp_path / CHOICE_FILENAME).write_bytes(b"opt-in\n")
    (tmp_path / usage.USAGE_STATE_FILENAME).write_bytes(state)
    events = []
    instance = _client(tmp_path, events)
    try:
        assert not instance.consent_enabled
        assert not instance.record("addon_received")
        assert events == []
    finally:
        instance.close()


@pytest.mark.parametrize("explicit", [True, False])
def test_explicit_constructor_choice_overrides_installer_choice(tmp_path, explicit):
    (tmp_path / CHOICE_FILENAME).write_bytes(b"opt-out\n" if explicit else b"opt-in\n")
    events = []
    instance = _client(tmp_path, events, consent=explicit)
    try:
        assert instance.consent_enabled is explicit
        assert json.loads((tmp_path / usage.USAGE_STATE_FILENAME).read_text())["consent"] is explicit
        if not explicit:
            assert events == []
    finally:
        instance.close()


def test_legacy_optout_wins_over_installer_optin(tmp_path):
    (tmp_path / CHOICE_FILENAME).write_bytes(b"opt-in\n")
    (tmp_path / usage.USAGE_INSTALLER_OPT_OUT_FILENAME).write_bytes(b"opt-out\n")
    events = []
    instance = _client(tmp_path, events)
    try:
        assert not instance.consent_enabled
        assert events == []
    finally:
        instance.close()


def test_installer_optin_save_failure_keeps_reporting_off(monkeypatch, tmp_path):
    (tmp_path / CHOICE_FILENAME).write_bytes(b"opt-in\n")

    def fail(*_args, **_kwargs):
        raise PermissionError("synthetic persistence failure")

    monkeypatch.setattr(usage, "atomic_write_text", fail)
    events = []
    instance = _client(tmp_path, events)
    try:
        assert not instance.consent_enabled
        assert not instance.record("addon_received")
        assert events == []
        assert not (tmp_path / usage.USAGE_STATE_FILENAME).exists()
    finally:
        instance.close()


@pytest.mark.parametrize("denied_file", [CHOICE_FILENAME, usage.USAGE_STATE_FILENAME])
def test_unreadable_choice_or_state_cannot_enable_reporting(monkeypatch, tmp_path, denied_file):
    (tmp_path / CHOICE_FILENAME).write_bytes(b"opt-in\n")
    denied = tmp_path / denied_file
    if denied_file == usage.USAGE_STATE_FILENAME:
        denied.write_bytes(b'{"schema":1,"consent":true}')
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == denied:
            raise PermissionError("synthetic inaccessible preference")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    events = []
    instance = _client(tmp_path, events)
    try:
        assert not instance.consent_enabled
        assert events == []
    finally:
        instance.close()
