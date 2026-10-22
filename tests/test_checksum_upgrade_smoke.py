from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-checksum-upgrade.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "checksum-upgrade-smoke.yml"


def _pwsh(code: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 is required for native script verification")
    return subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", code],
        capture_output=True, text=True, check=False, timeout=30,
    )


def _quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _function(name: str) -> str:
    # Execute the production helper without emulating a runner or installing.
    return (
        "$errors=$null; $tokens=$null; "
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile({_quote(SCRIPT)},"
        "[ref]$tokens,[ref]$errors); if ($errors.Count) {throw 'Invalid script'}; "
        "$fn=$ast.FindAll({param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        f"-and $node.Name -ceq '{name}'"
        "},$true); if (@($fn).Count -ne 1) {throw 'Missing function'}; "
        ". ([scriptblock]::Create($fn.Extent.Text)); "
    )


@pytest.mark.parametrize("case", ["valid", "wrong-digest", "wrong-name", "malformed"])
def test_checksum_verification_executes_production_helper(tmp_path: Path, case: str) -> None:
    installer = tmp_path / "ApplicantScoutCompanionSetup-0.24.3.exe"
    installer.write_bytes(b"checksum fixture, never executable")
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    sidecar = tmp_path / (installer.name + ".sha256")
    values = {
        "valid": f"{digest}  {installer.name}",
        "wrong-digest": f"{'0' * 64}  {installer.name}",
        "wrong-name": f"{digest}  AnotherSetup.exe",
        "malformed": f"{digest}  {installer.name}\nextra record",
    }
    sidecar.write_text(values[case], encoding="ascii")
    result = _pwsh(
        "$ErrorActionPreference='Stop'; " + _function("Assert-Checksum")
        + f"Assert-Checksum {_quote(installer)} {_quote(sidecar)}"
    )
    assert (result.returncode == 0) == (case == "valid"), result.stdout + result.stderr
    if case == "valid":
        assert result.stdout.strip() == digest
    assert installer.exists()


def test_cleanup_rejects_unowned_directory(tmp_path: Path) -> None:
    unowned = tmp_path / "preserve"
    unowned.mkdir()
    marker = unowned / "marker.txt"
    marker.write_text("preserve", encoding="ascii")
    result = _pwsh(
        "$ErrorActionPreference='Stop'; " + _function("Remove-OwnedTree")
        + f"$Work={_quote(tmp_path / 'work')}; "
        + f"$InstallRoot={_quote(tmp_path / 'install')}; "
        + f"$ConfigRoot={_quote(tmp_path / 'config')}; "
        + f"Remove-OwnedTree {_quote(unowned)}"
    )
    assert result.returncode != 0
    assert "Refusing unowned cleanup path" in result.stderr
    assert marker.read_text(encoding="ascii") == "preserve"


def test_runner_boundary_rejects_parent_escape(tmp_path: Path) -> None:
    result = _pwsh(
        "$ErrorActionPreference='Stop'; " + _function("Assert-OwnedRoot")
        + f"Assert-OwnedRoot {_quote(tmp_path / '..' / 'escape')} {_quote(tmp_path)}"
    )
    assert result.returncode != 0
    assert "escapes its explicit runner boundary" in result.stderr


def test_real_script_rejects_workstation_before_any_effect() -> None:
    result = _pwsh(
        "Remove-Item Env:GITHUB_ACTIONS -ErrorAction SilentlyContinue; "
        f"& {_quote(SCRIPT)} -Tag v0.24.3 -ReleaseRunId 123"
    )
    assert result.returncode != 0
    assert "requires an ephemeral GitHub-hosted Windows runner" in result.stderr


def test_workflow_is_manual_and_read_only() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in source
    assert "push:" not in source and "schedule:" not in source
    assert "contents: read" in source and "actions: read" in source
    assert ": write" not in source and "secrets." not in source
    assert "runs-on: windows-2022" in source
    assert "persist-credentials: false" in source
    assert "${{ inputs.tag }}" not in source.split("run: |", 1)[1]
    assert "if-no-files-found: error" in source


def test_proof_chain_uses_final_manifest_and_withholds_receipt_on_failure() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"companion-release-assets-attempt-$Attempt"' in source
    assert "-Mode Verify -Purpose Release" in source
    assert "$Baseline.immutable -ne $true" in source
    assert '"sha256:$Hash" -cne [string]$Assets[0].digest' in source
    assert source.index("Remove-Item Env:GH_TOKEN") < source.index("Invoke-Install $BaselineInstaller")
    assert source.index("Invoke-Install $BaselineInstaller") < source.index("Invoke-Install $Candidate")
    assert source.index("Remove-OwnedTree $Work") < source.index("[IO.File]::WriteAllText($ReceiptPath")
    assert "gh release edit" not in source and "gh workflow run" not in source
    assert "PendingFileRenameOperations" not in source
    assert "candidateInstallerSha256 = $CandidateHash" in source
    assert "configFilePreserved = $true; usageOptOutPreserved = $true" in source
    assert "$UsagePath = Join-Path $ConfigDirectory 'usage.json'" in source


def test_powershell_script_has_no_parse_errors() -> None:
    result = _pwsh(
        "$errors=$null; $tokens=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile({_quote(SCRIPT)},"
        "[ref]$tokens,[ref]$errors) | Out-Null; "
        "$errors | ForEach-Object {$_.ToString()}; if ($errors.Count) {exit 1}"
    )
    assert result.returncode == 0, result.stdout + result.stderr
