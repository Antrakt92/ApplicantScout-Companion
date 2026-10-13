param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^season-[a-z0-9]+-[0-9]+$')]
    [string]$Season,
    [Parameter(Mandatory = $true)]
    [string]$SeasonName,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$Target = Join-Path $RepoRoot 'src/applicant_scout/rio_score_tiers.py'
$Source = "https://raider.io/api/v1/mythic-plus/score-tiers?season=$Season"
$Response = Invoke-RestMethod -Uri $Source -TimeoutSec 30
$Rows = @($Response)
if ($Rows.Count -lt 10 -or $Rows.Count -gt 1000) {
    throw 'Unexpected seasonal palette size.'
}
$Seen = [System.Collections.Generic.HashSet[int]]::new()
foreach ($Row in $Rows) {
    if (($Row.score -isnot [int] -and $Row.score -isnot [long]) -or $Row.score -lt 0 -or
        $Row.rgbHex -isnot [string] -or $Row.rgbHex -cnotmatch '^#[0-9a-f]{6}$' -or
        -not $Seen.Add($Row.score)) {
        throw 'Invalid or duplicate palette band.'
    }
}
if ($SeasonName -notmatch '^[A-Za-z0-9 -]+$') {
    throw 'Season name must be a plain English label.'
}
$SortedBands = @($Rows | Sort-Object score -Descending | ForEach-Object {
    "    ($($_.score), `"$($_.rgbHex)`"),"
})
$Lines = @(
    '"""Reviewed Raider.IO season palette, refreshed by the seasonal helper."""',
    '',
    "RIO_SCORE_SEASON = `"$Season`"",
    "RIO_SCORE_SEASON_NAME = `"$SeasonName`"",
    "RIO_SCORE_SOURCE = `"$Source`"",
    "RIO_SCORE_REVIEWED_DATE = `"$([DateTime]::UtcNow.ToString('yyyy-MM-dd'))`"",
    'RIO_SCORE_BUCKETS: list[tuple[int, str]] = ['
) + $SortedBands + @(']', '')
if ($Check) {
    $Existing = Get-Content -LiteralPath $Target
    $ActualBands = @($Existing | Where-Object { $_ -match '^    \([0-9]+, "#[0-9a-f]{6}"\),$' })
    if ($Existing -notcontains "RIO_SCORE_SEASON = `"$Season`"" -or
        $Existing -notcontains "RIO_SCORE_SEASON_NAME = `"$SeasonName`"" -or
        $Existing -notcontains "RIO_SCORE_SOURCE = `"$Source`"" -or
        $ActualBands.Count -ne $SortedBands.Count -or
        -not [string]::Equals(($ActualBands -join "`n"), ($SortedBands -join "`n"),
            [System.StringComparison]::Ordinal)) {
        throw 'Bundled palette differs from the requested provider season.'
    }
    Write-Output "Season palette matches: $Season ($($Rows.Count) bands)."
    return
}
[System.IO.File]::WriteAllText($Target, ($Lines -join "`n"), [System.Text.UTF8Encoding]::new($false))
Write-Output "Refreshed $Season ($($Rows.Count) bands). Review the diff before publication."
