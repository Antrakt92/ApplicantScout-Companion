param(
    [string]$Tag = $env:GITHUB_REF_NAME,
    [switch]$RequireAssets,
    [string]$PairedAddonRefOutputPath
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Get-SingleRegexMatch {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Pattern,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $FullPath = Join-Path $RepoRoot $Path
    if (-not (Test-Path -LiteralPath $FullPath)) {
        throw "Missing $Description file: $FullPath"
    }
    $Text = Get-Content -LiteralPath $FullPath -Raw -Encoding UTF8
    $MatchesFound = [regex]::Matches($Text, $Pattern, [System.Text.RegularExpressions.RegexOptions]::Multiline)
    if ($MatchesFound.Count -ne 1) {
        throw "Expected exactly one $Description match in ${Path}, found $($MatchesFound.Count)."
    }
    return $MatchesFound[0].Groups[1].Value
}

function Get-FirstRegexMatch {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Pattern,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $FullPath = Join-Path $RepoRoot $Path
    if (-not (Test-Path -LiteralPath $FullPath)) {
        throw "Missing $Description file: $FullPath"
    }
    $Text = Get-Content -LiteralPath $FullPath -Raw -Encoding UTF8
    $Match = [regex]::Match($Text, $Pattern, [System.Text.RegularExpressions.RegexOptions]::Multiline)
    if (-not $Match.Success) {
        throw "Could not find $Description in $Path."
    }
    return $Match.Groups[1].Value
}

function Test-InstallerChecksum {
    param(
        [Parameter(Mandatory = $true)]
        [string]$InstallerPath,
        [Parameter(Mandatory = $true)]
        [string]$ChecksumPath,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedName
    )

    $ChecksumText = (Get-Content -LiteralPath $ChecksumPath -Raw -Encoding ASCII).Trim()
    if (-not $ChecksumText) {
        return "Malformed checksum: dist\$ExpectedName.sha256 is empty."
    }

    $Parts = $ChecksumText -split "\s+", 3
    if ($Parts.Count -ne 2) {
        return "Malformed checksum: expected '<sha256>  $ExpectedName'."
    }

    $ExpectedDigest = $Parts[0].ToLowerInvariant()
    if ($ExpectedDigest -notmatch "^[0-9a-f]{64}$") {
        return "Malformed checksum: expected a 64-character SHA256 digest."
    }

    $ChecksumName = $Parts[1].TrimStart("*")
    if ($ChecksumName.ToLowerInvariant() -ne $ExpectedName.ToLowerInvariant()) {
        return "Checksum filename is $ChecksumName, expected $ExpectedName."
    }

    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    $Stream = [System.IO.File]::OpenRead($InstallerPath)
    try {
        $ActualDigest = ([System.BitConverter]::ToString($Sha256.ComputeHash($Stream)) -replace "-", "").ToLowerInvariant()
    }
    finally {
        $Stream.Dispose()
        $Sha256.Dispose()
    }
    if ($ActualDigest -ne $ExpectedDigest) {
        return "Installer checksum mismatch for dist\$ExpectedName."
    }

    return $null
}

if (-not $Tag) {
    throw "Missing release tag. Pass -Tag vX.Y.Z or set GITHUB_REF_NAME."
}

$TagName = $Tag
if ($TagName -match "^refs/tags/(.+)$") {
    $TagName = $Matches[1]
}
$TagVersion = if ($TagName.StartsWith("v")) {
    $TagName.Substring(1)
}
else {
    $TagName
}
if ($TagVersion -notmatch "^\d+\.\d+\.\d+$") {
    throw "Malformed release tag '$Tag'. Expected vX.Y.Z or X.Y.Z."
}

$PyprojectVersion = Get-SingleRegexMatch `
    -Path "pyproject.toml" `
    -Pattern '^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$' `
    -Description "pyproject version"
$RuntimeVersion = Get-SingleRegexMatch `
    -Path "src\applicant_scout\__init__.py" `
    -Pattern '^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$' `
    -Description "runtime version"
$ReleaseNotesPath = Join-Path $RepoRoot "RELEASE_NOTES.md"
if (-not (Test-Path -LiteralPath $ReleaseNotesPath)) {
    throw "Missing top release notes entry file: $ReleaseNotesPath"
}
$ReleaseNotesText = Get-Content -LiteralPath $ReleaseNotesPath -Raw -Encoding UTF8
$TopReleaseNotesMatch = [regex]::Match(
    $ReleaseNotesText,
    '(?ms)^##\s+([0-9]+\.[0-9]+\.[0-9]+)\s+-\s+.*?(?=^##\s+\d+\.\d+\.\d+\s+-\s+|\z)'
)
if (-not $TopReleaseNotesMatch.Success) {
    throw "Could not find top release notes entry in RELEASE_NOTES.md."
}
$ReleaseNotesVersion = $TopReleaseNotesMatch.Groups[1].Value
$TopReleaseNotesEntry = $TopReleaseNotesMatch.Value
$PairedAddonLineMatch = [regex]::Match(
    $TopReleaseNotesEntry,
    '(?m)^-\s+Requires the ApplicantScout WoW addon\s+`([^`]+)`\.\s*$'
)
$PairedAddonVersion = $null
$ConstraintsVersion = Get-FirstRegexMatch `
    -Path "constraints-release.txt" `
    -Pattern '^# Release build constraints for ApplicantScout Companion ([0-9]+\.[0-9]+\.[0-9]+)\.' `
    -Description "Release constraints header"

$Readme = Get-Content -LiteralPath (Join-Path $RepoRoot "README.md") -Raw -Encoding UTF8
$Errors = @()
$InstallerName = "ApplicantScoutCompanionSetup-$TagVersion.exe"
$ChecksumName = "$InstallerName.sha256"
$PortableName = "ApplicantScoutCompanion-$TagVersion-portable.zip"
if ($PyprojectVersion -ne $TagVersion) {
    $Errors += "pyproject.toml version is $PyprojectVersion, expected $TagVersion from tag $TagName."
}
if ($RuntimeVersion -ne $TagVersion) {
    $Errors += "src/applicant_scout/__init__.py __version__ is $RuntimeVersion, expected $TagVersion from tag $TagName."
}
if ($ReleaseNotesVersion -ne $TagVersion) {
    $Errors += "RELEASE_NOTES.md top entry is $ReleaseNotesVersion, expected $TagVersion from tag $TagName."
}
if ($ConstraintsVersion -ne $TagVersion) {
    $Errors += "constraints-release.txt header is $ConstraintsVersion, expected $TagVersion from tag $TagName."
}
if (-not $TopReleaseNotesEntry.Contains($InstallerName)) {
    $Errors += "RELEASE_NOTES.md top entry does not mention expected installer asset $InstallerName."
}
if (-not $TopReleaseNotesEntry.Contains($ChecksumName)) {
    $Errors += "RELEASE_NOTES.md top entry does not mention expected checksum asset $ChecksumName."
}
if (-not $PairedAddonLineMatch.Success) {
    $Errors += "RELEASE_NOTES.md top entry does not mention the paired ApplicantScout addon version."
}
elseif ($PairedAddonLineMatch.Groups[1].Value -notmatch "^\d+\.\d+\.\d+$") {
    $Errors += "RELEASE_NOTES.md paired ApplicantScout addon version is malformed: $($PairedAddonLineMatch.Groups[1].Value)."
}
else {
    $PairedAddonVersion = $PairedAddonLineMatch.Groups[1].Value
}
$CompanionMarkdown = "ApplicantScout Companion ``$TagVersion``"
$AddonLatestUrl = "https://github.com/Antrakt92/ApplicantScout-Addon/releases/latest"
if ($Readme.Contains($CompanionMarkdown)) {
    $Errors += "README.md should not pin the current companion version; RELEASE_NOTES.md owns release-specific version copy."
}
if (-not $Readme.Contains($AddonLatestUrl)) {
    $Errors += "README.md does not point addon installs at releases/latest."
}
if ($Readme -match "ApplicantScout-v?\d+\.\d+\.\d+\.zip|ApplicantScout WoW addon\s*`?\d+\.\d+\.\d+`?|releases/tag/v\d+\.\d+\.\d+") {
    $Errors += "README.md pins addon install/version copy; use releases/latest for cross-component docs."
}

if ($RequireAssets) {
    foreach ($AssetName in @($InstallerName, $ChecksumName, $PortableName)) {
        $AssetPath = Join-Path $RepoRoot "dist\$AssetName"
        if (-not (Test-Path -LiteralPath $AssetPath)) {
            $Errors += "Missing release asset: dist\$AssetName"
        }
    }
    $InstallerPath = Join-Path $RepoRoot "dist\$InstallerName"
    $ChecksumPath = Join-Path $RepoRoot "dist\$ChecksumName"
    if (
        (Test-Path -LiteralPath $InstallerPath) -and
        (Test-Path -LiteralPath $ChecksumPath)
    ) {
        $ChecksumError = Test-InstallerChecksum `
            -InstallerPath $InstallerPath `
            -ChecksumPath $ChecksumPath `
            -ExpectedName $InstallerName
        if ($ChecksumError) {
            $Errors += $ChecksumError
        }
    }
}

if ($Errors.Count -gt 0) {
    foreach ($ErrorMessage in $Errors) {
        Write-Host "ERROR: $ErrorMessage" -ForegroundColor Red
    }
    throw "Release version check failed."
}

if ($PairedAddonRefOutputPath) {
    $OutputDirectory = Split-Path -Parent $PairedAddonRefOutputPath
    if ($OutputDirectory -and -not (Test-Path -LiteralPath $OutputDirectory)) {
        New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    }
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::AppendAllText(
        $PairedAddonRefOutputPath,
        "ref=v$PairedAddonVersion`n",
        $Utf8NoBom
    )
}

Write-Host "Release version check passed: $TagName -> $TagVersion"
Write-Host "Expected paired addon ref: v$PairedAddonVersion"
Write-Host "Expected installer asset: $InstallerName"
Write-Host "Expected checksum asset: $ChecksumName"
Write-Host "Expected portable asset: $PortableName"
