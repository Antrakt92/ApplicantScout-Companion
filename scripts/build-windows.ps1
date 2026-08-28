param(
    [switch]$SkipChecks,
    [switch]$SkipInstaller,
    [switch]$SkipPortable,
    [switch]$AllowDirtyReleaseInputs,
    [switch]$RequireSigning
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$PyInstaller = Join-Path $RepoRoot ".venv\Scripts\pyinstaller.exe"
$EntryPoint = Join-Path $RepoRoot "packaging\pyinstaller\run_applicant_scout.py"
$InnoScript = Join-Path $RepoRoot "packaging\inno\ApplicantScoutCompanion.iss"
$AppIcon = Join-Path $RepoRoot "src\applicant_scout\assets\app_icon.ico"
$InstallerSigner = Join-Path $RepoRoot "scripts\sign-windows-installer.ps1"
. (Join-Path $PSScriptRoot "native-command.ps1")

function Copy-ReleaseTextArtifacts {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetDir
    )

    foreach ($Name in @("LICENSE", "THIRD-PARTY-NOTICES.md", "RELEASE_NOTES.md")) {
        $Source = Join-Path $RepoRoot $Name
        if (-not (Test-Path -LiteralPath $Source)) {
            throw "Missing release text artifact: $Source"
        }
        Copy-Item -LiteralPath $Source -Destination (Join-Path $TargetDir $Name) -Force
    }
}

function Copy-DependencyLicenseArtifacts {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetDir,
        [Parameter(Mandatory = $true)]
        [string]$BasePrefix
    )

    $LicenseDir = Join-Path $TargetDir "licenses"
    $Constraints = Join-Path $RepoRoot "constraints-release.txt"
    $Pyproject = Join-Path $RepoRoot "pyproject.toml"
    $LicenseOverrides = Join-Path $RepoRoot "packaging\dependency-license-overrides.toml"
    $Collector = Join-Path $RepoRoot "scripts\collect_dependency_licenses.py"
    if (-not (Test-Path -LiteralPath $Collector)) {
        throw "Missing dependency license collector: $Collector"
    }
    New-Item -ItemType Directory -Path $LicenseDir -Force | Out-Null
    Invoke-NativeChecked -Label "Collect dependency license files" -Command {
        & $Python $Collector `
            --constraints $Constraints `
            --pyproject $Pyproject `
            --runtime-license-set `
            --module-toc (Join-Path $RepoRoot "build\ApplicantScout\PYZ-00.toc") `
            --overrides $LicenseOverrides `
            --dest $LicenseDir
    }

    $PythonLicense = Join-Path $BasePrefix "LICENSE.txt"
    if (-not (Test-Path -LiteralPath $PythonLicense -PathType Leaf) -or
        (Get-Item -LiteralPath $PythonLicense).Length -le 0) {
        throw "Missing non-empty CPython license file: $PythonLicense"
    }
    $PythonLicenseText = Get-Content -LiteralPath $PythonLicense -Raw
    if ($PythonLicenseText -notmatch 'PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2') {
        throw "CPython license evidence did not contain the expected PSF license text: $PythonLicense"
    }
    $PythonLicenseDir = Join-Path $LicenseDir "CPython"
    New-Item -ItemType Directory -Path $PythonLicenseDir -Force | Out-Null
    Copy-Item -LiteralPath $PythonLicense `
        -Destination (Join-Path $PythonLicenseDir "LICENSE.txt") `
        -Force
}

function Write-PayloadVersionMarker {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetDir,
        [Parameter(Mandatory = $true)]
        [string]$VersionText
    )

    $Marker = Join-Path $TargetDir ".apscout-payload-version"
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Marker, "$VersionText`n", $Utf8NoBom)
}

function Assert-ReleaseConstraints {
    $Constraints = Join-Path $RepoRoot "constraints-release.txt"
    $Pyproject = Join-Path $RepoRoot "pyproject.toml"
    $LicenseCollector = Join-Path $RepoRoot "scripts\collect_dependency_licenses.py"
    if (-not (Test-Path -LiteralPath $Constraints)) {
        throw "Missing release constraints file: $Constraints"
    }
    if (-not (Test-Path -LiteralPath $Pyproject)) {
        throw "Missing pyproject file: $Pyproject"
    }
    if (-not (Test-Path -LiteralPath $LicenseCollector)) {
        throw "Missing dependency license collector: $LicenseCollector"
    }

    $PreviousConstraintsFile = $env:APSCOUT_CONSTRAINTS_FILE
    $PreviousPyprojectFile = $env:APSCOUT_PYPROJECT_FILE
    $PreviousLicenseCollector = $env:APSCOUT_LICENSE_COLLECTOR
    $env:APSCOUT_CONSTRAINTS_FILE = $Constraints
    $env:APSCOUT_PYPROJECT_FILE = $Pyproject
    $env:APSCOUT_LICENSE_COLLECTOR = $LicenseCollector
    try {
        $PythonCode = @'
from importlib import metadata
import os
import re
import runpy
import sys
from pathlib import Path

collector = runpy.run_path(os.environ["APSCOUT_LICENSE_COLLECTOR"])
missing_pyproject_constraints = collector["missing_pyproject_constraints"]

constraints = Path(os.environ["APSCOUT_CONSTRAINTS_FILE"])
pyproject = Path(os.environ["APSCOUT_PYPROJECT_FILE"])
missing = []
mismatched = []
malformed = []
for raw in constraints.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)==(.+)", line)
    if match is None:
        malformed.append(line)
        continue
    name, expected = match.groups()
    try:
        actual = metadata.version(name)
    except metadata.PackageNotFoundError:
        missing.append(name)
        continue
    if actual != expected:
        mismatched.append(f"{name}: installed {actual}, expected {expected}")

unconstrained_pyproject = []
try:
    unconstrained_pyproject = missing_pyproject_constraints(pyproject, constraints)
except ValueError as exc:
    malformed.append(str(exc))

if malformed or missing or mismatched or unconstrained_pyproject:
    for item in malformed:
        print(f"Malformed release constraint: {item}", file=sys.stderr)
    for item in missing:
        print(f"missing package: {item}", file=sys.stderr)
    for item in mismatched:
        print(item, file=sys.stderr)
    for item in unconstrained_pyproject:
        print(
            f"missing release constraint for pyproject dependency: {item}",
            file=sys.stderr,
        )
    sys.exit(1)
'@
        Invoke-NativeChecked -Label "Validate release constraints" -Command {
            & $Python -c $PythonCode
        }
    }
    finally {
        $env:APSCOUT_CONSTRAINTS_FILE = $PreviousConstraintsFile
        $env:APSCOUT_PYPROJECT_FILE = $PreviousPyprojectFile
        $env:APSCOUT_LICENSE_COLLECTOR = $PreviousLicenseCollector
    }
}

function Assert-CleanReleaseInputs {
    $ReleaseInputPaths = @(
        "pyproject.toml",
        "constraints-release.txt",
        "LICENSE",
        "THIRD-PARTY-NOTICES.md",
        "RELEASE_NOTES.md",
        "src",
        "packaging",
        "scripts\build-windows.ps1",
        "scripts\check.ps1",
        "scripts\check-release-version.ps1",
        "scripts\native-command.ps1",
        "scripts\sign-windows-installer.ps1",
        "scripts\smoke-installer-upgrade.ps1",
        "scripts\verify_frozen_runtime.py",
        "scripts\collect_dependency_licenses.py",
        "scripts\export_public_visual_assets.py",
        "scripts\overlay_visual_fixture.py",
        "scripts\render_overlay_fixture.py",
        "scripts\settings_dialog_visual_fixture.py",
        "scripts\render_settings_dialog_fixture.py",
        "scripts\visual_fixture_checks.py",
        "docs\visual"
    )

    $Git = Get-Command "git" -ErrorAction SilentlyContinue
    if ($null -eq $Git) {
        throw "Cannot verify release input cleanliness because git is not available."
    }

    $Dirty = Invoke-NativeChecked -Label "Inspect release input cleanliness" -Command {
        & $Git.Source -C $RepoRoot status --porcelain --untracked-files=all -- $ReleaseInputPaths
    }
    if ($Dirty) {
        $Joined = ($Dirty -join [Environment]::NewLine)
        throw (
            "Refusing to build release artifacts from dirty release inputs. " +
            "Commit or revert these paths first, or rerun with -AllowDirtyReleaseInputs for a local smoke build:" +
            [Environment]::NewLine + $Joined
        )
    }
}

function Get-VenvBasePrefix {
    $VenvConfig = Join-Path $RepoRoot ".venv\pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $VenvConfig -PathType Leaf)) {
        throw "Missing virtual environment configuration: $VenvConfig"
    }
    $VenvConfigLines = Get-Content -LiteralPath $VenvConfig
    $SystemSitePackagesLine = $VenvConfigLines | Where-Object {
        $_ -match '^\s*include-system-site-packages\s*='
    } | Select-Object -First 1
    if ($null -eq $SystemSitePackagesLine -or
        $SystemSitePackagesLine -notmatch '^\s*include-system-site-packages\s*=\s*false\s*$') {
        throw (
            "Release builds require include-system-site-packages = false in " +
            "$VenvConfig."
        )
    }
    $HomeLine = $VenvConfigLines | Where-Object {
        $_ -match '^\s*home\s*='
    } | Select-Object -First 1
    $BasePrefix = ""
    if ($null -ne $HomeLine -and $HomeLine -match '^\s*home\s*=\s*(?<home>.+?)\s*$') {
        $BasePrefix = $Matches.home
    }
    if (-not $BasePrefix -or -not (Test-Path -LiteralPath $BasePrefix -PathType Container)) {
        throw "Could not resolve base Python from $VenvConfig."
    }
    return [System.IO.Path]::GetFullPath($BasePrefix).TrimEnd('\')
}

function Get-IsolatedPyInstallerPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BasePrefix
    )

    $Candidates = @(
        (Split-Path -Parent $Python),
        $BasePrefix,
        (Join-Path $BasePrefix "DLLs"),
        (Join-Path $RepoRoot ".venv\Lib\site-packages\PyQt6\Qt6\bin"),
        (Join-Path $env:SystemRoot "System32"),
        $env:SystemRoot,
        (Join-Path $env:SystemRoot "System32\Wbem")
    )
    $Seen = @{}
    $SafeEntries = foreach ($Candidate in $Candidates) {
        if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Container)) {
            continue
        }
        $FullPath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
        $Key = $FullPath.ToLowerInvariant()
        if (-not $Seen.ContainsKey($Key)) {
            $Seen[$Key] = $true
            $FullPath
        }
    }
    return $SafeEntries -join [System.IO.Path]::PathSeparator
}

function Invoke-WithIsolatedBuildEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BasePrefix,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action
    )

    $Names = @(
        "PATH",
        "PYTHONHOME",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "PYTHONSAFEPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYINSTALLER_CONFIG_DIR",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QML_IMPORT_PATH",
        "QML2_IMPORT_PATH",
        "UPX",
        "UPX_DIR"
    )
    $Names += @(Get-ChildItem Env: | Where-Object Name -Like "_PYI_*" | Select-Object -ExpandProperty Name)
    $Names = @($Names | Sort-Object -Unique)
    $Saved = @{}
    foreach ($Name in $Names) {
        $Saved[$Name] = @{
            Exists = Test-Path -LiteralPath "Env:$Name"
            Value = [Environment]::GetEnvironmentVariable($Name, "Process")
        }
    }
    $BuildTemp = $null
    $PreviousLocation = $null
    try {
        foreach ($Name in $Names) {
            [Environment]::SetEnvironmentVariable($Name, $null, "Process")
        }
        $TempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
        $BuildTemp = [System.IO.Path]::GetFullPath(
            (Join-Path $TempRoot ("ApplicantScout-build-" + [guid]::NewGuid().ToString("N")))
        )
        if (-not $BuildTemp.StartsWith("$TempRoot\", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing unsafe isolated build directory: $BuildTemp"
        }
        New-Item -ItemType Directory -Path $BuildTemp | Out-Null
        $PreviousLocation = Get-Location
        # WHY: native discovery uses PATH, process environment, and the current
        # directory. Keep every artifact-affecting probe inside one clean scope.
        $env:PATH = Get-IsolatedPyInstallerPath -BasePrefix $BasePrefix
        $env:PYTHONNOUSERSITE = "1"
        $env:PYTHONSAFEPATH = "1"
        $env:PYTHONDONTWRITEBYTECODE = "1"
        $env:PYINSTALLER_CONFIG_DIR = Join-Path $BuildTemp "pyinstaller-config"
        Set-Location -LiteralPath $BuildTemp
        & $Action
    }
    finally {
        $CleanupFailures = [System.Collections.Generic.List[string]]::new()
        foreach ($Name in $Names) {
            try {
                if ($Saved[$Name].Exists) {
                    [Environment]::SetEnvironmentVariable($Name, $Saved[$Name].Value, "Process")
                }
                else {
                    [Environment]::SetEnvironmentVariable($Name, $null, "Process")
                }
            }
            catch {
                $CleanupFailures.Add("restore environment variable $Name`: $($_.Exception.Message)")
            }
        }
        if ($null -ne $PreviousLocation) {
            try {
                Set-Location -LiteralPath $PreviousLocation.Path
            }
            catch {
                $CleanupFailures.Add("restore working directory: $($_.Exception.Message)")
            }
        }
        if ($BuildTemp -and (Test-Path -LiteralPath $BuildTemp)) {
            try {
                Remove-Item -LiteralPath $BuildTemp -Recurse -Force
            }
            catch {
                $CleanupFailures.Add("remove isolated build directory: $($_.Exception.Message)")
            }
        }
        if ($CleanupFailures.Count -gt 0) {
            throw "Isolated build cleanup failed: $($CleanupFailures -join '; ')"
        }
    }
}

function Invoke-PyInstaller {
    $PyzbarDir = Join-Path $RepoRoot ".venv\Lib\site-packages\pyzbar"
    $PyzbarIconv = Join-Path $PyzbarDir "libiconv.dll"
    $PyzbarZbar = Join-Path $PyzbarDir "libzbar-64.dll"
    foreach ($NativeDecoder in @($PyzbarIconv, $PyzbarZbar)) {
        if (-not (Test-Path -LiteralPath $NativeDecoder -PathType Leaf)) {
            throw "Missing pinned pyzbar native runtime: $NativeDecoder"
        }
    }
    Invoke-NativeChecked -Label "PyInstaller" -Command {
        & $PyInstaller `
            --noconfirm `
            --clean `
            --onedir `
            --windowed `
            --name ApplicantScout `
            --specpath (Join-Path $RepoRoot "build") `
            --workpath (Join-Path $RepoRoot "build") `
            --distpath (Join-Path $RepoRoot "dist") `
            --paths (Join-Path $RepoRoot "src") `
            --collect-data applicant_scout `
            --exclude-module _pytest `
            --exclude-module _pyinstaller_hooks_contrib `
            --exclude-module altgraph `
            --exclude-module build `
            --exclude-module colorama `
            --exclude-module iniconfig `
            --exclude-module nodeenv `
            --exclude-module numpy `
            --exclude-module packaging `
            --exclude-module pefile `
            --exclude-module pip `
            --exclude-module pluggy `
            --exclude-module pygments `
            --exclude-module PyInstaller `
            --exclude-module pyproject_hooks `
            --exclude-module pyright `
            --exclude-module pytest `
            --exclude-module pytestqt `
            --exclude-module ruff `
            --exclude-module setuptools `
            --exclude-module wheel `
            --exclude-module win32ctypes `
            --add-binary "$PyzbarIconv;pyzbar" `
            --add-binary "$PyzbarZbar;pyzbar" `
            --version-file $VersionInfoFile `
            --icon $AppIcon `
            $EntryPoint
    }
}

function Assert-FrozenRuntimeLayout {
    param(
        [Parameter(Mandatory = $true)]
        [string]$AppDir,
        [Parameter(Mandatory = $true)]
        [string]$BasePrefix,
        [switch]$PackagedLayout
    )

    $Verifier = Join-Path $RepoRoot "scripts\verify_frozen_runtime.py"
    if (-not (Test-Path -LiteralPath $Verifier -PathType Leaf)) {
        throw "Missing frozen runtime verifier: $Verifier"
    }
    Invoke-NativeChecked -Label "Verify frozen runtime provenance and architecture" -Command {
        $Arguments = @(
            $Verifier,
            "--repo-root", $RepoRoot,
            "--app-dir", $AppDir,
            "--work-dir", (Join-Path $RepoRoot "build"),
            "--base-prefix", $BasePrefix,
            "--windows-dir", $env:SystemRoot,
            "--producer-python", $Python
        )
        if ($PackagedLayout) {
            $Arguments += "--packaged-layout"
        }
        & $Python @Arguments
    }
}

function Assert-FrozenStartupImports {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Exe
    )

    $Probe = Start-Process `
        -FilePath $Exe `
        -ArgumentList "--startup-import-probe" `
        -PassThru `
        -WindowStyle Hidden
    if (-not $Probe.WaitForExit(15000)) {
        Stop-Process -Id $Probe.Id -Force -ErrorAction SilentlyContinue
        throw "Frozen startup import probe did not exit within 15 seconds."
    }
    if ($Probe.ExitCode -ne 0) {
        throw "Frozen startup import probe failed with exit code $($Probe.ExitCode)."
    }
}

function New-VersionInfoFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$VersionText,
        [Parameter(Mandatory = $true)]
        [string]$OutputPath
    )

    if ($VersionText -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
        throw "Version must be strict SemVer for Windows version resources: $VersionText"
    }
    $Major = [int]$Matches[1]
    $Minor = [int]$Matches[2]
    $Patch = [int]$Matches[3]
    $VersionInfoDir = Split-Path -Parent $OutputPath
    New-Item -ItemType Directory -Path $VersionInfoDir -Force | Out-Null
    @"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($Major, $Minor, $Patch, 0),
    prodvers=($Major, $Minor, $Patch, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Antrakt'),
          StringStruct('FileDescription', 'ApplicantScout Companion'),
          StringStruct('FileVersion', '$VersionText'),
          StringStruct('InternalName', 'ApplicantScout'),
          StringStruct('OriginalFilename', 'ApplicantScout.exe'),
          StringStruct('ProductName', 'ApplicantScout Companion'),
          StringStruct('ProductVersion', '$VersionText'),
          StringStruct('LegalCopyright', 'Copyright (c) 2026 Antrakt')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -LiteralPath $OutputPath -Encoding UTF8
}

function Get-InnoSetupRegistrations {
    $Subkey = "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"
    foreach ($Hive in @(
        [Microsoft.Win32.RegistryHive]::CurrentUser,
        [Microsoft.Win32.RegistryHive]::LocalMachine
    )) {
        foreach ($View in @(
            [Microsoft.Win32.RegistryView]::Registry64,
            [Microsoft.Win32.RegistryView]::Registry32
        )) {
            $BaseKey = $null
            $Key = $null
            try {
                $BaseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, $View)
                $Key = $BaseKey.OpenSubKey($Subkey, $false)
                if ($null -eq $Key) {
                    continue
                }
                [pscustomobject]@{
                    DisplayVersion = [string]$Key.GetValue("DisplayVersion", "")
                    InstallLocation = [string]$Key.GetValue("InstallLocation", "")
                }
            }
            finally {
                if ($null -ne $Key) {
                    $Key.Dispose()
                }
                if ($null -ne $BaseKey) {
                    $BaseKey.Dispose()
                }
            }
        }
    }
}

function Test-PathTreeHasReparsePoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LeafPath,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $Current = [System.IO.Path]::GetFullPath($LeafPath).TrimEnd('\')
    $Root = [System.IO.Path]::GetFullPath($TrustedRoot).TrimEnd('\')
    $Comparison = [System.StringComparison]::OrdinalIgnoreCase
    if (-not $Current.StartsWith($Root + '\', $Comparison) -and
        -not $Current.Equals($Root, $Comparison)) {
        return $true
    }
    while ($true) {
        $Item = Get-Item -LiteralPath $Current -Force -ErrorAction Stop
        if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $true
        }
        if ($Current.Equals($Root, $Comparison)) {
            return $false
        }
        $Parent = [System.IO.Directory]::GetParent($Current)
        if ($null -eq $Parent) {
            return $true
        }
        $Current = $Parent.FullName.TrimEnd('\')
    }
}

function Get-NormalizedUtf8SHA256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $Text = [System.IO.File]::ReadAllText($Path) -replace "`r`n?", "`n"
    $Bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($Text)
    return [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($Bytes))
}

function Find-InnoSetupCompiler {
    $ExpectedVersion = "6.7.1"
    # Pinned from Files/ISPPBuiltins.iss at the official is-6_7_1 source tag
    # (commit cfdf48923178df4b4f040e038b423aa555a61ffc), after normalizing newlines.
    # Unlike compiler DLLs, this preprocessor input is not covered by Inno's
    # own .issig verification and can change how the version gate is evaluated.
    $ExpectedBuiltinsSHA256 = "2557F3716610DED2D5ADF61BFE6FF872992E1EEFC1AA2D130452AAD2CD31A554"
    $KnownLocations = @(
        [pscustomobject]@{
            Root = [System.Environment]::GetFolderPath(
                [System.Environment+SpecialFolder]::LocalApplicationData
            )
            Relative = "Programs\Inno Setup 6"
        },
        [pscustomobject]@{
            Root = [System.Environment]::GetFolderPath(
                [System.Environment+SpecialFolder]::ProgramFilesX86
            )
            Relative = "Inno Setup 6"
        },
        [pscustomobject]@{
            Root = [System.Environment]::GetFolderPath(
                [System.Environment+SpecialFolder]::ProgramFiles
            )
            Relative = "Inno Setup 6"
        }
    ) | Where-Object { $_.Root }
    $Registrations = @(Get-InnoSetupRegistrations)

    foreach ($Location in $KnownLocations) {
        $KnownRoot = $Location.Root
        $InstallDir = Join-Path $KnownRoot $Location.Relative
        $Candidate = Join-Path $InstallDir "ISCC.exe"
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            continue
        }

        $CanonicalInstallDir = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
        $Registration = $Registrations | Where-Object {
            $_.DisplayVersion -eq $ExpectedVersion -and
            $_.InstallLocation -and
            [System.IO.Path]::GetFullPath($_.InstallLocation).TrimEnd('\').Equals(
                $CanonicalInstallDir,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        } | Select-Object -First 1
        if ($null -eq $Registration) {
            Write-Warning "Ignoring unregistered or wrong-version Inno Setup toolchain: $InstallDir"
            continue
        }

        $RequiredFiles = @(
            "ISCC.exe",
            "ISCmplr.dll",
            "ISCmplr.dll.issig",
            "ISPP.dll",
            "ISPP.dll.issig",
            "ISPPBuiltins.iss"
        )
        $InvalidTree = $false
        foreach ($Name in $RequiredFiles) {
            $RequiredPath = Join-Path $InstallDir $Name
            if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf) -or
                (Get-Item -LiteralPath $RequiredPath -Force).Length -le 0 -or
                (Test-PathTreeHasReparsePoint -LeafPath $RequiredPath -TrustedRoot $KnownRoot)) {
                $InvalidTree = $true
                break
            }
        }
        if ($InvalidTree) {
            Write-Warning "Ignoring incomplete or redirected Inno Setup toolchain: $InstallDir"
            continue
        }

        $Signature = Get-AuthenticodeSignature -LiteralPath $Candidate
        $Subject = if ($null -ne $Signature.SignerCertificate) {
            $Signature.SignerCertificate.Subject
        }
        else {
            ""
        }
        if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
            $Subject -notmatch '(?i)(^|, )O=Pyrsys B\.V\.(,|$)') {
            Write-Warning "Ignoring untrusted Inno Setup compiler: $Candidate"
            continue
        }

        $Builtins = Join-Path $InstallDir "ISPPBuiltins.iss"
        if ((Get-NormalizedUtf8SHA256 -Path $Builtins) -ne $ExpectedBuiltinsSHA256) {
            Write-Warning "Ignoring Inno Setup toolchain with unexpected ISPP builtins: $InstallDir"
            continue
        }

        return (Get-Item -LiteralPath $Candidate -Force).FullName
    }
    return $null
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing venv Python: $Python"
}
if (-not (Test-Path -LiteralPath $PyInstaller)) {
    throw "Missing PyInstaller: $PyInstaller"
}
if (-not (Test-Path -LiteralPath $EntryPoint)) {
    throw "Missing PyInstaller entrypoint: $EntryPoint"
}
if (-not (Test-Path -LiteralPath $InnoScript)) {
    throw "Missing Inno Setup script: $InnoScript"
}
if (-not (Test-Path -LiteralPath $AppIcon)) {
    throw "Missing app icon: $AppIcon"
}
if (-not (Test-Path -LiteralPath $InstallerSigner)) {
    throw "Missing installer signing helper: $InstallerSigner"
}

if (-not $AllowDirtyReleaseInputs) {
    Assert-CleanReleaseInputs
}

$AppDir = Join-Path $RepoRoot "dist\ApplicantScout"
$Exe = Join-Path $AppDir "ApplicantScout.exe"
$BasePythonPrefix = Get-VenvBasePrefix
$BuildState = @{}
Invoke-WithIsolatedBuildEnvironment -BasePrefix $BasePythonPrefix -Action {
    Assert-ReleaseConstraints
    $VersionOutput = Invoke-NativeChecked -Label "Read applicant_scout.__version__" -Command {
        & $Python -c "import applicant_scout; print(applicant_scout.__version__)" 2>$null
    }
    $VersionLine = $VersionOutput | Select-Object -First 1
    $Version = if ($null -eq $VersionLine) { "" } else { $VersionLine.Trim() }
    if (-not $Version) {
        throw "Could not read applicant_scout.__version__ for artifact naming."
    }
    $Archive = Join-Path $RepoRoot "dist\ApplicantScoutCompanion-$Version-portable.zip"
    $Installer = Join-Path $RepoRoot "dist\ApplicantScoutCompanionSetup-$Version.exe"
    $InstallerChecksum = Join-Path $RepoRoot "dist\ApplicantScoutCompanionSetup-$Version.exe.sha256"
    $VersionInfoFile = Join-Path $RepoRoot "build\ApplicantScout-version-info.txt"
    $BuildState.Version = $Version
    $BuildState.Archive = $Archive
    $BuildState.Installer = $Installer
    $BuildState.InstallerChecksum = $InstallerChecksum
    New-VersionInfoFile -VersionText $Version -OutputPath $VersionInfoFile

    if (-not $SkipChecks) {
        & (Join-Path $RepoRoot "scripts\check.ps1")
    }
    Invoke-PyInstaller
    if (-not (Test-Path -LiteralPath $Exe)) {
        throw "Build did not produce expected executable: $Exe"
    }
    Assert-FrozenRuntimeLayout -AppDir $AppDir -BasePrefix $BasePythonPrefix
    Assert-FrozenStartupImports -Exe $Exe
    Copy-ReleaseTextArtifacts -TargetDir $AppDir
    Copy-DependencyLicenseArtifacts -TargetDir $AppDir -BasePrefix $BasePythonPrefix
    Write-PayloadVersionMarker -TargetDir $AppDir -VersionText $Version
    Assert-FrozenRuntimeLayout `
        -AppDir $AppDir `
        -BasePrefix $BasePythonPrefix `
        -PackagedLayout
}

$Version = [string]$BuildState.Version
$Archive = [string]$BuildState.Archive
$Installer = [string]$BuildState.Installer
$InstallerChecksum = [string]$BuildState.InstallerChecksum
if (-not $Version -or -not $Archive -or -not $Installer -or -not $InstallerChecksum) {
    throw "Isolated build did not return complete artifact identity."
}

if (-not $SkipPortable) {
    if (Test-Path -LiteralPath $Archive) {
        Remove-Item -LiteralPath $Archive
    }
    Compress-Archive -LiteralPath $AppDir -DestinationPath $Archive -Force
}

if (-not $SkipInstaller) {
    $Iscc = Find-InnoSetupCompiler
    if (-not $Iscc) {
        throw "Missing trusted Inno Setup compiler (officially signed Inno Setup 6.7.1 in a standard install directory), or rerun with -SkipInstaller for a portable ZIP only."
    }
    $PreviousInnoVersion = $env:APSCOUT_INNO_VERSION
    $PreviousInnoSourceDir = $env:APSCOUT_INNO_SOURCE_DIR
    $PreviousInnoIcon = $env:APSCOUT_INNO_ICON
    try {
        $env:APSCOUT_INNO_VERSION = $Version
        $env:APSCOUT_INNO_SOURCE_DIR = $AppDir
        $env:APSCOUT_INNO_ICON = $AppIcon
        Invoke-NativeChecked -Label "Inno Setup compiler" -Command {
            & $Iscc $InnoScript
        }
        if (-not (Test-Path -LiteralPath $Installer)) {
            throw "Installer build did not produce expected artifact: $Installer"
        }
        & $InstallerSigner `
            -InstallerPath $Installer `
            -ChecksumPath $InstallerChecksum `
            -RequireSigning:$RequireSigning
    }
    finally {
        $env:APSCOUT_INNO_VERSION = $PreviousInnoVersion
        $env:APSCOUT_INNO_SOURCE_DIR = $PreviousInnoSourceDir
        $env:APSCOUT_INNO_ICON = $PreviousInnoIcon
    }
}

Write-Host "Built: $Exe"
if (-not $SkipPortable) {
    Write-Host "Packed portable ZIP: $Archive"
}
if (-not $SkipInstaller) {
    Write-Host "Packed installer: $Installer"
    Write-Host "Packed installer checksum: $InstallerChecksum"
}
