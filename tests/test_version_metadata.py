from __future__ import annotations

import re
import tomllib
from pathlib import Path

import applicant_scout


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_project_version_matches_runtime_version():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == applicant_scout.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", applicant_scout.__version__)
