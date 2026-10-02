# Native regression gate for the install-time usage choice. It runs only an
# isolated harness; the complete production installer is compiled, never run.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepoRoot = Split-Path $PSScriptRoot -Parent
$BuildScript = Join-Path $PSScriptRoot 'build-windows.ps1'
$InstallerScript = Join-Path $RepoRoot 'packaging/inno/ApplicantScoutCompanion.iss'
$ParseErrors = $null
$Tokens = $null
$BuildAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $BuildScript, [ref]$Tokens, [ref]$ParseErrors
)
if ($ParseErrors.Count) { throw "Cannot parse compiler validation: $ParseErrors" }
# Load only the existing, pinned compiler validator, never the build entrypoint.
foreach ($Name in @('Get-InnoSetupRegistrations', 'Test-PathTreeHasReparsePoint',
                    'Get-NormalizedUtf8SHA256', 'Find-InnoSetupCompiler')) {
    $Functions = @($BuildAst.FindAll({
        param($Node)
        $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $Node.Name -eq $Name
    }, $false))
    if ($Functions.Count -ne 1) { throw "Expected one compiler validator: $Name" }
    . ([scriptblock]::Create($Functions[0].Extent.Text))
}
$Compiler = Find-InnoSetupCompiler
if (-not $Compiler) { throw 'The trusted, pinned Inno Setup compiler is unavailable.' }
$Source = [IO.File]::ReadAllText($InstallerScript)
$Procedure = [regex]::Match($Source,
    '(?ms)^procedure ApplyInstallerUsageChoice\(\);.*?(?=^(?:function|procedure)\s)')
$UsageTask = [regex]::Match($Source, '(?m)^Name: "usage";[^\r\n]*')
if (-not $Procedure.Success -or -not $UsageTask.Success) {
    throw 'Cannot extract the actual installer usage procedure/task.'
}
$GateRoot = Join-Path $RepoRoot ('.pytest_cache/installer-usage-' + [guid]::NewGuid())
[IO.Directory]::CreateDirectory($GateRoot) | Out-Null
$ConfigDir = Join-Path $GateRoot 'config'
$HarnessPath = Join-Path $GateRoot 'usage-choice.iss'
$HarnessExe = Join-Path $GateRoot 'usage-choice.exe'
$AppId = [guid]::NewGuid().ToString()
$PascalConfigDir = $ConfigDir.Replace("'", "''")
$Harness = @"
[Setup]
AppId=$AppId
AppName=Usage choice regression
AppVersion=1.0
DefaultDirName=$GateRoot
CreateAppDir=no
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=lowest
UsePreviousTasks=no
OutputDir=$GateRoot
OutputBaseFilename=usage-choice
[Tasks]
$($UsageTask.Value)
[Code]
function UsageConfigDir(): String;
begin
  Result := '$PascalConfigDir';
end;
$($Procedure.Value)
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then ApplyInstallerUsageChoice();
end;
"@
[IO.File]::WriteAllText($HarnessPath, $Harness, [Text.UTF8Encoding]::new($false))

function Invoke-Compiler([string]$Script, [string[]]$ExtraArguments = @()) {
    $Output = & $Compiler /Qp @ExtraArguments $Script 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Installer compilation failed: $Output" }
}
function Invoke-Choice([string]$CaseName, [string[]]$TaskArguments, [bool]$ShouldFail = $false) {
    $Log = Join-Path $GateRoot ($CaseName + '.log')
    $Arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
                   ('/LOG="' + $Log + '"')) + $TaskArguments
    $Process = Start-Process -FilePath $HarnessExe -ArgumentList $Arguments `
        -WindowStyle Hidden -PassThru
    try {
        if (-not $Process.WaitForExit(30000)) {
            $Process.Kill()
            throw "Usage choice harness timed out: $CaseName"
        }
        if (($Process.ExitCode -ne 0) -ne $ShouldFail) {
            throw "Unexpected harness exit $($Process.ExitCode): $CaseName; inspect $Log"
        }
    }
    finally {
        $Process.Dispose()
    }
}
function New-CaseDirectory {
    # Delete only this invocation's verified, non-reparse test directory.
    if (Test-Path -LiteralPath $ConfigDir) {
        $Resolved = (Get-Item -LiteralPath $ConfigDir -Force).FullName
        if ($Resolved -ne [IO.Path]::GetFullPath($ConfigDir) -or
            -not $Resolved.StartsWith([IO.Path]::GetFullPath($GateRoot) + '\',
                [StringComparison]::OrdinalIgnoreCase) -or
            (Test-PathTreeHasReparsePoint -LeafPath $Resolved -TrustedRoot $GateRoot)) {
            throw 'Refusing redirected test directory cleanup.'
        }
        Remove-Item -LiteralPath $ConfigDir -Recurse -Force
    }
    [IO.Directory]::CreateDirectory($ConfigDir) | Out-Null
}
function Assert-Choice([string]$Expected) {
    $Bytes = [IO.File]::ReadAllBytes((Join-Path $ConfigDir 'usage-installer-choice'))
    $Actual = [Convert]::ToHexString($Bytes)
    $Wanted = [Convert]::ToHexString([Text.Encoding]::ASCII.GetBytes($Expected + "`r`n"))
    if ($Actual -ne $Wanted) { throw "Unexpected choice bytes: $Actual; expected $Wanted" }
}

Invoke-Compiler $HarnessPath
foreach ($Case in @(
    @{ Name = 'selected'; Arguments = @('/TASKS="usage"'); Expected = 'opt-in' },
    @{ Name = 'unselected'; Arguments = @('/TASKS=""'); Expected = 'opt-out' },
    @{ Name = 'silent-default'; Arguments = @(); Expected = 'opt-out' }
)) {
    New-CaseDirectory
    [IO.File]::WriteAllText((Join-Path $ConfigDir 'usage-installer-optout'), 'legacy')
    Invoke-Choice $Case.Name $Case.Arguments
    Assert-Choice $Case.Expected
    if (Test-Path -LiteralPath (Join-Path $ConfigDir 'usage-installer-optout')) {
        throw "Legacy opt-out was not removed: $($Case.Name)"
    }
}
foreach ($Case in @(
    @{ Name = 'previous-opt-in'; Initial = 'opt-in'; Tasks = ''; Expected = 'opt-out' },
    @{ Name = 'previous-opt-out'; Initial = 'opt-out'; Tasks = 'usage'; Expected = 'opt-in' }
)) {
    New-CaseDirectory
    [IO.File]::WriteAllText((Join-Path $ConfigDir 'usage-installer-choice'),
        $Case.Initial + "`r`n", [Text.Encoding]::ASCII)
    Invoke-Choice $Case.Name @('/TASKS="' + $Case.Tasks + '"')
    Assert-Choice $Case.Expected
}
foreach ($State in @('{"schema":1,"consent":true}', '{"schema":1,"consent":false}',
                     '{broken', '{"schema":1}')) {
    foreach ($Tasks in @('usage', '')) {
        New-CaseDirectory
        $StatePath = Join-Path $ConfigDir 'usage.json'
        [IO.File]::WriteAllText($StatePath, $State, [Text.UTF8Encoding]::new($false))
        $Before = [Convert]::ToHexString([IO.File]::ReadAllBytes($StatePath))
        Invoke-Choice ('saved-' + [guid]::NewGuid()) @('/TASKS="' + $Tasks + '"')
        if ([Convert]::ToHexString([IO.File]::ReadAllBytes($StatePath)) -ne $Before -or
            (Test-Path -LiteralPath (Join-Path $ConfigDir 'usage-installer-choice'))) {
            throw 'Installer rewrote an existing usage preference.'
        }
    }
}
New-CaseDirectory
[IO.Directory]::CreateDirectory((Join-Path $ConfigDir 'usage-installer-choice')) | Out-Null
Invoke-Choice 'choice-write-failure' @('/TASKS="usage"') -ShouldFail $true

$EnvironmentNames = @('APSCOUT_INNO_VERSION', 'APSCOUT_INNO_SOURCE_DIR', 'APSCOUT_INNO_ICON')
$PreviousEnvironment = @{}
foreach ($Name in $EnvironmentNames) {
    $PreviousEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
}
try {
    $PayloadDir = Join-Path $GateRoot 'dummy-payload'
    [IO.Directory]::CreateDirectory($PayloadDir) | Out-Null
    [IO.File]::WriteAllText((Join-Path $PayloadDir 'ApplicantScout.exe'), 'compile-only dummy')
    [IO.File]::WriteAllText((Join-Path $PayloadDir '.apscout-payload-version'), '0.0.0')
    $env:APSCOUT_INNO_VERSION = '0.0.0'
    $env:APSCOUT_INNO_SOURCE_DIR = $PayloadDir
    $env:APSCOUT_INNO_ICON = [IO.Path]::GetFullPath(
        (Join-Path $RepoRoot 'src/applicant_scout/assets/app_icon.ico')
    )
    Invoke-Compiler $InstallerScript @(('/O' + $GateRoot), '/Fproduction-compile-only')
}
finally {
    foreach ($Name in $EnvironmentNames) {
        [Environment]::SetEnvironmentVariable($Name, $PreviousEnvironment[$Name], 'Process')
    }
}
Write-Host 'Installer usage choice: 14 native cases passed; production script compiled only.'
Write-Host "Isolated evidence: $GateRoot"
