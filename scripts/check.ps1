param(
    [string]$AddonRoot = ""
)

$ErrorActionPreference = "Stop"

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    & $Command
    $ExitCode = $LASTEXITCODE
    if ($null -ne $ExitCode -and $ExitCode -ne 0) {
        throw "$Label failed with exit code $ExitCode."
    }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not $AddonRoot) {
    $AddonRootCandidates = @(
        (Join-Path $RepoRoot "..\..\ApplicantScout-Addon"),
        (Join-Path $RepoRoot "..\ApplicantScout-Addon")
    )
    $AddonRoot = $AddonRootCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $AddonRoot) {
        throw "Could not find ApplicantScout-Addon checkout. Pass -AddonRoot explicitly."
    }
}
$AddonRoot = (Resolve-Path $AddonRoot).Path

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Ruff = Join-Path $RepoRoot ".venv\Scripts\ruff.exe"
$Pyright = Join-Path $RepoRoot ".venv\Scripts\pyright.exe"

if (-not (Test-Path $Python)) {
    throw "Missing venv Python: $Python"
}
if (-not (Test-Path $Ruff)) {
    throw "Missing ruff: $Ruff. Install with: .venv\Scripts\python -m pip install -e `".[dev]`""
}
if (-not (Test-Path $Pyright)) {
    throw "Missing pyright: $Pyright. Install with: .venv\Scripts\python -m pip install -e `".[dev]`""
}

$LuacCandidates = @(
    (Get-Command luac5.1 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source),
    "C:\ProgramData\chocolatey\lib\lua51\tools\luac5.1.exe"
) | Where-Object { $_ -and (Test-Path $_) }

$Luac = $LuacCandidates | Select-Object -First 1
if (-not $Luac) {
    throw "Missing luac 5.1. Install with: choco install lua51 -y"
}

Write-Host "== Python tests =="
Invoke-NativeChecked -Label "Python tests" -Command {
    & $Python -m pytest
}

Write-Host "== Ruff =="
Invoke-NativeChecked -Label "Ruff" -Command {
    & $Ruff check .
}

Write-Host "== Pyright =="
Invoke-NativeChecked -Label "Pyright" -Command {
    & $Pyright --warnings
}

Write-Host "== Lua syntax =="
Push-Location $AddonRoot
try {
    Invoke-NativeChecked -Label "Lua syntax" -Command {
        & $Luac -p ApplicantScout.lua libs\qrencode.lua
    }
}
finally {
    Pop-Location
}

Write-Host "== Addon Python contract tests =="
Push-Location $AddonRoot
try {
    Invoke-NativeChecked -Label "Addon Python contract tests" -Command {
        & $Python -m pytest -q tests
    }
}
finally {
    Pop-Location
}

Write-Host "All checks passed."
