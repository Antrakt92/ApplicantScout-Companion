param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$ChecksumPath,
    [switch]$RequireSigning
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

function Find-SignTool {
    $Configured = ""
    if ($null -ne $env:APSCOUT_SIGNTOOL_PATH) {
        $Configured = $env:APSCOUT_SIGNTOOL_PATH.Trim()
    }
    if ($Configured) {
        if (Test-Path -LiteralPath $Configured -PathType Leaf) {
            return $Configured
        }
        throw "APSCOUT_SIGNTOOL_PATH is set but does not exist: $Configured"
    }

    $Command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($null -ne $Command) {
        return $Command.Source
    }

    $Candidates = @()
    foreach ($ProgramFilesRoot in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $ProgramFilesRoot) {
            continue
        }
        $KitsRoot = Join-Path $ProgramFilesRoot "Windows Kits\10\bin"
        if (-not (Test-Path -LiteralPath $KitsRoot -PathType Container)) {
            continue
        }
        $Kits = Get-ChildItem -LiteralPath $KitsRoot -Directory -ErrorAction SilentlyContinue |
            Sort-Object -Property Name -Descending
        foreach ($Kit in $Kits) {
            $Candidates += Join-Path $Kit.FullName "x64\signtool.exe"
            $Candidates += Join-Path $Kit.FullName "x86\signtool.exe"
        }
    }

    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return $Candidate
        }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
    throw "Installer does not exist: $InstallerPath"
}
$Installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$ChecksumDirectory = Split-Path -Parent $ChecksumPath
if ($ChecksumDirectory -and -not (Test-Path -LiteralPath $ChecksumDirectory -PathType Container)) {
    throw "Checksum directory does not exist: $ChecksumDirectory"
}

$CertSha1 = ""
if ($null -ne $env:APSCOUT_SIGNING_CERT_SHA1) {
    $CertSha1 = $env:APSCOUT_SIGNING_CERT_SHA1.Trim()
}
if (-not $CertSha1) {
    if ($RequireSigning) {
        throw "Installer signing is required, but APSCOUT_SIGNING_CERT_SHA1 is not set."
    }
    Write-Host "Installer signing skipped: set APSCOUT_SIGNING_CERT_SHA1 to enable Authenticode signing."
}
else {
    if ($CertSha1 -notmatch '^[0-9A-Fa-f]{40}$') {
        throw "APSCOUT_SIGNING_CERT_SHA1 must be a 40-character SHA-1 certificate thumbprint."
    }
    $TimestampUrl = ""
    if ($null -ne $env:APSCOUT_SIGNING_TIMESTAMP_URL) {
        $TimestampUrl = $env:APSCOUT_SIGNING_TIMESTAMP_URL.Trim()
    }
    if (-not $TimestampUrl) {
        $TimestampUrl = "http://timestamp.digicert.com"
    }
    $ParsedTimestampUrl = $null
    if (
        -not [System.Uri]::TryCreate(
            $TimestampUrl,
            [System.UriKind]::Absolute,
            [ref]$ParsedTimestampUrl
        ) -or
        $ParsedTimestampUrl.Scheme -notin @("http", "https")
    ) {
        throw "APSCOUT_SIGNING_TIMESTAMP_URL must be an absolute HTTP(S) URL."
    }
    $SignTool = Find-SignTool
    if (-not $SignTool) {
        throw "Missing signtool.exe. Install the Windows SDK or set APSCOUT_SIGNTOOL_PATH."
    }

    Invoke-NativeChecked -Label "Sign installer" -Command {
        & $SignTool sign /sha1 $CertSha1 /fd SHA256 /tr $TimestampUrl /td SHA256 $Installer
    }
    Invoke-NativeChecked -Label "Verify installer signature" -Command {
        & $SignTool verify /pa /all $Installer
    }
}

$InstallerStream = [System.IO.File]::OpenRead($Installer)
try {
    $Hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $InstallerHash = (
            [System.BitConverter]::ToString($Hasher.ComputeHash($InstallerStream)) -replace '-', ''
        ).ToLowerInvariant()
    }
    finally {
        $Hasher.Dispose()
    }
}
finally {
    $InstallerStream.Dispose()
}
$ChecksumLine = "$InstallerHash  $(Split-Path -Leaf $Installer)"
$TempChecksumPath = "$ChecksumPath.tmp"
$ChecksumLine | Set-Content -LiteralPath $TempChecksumPath -Encoding ASCII
Move-Item -LiteralPath $TempChecksumPath -Destination $ChecksumPath -Force
Write-Host "Refreshed installer checksum: $ChecksumPath"
