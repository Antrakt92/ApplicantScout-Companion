"""Execute CI tool-install control flow with Chocolatey replaced by a local stub."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
LUA51_WIN64_SHA256 = "5f34cf7d40a20a587ea351482a4207d93b92ef6f1983e910a13338253819fe93"
STEPS = [
    ("check.yml", "Install Lua 5.1", False),
    ("release.yml", "Install Windows build tools", True),
    ("windows-vs2026-canary.yml", "Install Lua 5.1", False),
    ("addon/check.yml", "Install Lua 5.1", False),
    ("addon/release.yml", "Install Lua 5.1", False),
]


def _install_command(workflow: str, step: str) -> str:
    root = ROOT
    if workflow.startswith("addon/"):
        root = ROOT.parent / "ApplicantScout-Addon"
        workflow = workflow.removeprefix("addon/")
        if not root.is_dir():
            pytest.skip("Paired addon checkout is required to inspect its CI install steps")
    source = (root / ".github/workflows" / workflow).read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^      - name: {re.escape(step)}\n(?P<body>.*?)(?=^      - name:|\Z)",
        source,
    )
    assert match is not None
    body = match["body"]
    command = re.search(r"(?ms)^        run: \|\n(?P<command>.*)\Z", body)
    assert command is not None, "Install must explicitly check each native command exit code"
    return textwrap.dedent(command["command"])


@pytest.mark.parametrize(("workflow", "step", "installs_inno"), STEPS)
def test_ci_lua_download_keeps_exact_payload_checksum(
    workflow: str, step: str, installs_inno: bool
):
    command = _install_command(workflow, step)
    lua_command = next(line for line in command.splitlines() if line.startswith("choco install lua51 "))
    assert "--version=5.1.5" in lua_command
    assert f"--checksum64={LUA51_WIN64_SHA256}" in lua_command
    assert "--checksumtype64=sha256" in lua_command
    assert ("choco install innosetup " in command) is installs_inno
    assert "if (-not [Environment]::Is64BitOperatingSystem) { throw" in command
    assert command.index("Is64BitOperatingSystem") < command.index("choco install")
    assert not re.search(r"--(?:allow-?empty|ignore-?checksums?)", command, re.I)


@pytest.mark.parametrize(("workflow", "step", "installs_inno"), STEPS)
@pytest.mark.parametrize("failed_package", ["", "lua51", "innosetup"])
def test_ci_tool_failure_cannot_be_masked_by_next_install(
    workflow: str, step: str, installs_inno: bool, failed_package: str, tmp_path: Path
):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell 7 is required to execute Windows workflow control flow")
    script = tmp_path / "mock-ci-install.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "function choco {\n"
        "    Write-Output ('MOCK_CHOCO:' + $args[1])\n"
        f"    if ($args[1] -eq '{failed_package}') {{ $global:LASTEXITCODE = 19 }}\n"
        "    else { $global:LASTEXITCODE = 0 }\n"
        "}\n"
        + _install_command(workflow, step)
        + "\nWrite-Output 'INSTALL_STEP_REACHED_END'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    expected_failure = failed_package == "lua51" or (
        failed_package == "innosetup" and installs_inno
    )
    assert (result.returncode != 0) is expected_failure, result.stdout + result.stderr
    assert ("INSTALL_STEP_REACHED_END" in result.stdout) is not expected_failure
    if failed_package == "lua51":
        assert "MOCK_CHOCO:innosetup" not in result.stdout
    elif installs_inno:
        assert "MOCK_CHOCO:innosetup" in result.stdout
