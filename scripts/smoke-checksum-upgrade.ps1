param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [Parameter(Mandatory = $true)][string]$ReleaseRunId
)

$ErrorActionPreference = 'Stop'
# This installs real binaries into the disposable runner's profile. Never
# redirect these paths or emulate the runner environment on a workstation.
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or
    $env:RUNNER_OS -cne 'Windows' -or $env:GITHUB_REPOSITORY -cne 'Antrakt92/ApplicantScout-Companion') {
    throw 'Checksum upgrade smoke requires an ephemeral GitHub-hosted Windows runner.'
}
if ($Tag -cnotmatch '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
    $ReleaseRunId -cnotmatch '^[1-9][0-9]*$') {
    throw 'Exact canonical release tag and positive build run ID are required.'
}
$Repo = $env:GITHUB_REPOSITORY
$Version = $Tag.Substring(1)
function Assert-OwnedRoot([string]$Path, [string]$Parent) {
    $Resolved = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $Boundary = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    if (-not $Resolved.StartsWith($Boundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Smoke path escapes its explicit runner boundary.'
    }
    $Ancestor = [IO.Path]::GetDirectoryName($Resolved)
    while ($Ancestor) {
        if ((Test-Path -LiteralPath $Ancestor) -and
            ((Get-Item -LiteralPath $Ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Refusing smoke path beneath a reparse point.'
        }
        $Ancestor = [IO.Path]::GetDirectoryName($Ancestor)
    }
    return $Resolved
}
$Work = Assert-OwnedRoot (Join-Path $env:RUNNER_TEMP 'checksum-upgrade-smoke') $env:RUNNER_TEMP
$ReceiptPath = Assert-OwnedRoot (Join-Path $env:RUNNER_TEMP 'checksum-upgrade-smoke-receipt.json') $env:RUNNER_TEMP
$ProgramsRoot = Join-Path $env:LOCALAPPDATA 'Programs'
$InstallRoot = Assert-OwnedRoot (Join-Path $ProgramsRoot 'ApplicantScout Companion') $ProgramsRoot
$ConfigRoot = Assert-OwnedRoot (Join-Path $env:LOCALAPPDATA 'applicant-scout') $env:LOCALAPPDATA
foreach ($Path in @($Work, $ReceiptPath, $InstallRoot, $ConfigRoot)) {
    if (Test-Path -LiteralPath $Path) { throw "Smoke target already exists: $Path" }
}

function Read-GitHubJson([string]$Endpoint) {
    $Json = gh api $Endpoint
    if ($LASTEXITCODE -ne 0) { throw "GitHub read failed: $Endpoint" }
    return ($Json | ConvertFrom-Json)
}
function Assert-Checksum([string]$Installer, [string]$Sidecar) {
    $Name = [IO.Path]::GetFileName($Installer)
    $Text = [IO.File]::ReadAllText($Sidecar).Trim()
    if ($Text -cnotmatch '^([0-9a-fA-F]{64})\s+\*?([^\r\n]+)$' -or $Matches[2] -cne $Name) {
        throw 'Installer checksum sidecar is malformed or names another file.'
    }
    $Expected = $Matches[1].ToLowerInvariant()
    $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLowerInvariant()
    if ($Actual -cne $Expected) { throw 'Installer checksum mismatch.' }
    return $Actual
}
function Invoke-Install([string]$Installer, [string]$ExpectedVersion) {
    $Process = Start-Process -FilePath $Installer -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS',
        "/DIR=`"$InstallRoot`""
    ) -WindowStyle Hidden -PassThru -Wait
    if ($Process.ExitCode -ne 0) { throw "Installer failed: $($Process.ExitCode)" }
    $Payload = Join-Path $InstallRoot 'current'
    $Marker = Join-Path $Payload '.apscout-payload-version'
    if ([IO.File]::ReadAllText($Marker).Trim() -cne $ExpectedVersion) {
        throw 'Installed payload version does not match the exercised candidate.'
    }
    foreach ($Name in @('.apscout-backup', '.apscout-next', '.apscout-promotion-pending')) {
        if (Test-Path -LiteralPath (Join-Path $InstallRoot $Name)) { throw 'Incomplete installer promotion.' }
    }
    $Probe = Start-Process -FilePath (Join-Path $Payload 'ApplicantScout.exe') `
        -ArgumentList '--startup-import-probe' -WindowStyle Hidden -PassThru -Wait
    if ($Probe.ExitCode -ne 0) { throw "Installed startup probe failed: $($Probe.ExitCode)" }
}
function Remove-OwnedTree([string]$Path) {
    $Path = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($Path -notin @($Work, $InstallRoot, $ConfigRoot)) { throw 'Refusing unowned cleanup path.' }
    $Parent = if ($Path -eq $Work) { $env:RUNNER_TEMP }
        elseif ($Path -eq $InstallRoot) { $ProgramsRoot } else { $env:LOCALAPPDATA }
    $null = Assert-OwnedRoot $Path $Parent
    $Items = @(Get-Item -LiteralPath $Path) + @(Get-ChildItem -LiteralPath $Path -Recurse -Force)
    if (@($Items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) {
        throw 'Refusing smoke cleanup through a reparse point.'
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

$Commit = (git rev-parse HEAD).Trim()
$TagCommit = (git rev-parse "$Tag^{commit}").Trim()
if ($LASTEXITCODE -ne 0 -or $Commit -cnotmatch '^[0-9a-f]{40}$' -or $Commit -cne $TagCommit) {
    throw 'Checkout must match the exact candidate tag.'
}
git merge-base --is-ancestor $Commit refs/remotes/origin/main
if ($LASTEXITCODE -ne 0) { throw 'Candidate tag must be reachable from origin/main.' }
$Run = Read-GitHubJson "repos/$Repo/actions/runs/$ReleaseRunId"
if ([string]$Run.path -notmatch '^\.github/workflows/release\.yml(?:@.*)?$' -or
    [string]$Run.event -cne 'push' -or [string]$Run.status -cne 'completed' -or
    [string]$Run.conclusion -cne 'success' -or [string]$Run.head_sha -cne $Commit -or
    [string]$Run.head_branch -cne $Tag -or [string]$Run.run_attempt -cnotmatch '^[1-9][0-9]*$') {
    throw 'Candidate must come from the exact successful tag-push release run.'
}
$Attempt = [int]$Run.run_attempt
# This is the final post-signing Release bundle, never the unsigned Build bundle.
$ArtifactName = "companion-release-assets-attempt-$Attempt"
$Artifacts = Read-GitHubJson "repos/$Repo/actions/runs/$ReleaseRunId/artifacts?name=$ArtifactName&per_page=100"
$Records = @($Artifacts.artifacts)
if ($Records.Count -ne 1 -or [string]$Records[0].name -cne $ArtifactName -or $Records[0].expired -ne $false) {
    throw 'Exact final release artifact is missing, ambiguous, or expired.'
}
$Baseline = Read-GitHubJson "repos/$Repo/releases/latest"
$BaselineTag = [string]$Baseline.tag_name
if ($Baseline.draft -ne $false -or $Baseline.prerelease -ne $false -or $Baseline.immutable -ne $true -or
    $BaselineTag -cnotmatch '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
    [version]$BaselineTag.Substring(1) -ge [version]$Version) {
    throw 'Latest stable baseline must be immutable and older than the candidate.'
}
$BaselineVersion = $BaselineTag.Substring(1)
$OwnedWork = $false
$OwnedInstall = $false
$OwnedConfig = $false
$ReadToken = $env:GH_TOKEN
try {
    New-Item -ItemType Directory -Path $Work | Out-Null
    $OwnedWork = $true
    $CandidateRoot = Join-Path $Work 'candidate'
    gh run download $ReleaseRunId --repo $Repo --name $ArtifactName --dir $CandidateRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not download authoritative final release bundle.' }
    $ManifestPath = Join-Path $CandidateRoot "ApplicantScoutCompanion-$Version-release-manifest.json"
    $Manifest = [IO.File]::ReadAllText($ManifestPath) | ConvertFrom-Json
    & "$PSScriptRoot/release-artifact-manifest.ps1" -Mode Verify -Purpose Release -Tag $Tag `
        -CommitSha $Commit -PairedAddonTag ([string]$Manifest.pairedAddonTag) `
        -PairedAddonCommit ([string]$Manifest.pairedAddonCommit) -WorkflowRunId $ReleaseRunId `
        -WorkflowRunAttempt $Attempt -RootPath $CandidateRoot
    if (-not $?) { throw 'Authoritative final release manifest verification failed.' }
    $Candidate = Join-Path $CandidateRoot "ApplicantScoutCompanionSetup-$Version.exe"
    $CandidateHash = Assert-Checksum $Candidate "$Candidate.sha256"
    $BaselineRoot = Join-Path $Work 'baseline'
    New-Item -ItemType Directory -Path $BaselineRoot | Out-Null
    $BaselineName = "ApplicantScoutCompanionSetup-$BaselineVersion.exe"
    foreach ($Name in @($BaselineName, "$BaselineName.sha256")) {
        $Assets = @($Baseline.assets | Where-Object { [string]$_.name -ceq $Name })
        if ($Assets.Count -ne 1 -or [string]$Assets[0].digest -cnotmatch '^sha256:[0-9a-f]{64}$') {
            throw 'Baseline installer/checksum lacks a unique server digest.'
        }
        gh release download $BaselineTag --repo $Repo --pattern $Name --dir $BaselineRoot
        if ($LASTEXITCODE -ne 0) { throw 'Could not download immutable public baseline.' }
        $Downloaded = Join-Path $BaselineRoot $Name
        $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Downloaded).Hash.ToLowerInvariant()
        if ("sha256:$Hash" -cne [string]$Assets[0].digest -or
            (Get-Item -LiteralPath $Downloaded).Length -ne [long]$Assets[0].size) {
            throw 'Baseline download differs from immutable server metadata.'
        }
    }
    $BaselineInstaller = Join-Path $BaselineRoot $BaselineName
    $BaselineHash = Assert-Checksum $BaselineInstaller "$BaselineInstaller.sha256"
    # Executed release binaries receive no repository credential.
    Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:GITHUB_TOKEN -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $InstallRoot | Out-Null
    $OwnedInstall = $true
    New-Item -ItemType Directory -Path $ConfigRoot | Out-Null
    $OwnedConfig = $true
    $Sentinel = Join-Path $ConfigRoot 'checksum-upgrade-sentinel.txt'
    $SentinelText = [guid]::NewGuid().ToString()
    [IO.File]::WriteAllText($Sentinel, $SentinelText)
    $ConfigDirectory = Join-Path $ConfigRoot 'config'
    New-Item -ItemType Directory -Path $ConfigDirectory | Out-Null
    $ConfigPath = Join-Path $ConfigDirectory 'config.env'
    [IO.File]::WriteAllText($ConfigPath,
        "APSCOUT_CONFIG_SCHEMA=1`nAPSCOUT_REGION=EU`nAPSCOUT_SYNC_WITH_WOW=0`nAPSCOUT_FETCH_MPLUS=0`n")
    $UsagePath = Join-Path $ConfigDirectory 'usage.json'
    [IO.File]::WriteAllText($UsagePath, '{"schema":1,"consent":false}')
    $ConfigHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ConfigPath).Hash
    $UsageHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $UsagePath).Hash
    Invoke-Install $BaselineInstaller $BaselineVersion
    Invoke-Install $Candidate $Version
    if ([IO.File]::ReadAllText($Sentinel) -cne $SentinelText) { throw 'Upgrade lost user-data sentinel.' }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $ConfigPath).Hash -cne $ConfigHash -or
        (Get-FileHash -Algorithm SHA256 -LiteralPath $UsagePath).Hash -cne $UsageHash) {
        throw 'Upgrade altered saved configuration or persisted usage opt-out.'
    }
    $env:GH_TOKEN = $ReadToken
    $LatestAfter = Read-GitHubJson "repos/$Repo/releases/latest"
    if ([string]$LatestAfter.tag_name -cne $BaselineTag -or $LatestAfter.immutable -ne $true) {
        throw 'Latest stable changed during smoke; repeat against its new baseline.'
    }
    $Receipt = [ordered]@{
        schemaVersion = 1; tag = $Tag; commit = $Commit; releaseRunId = $ReleaseRunId
        releaseRunAttempt = $Attempt; artifactName = $ArtifactName
        manifestSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ManifestPath).Hash.ToLowerInvariant()
        baselineVersion = $BaselineVersion; baselineInstallerSha256 = $BaselineHash
        candidateInstallerSha256 = $CandidateHash; startupProbesPassed = $true
        configSentinelPreserved = $true; smokeRunId = $env:GITHUB_RUN_ID
        configFilePreserved = $true; usageOptOutPreserved = $true
        completedAt = [DateTime]::UtcNow.ToString('o')
        scope = 'Checksum-verified candidate installer upgrade; draft feed is not exercised.'
    }
}
finally {
    $env:GH_TOKEN = $ReadToken
    if ($OwnedInstall) { Remove-OwnedTree $InstallRoot }
    if ($OwnedConfig) { Remove-OwnedTree $ConfigRoot }
    if ($OwnedWork) { Remove-OwnedTree $Work }
}
# A receipt exists only after successful checks and protocol-owned cleanup.
[IO.File]::WriteAllText($ReceiptPath, ($Receipt | ConvertTo-Json -Depth 5) + "`n", [Text.UTF8Encoding]::new($false))
Write-Output "Checksum upgrade smoke passed: baseline=$BaselineVersion candidate=$Version sha256=$CandidateHash"
