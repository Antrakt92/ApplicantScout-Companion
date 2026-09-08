from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_dependency_snapshot.py"
spec = importlib.util.spec_from_file_location("dependency_snapshot", SCRIPT)
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPT.parent))
try:
    spec.loader.exec_module(builder)
finally:
    sys.path.pop(0)

SHA = "a" * 40
SCANNED = datetime(2026, 9, 9, tzinfo=timezone.utc)


def _build(path, **overrides):
    kwargs = dict(sha=SHA, run_id="42", run_attempt="1", scanned=SCANNED)
    kwargs.update(overrides)
    return builder.build_snapshot(path, **kwargs)


def test_release_snapshot_covers_every_exact_pin_without_invented_edges_or_scope():
    payload = _build(ROOT / "constraints-release.txt")
    manifest = payload["manifests"]["constraints-release.txt"]
    expected = {}
    for line in (ROOT / "constraints-release.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, version = line.split("==")
            name = name.lower().replace("_", "-").replace(".", "-")
            expected[name] = {"package_url": f"pkg:pypi/{name}@{version}"}
    assert manifest["resolved"] == expected
    assert manifest["file"]["source_location"] == "constraints-release.txt"
    assert payload["sha"] == SHA
    assert payload["ref"] == "refs/heads/main"
    assert payload["version"] == 0
    assert payload["scanned"] == "2026-09-09T00:00:00Z"


def test_new_snapshot_replaces_same_manifest_without_old_pins(tmp_path):
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("old==1\nkeep==2\n", encoding="utf-8")
    old = _build(constraints)
    constraints.write_text("keep==3\n", encoding="utf-8")
    new = _build(constraints, sha="b" * 40, run_id="43", run_attempt="2")
    assert new["detector"] == old["detector"]
    assert new["job"]["correlator"] == old["job"]["correlator"]
    assert new["job"]["id"] != old["job"]["id"]
    assert new["manifests"]["constraints-release.txt"]["resolved"] == {
        "keep": {"package_url": "pkg:pypi/keep@3"},
    }


def test_snapshot_normalizes_pypi_names_and_escapes_version_delimiters(tmp_path):
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("Demo_Package==1!2.0+local\n", encoding="utf-8")
    resolved = _build(constraints)["manifests"]["constraints-release.txt"]["resolved"]
    assert resolved == {"demo-package": {"package_url": "pkg:pypi/demo-package@1%212.0%2Blocal"}}


@pytest.mark.parametrize("contents", ["", "foo>=1", "foo==1\nFoo==2", "-r other.txt"])
def test_invalid_or_partial_constraints_cannot_become_graph_snapshot(tmp_path, contents):
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        _build(constraints)


@pytest.mark.parametrize("overrides", [
    {"sha": "main"}, {"sha": "a" * 40 + "\n"}, {"run_id": ""},
    {"run_attempt": "0"}, {"scanned": datetime(2026, 9, 9)},
])
def test_snapshot_requires_exact_commit_run_and_aware_time(overrides):
    with pytest.raises(ValueError):
        _build(ROOT / "constraints-release.txt", **overrides)


def _trusted_env(monkeypatch):
    for key, value in {
        "GITHUB_REPOSITORY": "Antrakt92/ApplicantScout-Companion",
        "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push",
        "GITHUB_SHA": SHA, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("key,value", [
    ("GITHUB_REPOSITORY", "someone/fork"),
    ("GITHUB_REF", "refs/heads/feature"), ("GITHUB_REF", "refs/pull/1/merge"),
    ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_EVENT_NAME", "pull_request_target"),
])
def test_cli_rejects_untrusted_context_before_reading_checkout(monkeypatch, tmp_path, key, value):
    _trusted_env(monkeypatch)
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *_a, **_k: pytest.fail("read checkout"))
    output = tmp_path / "snapshot.json"
    assert builder.main(["--output", str(output)]) == 2
    assert not output.exists()


def test_cli_rejects_mismatched_checkout_without_writing(monkeypatch, tmp_path):
    _trusted_env(monkeypatch)
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *_a, **_k: "b" * 40)
    output = tmp_path / "snapshot.json"
    assert builder.main(["--output", str(output)]) == 2
    assert not output.exists()


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_cli_builds_offline_from_trusted_checkout_without_token(monkeypatch, tmp_path, event):
    _trusted_env(monkeypatch)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *_a, **_k: SHA + "\n")
    output = tmp_path / "snapshot.json"
    assert builder.main(["--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["sha"] == SHA


def test_workflow_limits_graph_write_to_trusted_default_branch_and_current_commit():
    workflow = (ROOT / ".github/workflows/dependency-advisories.yml").read_text(encoding="utf-8")
    advisory, graph = workflow.split("  submit-release-graph:", 1)
    assert "contents: write" not in advisory
    assert "contents: write" in graph
    assert "github.repository == 'Antrakt92/ApplicantScout-Companion'" in graph
    assert "github.ref == 'refs/heads/main'" in graph
    for event in ("push", "schedule", "workflow_dispatch"):
        assert f"github.event_name == '{event}'" in graph
    for forbidden in ("pull_request_target", "needs:", "pip install", "continue-on-error", "secrets."):
        assert forbidden not in graph
    assert "ref: ${{ github.sha }}" in graph
    assert "persist-credentials: false" in graph
    assert "cancel-in-progress: false" in graph
    build, submit = graph.split("      - name: Submit current default-branch snapshot", 1)
    assert "GH_TOKEN" not in build
    assert "GH_TOKEN: ${{ github.token }}" in submit
    assert 'if [ "$current_sha" != "$GITHUB_SHA" ]; then' in submit
    assert submit.index("exit 0") < submit.index("gh api --method POST")
    assert '--input "$RUNNER_TEMP/release-dependency-snapshot.json"' in submit
    assert submit.count("gh api --method POST") == 1
