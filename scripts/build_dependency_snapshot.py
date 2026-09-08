"""Build an offline GitHub dependency snapshot from exact release constraints.

Submission is a separate trusted workflow step. Constraints record package
versions, not dependency edges or runtime/development scope; neither is inferred.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote

from check_dependency_advisories import AdvisoryCheckError, read_pins


REPOSITORY = "Antrakt92/ApplicantScout-Companion"
DEFAULT_REF = "refs/heads/main"
MANIFEST = "constraints-release.txt"
ROOT = Path(__file__).resolve().parents[1]


def build_snapshot(
    constraints: Path, *, sha: str, run_id: str, run_attempt: str, scanned: datetime,
) -> dict:
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("snapshot requires an exact commit SHA")
    if any(re.fullmatch(r"[1-9][0-9]*", value) is None for value in (run_id, run_attempt)):
        raise ValueError("snapshot requires a valid workflow run and attempt")
    if scanned.tzinfo is None:
        raise ValueError("snapshot timestamp requires a timezone")
    pins = read_pins(constraints)
    resolved = {
        pin.name: {"package_url": f"pkg:pypi/{pin.name}@{quote(pin.version, safe='')}"}
        for pin in sorted(pins, key=lambda pin: pin.name)
    }
    return {
        "version": 0,
        "sha": sha,
        "ref": DEFAULT_REF,
        "job": {
            "id": f"{run_id}-{run_attempt}",
            "correlator": "release-constraints-main",
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        },
        "detector": {
            "name": "applicantscout-release-constraints",
            "version": "1.0.0",
            "url": f"https://github.com/{REPOSITORY}",
        },
        "scanned": scanned.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifests": {
            MANIFEST: {
                "name": MANIFEST,
                "file": {"source_location": MANIFEST},
                "resolved": resolved,
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if (
            os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
            or os.environ.get("GITHUB_REF") != DEFAULT_REF
            or os.environ.get("GITHUB_EVENT_NAME") not in {"push", "schedule", "workflow_dispatch"}
        ):
            raise ValueError("snapshot requires a trusted default-branch workflow")
        sha = os.environ.get("GITHUB_SHA", "")
        checkout_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        ).strip()
        if checkout_sha != sha:
            raise ValueError("checkout does not match the workflow commit")
        snapshot = build_snapshot(
            ROOT / MANIFEST, sha=sha,
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            scanned=datetime.now(timezone.utc),
        )
        args.output.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, AdvisoryCheckError, subprocess.CalledProcessError) as exc:
        # Filesystem and subprocess errors may contain machine paths; keep CI output bounded.
        print(f"Cannot build release dependency snapshot ({type(exc).__name__}).", file=sys.stderr)
        return 2
    print(f"Built exact dependency snapshot for {len(snapshot['manifests'][MANIFEST]['resolved'])} pins.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
