param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Create", "Verify")]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [ValidateSet("Build", "Release")]
    [string]$Purpose,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [Parameter(Mandatory = $true)]
    [string]$CommitSha,
    [Parameter(Mandatory = $true)]
    [string]$PairedAddonTag,
    [Parameter(Mandatory = $true)]
    [string]$PairedAddonCommit,
    [Parameter(Mandatory = $true)]
    [string]$WorkflowRunId,
    [Parameter(Mandatory = $true)]
    [int]$WorkflowRunAttempt,
    [Parameter(Mandatory = $true)]
    [string]$RootPath
)

$ErrorActionPreference = "Stop"
$Repository = "Antrakt92/ApplicantScout-Companion"
$RequiredPortableEntries = @(
    "ApplicantScout/ApplicantScout.exe",
    "ApplicantScout/LICENSE",
    "ApplicantScout/RELEASE_NOTES.md",
    "ApplicantScout/THIRD-PARTY-NOTICES.md"
)

if ($Tag -notmatch '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
    throw "Release tag must use strict vX.Y.Z format: $Tag"
}
$Version = $Tag.Substring(1)
$Commit = $CommitSha.Trim().ToLowerInvariant()
if ($Commit -notmatch '^[0-9a-f]{40}$') {
    throw "Release commit must be a full 40-character Git SHA: $CommitSha"
}
if ($PairedAddonTag -notmatch '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
    throw "Paired addon tag must use strict vX.Y.Z format: $PairedAddonTag"
}
$AddonCommit = $PairedAddonCommit.Trim().ToLowerInvariant()
if ($AddonCommit -notmatch '^[0-9a-f]{40}$') {
    throw "Paired addon commit must be a full 40-character Git SHA: $PairedAddonCommit"
}
$RunId = $WorkflowRunId.Trim()
if ($RunId -notmatch '^[1-9][0-9]*$') {
    throw "Workflow run ID must be a positive integer: $WorkflowRunId"
}
if ($WorkflowRunAttempt -lt 1) {
    throw "Workflow run attempt must be a positive integer: $WorkflowRunAttempt"
}
if (-not (Test-Path -LiteralPath $RootPath -PathType Container)) {
    throw "Release artifact root does not exist: $RootPath"
}
$Root = (Resolve-Path -LiteralPath $RootPath).Path

$InstallerName = "ApplicantScoutCompanionSetup-$Version.exe"
$ChecksumName = "$InstallerName.sha256"
$PortableName = "ApplicantScoutCompanion-$Version-portable.zip"
$ManifestName = if ($Purpose -eq "Build") {
    "release-build-manifest.json"
}
else {
    "ApplicantScoutCompanion-$Version-release-manifest.json"
}
$ExpectedFileNames = @($InstallerName, $ChecksumName, $PortableName)
if ($Purpose -eq "Build") {
    $ExpectedFileNames += "release-body.md"
}
$ExpectedFileNames = @($ExpectedFileNames | Sort-Object)
$ManifestPath = Join-Path $Root $ManifestName

function Get-Sha256Hex {
    param(
        [Parameter(Mandatory = $true)]
        [System.IO.Stream]$Stream
    )

    $Hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Hash = $Hasher.ComputeHash($Stream)
        return ([System.BitConverter]::ToString($Hash) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $Hasher.Dispose()
    }
}

function Get-FileRecord {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $Path = Join-Path $Root $Name
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing release artifact file: $Name"
    }
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Release artifact file must not be a reparse point: $Name"
    }
    $Stream = [System.IO.File]::OpenRead($Path)
    try {
        $Digest = Get-Sha256Hex -Stream $Stream
    }
    finally {
        $Stream.Dispose()
    }
    return [ordered]@{
        name = $Name
        size = [long]$Item.Length
        sha256 = $Digest
    }
}

function Get-PortableEntryRecords {
    $PortablePath = Join-Path $Root $PortableName
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [System.IO.Compression.ZipFile]::OpenRead($PortablePath)
    try {
        $Records = @()
        foreach ($EntryName in $RequiredPortableEntries) {
            $Matches = @($Archive.Entries | Where-Object { $_.FullName -ceq $EntryName })
            if ($Matches.Count -ne 1) {
                throw "Portable archive must contain exactly one entry named $EntryName."
            }
            $Entry = $Matches[0]
            $Stream = $Entry.Open()
            try {
                $Records += [ordered]@{
                    name = $EntryName
                    size = [long]$Entry.Length
                    sha256 = Get-Sha256Hex -Stream $Stream
                }
            }
            finally {
                $Stream.Dispose()
            }
        }
        return @($Records)
    }
    finally {
        $Archive.Dispose()
    }
}

function Assert-ExactRootFiles {
    $Expected = @($ExpectedFileNames + $ManifestName | Sort-Object)
    $Items = @(Get-ChildItem -LiteralPath $Root -Force)
    foreach ($Item in $Items) {
        if ($Item.PSIsContainer) {
            throw "Release artifact root contains an unexpected directory: $($Item.Name)"
        }
        if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Release artifact root contains a reparse point: $($Item.Name)"
        }
    }
    $Actual = @($Items.Name | Sort-Object)
    $Difference = @(Compare-Object -ReferenceObject $Expected -DifferenceObject $Actual -CaseSensitive)
    if ($Difference.Count -gt 0) {
        $Summary = $Difference | ForEach-Object { "$($_.SideIndicator)$($_.InputObject)" }
        throw "Release artifact root has the wrong exact file set: $($Summary -join ', ')"
    }
}

function Assert-RecordMatches {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Expected,
        [Parameter(Mandatory = $true)]
        [object]$Actual,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if ($null -eq $Expected.name -or $null -eq $Expected.sha256 -or $null -eq $Expected.size) {
        throw "$Description manifest record is missing name, size, or SHA-256."
    }
    if ([string]$Expected.size -notmatch '^(0|[1-9][0-9]*)$') {
        throw "$Description manifest size is malformed: $($Expected.size)"
    }
    if ([string]$Expected.name -cne [string]$Actual.name) {
        throw "$Description name mismatch: expected $($Expected.name), got $($Actual.name)."
    }
    if ([string]$Expected.sha256 -notmatch '^[0-9a-f]{64}$') {
        throw "$Description manifest SHA-256 is malformed: $($Expected.sha256)"
    }
    if ([string]$Expected.sha256 -cne [string]$Actual.sha256) {
        throw "$Description SHA-256 mismatch for $($Actual.name)."
    }
    if ([long]$Expected.size -ne [long]$Actual.size) {
        throw "$Description size mismatch for $($Actual.name)."
    }
}

if ($Mode -eq "Create") {
    if (Test-Path -LiteralPath $ManifestPath) {
        throw "Refusing to overwrite an existing release artifact manifest: $ManifestName"
    }
    $Files = @($ExpectedFileNames | ForEach-Object { Get-FileRecord -Name $_ })
    $PortableEntries = @(Get-PortableEntryRecords)
    $Manifest = [ordered]@{
        schemaVersion = 1
        repository = $Repository
        purpose = $Purpose
        tag = $Tag
        commit = $Commit
        pairedAddonTag = $PairedAddonTag
        pairedAddonCommit = $AddonCommit
        workflowRunId = $RunId
        workflowRunAttempt = $WorkflowRunAttempt
        files = $Files
        portableEntries = $PortableEntries
    }
    $Json = $Manifest | ConvertTo-Json -Depth 6
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($ManifestPath, $Json + "`n", $Utf8NoBom)
    Assert-ExactRootFiles
    Write-Host "Created $Purpose release artifact manifest: $ManifestPath"
    exit 0
}

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Missing release artifact manifest: $ManifestName"
}
Assert-ExactRootFiles
try {
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw "Release artifact manifest is malformed JSON: $($_.Exception.Message)"
}
if ([int]$Manifest.schemaVersion -ne 1) {
    throw "Unsupported release artifact manifest schema: $($Manifest.schemaVersion)"
}
if ([string]$Manifest.repository -cne $Repository) {
    throw "Release artifact manifest repository mismatch: $($Manifest.repository)"
}
if ([string]$Manifest.purpose -cne $Purpose) {
    throw "Release artifact manifest purpose mismatch: $($Manifest.purpose)"
}
if ([string]$Manifest.tag -cne $Tag) {
    throw "Release artifact manifest tag mismatch: expected $Tag, got $($Manifest.tag)."
}
if ([string]$Manifest.commit -cne $Commit) {
    throw "Release artifact manifest commit mismatch: expected $Commit, got $($Manifest.commit)."
}
if ([string]$Manifest.pairedAddonTag -cne $PairedAddonTag) {
    throw "Release artifact manifest paired addon tag mismatch: expected $PairedAddonTag, got $($Manifest.pairedAddonTag)."
}
if ([string]$Manifest.pairedAddonCommit -cne $AddonCommit) {
    throw "Release artifact manifest paired addon commit mismatch: expected $AddonCommit, got $($Manifest.pairedAddonCommit)."
}
if ([string]$Manifest.workflowRunId -cne $RunId) {
    throw "Release artifact manifest workflow run mismatch: expected $RunId, got $($Manifest.workflowRunId)."
}
if ([int]$Manifest.workflowRunAttempt -ne $WorkflowRunAttempt) {
    throw "Release artifact manifest workflow attempt mismatch: expected $WorkflowRunAttempt, got $($Manifest.workflowRunAttempt)."
}

$ManifestFiles = @($Manifest.files)
if ($ManifestFiles.Count -ne $ExpectedFileNames.Count) {
    throw "Release artifact manifest has the wrong file count."
}
for ($Index = 0; $Index -lt $ExpectedFileNames.Count; $Index++) {
    $ExpectedName = $ExpectedFileNames[$Index]
    $Declared = $ManifestFiles[$Index]
    if ([string]$Declared.name -cne $ExpectedName) {
        throw "Release artifact manifest file set mismatch at ${Index}: expected $ExpectedName."
    }
    $Actual = Get-FileRecord -Name $ExpectedName
    Assert-RecordMatches -Expected $Declared -Actual $Actual -Description "Release artifact"
}

$ManifestPortableEntries = @($Manifest.portableEntries)
$ActualPortableEntries = @(Get-PortableEntryRecords)
if ($ManifestPortableEntries.Count -ne $RequiredPortableEntries.Count) {
    throw "Release artifact manifest has the wrong portable entry count."
}
for ($Index = 0; $Index -lt $RequiredPortableEntries.Count; $Index++) {
    if ([string]$ManifestPortableEntries[$Index].name -cne $RequiredPortableEntries[$Index]) {
        throw "Release artifact manifest portable entry set mismatch at $Index."
    }
    Assert-RecordMatches `
        -Expected $ManifestPortableEntries[$Index] `
        -Actual $ActualPortableEntries[$Index] `
        -Description "Portable entry"
}

Write-Host "Verified $Purpose release artifact manifest for $Tag at $Commit."
