param(
    [string]$Tag = $env:GITHUB_REF_NAME,
    [switch]$RequireAssets
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
$ReleaseNotesVersion = Get-FirstRegexMatch `
    -Path "RELEASE_NOTES.md" `
    -Pattern '^##\s+([0-9]+\.[0-9]+\.[0-9]+)\s+-\s+' `
    -Description "top release notes entry"

$Readme = Get-Content -LiteralPath (Join-Path $RepoRoot "README.md") -Raw -Encoding UTF8
$Errors = @()
if ($PyprojectVersion -ne $TagVersion) {
    $Errors += "pyproject.toml version is $PyprojectVersion, expected $TagVersion from tag $TagName."
}
if ($RuntimeVersion -ne $TagVersion) {
    $Errors += "src/applicant_scout/__init__.py __version__ is $RuntimeVersion, expected $TagVersion from tag $TagName."
}
if ($ReleaseNotesVersion -ne $TagVersion) {
    $Errors += "RELEASE_NOTES.md top entry is $ReleaseNotesVersion, expected $TagVersion from tag $TagName."
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

$InstallerName = "ApplicantScoutCompanionSetup-$TagVersion.exe"
$ChecksumName = "$InstallerName.sha256"
$PortableName = "ApplicantScoutCompanion-$TagVersion-portable.zip"
if ($RequireAssets) {
    foreach ($AssetName in @($InstallerName, $ChecksumName, $PortableName)) {
        $AssetPath = Join-Path $RepoRoot "dist\$AssetName"
        if (-not (Test-Path -LiteralPath $AssetPath)) {
            $Errors += "Missing release asset: dist\$AssetName"
        }
    }
}

if ($Errors.Count -gt 0) {
    foreach ($ErrorMessage in $Errors) {
        Write-Host "ERROR: $ErrorMessage" -ForegroundColor Red
    }
    throw "Release version check failed."
}

Write-Host "Release version check passed: $TagName -> $TagVersion"
Write-Host "Expected installer asset: $InstallerName"
Write-Host "Expected checksum asset: $ChecksumName"
Write-Host "Expected portable asset: $PortableName"
