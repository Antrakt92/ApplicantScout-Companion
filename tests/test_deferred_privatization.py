"""Deferred hardening keeps truthful ownership until protection is verified."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from applicant_scout import atomic_io


@pytest.fixture(autouse=True)
def isolated_acl_state(monkeypatch):
    monkeypatch.setattr(atomic_io, "_is_windows", lambda: True)
    atomic_io._PRIVATE_ACL_CACHE.clear()
    atomic_io.set_startup_privatization_deferred(True)
    yield
    atomic_io.set_startup_privatization_deferred(False)
    atomic_io._PRIVATE_ACL_CACHE.clear()


def _queue(path):
    path.write_bytes(b"fixture")
    return atomic_io.apply_private_file_mode(path, allow_deferred=True)


def _pending():
    with atomic_io._PRIVATE_ACL_LOCK:
        return len(atomic_io._DEFERRED_PRIVATE_PATHS)


def test_queued_protection_is_not_reported_as_verified(tmp_path):
    assert _queue(tmp_path / "config.env") is False
    assert _pending() == 1
    assert not atomic_io._PRIVATE_ACL_CACHE


def test_default_private_mode_bypasses_startup_deferral(monkeypatch, tmp_path):
    path = tmp_path / "config.env"
    path.write_bytes(b"fixture")
    secured = []
    monkeypatch.setattr(
        atomic_io, "_apply_windows_private_acl",
        lambda target, **_kwargs: secured.append(target) or True,
    )
    assert atomic_io.apply_private_file_mode(path) is True
    assert secured == [path]
    assert _pending() == 0


def test_false_acl_result_remains_pending_and_retries(monkeypatch, tmp_path):
    _queue(tmp_path / "config.env")
    monkeypatch.setattr(atomic_io, "_apply_windows_private_acl", lambda *_args, **_kwargs: False)
    assert atomic_io.flush_deferred_privatization() == 0
    assert _pending() == 1
    assert not atomic_io._PRIVATE_ACL_CACHE
    monkeypatch.setattr(atomic_io, "_apply_windows_private_acl", lambda *_args, **_kwargs: True)
    assert atomic_io.flush_deferred_privatization() == 1
    assert _pending() == 0
    assert atomic_io._PRIVATE_ACL_CACHE


def test_inaccessible_path_is_retained_instead_of_treated_as_missing(monkeypatch, tmp_path):
    target = tmp_path / "config.env"
    _queue(target)
    real_stat = Path.stat

    def denied_stat(path, *args, **kwargs):
        if path == target:
            raise PermissionError("synthetic inaccessible path")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    assert atomic_io.flush_deferred_privatization() == 0
    assert _pending() == 1


def test_exception_keeps_failed_and_unprocessed_work(monkeypatch, tmp_path):
    _queue(tmp_path / "a-config.env")
    _queue(tmp_path / "b-token.json")

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic ACL failure")

    monkeypatch.setattr(atomic_io, "_apply_windows_private_acl", fail)
    with pytest.raises(RuntimeError, match="synthetic ACL failure"):
        atomic_io.flush_deferred_privatization()
    assert _pending() == 2
    monkeypatch.setattr(atomic_io, "_apply_windows_private_acl", lambda *_args, **_kwargs: True)
    assert atomic_io.flush_deferred_privatization() == 2
    assert _pending() == 0


def test_pending_count_tracks_queue_and_verified_flush(monkeypatch, tmp_path):
    assert atomic_io.deferred_privatization_pending_count() == 0
    _queue(tmp_path / "config.env")
    assert atomic_io.deferred_privatization_pending_count() == 1
    monkeypatch.setattr(atomic_io, "_apply_windows_private_acl", lambda *_args, **_kwargs: True)
    atomic_io.flush_deferred_privatization()
    assert atomic_io.deferred_privatization_pending_count() == 0


def test_concurrent_flushes_share_ownership_of_requeued_path(monkeypatch, tmp_path):
    path = tmp_path / "config.env"
    _queue(path)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    calls = []
    failures = []

    def apply(path, **_kwargs):
        calls.append(path)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=2)
        else:
            second_entered.set()
        return True

    monkeypatch.setattr(atomic_io, "_apply_windows_private_acl", apply)

    def flush():
        try:
            atomic_io.flush_deferred_privatization()
        except Exception as exc:
            failures.append(exc)

    first = threading.Thread(target=flush, daemon=True)
    second = threading.Thread(target=flush, daemon=True)
    first.start()
    try:
        assert entered.wait(timeout=1)
        atomic_io.apply_private_file_mode(path, allow_deferred=True)
        second.start()
        assert not second_entered.wait(timeout=0.1)
    finally:
        release.set()
        first.join(timeout=2)
        if second.ident is not None:
            second.join(timeout=2)
        assert not first.is_alive()
        assert not second.is_alive()
    assert failures == []
    assert calls == [path]
    assert _pending() == 0


def test_new_path_added_during_flush_remains_for_next_flush(monkeypatch, tmp_path):
    original = tmp_path / "a-config.env"
    later = tmp_path / "b-token.json"
    _queue(original)
    calls = []

    def apply(path, **_kwargs):
        calls.append(path)
        if path == original:
            _queue(later)
        return True

    monkeypatch.setattr(atomic_io, "_apply_windows_private_acl", apply)
    assert atomic_io.flush_deferred_privatization() == 1
    assert calls == [original]
    assert _pending() == 1
    assert atomic_io.flush_deferred_privatization() == 1
    assert calls == [original, later]
    assert _pending() == 0


def test_replacement_during_acl_application_gets_no_stale_cache_proof(monkeypatch, tmp_path):
    target = tmp_path / "config.env"
    replacement = tmp_path / "replacement.env"
    _queue(target)
    replacement.write_bytes(b"new identity")

    def apply(path, **_kwargs):
        replacement.replace(path)
        return True

    monkeypatch.setattr(atomic_io, "_apply_windows_private_acl", apply)
    atomic_io.flush_deferred_privatization()
    new_identity = atomic_io._private_acl_cache_key(target, directory=False)
    assert new_identity not in atomic_io._PRIVATE_ACL_CACHE
    assert _pending() == 1
