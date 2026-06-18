param(
    [string]$Tag = $env:GITHUB_REF_NAME,
    [switch]$RequireAssets,
    [switch]$RequireDraftReleaseAssets,
    [switch]$RequirePublishedReleaseAssets,
    [switch]$RefuseExistingRelease,
    [switch]$RequirePublishedPairedAddonAssets,
    [string]$PairedAddonRefOutputPath,
    [string]$PairedAddonRoot,
    [string]$GitHubCliPath = "gh",
    [string]$GitHubRepository = $env:GITHUB_REPOSITORY,
    [int]$PublishedReleaseWaitSeconds = 120,
    [int]$PublishedReleasePollSeconds = 10
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

function Get-TopChangelogSection {
    param(
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing paired addon changelog file: $Path"
    }
    $Text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $Options = [System.Text.RegularExpressions.RegexOptions]::Multiline -bor
        [System.Text.RegularExpressions.RegexOptions]::Singleline
    $Match = [regex]::Match(
        $Text,
        "^##\s+([0-9]+\.[0-9]+\.[0-9]+)\s+-\s+.+?(?=^##\s+[0-9]+\.[0-9]+\.[0-9]+\s+-\s+|\z)",
        $Options
    )
    if (-not $Match.Success) {
        throw "Missing top paired addon changelog section in $Path"
    }
    return $Match
}

function Assert-PublicInstallLinksUseLatest {
    param(
        [string]$Name,
        [string]$Text,
        [string[]]$RequiredLatestUrls
    )

    $LinkErrors = @()
    foreach ($Url in $RequiredLatestUrls) {
        if (-not $Text.Contains($Url)) {
            $LinkErrors += "$Name does not point installs at $Url."
        }
    }

    $PinnedPatterns = @(
        'https://github\.com/Antrakt92/(ApplicantScout-Addon|ApplicantScout-Companion)/(releases(?!/latest)(?:[/?#\s\)\]\}]|$)|archive(/|$)|archive/refs/tags/|zipball/|tarball/)',
        '\bApplicantScout\s+WoW\s+addon\s+`?\d+\.\d+\.\d+`?',
        '\bApplicantScout\s+Companion\s+`?\d+\.\d+\.\d+`?',
        '\bApplicantScout-v?\d+\.\d+\.\d+\.zip\b',
        '\bApplicantScoutCompanionSetup-\d+\.\d+\.\d+\.exe(?:\.sha256)?\b',
        '\bApplicantScoutCompanion-\d+\.\d+\.\d+-portable\.zip\b'
    )
    foreach ($Pattern in $PinnedPatterns) {
        if ([regex]::IsMatch($Text, $Pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
            $LinkErrors += "$Name pins install/version copy; use releases/latest for cross-component docs."
            break
        }
    }

    return $LinkErrors
}

function Get-PairedAddonMetadata {
    param(
        [string]$Root
    )

    $ResolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    $TocPath = Join-Path $ResolvedRoot "ApplicantScout.toc"
    if (-not (Test-Path -LiteralPath $TocPath)) {
        throw "Missing paired addon TOC: $TocPath"
    }
    $TocText = Get-Content -LiteralPath $TocPath -Raw -Encoding UTF8
    $TocMatches = [regex]::Matches(
        $TocText,
        '^##\s+Version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$',
        [System.Text.RegularExpressions.RegexOptions]::Multiline
    )
    if ($TocMatches.Count -ne 1) {
        throw "Expected exactly one paired addon TOC version in $TocPath, found $($TocMatches.Count)."
    }

    $ChangelogPath = Join-Path $ResolvedRoot "CHANGELOG.md"
    $TopChangelogMatch = Get-TopChangelogSection -Path $ChangelogPath
    $TopChangelogSection = $TopChangelogMatch.Value
    $PairedCompanionMatches = [regex]::Matches(
        $TopChangelogSection,
        '(?i)(?:ApplicantScout\s+)?Companion\s+`?([0-9]+\.[0-9]+\.[0-9]+)`?',
        [System.Text.RegularExpressions.RegexOptions]::Multiline
    )
    $PairedCompanionVersions = @(
        $PairedCompanionMatches |
            ForEach-Object { $_.Groups[1].Value } |
            Sort-Object -Unique
    )

    return @{
        TocVersion = $TocMatches[0].Groups[1].Value
        ChangelogVersion = $TopChangelogMatch.Groups[1].Value
        PairedCompanionVersions = $PairedCompanionVersions
    }
}

function Compare-SemVer {
    param(
        [string]$Left,
        [string]$Right
    )

    $LeftParts = @($Left.Split(".") | ForEach-Object { [int]$_ })
    $RightParts = @($Right.Split(".") | ForEach-Object { [int]$_ })
    for ($Index = 0; $Index -lt 3; $Index++) {
        if ($LeftParts[$Index] -lt $RightParts[$Index]) {
            return -1
        }
        if ($LeftParts[$Index] -gt $RightParts[$Index]) {
            return 1
        }
    }
    return 0
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

function Test-PortableZipContract {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PortablePath,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedRoot,
        [Parameter(Mandatory = $true)]
        [string[]]$RequiredEntries
    )

    $ContractErrors = @()
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $Zip = [System.IO.Compression.ZipFile]::OpenRead($PortablePath)
    }
    catch {
        return @("Portable ZIP could not be opened: dist\$(Split-Path -Leaf $PortablePath). $($_.Exception.Message)")
    }

    try {
        $SeenEntries = @{}
        $FileEntries = @{}
        $HasLicensePayload = $false
        foreach ($Entry in $Zip.Entries) {
            $EntryName = $Entry.FullName -replace '\\', '/'
            if ([string]::IsNullOrWhiteSpace($EntryName)) {
                $ContractErrors += "Unsafe portable ZIP entry: empty name."
                continue
            }
            if ($EntryName.StartsWith("/") -or $EntryName -match "^[A-Za-z]:") {
                $ContractErrors += "Unsafe portable ZIP entry: $EntryName"
                continue
            }

            $NormalizedName = $EntryName.TrimEnd("/")
            if ([string]::IsNullOrWhiteSpace($NormalizedName)) {
                $ContractErrors += "Unsafe portable ZIP entry: $EntryName"
                continue
            }

            $Segments = @($NormalizedName -split "/")
            if ($Segments | Where-Object { $_ -eq "" -or $_ -eq "." -or $_ -eq ".." }) {
                $ContractErrors += "Unsafe portable ZIP entry: $EntryName"
                continue
            }
            if ($Segments[0] -ne $ExpectedRoot) {
                $ContractErrors += "Portable ZIP entry is outside ${ExpectedRoot}/: $EntryName"
                continue
            }

            $EntryKey = $NormalizedName.ToLowerInvariant()
            if ($SeenEntries.ContainsKey($EntryKey)) {
                $ContractErrors += "Portable ZIP has duplicate entry after normalization: $NormalizedName"
                continue
            }
            $SeenEntries[$EntryKey] = $true

            $IsDirectory = $EntryName.EndsWith("/")
            if (-not $IsDirectory) {
                $FileEntries[$NormalizedName] = $Entry
                if ($NormalizedName.StartsWith("$ExpectedRoot/licenses/")) {
                    $HasLicensePayload = $true
                }
            }
        }

        if ($FileEntries.Count -eq 0) {
            $ContractErrors += "Portable ZIP contains no files."
        }
        foreach ($RequiredEntry in $RequiredEntries) {
            if (-not $FileEntries.ContainsKey($RequiredEntry)) {
                $ContractErrors += "Portable ZIP is missing required entry: $RequiredEntry"
                continue
            }
            if ($FileEntries[$RequiredEntry].Length -le 0) {
                $ContractErrors += "Portable ZIP required entry is empty: $RequiredEntry"
            }
        }
        if (-not $HasLicensePayload) {
            $ContractErrors += "Portable ZIP is missing dependency license payload under $ExpectedRoot/licenses/."
        }
    }
    finally {
        $Zip.Dispose()
    }

    return $ContractErrors
}

function Invoke-GitHubReleaseView {
    param(
        [string]$CliPath,
        [string]$Repo,
        [string]$ReleaseTag
    )

    $ErrorPath = [System.IO.Path]::GetTempFileName()
    try {
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $JsonLines = & $CliPath release view $ReleaseTag --repo $Repo --json "tagName,isDraft,isPrerelease,assets" 2> $ErrorPath
            $ExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        $ErrorRaw = Get-Content -LiteralPath $ErrorPath -Raw -ErrorAction SilentlyContinue
        $ErrorText = if ($null -eq $ErrorRaw) { "" } else { $ErrorRaw.Trim() }
        if ($ExitCode -ne 0) {
            $Message = "gh release view failed for $Repo $ReleaseTag with exit code $ExitCode."
            if ($ErrorText) {
                $Message = "$Message $ErrorText"
            }
            throw $Message
        }

        $JsonText = ($JsonLines -join "`n").Trim()
        if (-not $JsonText) {
            throw "gh release view returned empty JSON for $Repo $ReleaseTag."
        }
        try {
            return ($JsonText | ConvertFrom-Json)
        }
        catch {
            throw "gh release view returned malformed JSON for $Repo $ReleaseTag."
        }
    }
    finally {
        if (Test-Path -LiteralPath $ErrorPath) {
            Remove-Item -LiteralPath $ErrorPath -Force
        }
    }
}

function Assert-GitHubReleaseDoesNotExist {
    param(
        [string]$CliPath,
        [string]$Repo,
        [string]$ReleaseTag
    )

    if ([string]::IsNullOrWhiteSpace($Repo)) {
        throw "Missing GitHub repository for release existence check."
    }
    if ([string]::IsNullOrWhiteSpace($ReleaseTag)) {
        throw "Missing release tag for release existence check."
    }

    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $OutputLines = @(
            & $CliPath release view $ReleaseTag --repo $Repo --json "tagName,isDraft,isPrerelease" 2>&1
        )
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    $OutputText = (
        $OutputLines | ForEach-Object { $_.ToString() }
    ) -join "`n"
    $OutputText = $OutputText.Trim()
    if ($ExitCode -eq 0) {
        throw "Release $ReleaseTag already exists; refusing to rebuild or republish companion assets."
    }
    if ($OutputText -match "(?i)\b(release not found|no release found)\b") {
        $global:LASTEXITCODE = 0
        return
    }

    $Message = "Could not determine whether release $ReleaseTag already exists in $Repo; gh release view exited with exit code $ExitCode."
    if ($OutputText) {
        $Message = "$Message $OutputText"
    }
    throw $Message
}

function Assert-GitHubReleaseLookupParameters {
    param(
        [string]$Repo,
        [string]$ReleaseTag,
        [string]$Description
    )

    if ([string]::IsNullOrWhiteSpace($Repo)) {
        throw "Missing GitHub repository for $Description."
    }
    if ([string]::IsNullOrWhiteSpace($ReleaseTag)) {
        throw "Missing release tag for $Description."
    }
}

function Test-GitHubReleaseAssets {
    param(
        [object]$Release,
        [string]$Repo,
        [string]$ReleaseTag,
        [string[]]$ExpectedAssets,
        [string[]]$ProtectedAssetPatterns = @(),
        [ValidateSet("Draft", "Published")]
        [string]$ExpectedState = "Published"
    )

    if ($null -eq $Release) {
        throw "GitHub Release $ReleaseTag in $Repo was not returned by gh."
    }
    if ($ExpectedState -eq "Draft") {
        if (-not $Release.isDraft) {
            throw "GitHub Release $ReleaseTag in $Repo was expected draft but is already public."
        }
    }
    elseif ($Release.isDraft) {
        throw "GitHub Release $ReleaseTag in $Repo is still draft; publish the release before continuing."
    }
    if ($Release.isPrerelease) {
        throw "GitHub Release $ReleaseTag in $Repo is marked prerelease; publish a stable release before continuing."
    }

    $Assets = if ($null -eq $Release.assets) { @() } else { @($Release.assets) }
    $AssetNames = @($Assets | ForEach-Object { $_.name })
    foreach ($AssetName in $ExpectedAssets) {
        if ($AssetNames -notcontains $AssetName) {
            throw "GitHub Release $ReleaseTag in $Repo is missing asset: $AssetName"
        }
    }
    foreach ($AssetName in $AssetNames) {
        if ($ExpectedAssets -contains $AssetName) {
            continue
        }
        foreach ($Pattern in $ProtectedAssetPatterns) {
            if ($AssetName -match $Pattern) {
                throw "GitHub Release $ReleaseTag in $Repo has unexpected asset: $AssetName"
            }
        }
    }
}

function Wait-GitHubReleaseAssets {
    param(
        [string]$CliPath,
        [string]$Repo,
        [string]$ReleaseTag,
        [string[]]$ExpectedAssets,
        [string[]]$ProtectedAssetPatterns = @(),
        [ValidateSet("Draft", "Published")]
        [string]$ExpectedState = "Published",
        [int]$WaitSeconds,
        [int]$PollSeconds
    )

    if ($WaitSeconds -lt 0) {
        throw "PublishedReleaseWaitSeconds must be zero or greater."
    }
    if ($PollSeconds -lt 1) {
        throw "PublishedReleasePollSeconds must be at least 1."
    }

    $Deadline = (Get-Date).AddSeconds($WaitSeconds)
    $LastError = $null
    do {
        try {
            $Release = Invoke-GitHubReleaseView -CliPath $CliPath -Repo $Repo -ReleaseTag $ReleaseTag
            Test-GitHubReleaseAssets `
                -Release $Release `
                -Repo $Repo `
                -ReleaseTag $ReleaseTag `
                -ExpectedAssets $ExpectedAssets `
                -ProtectedAssetPatterns $ProtectedAssetPatterns `
                -ExpectedState $ExpectedState
            return
        }
        catch {
            $LastError = $_.Exception.Message
            if ((Get-Date) -ge $Deadline) {
                break
            }
            Start-Sleep -Seconds $PollSeconds
        }
    } while ($true)

    throw $LastError
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
$IsCompanionOnlyPatch = [regex]::IsMatch(
    $TopReleaseNotesEntry,
    '\bcompanion-only\b',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
)
$ConstraintsVersion = Get-FirstRegexMatch `
    -Path "constraints-release.txt" `
    -Pattern '^# Release build constraints for ApplicantScout Companion ([0-9]+\.[0-9]+\.[0-9]+)\.' `
    -Description "Release constraints header"

$Readme = Get-Content -LiteralPath (Join-Path $RepoRoot "README.md") -Raw -Encoding UTF8
$Errors = @()
$InstallerName = "ApplicantScoutCompanionSetup-$TagVersion.exe"
$ChecksumName = "$InstallerName.sha256"
$PortableName = "ApplicantScoutCompanion-$TagVersion-portable.zip"
$ExpectedCompanionAssets = @($InstallerName, $ChecksumName, $PortableName)
$ProtectedCompanionAssetPatterns = @(
    '^ApplicantScoutCompanionSetup-\d+\.\d+\.\d+\.exe$',
    '^ApplicantScoutCompanionSetup-\d+\.\d+\.\d+\.exe\.sha256$',
    '^ApplicantScoutCompanion-\d+\.\d+\.\d+-portable\.zip$'
)
$ProtectedAddonAssetPatterns = @(
    '^ApplicantScout-v?\d+\.\d+\.\d+(?:-[A-Za-z0-9._-]+)?\.zip$'
)
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
$CompanionLatestUrl = "https://github.com/Antrakt92/ApplicantScout-Companion/releases/latest"
if ($Readme.Contains($CompanionMarkdown)) {
    $Errors += "README.md should not pin the current companion version; RELEASE_NOTES.md owns release-specific version copy."
}
$Errors += Assert-PublicInstallLinksUseLatest `
    -Name "README.md" `
    -Text $Readme `
    -RequiredLatestUrls @($AddonLatestUrl, $CompanionLatestUrl)

if ($RequireAssets) {
    foreach ($AssetName in $ExpectedCompanionAssets) {
        $AssetPath = Join-Path $RepoRoot "dist\$AssetName"
        if (-not (Test-Path -LiteralPath $AssetPath)) {
            $Errors += "Missing release asset: dist\$AssetName"
        }
    }
    $InstallerPath = Join-Path $RepoRoot "dist\$InstallerName"
    $ChecksumPath = Join-Path $RepoRoot "dist\$ChecksumName"
    $PortablePath = Join-Path $RepoRoot "dist\$PortableName"
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
    if (Test-Path -LiteralPath $PortablePath) {
        $Errors += Test-PortableZipContract `
            -PortablePath $PortablePath `
            -ExpectedRoot "ApplicantScout" `
            -RequiredEntries @(
                "ApplicantScout/ApplicantScout.exe",
                "ApplicantScout/LICENSE",
                "ApplicantScout/THIRD-PARTY-NOTICES.md",
                "ApplicantScout/RELEASE_NOTES.md"
            )
    }
}

if ($Errors.Count -gt 0) {
    foreach ($ErrorMessage in $Errors) {
        Write-Host "ERROR: $ErrorMessage" -ForegroundColor Red
    }
    throw "Release version check failed."
}

if ($RefuseExistingRelease) {
    Assert-GitHubReleaseDoesNotExist `
        -CliPath $GitHubCliPath `
        -Repo $GitHubRepository `
        -ReleaseTag $TagName
}

if ($RequireDraftReleaseAssets) {
    Assert-GitHubReleaseLookupParameters `
        -Repo $GitHubRepository `
        -ReleaseTag $TagName `
        -Description "release asset check"
    $Release = Invoke-GitHubReleaseView `
        -CliPath $GitHubCliPath `
        -Repo $GitHubRepository `
        -ReleaseTag $TagName
    Test-GitHubReleaseAssets `
        -Release $Release `
        -Repo $GitHubRepository `
        -ReleaseTag $TagName `
        -ExpectedAssets $ExpectedCompanionAssets `
        -ProtectedAssetPatterns $ProtectedCompanionAssetPatterns `
        -ExpectedState "Draft"
}

if ($RequirePublishedReleaseAssets) {
    Assert-GitHubReleaseLookupParameters `
        -Repo $GitHubRepository `
        -ReleaseTag $TagName `
        -Description "release asset check"
    Wait-GitHubReleaseAssets `
        -CliPath $GitHubCliPath `
        -Repo $GitHubRepository `
        -ReleaseTag $TagName `
        -ExpectedAssets $ExpectedCompanionAssets `
        -ProtectedAssetPatterns $ProtectedCompanionAssetPatterns `
        -ExpectedState "Published" `
        -WaitSeconds $PublishedReleaseWaitSeconds `
        -PollSeconds $PublishedReleasePollSeconds
}

if ($PairedAddonRoot) {
    $AddonMetadata = Get-PairedAddonMetadata -Root $PairedAddonRoot
    if ($AddonMetadata.ChangelogVersion -ne $AddonMetadata.TocVersion) {
        $Errors += "Paired addon CHANGELOG.md top entry is $($AddonMetadata.ChangelogVersion), expected $($AddonMetadata.TocVersion) from paired addon TOC."
    }
    if ($AddonMetadata.PairedCompanionVersions.Count -eq 0) {
        $Errors += "Paired addon CHANGELOG.md top entry must name exactly one paired ApplicantScout Companion version."
    }
    if ($AddonMetadata.PairedCompanionVersions.Count -gt 1) {
        $Errors += "Paired addon CHANGELOG.md top entry names multiple paired ApplicantScout Companion versions: $($AddonMetadata.PairedCompanionVersions -join ', ')."
    }
    if (
        $AddonMetadata.PairedCompanionVersions.Count -eq 1 -and
        $AddonMetadata.PairedCompanionVersions[0] -ne $TagVersion
    ) {
        if (-not $IsCompanionOnlyPatch) {
            $Errors += "Paired addon CHANGELOG.md top entry names companion $($AddonMetadata.PairedCompanionVersions[0]), expected $TagVersion."
        }
        elseif ((Compare-SemVer -Left $AddonMetadata.PairedCompanionVersions[0] -Right $TagVersion) -gt 0) {
            $Errors += "Paired addon CHANGELOG.md top entry names companion $($AddonMetadata.PairedCompanionVersions[0]), which is newer than companion-only release $TagVersion."
        }
    }
    if ((Compare-SemVer -Left $AddonMetadata.TocVersion -Right $PairedAddonVersion) -lt 0) {
        $Errors += "Paired addon version is $($AddonMetadata.TocVersion), which is older than required $PairedAddonVersion from RELEASE_NOTES.md."
    }
}

if ($Errors.Count -gt 0) {
    foreach ($ErrorMessage in $Errors) {
        Write-Host "ERROR: $ErrorMessage" -ForegroundColor Red
    }
    throw "Release version check failed."
}

if ($RequirePublishedPairedAddonAssets) {
    $PairedAddonTag = "v$PairedAddonVersion"
    $ExpectedAddonAssets = @(
        "ApplicantScout-$PairedAddonTag.zip",
        "release.json"
    )
    Wait-GitHubReleaseAssets `
        -CliPath $GitHubCliPath `
        -Repo "Antrakt92/ApplicantScout-Addon" `
        -ReleaseTag $PairedAddonTag `
        -ExpectedAssets $ExpectedAddonAssets `
        -ProtectedAssetPatterns $ProtectedAddonAssetPatterns `
        -WaitSeconds $PublishedReleaseWaitSeconds `
        -PollSeconds $PublishedReleasePollSeconds
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
