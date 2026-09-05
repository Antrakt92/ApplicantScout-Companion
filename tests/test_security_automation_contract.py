from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


REPO_ROOT = Path(__file__).resolve().parents[1]
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$", re.I)


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_codeql_scans_python_with_minimum_explicit_permissions_and_pinned_actions():
    workflow = _read(".github/workflows/codeql.yml")

    assert "languages: python" in workflow
    assert "security-events: write" in workflow
    assert "contents: read" in workflow
    assert "contents: write" not in workflow
    assert "actions: write" not in workflow
    assert "pull-requests: write" not in workflow
    assert re.search(r"(?m)^  push:\n    branches: \[main\]\s*$", workflow)
    assert re.search(r"(?m)^  pull_request:\n    branches: \[main\]\s*$", workflow)
    assert re.search(r"(?m)^  schedule:\n    - cron: '[^']+'\s*$", workflow)

    action_refs = re.findall(r"(?m)^\s*uses:\s*([^@\s]+)@([^\s#]+)", workflow)
    assert [action for action, _ref in action_refs] == [
        "actions/checkout",
        "github/codeql-action/init",
        "github/codeql-action/analyze",
    ]
    for action, ref in action_refs:
        assert _FULL_SHA.fullmatch(ref), f"{action} must be pinned to a full commit SHA"
    assert action_refs[1][1] == action_refs[2][1], (
        "CodeQL init and analyze must use the same release commit"
    )


def test_dependabot_covers_python_and_github_actions_on_a_bounded_schedule():
    config = _read(".github/dependabot.yml")

    assert config.count('package-ecosystem: "pip"') == 1
    assert config.count('package-ecosystem: "github-actions"') == 1
    assert config.count('directory: "/"') == 2
    assert config.count('interval: "weekly"') == 2
    assert config.count("open-pull-requests-limit: 5") == 2
    assert config.count("default-days: 14") == 2
    assert "codeql-actions:" in config
    assert '          - "github/codeql-action/*"' in config


def test_security_policy_documents_python_and_lua_coverage_boundary():
    policy = _read("SECURITY.md")

    assert "CodeQL" in policy
    assert "Python" in policy
    assert "Lua" in policy
    assert "does not support Lua" in policy
    assert "ApplicantScout-Addon" in policy


def test_image_decoder_requires_security_fixed_pillow():
    # Pillow 12.3.0 fixes malformed-image memory-safety and parser-loop issues.
    safe_floor = Version("12.3.0")
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    requirements = [Requirement(value) for value in project["dependencies"]]
    pillow = next(value for value in requirements if value.name.lower() == "pillow")
    assert any(
        item.operator == ">=" and Version(item.version) >= safe_floor
        for item in pillow.specifier
    ), "Source installs must require a security-fixed Pillow version"

    constraints = [
        Requirement(line)
        for line in _read("constraints-release.txt").splitlines()
        if line and not line.startswith("#")
    ]
    pinned = next(value for value in constraints if value.name.lower() == "pillow")
    assert any(
        item.operator == "==" and Version(item.version) >= safe_floor
        for item in pinned.specifier
    ), "Release builds must pin a security-fixed Pillow version"


def test_dependency_advisories_checks_exact_pins_without_installs_or_write_permissions():
    workflow = _read(".github/workflows/dependency-advisories.yml")
    assert "python scripts/check_dependency_advisories.py" in workflow
    assert "pull_request:" in workflow and "push:" in workflow
    assert "schedule:" in workflow and "workflow_dispatch:" in workflow
    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "timeout-minutes: 10" in workflow
    for forbidden in (": write", "pip install", "secrets.", "continue-on-error", "pull_request_target"):
        assert forbidden not in workflow
    for action, ref in re.findall(r"(?m)^\s*uses:\s*([^@\s]+)@([^\s#]+)", workflow):
        assert _FULL_SHA.fullmatch(ref), f"{action} must be pinned to a full commit SHA"
