param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath
)

$ErrorActionPreference = "Stop"

if ($env:GITHUB_ACTIONS -ne "true") {
    throw "Installer upgrade smoke is restricted to an ephemeral GitHub Actions runner."
}

$Installer = [System.IO.Path]::GetFullPath($InstallerPath)
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
    throw "Missing installer for upgrade smoke: $Installer"
}

$ExpectedRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "Programs\ApplicantScout Companion")
).TrimEnd('\')
$ProgramsRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "Programs")
).TrimEnd('\')
if (-not $ExpectedRoot.StartsWith("$ProgramsRoot\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing unsafe installer smoke target: $ExpectedRoot"
}
if (Test-Path -LiteralPath $ExpectedRoot) {
    throw "Installer smoke target already exists on the ephemeral runner: $ExpectedRoot"
}

$ConfigRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "applicant-scout")
).TrimEnd('\')
if (Test-Path -LiteralPath $ConfigRoot) {
    throw "Installer smoke config sentinel root already exists: $ConfigRoot"
}

function Invoke-InstallerSmoke {
    param(
        [switch]$ExpectFailure,
        [switch]$PostPromotionFailure,
        [switch]$FinalizationFailure,
        [switch]$PendingRenameFailure
    )

    $Arguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS")
    if ($PostPromotionFailure) {
        $ExpectFailure = $true
        $Arguments += "/APSCOUT_TEST_FAIL_POST_PROMOTION=1"
    }
    elseif ($FinalizationFailure) {
        $ExpectFailure = $true
        $Arguments += "/APSCOUT_TEST_FAIL_FINALIZATION=1"
    }
    elseif ($PendingRenameFailure) {
        $ExpectFailure = $true
    }
    elseif ($ExpectFailure) {
        $Arguments += "/APSCOUT_TEST_FAIL_PROMOTION=1"
    }
    $Process = Start-Process `
        -FilePath $Installer `
        -ArgumentList $Arguments `
        -PassThru `
        -Wait `
        -WindowStyle Hidden
    if ($ExpectFailure -and $Process.ExitCode -eq 0) {
        throw "Installer failure smoke unexpectedly succeeded."
    }
    if (-not $ExpectFailure -and $Process.ExitCode -ne 0) {
        throw "Installer upgrade smoke failed with exit code $($Process.ExitCode)."
    }
}

function Assert-InstalledStartupProbe {
    $InstalledExe = Join-Path $ExpectedRoot "current\ApplicantScout.exe"
    $Probe = Start-Process `
        -FilePath $InstalledExe `
        -ArgumentList "--startup-import-probe" `
        -PassThru `
        -Wait `
        -WindowStyle Hidden
    if ($Probe.ExitCode -ne 0) {
        throw "Installed companion startup probe failed with exit code $($Probe.ExitCode)."
    }
}

function Assert-PathMissing {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path -LiteralPath $Path) {
        throw "Stale installer payload survived upgrade: $Path"
    }
}

function Invoke-PendingRenameGuardSmoke {
    $SessionManagerPath = "SYSTEM\CurrentControlSet\Control\Session Manager"
    $ValueName = "PendingFileRenameOperations"
    $BaseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        [Microsoft.Win32.RegistryView]::Registry64
    )
    $SessionManager = $null
    $HadValue = $false
    [string[]]$OriginalValue = @()
    $InjectionWritten = $false
    try {
        $SessionManager = $BaseKey.OpenSubKey($SessionManagerPath, $true)
        if ($null -eq $SessionManager) {
            throw "Could not open Session Manager registry key for pending-rename smoke."
        }
        $HadValue = $SessionManager.GetValueNames() -contains $ValueName
        if ($HadValue) {
            if ($SessionManager.GetValueKind($ValueName) -ne [Microsoft.Win32.RegistryValueKind]::MultiString) {
                throw "PendingFileRenameOperations was not REG_MULTI_SZ."
            }
            $OriginalValue = [string[]]$SessionManager.GetValue(
                $ValueName,
                [string[]]@(),
                [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
            )
        }
        $PendingTarget = Join-Path $ExpectedRoot "current\ApplicantScout.exe"
        [string[]]$InjectedPair = @("\??\$PendingTarget", "")
        [string[]]$InjectedValue = @($OriginalValue) + @($InjectedPair)
        $SessionManager.SetValue(
            $ValueName,
            $InjectedValue,
            [Microsoft.Win32.RegistryValueKind]::MultiString
        )
        $InjectionWritten = $true
        Invoke-InstallerSmoke -PendingRenameFailure
    }
    finally {
        try {
            if ($null -ne $SessionManager -and $InjectionWritten) {
                if (-not ($SessionManager.GetValueNames() -contains $ValueName) -or
                    $SessionManager.GetValueKind($ValueName) -ne [Microsoft.Win32.RegistryValueKind]::MultiString) {
                    throw "PendingFileRenameOperations changed type or disappeared during smoke cleanup."
                }
                [string[]]$CurrentValue = $SessionManager.GetValue(
                    $ValueName,
                    [string[]]@(),
                    [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
                )
                $OriginalCount = $OriginalValue.Count
                $InjectedCount = $InjectedPair.Count
                if ($CurrentValue.Count -lt ($OriginalCount + $InjectedCount)) {
                    throw "PendingFileRenameOperations was truncated during smoke cleanup."
                }
                for ($Index = 0; $Index -lt $OriginalCount; $Index++) {
                    if (-not [string]::Equals(
                        $CurrentValue[$Index],
                        $OriginalValue[$Index],
                        [System.StringComparison]::Ordinal
                    )) {
                        throw "Refusing to overwrite concurrent pending-rename changes."
                    }
                }
                for ($Index = 0; $Index -lt $InjectedCount; $Index++) {
                    if (-not [string]::Equals(
                        $CurrentValue[$OriginalCount + $Index],
                        $InjectedPair[$Index],
                        [System.StringComparison]::Ordinal
                    )) {
                        throw "Injected pending-rename guard changed before smoke cleanup."
                    }
                }
                [string[]]$ConcurrentSuffix = @()
                $SuffixStart = $OriginalCount + $InjectedCount
                if ($CurrentValue.Count -gt $SuffixStart) {
                    $ConcurrentSuffix = [string[]]@(
                        $CurrentValue[$SuffixStart..($CurrentValue.Count - 1)]
                    )
                }
                [string[]]$RestoredValue = @($OriginalValue) + @($ConcurrentSuffix)
                if ($HadValue -or $RestoredValue.Count -gt 0) {
                    $SessionManager.SetValue(
                        $ValueName,
                        $RestoredValue,
                        [Microsoft.Win32.RegistryValueKind]::MultiString
                    )
                }
                else {
                    $SessionManager.DeleteValue($ValueName, $false)
                }
            }
        }
        finally {
            if ($null -ne $SessionManager) {
                $SessionManager.Dispose()
            }
            $BaseKey.Dispose()
        }
    }
}

try {
    New-Item -ItemType Directory -Path (Join-Path $ExpectedRoot "_internal") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $ExpectedRoot "licenses") -Force | Out-Null
    New-Item -ItemType Directory -Path $ConfigRoot -Force | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $ExpectedRoot "ApplicantScout.exe"), "legacy")
    [System.IO.File]::WriteAllText((Join-Path $ExpectedRoot "_internal\obsolete.dll"), "stale")
    [System.IO.File]::WriteAllText((Join-Path $ExpectedRoot "licenses\obsolete.txt"), "stale")
    [System.IO.File]::WriteAllText((Join-Path $ExpectedRoot "user-marker.txt"), "preserve")
    [System.IO.File]::WriteAllText((Join-Path $ConfigRoot "config-marker.txt"), "preserve")

    Invoke-InstallerSmoke
    Assert-InstalledStartupProbe

    $Current = Join-Path $ExpectedRoot "current"
    foreach ($Required in @(
        (Join-Path $Current "ApplicantScout.exe"),
        (Join-Path $Current "_internal"),
        (Join-Path $Current ".apscout-payload-version"),
        (Join-Path $ExpectedRoot "user-marker.txt"),
        (Join-Path $ConfigRoot "config-marker.txt")
    )) {
        if (-not (Test-Path -LiteralPath $Required)) {
            throw "Installer upgrade smoke lost required path: $Required"
        }
    }
    Assert-PathMissing -Path (Join-Path $ExpectedRoot "ApplicantScout.exe")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot "_internal")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot "licenses")

    Invoke-PendingRenameGuardSmoke
    Assert-InstalledStartupProbe
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-backup")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-next")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-promotion-pending")

    [System.IO.File]::WriteAllText((Join-Path $Current "_internal\obsolete-second.dll"), "stale")
    [System.IO.File]::WriteAllText((Join-Path $Current "licenses\obsolete-second.txt"), "stale")
    Invoke-InstallerSmoke -ExpectFailure
    Assert-InstalledStartupProbe
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-backup")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-next")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-promotion-pending")
    if (-not (Test-Path -LiteralPath (Join-Path $Current "_internal\obsolete-second.dll"))) {
        throw "Failed upgrade did not preserve the previous working payload."
    }

    Invoke-InstallerSmoke -PostPromotionFailure
    Assert-InstalledStartupProbe
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-backup")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-next")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-promotion-pending")
    if (-not (Test-Path -LiteralPath (Join-Path $Current "_internal\obsolete-second.dll"))) {
        throw "Post-promotion rollback did not preserve the previous working payload."
    }

    Invoke-InstallerSmoke -FinalizationFailure
    Assert-InstalledStartupProbe
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-backup")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-next")
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-promotion-pending")
    if (-not (Test-Path -LiteralPath (Join-Path $Current "_internal\obsolete-second.dll"))) {
        throw "Finalization rollback did not preserve the previous working payload."
    }

    Invoke-InstallerSmoke
    Assert-InstalledStartupProbe
    Assert-PathMissing -Path (Join-Path $Current "_internal\obsolete-second.dll")
    Assert-PathMissing -Path (Join-Path $Current "licenses\obsolete-second.txt")

    $Backup = Join-Path $ExpectedRoot ".apscout-backup"
    $Next = Join-Path $ExpectedRoot ".apscout-next"
    Move-Item -LiteralPath $Current -Destination $Backup
    New-Item -ItemType Directory -Path $Current | Out-Null
    New-Item -ItemType Directory -Path $Next | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $Current "partial.txt"), "partial")
    [System.IO.File]::WriteAllText((Join-Path $Next "partial.txt"), "partial")
    Invoke-InstallerSmoke
    Assert-InstalledStartupProbe
    if (-not (Test-Path -LiteralPath (Join-Path $Current "ApplicantScout.exe"))) {
        throw "Interrupted payload swap recovery did not restore a working current payload."
    }
    Assert-PathMissing -Path $Backup
    Assert-PathMissing -Path $Next
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-promotion-pending")

    # Simulate a process loss after next was promoted but before Inno finalized
    # its install metadata. The durable marker must force restoration of backup.
    Move-Item -LiteralPath $Current -Destination $Backup
    Copy-Item -LiteralPath $Backup -Destination $Current -Recurse
    New-Item -ItemType Directory -Path $Next | Out-Null
    [System.IO.File]::WriteAllText(
        (Join-Path $ExpectedRoot ".apscout-promotion-pending"),
        "upgrade"
    )
    [System.IO.File]::WriteAllText((Join-Path $Next "partial.txt"), "partial")
    Invoke-InstallerSmoke
    Assert-InstalledStartupProbe
    Assert-PathMissing -Path $Backup
    Assert-PathMissing -Path $Next
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-promotion-pending")

    $Uninstaller = Join-Path $ExpectedRoot "unins000.exe"
    if (-not (Test-Path -LiteralPath $Uninstaller -PathType Leaf)) {
        throw "Installer smoke did not create the expected uninstaller."
    }
    $ReparseTarget = Join-Path $ConfigRoot "uninstall-redirection-target"
    $ReparseSentinel = Join-Path $ReparseTarget "must-survive.txt"
    $ReparsePath = Join-Path $Current "_internal\uninstall-redirection-probe"
    New-Item -ItemType Directory -Path $ReparseTarget | Out-Null
    [System.IO.File]::WriteAllText($ReparseSentinel, "preserve")
    New-Item -ItemType Junction -Path $ReparsePath -Target $ReparseTarget | Out-Null
    try {
        $BlockedUninstall = Start-Process `
            -FilePath $Uninstaller `
            -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
            -PassThru `
            -Wait `
            -WindowStyle Hidden
        if ($BlockedUninstall.ExitCode -eq 0) {
            throw "Uninstall unexpectedly accepted a reparse point inside its payload."
        }
        if (-not (Test-Path -LiteralPath $ReparseSentinel -PathType Leaf)) {
            throw "Blocked uninstall followed a payload reparse point into user data."
        }
        if (-not (Test-Path -LiteralPath $Current -PathType Container)) {
            throw "Blocked uninstall mutated the working payload."
        }
    }
    finally {
        if (Test-Path -LiteralPath $ReparsePath) {
            Remove-Item -LiteralPath $ReparsePath -Force
        }
    }
    $Uninstall = Start-Process `
        -FilePath $Uninstaller `
        -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
        -PassThru `
        -Wait `
        -WindowStyle Hidden
    if ($Uninstall.ExitCode -ne 0) {
        throw "Installer smoke uninstall failed with exit code $($Uninstall.ExitCode)."
    }
    Assert-PathMissing -Path $Current
    Assert-PathMissing -Path $Backup
    Assert-PathMissing -Path $Next
    Assert-PathMissing -Path (Join-Path $ExpectedRoot ".apscout-promotion-pending")
    if (-not (Test-Path -LiteralPath (Join-Path $ConfigRoot "config-marker.txt"))) {
        throw "Uninstall removed the companion user-data sentinel."
    }
}
finally {
    if (Test-Path -LiteralPath $ExpectedRoot) {
        $ResolvedRoot = [System.IO.Path]::GetFullPath($ExpectedRoot).TrimEnd('\')
        if (-not [string]::Equals($ResolvedRoot, $ExpectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing cleanup of unexpected installer smoke path: $ResolvedRoot"
        }
        Remove-Item -LiteralPath $ResolvedRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $ConfigRoot) {
        Remove-Item -LiteralPath $ConfigRoot -Recurse -Force
    }
}
