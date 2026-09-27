"""Startup maintenance must finish before the next test replaces OS seams."""

from pathlib import Path
import subprocess
import sys


def test_private_maintenance_cannot_escape_its_test(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "conftest.py").write_bytes((root / "tests" / "conftest.py").read_bytes())
    (tmp_path / "test_scopes.py").write_text(
        """
import threading
import time
from applicant_scout import atomic_io

completed = []

def test_first_scope_leaves_startup_work(tmp_path, monkeypatch):
    atomic_io.set_startup_privatization_deferred(True)
    with atomic_io._PRIVATE_ACL_LOCK:
        atomic_io._DEFERRED_PRIVATE_PATHS.add((str(tmp_path / 'old-profile'), True))
    def finish():
        time.sleep(0.15)
        completed.append(True)
    threading.Thread(target=finish, name='ApplicantScoutACLPrivatize', daemon=True).start()
    clock_ticks = iter([1.0])
    monkeypatch.setattr(time, 'monotonic', lambda: next(clock_ticks))
    assert time.monotonic() == 1.0

def test_second_scope_has_no_prior_profile_work():
    assert completed == [True], 'startup worker escaped its producing test'
    assert not atomic_io._STARTUP_ACL_DEFERRED
    assert not atomic_io._DEFERRED_PRIVATE_PATHS
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
