"""Execute emitted installer probes against a bounded mocked CIM inventory."""

import os
from pathlib import Path
import re
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "packaging/inno/ApplicantScoutCompanion.iss"


def _probe_script(function: str) -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    body = source.split(f"function {function}(Terminate: Boolean): String;", 1)[1]
    initial = body.split("  Result :=", 1)[1].split("  if Terminate then begin", 1)[0]
    absent_branch = body.split("  end else begin", 1)[1].split("  end;", 1)[0]
    suffix = absent_branch.split("Result := Result +", 1)[1]
    values = {name: "'C:\\Scout\\ApplicantScout.exe'" for name in (
        "BackupTarget", "CompanionBackupTarget", "CompanionCurrentTarget",
        "CompanionLegacyTarget", "CompanionNextTarget", "CurrentTarget",
        "LegacyTarget", "NextTarget", "SourcePath",
    )}
    values["SourcePid"] = "4242"

    def expand(expression):
        return "".join(
            token[1:-1].replace("''", "'") if token.startswith("'") else values[token]
            for token in re.findall(r"'(?:''|[^'])*'|[A-Za-z_]\w*", expression)
        )

    command = expand(initial) + expand(suffix)
    return command.split('-Command "', 1)[1][:-1]


@pytest.mark.skipif(os.name != "nt", reason="installer probes use Windows PowerShell")
@pytest.mark.parametrize("function,expected_filter", [
    ("CompanionProcessScript", "Name = 'ApplicantScout.exe' OR Name = 'ApplicantScoutCompanion.exe'"),
    ("SelfUpdateProcessScript", "ProcessId = 4242"),
])
@pytest.mark.parametrize("inventory,companion_expected,self_update_expected", [
    ("empty", 1, 1),
    ("matching", 0, 0),
    ("other_path", 1, 1),
    ("missing_path", 2, 2),
    ("query_error", 2, 2),
    ("foreign_owner", 1, 0),
    ("owner_error", 2, 0),
    ("portable", 0, 1),
])
def test_emitted_probe_filters_inventory_at_provider_and_keeps_path_fail_closed(
    function, expected_filter, inventory, companion_expected, self_update_expected,
):
    script = _probe_script(function)
    escaped_filter = expected_filter.replace("'", "''")
    setup = rf"""
function Get-CimInstance {{
    param($ClassName, $Filter, $OperationTimeoutSec)
    if ($ClassName -ne 'Win32_Process' -or $Filter -cne '{escaped_filter}' -or
        $OperationTimeoutSec -ne 20) {{ throw 'Unbounded inventory query' }}
    if ('{inventory}' -eq 'query_error') {{ throw 'Provider unavailable' }}
    if ('{inventory}' -eq 'empty') {{ return }}
    $path = 'C:\Scout\ApplicantScout.exe'
    if ('{inventory}' -in @('other_path', 'portable')) {{ $path = 'C:\Other\ApplicantScout.exe' }}
    if ('{inventory}' -eq 'missing_path') {{ $path = $null }}
    $name = 'ApplicantScout.exe'
    if ('{inventory}' -eq 'portable') {{ $name = 'ApplicantScoutCompanion.exe' }}
    [pscustomobject]@{{ Name=$name; ProcessId=4242;
        ExecutablePath=$path; SessionId=[System.Diagnostics.Process]::GetCurrentProcess().SessionId }}
}}
function Invoke-CimMethod {{
    param($InputObject, $MethodName, $OperationTimeoutSec)
    if ($MethodName -ne 'GetOwnerSid') {{ throw 'Probe attempted mutation' }}
    if ($OperationTimeoutSec -ne 20) {{ throw 'Owner lookup timed out' }}
    if ('{inventory}' -eq 'owner_error') {{ throw 'Owner unavailable' }}
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ('{inventory}' -eq 'foreign_owner') {{ $sid = 'S-1-5-18' }}
    [pscustomobject]@{{ ReturnValue=0; Sid=$sid }}
}}
"""
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", setup + script],
        capture_output=True, text=True, timeout=30, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    expected = companion_expected if function == "CompanionProcessScript" else self_update_expected
    assert result.returncode == expected, result.stdout + result.stderr
