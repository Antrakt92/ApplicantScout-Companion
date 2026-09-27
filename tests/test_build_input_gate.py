"""Execute release cleanliness gates without building or touching real settings."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_release_build_rejects_uncommitted_installer_privacy(tmp_path: Path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    git = shutil.which("git")
    if shell is None or git is None:
        pytest.skip("PowerShell and Git required for executable build gate")
    source = (ROOT / "scripts/build-windows.ps1").read_text(encoding="utf-8")
    start = source.index("function Assert-CleanReleaseInputs {")
    end = source.index("\nfunction ", start + 1)
    function = source[start:end]
    subprocess.run([git, "init", "-q", str(tmp_path)], check=True)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "PRIVACY.md").write_text("Synthetic changed installer copy", encoding="utf-8")
    helper = str(ROOT / "scripts/native-command.ps1").replace("'", "''")
    root = str(tmp_path).replace("'", "''")
    script = tmp_path / "gate.ps1"
    script.write_text(
        f"$ErrorActionPreference = 'Stop'\n$RepoRoot = '{root}'\n. '{helper}'\n"
        + function + "\nAssert-CleanReleaseInputs\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "PRIVACY.md" in result.stderr
    assert "dirty release inputs" in result.stderr
