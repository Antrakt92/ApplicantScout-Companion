from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_copy.py"
spec = importlib.util.spec_from_file_location("release_copy", SCRIPT)
assert spec is not None and spec.loader is not None
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

PREFIX = "## 0.18.3 - 16-Sep-2026\n\nPaired release with ApplicantScout addon `0.10.6`.\n\n### Fixed\n\n- Preserve Cyrillic: Игрок.\n\n"
BLOCK = (
    "### Release Assets\n\n"
    "- Requires the ApplicantScout WoW addon `0.10.6`.\n"
    "- Installer: `ApplicantScoutCompanionSetup-0.18.3.exe`\n"
    "- Installer checksum: `ApplicantScoutCompanionSetup-0.18.3.exe.sha256`\n"
    "- Portable archive: `ApplicantScoutCompanion-0.18.3-portable.zip`\n"
    "- Immutable manifest: `ApplicantScoutCompanion-0.18.3-release-manifest.json`\n\n"
)
SUFFIX = "## 0.1.0 - 15-May-2026\n\n- First release.\n"


def manifest(body=PREFIX + BLOCK + SUFFIX, *, tag="v0.18.3"):
    raw = body.encode("utf-8")
    return {"schemaVersion": 2, "tag": tag, "releaseCopy": {"title": tag, "body": {
        "encoding": "utf-8", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "contentBase64": base64.b64encode(raw).decode("ascii"),
    }}}


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_clean_removes_only_exact_generated_ranges(newline):
    original = (PREFIX + BLOCK + SUFFIX).replace("\n", newline)
    expected = (PREFIX + SUFFIX).replace("\n", newline)
    assert policy.clean_legacy_asset_lists(original) == expected
    assert policy.clean_legacy_asset_lists(expected) == expected


def test_historical_body_with_only_final_crlf_is_cleaned():
    block = BLOCK.replace("- Immutable manifest: `ApplicantScoutCompanion-0.18.3-release-manifest.json`\n\n", "").rstrip("\n") + "\r\n"
    assert policy.clean_legacy_asset_lists(PREFIX + block) == PREFIX


def test_updater_filename_prefix_with_extra_prose_is_preserved():
    updater = "- In-app updates require GitHub Release assets named\n  `ApplicantScoutCompanionSetup-0.18.3.exe` and\n  `ApplicantScoutCompanionSetup-0.18.3.exe.sha256`. Keep this explanation.\n"
    original = PREFIX + updater + SUFFIX
    assert policy.clean_legacy_asset_lists(original) == original


def test_clean_handles_complete_cumulative_history():
    old_prefix = PREFIX.replace("0.18.3", "0.17.1")
    old_block = BLOCK.replace("0.18.3", "0.17.1")
    original = PREFIX + BLOCK + old_prefix + old_block + SUFFIX
    assert policy.clean_legacy_asset_lists(original) == PREFIX + old_prefix + SUFFIX


def test_known_notes_heading_and_old_updater_list_are_removed():
    updater = "- In-app updates require GitHub Release assets named\n  `ApplicantScoutCompanionSetup-0.18.3.exe` and\n  `ApplicantScoutCompanionSetup-0.18.3.exe.sha256`.\n\n"
    notes = BLOCK.replace("### Release Assets", "### Notes")
    assert policy.clean_legacy_asset_lists(PREFIX + notes + SUFFIX) == PREFIX + SUFFIX
    compatibility = "### Compatibility\n\n- Requires addon.\n\n"
    assert policy.clean_legacy_asset_lists(PREFIX + compatibility + updater + SUFFIX) == PREFIX + compatibility + SUFFIX
    unrelated = PREFIX + "### Notes\n\n- Useful player detail.\n" + SUFFIX
    assert policy.clean_legacy_asset_lists(unrelated) == unrelated


@pytest.mark.parametrize("replacement", [
    BLOCK.replace("- Installer:", "- Surprise:"),
    BLOCK.replace("0.18.3.exe.sha256", "0.18.2.exe.sha256"),
    BLOCK + "- A meaningful extra note.\n\n",
    BLOCK.replace("CompanionSetup", "UnrelatedSetup"),
])
def test_unrecognized_asset_sections_are_never_deleted(replacement):
    with pytest.raises(policy.ReleaseCopyError, match="Unrecognized"):
        policy.clean_legacy_asset_lists(PREFIX + replacement + SUFFIX)


def test_block_version_must_match_its_own_release_heading():
    with pytest.raises(policy.ReleaseCopyError, match="historical release heading"):
        policy.clean_legacy_asset_lists(PREFIX.replace("0.18.3", "0.18.2") + BLOCK)


def test_original_and_complete_projection_are_the_only_accepted_bodies():
    record = manifest()
    unchanged = copy.deepcopy(record)
    assert policy.published_body_matches(record, PREFIX + BLOCK + SUFFIX)
    assert policy.published_body_matches(record, PREFIX + SUFFIX)
    assert record == unchanged


@pytest.mark.parametrize("actual", [
    (PREFIX + SUFFIX).replace("Fixed", "Improved"),
    (PREFIX + SUFFIX).replace("Игрок", "Player"),
    (PREFIX + SUFFIX).replace("\n\n", "\n", 1),
    (PREFIX + SUFFIX).replace("\n", "\r\n"),
    PREFIX + SUFFIX.rstrip("\n"),
    SUFFIX + PREFIX,
    PREFIX + BLOCK.replace("- Requires the ApplicantScout WoW addon `0.10.6`.\n", "") + SUFFIX,
])
def test_unrelated_text_whitespace_partial_deletion_and_order_changes_fail(actual):
    assert not policy.published_body_matches(manifest(), actual)


@pytest.mark.parametrize("tag,schema", [("v0.18.4", 2), ("v1.0.0", 2), ("v0.18.3", 3), ("v00.18.3", 2), ("v0.18.3", True)])
def test_projection_is_limited_to_original_schema_and_historical_tags(tag, schema):
    record = manifest(tag=tag)
    record["schemaVersion"] = schema
    assert not policy.published_body_matches(record, PREFIX + SUFFIX)


def test_new_clean_body_remains_exact_only():
    expected = "## 0.18.4 - 17-Sep-2026\n\n- New notes.\n\n" + PREFIX + SUFFIX
    assert policy.published_body_matches(manifest(expected, tag="v0.18.4"), expected)
    assert not policy.published_body_matches(manifest(expected, tag="v0.18.4"), expected + "\n")


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("size", 1), ("size", True),
                                        ("encoding", "latin1"), ("contentBase64", "not base64")])
def test_projection_never_ignores_manifest_body_integrity(field, value):
    record = manifest()
    record["releaseCopy"]["body"][field] = value
    with pytest.raises(policy.ReleaseCopyError):
        policy.published_body_matches(record, PREFIX + SUFFIX)


@pytest.mark.parametrize("body", ["", " \n", "\ufeff" + PREFIX, PREFIX.replace("\n", "\r\n"), PREFIX + "\x00\n", PREFIX.rstrip()])
def test_noncanonical_manifest_body_is_rejected(body):
    with pytest.raises(policy.ReleaseCopyError):
        policy.published_body_matches(manifest(body), body)


def test_cli_verifies_cleaned_body_then_rejects_text_change(tmp_path):
    manifest_file = tmp_path / "manifest.json"
    release_file = tmp_path / "release.json"
    manifest_file.write_text(json.dumps(manifest()), encoding="utf-8")
    release_file.write_text(json.dumps({"body": PREFIX + SUFFIX}), encoding="utf-8")
    args = [sys.executable, str(SCRIPT), "verify-body", "--manifest", str(manifest_file), "--release-json", str(release_file)]
    assert subprocess.run(args, capture_output=True).returncode == 0
    release_file.write_text(json.dumps({"body": PREFIX + SUFFIX + "Edited"}), encoding="utf-8")
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 1
    assert "differs from the authoritative" in result.stderr


def test_source_notes_do_not_reintroduce_asset_lists():
    text = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    assert "### Release Assets" not in text
    assert "- Installer:" not in text
    assert "- Installer checksum:" not in text
    assert "- Portable archive:" not in text
    assert "- Immutable manifest:" not in text
    assert "- In-app updates require GitHub Release assets named" not in text
    assert "## 0.18.3 -" in text and "## 0.1.0 -" in text
