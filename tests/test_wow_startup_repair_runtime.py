"""Explicit startup repair is serialized without changing ordinary launch policy."""
from __future__ import annotations

import threading

import pytest

import applicant_scout.__main__ as main_mod


@pytest.fixture
def queued():
    workers = []
    notifications = []
    calls = []
    configurator = main_mod._WowSyncStartupConfigurator(
        configure=lambda enabled: calls.append(("configure", enabled)),
        enable_approval=lambda: calls.append(("approve", True)),
        runner=workers.append,
        notify=notifications.append,
    )
    return configurator, workers, notifications, calls


def test_ordinary_requests_preserve_windows_block_and_deduplicate(queued):
    configurator, workers, notifications, calls = queued
    succeeded = []
    configurator.request(True)
    workers.pop(0)()
    configurator.request(True, on_success=lambda: succeeded.append(True))
    workers.pop(0)()
    assert calls == [("configure", True)]
    assert succeeded == []
    notifications.pop(0)()
    assert succeeded == [True]
    assert configurator.close() is None
    assert calls == [("configure", True)]


def test_explicit_repair_bypasses_enabled_dedup_and_runs_off_caller(queued):
    configurator, workers, notifications, calls = queued
    succeeded = []
    configurator.request(True)
    workers.pop(0)()
    configurator.request(True, restore_windows_approval=True,
                         on_success=lambda: succeeded.append(True))
    assert calls == [("configure", True)]
    workers.pop(0)()
    assert calls == [("configure", True), ("configure", True), ("approve", True)]
    assert succeeded == []
    notifications.pop(0)()
    assert succeeded == [True]
    assert configurator.close() is None
    assert len(calls) == 3


def test_queued_repair_superseded_by_disable_never_approves(queued):
    configurator, workers, notifications, calls = queued
    configurator.request(True, restore_windows_approval=True,
                         on_success=lambda: pytest.fail("stale success"))
    configurator.request(False)
    for worker in workers:
        worker()
    assert calls == [("configure", False)]
    assert notifications == []


@pytest.mark.parametrize("close", [False, True])
def test_success_delivery_is_suppressed_after_supersession_or_close(queued, close):
    configurator, workers, notifications, _calls = queued
    succeeded = []
    configurator.request(True, restore_windows_approval=True,
                         on_success=lambda: succeeded.append(True))
    workers.pop(0)()
    if close:
        configurator.close()
    else:
        configurator.request(False)
    notifications.pop(0)()
    assert succeeded == []


def test_configure_failure_never_restores_approval(queued):
    configurator, workers, notifications, calls = queued
    errors = []

    def fail(_enabled):
        raise OSError("shortcut denied")

    configurator._configure = fail
    configurator.request(True, restore_windows_approval=True,
                         on_success=lambda: pytest.fail("unexpected success"),
                         on_error=errors.append)
    workers.pop(0)()
    assert calls == []
    notifications.pop(0)()
    assert str(errors[0]) == "shortcut denied"


def test_approval_failure_reports_error_and_close_retries_latest_repair(queued):
    configurator, workers, notifications, calls = queued
    errors = []

    def approve():
        calls.append(("approve", True))
        if len(calls) == 2:
            raise PermissionError("approval denied")

    configurator._enable_approval = approve
    configurator.request(True, restore_windows_approval=True,
                         on_success=lambda: pytest.fail("unexpected success"),
                         on_error=errors.append)
    workers.pop(0)()
    notifications.pop(0)()
    assert str(errors[0]) == "approval denied"
    assert configurator.close() is None
    assert calls == [("configure", True), ("approve", True),
                     ("configure", True), ("approve", True)]


def test_close_reconciles_explicit_queued_repair_without_gui_callback(queued):
    configurator, workers, notifications, calls = queued
    configurator.request(True, restore_windows_approval=True,
                         on_success=lambda: pytest.fail("closed callback"))
    assert configurator.close() is None
    workers.pop(0)()
    assert calls == [("configure", True), ("approve", True)]
    assert notifications == []


def test_close_reconciles_latest_disable_without_approval(queued):
    configurator, workers, notifications, calls = queued
    configurator.request(True, restore_windows_approval=True)
    configurator.request(False)
    assert configurator.close() is None
    for worker in workers:
        worker()
    assert calls == [("configure", False)]
    assert notifications == []


def test_stale_approval_error_is_not_delivered_after_disable(queued):
    configurator, workers, notifications, calls = queued
    errors = []

    def fail():
        raise PermissionError("approval denied")

    configurator._enable_approval = fail
    configurator.request(True, restore_windows_approval=True, on_error=errors.append)
    workers.pop(0)()
    configurator.request(False)
    workers.pop(0)()
    notifications.pop(0)()
    assert errors == []
    assert configurator.close() is None
    assert calls == [("configure", True), ("configure", False)]


def test_close_returns_approval_error_and_does_not_deliver_callbacks(queued):
    configurator, workers, notifications, _calls = queued

    def fail():
        raise PermissionError("approval denied")

    configurator._enable_approval = fail
    configurator.request(True, restore_windows_approval=True,
                         on_error=lambda _exc: pytest.fail("closed callback"))
    error = configurator.close()
    assert isinstance(error, PermissionError)
    assert configurator.close() is error
    workers.pop(0)()
    assert notifications == []


def test_disable_never_restores_approval_even_when_requested(queued):
    configurator, workers, _notifications, calls = queued
    configurator.request(False, restore_windows_approval=True)
    workers.pop(0)()
    assert configurator.close() is None
    assert calls == [("configure", False)]


def test_disable_during_inflight_configure_suppresses_stale_approval():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def configure(enabled):
        calls.append(("configure", enabled))
        if enabled:
            entered.set()
            assert release.wait(timeout=5)

    configurator = main_mod._WowSyncStartupConfigurator(
        configure=configure,
        enable_approval=lambda: calls.append(("approve", True)),
    )
    try:
        configurator.request(True, restore_windows_approval=True)
        assert entered.wait(timeout=5)
        configurator.request(False)
    finally:
        release.set()
        assert configurator.close() is None
    assert calls == [("configure", True), ("configure", False)]
