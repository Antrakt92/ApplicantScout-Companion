"""Unreadable promotion markers cannot confirm an installed update."""
from pathlib import Path

from applicant_scout.updater import read_installed_payload_version, verify_install_promotion


def test_invalid_utf8_payload_marker_fails_promotion_verification(tmp_path: Path):
    current = tmp_path / "current"
    current.mkdir()
    (current / ".apscout-payload-version").write_bytes(b"\xff\xfe0.21.0")

    assert read_installed_payload_version(tmp_path) is None
    assert verify_install_promotion(expected_version="0.21.0", install_root=tmp_path) is False
