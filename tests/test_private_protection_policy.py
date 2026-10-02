"""Private-file consumers distinguish failed protection from verified access."""
from __future__ import annotations

import logging

import pytest

from applicant_scout import atomic_io, wcl
import applicant_scout.__main__ as main_mod


@pytest.mark.parametrize("secured", [False, True])
def test_oauth_reads_existing_token_only_after_verified_protection(monkeypatch, tmp_path, secured):
    token_path = tmp_path / "token.json"
    token_path.write_text("synthetic token cache", encoding="utf-8")
    reads = []
    protection = []
    monkeypatch.setattr(
        wcl, "apply_private_file_mode",
        lambda path: protection.append(path) or secured,
    )
    monkeypatch.setattr(wcl.WCLAuth, "_load_cached", lambda self: reads.append(self._token_path))
    auth = wcl.WCLAuth("fixture-id", "fixture-secret", tmp_path)
    assert auth._token is None
    assert protection == [token_path]
    assert reads == ([token_path] if secured else [])


@pytest.mark.parametrize("raises", [False, True])
def test_startup_reports_unverified_queue_after_failed_flush(monkeypatch, tmp_path, caplog, raises):
    target = tmp_path / "character-cache.json"
    target.write_text("synthetic fixture", encoding="utf-8")
    monkeypatch.setattr(atomic_io, "_is_windows", lambda: True)
    atomic_io.set_startup_privatization_deferred(True)
    assert not atomic_io.apply_private_file_mode(target, allow_deferred=True)

    def failed_acl(*_args, **_kwargs):
        if raises:
            raise OSError("synthetic ACL failure")
        return False

    monkeypatch.setattr(atomic_io, "_apply_windows_private_acl", failed_acl)
    with caplog.at_level(logging.WARNING):
        main_mod._flush_startup_file_privatization()
    assert atomic_io.deferred_privatization_pending_count() == 1
    assert "remains unverified for 1 path(s); retained for retry" in caplog.text
    assert "Applied deferred file privatization" not in caplog.text
