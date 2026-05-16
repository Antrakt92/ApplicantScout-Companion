param(
    [switch]$SkipChecks,
    [switch]$SkipInstaller,
    [switch]$SkipPortable,
    [switch]$AllowDirtyReleaseInputs
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$PyInstaller = Join-Path $RepoRoot ".venv\Scripts\pyinstaller.exe"
$EntryPoint = Join-Path $RepoRoot "packaging\pyinstaller\run_applicant_scout.py"
$InnoScript = Join-Path $RepoRoot "packaging\inno\ApplicantScoutCompanion.iss"
$AppIcon = Join-Path $RepoRoot "src\applicant_scout\assets\app_icon.ico"

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
        [string]$TargetDir
    )

    $LicenseDir = Join-Path $TargetDir "licenses"
    New-Item -ItemType Directory -Path $LicenseDir -Force | Out-Null
    $PreviousLicenseDest = $env:APSCOUT_LICENSE_DEST
    $env:APSCOUT_LICENSE_DEST = $LicenseDir
    try {
        $PythonCode = @'
from importlib import metadata
import os
import shutil
from pathlib import Path

dest = Path(os.environ["APSCOUT_LICENSE_DEST"])
packages = [
    "altgraph",
    "anyio",
    "build",
    "certifi",
    "colorama",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "iniconfig",
    "nodeenv",
    "numpy",
    "packaging",
    "pefile",
    "Pillow",
    "pluggy",
    "Pygments",
    "PyInstaller",
    "pyinstaller-hooks-contrib",
    "pyproject_hooks",
    "PyQt6",
    "PyQt6-Qt6",
    "PyQt6-sip",
    "pyright",
    "pyzbar",
    "pytest",
    "pytest-qt",
    "python-dotenv",
    "pywin32-ctypes",
    "ruff",
    "setuptools",
    "typing_extensions",
    "watchdog",
]
tokens = ("license", "copying", "notice")
for package in packages:
    try:
        dist = metadata.distribution(package)
    except metadata.PackageNotFoundError:
        continue
    name = dist.metadata.get("Name", package)
    package_dest = dest / name
    copied = False
    for file in dist.files or ():
        rel = Path(str(file))
        if not any(token in rel.name.lower() for token in tokens):
            continue
        source = Path(dist.locate_file(file))
        if not source.is_file():
            continue
        target = package_dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied = True
    if not copied:
        package_dest.mkdir(parents=True, exist_ok=True)
        (package_dest / "NO-LICENSE-FILE-FOUND.txt").write_text(
            f"No license-like file was exposed by installed metadata for {name}.\n",
            encoding="utf-8",
        )
'@
        Invoke-NativeChecked -Label "Collect dependency license files" -Command {
            & $Python -c $PythonCode
        }
    }
    finally {
        $env:APSCOUT_LICENSE_DEST = $PreviousLicenseDest
    }
}

function Assert-ReleaseConstraints {
    $Constraints = Join-Path $RepoRoot "constraints-release.txt"
    if (-not (Test-Path -LiteralPath $Constraints)) {
        throw "Missing release constraints file: $Constraints"
    }

    $PreviousConstraintsFile = $env:APSCOUT_CONSTRAINTS_FILE
    $env:APSCOUT_CONSTRAINTS_FILE = $Constraints
    try {
        $PythonCode = @'
from importlib import metadata
import os
import re
import sys
from pathlib import Path

constraints = Path(os.environ["APSCOUT_CONSTRAINTS_FILE"])
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

if malformed or missing or mismatched:
    for item in malformed:
        print(f"Malformed release constraint: {item}", file=sys.stderr)
    for item in missing:
        print(f"missing package: {item}", file=sys.stderr)
    for item in mismatched:
        print(item, file=sys.stderr)
    sys.exit(1)
'@
        Invoke-NativeChecked -Label "Validate release constraints" -Command {
            & $Python -c $PythonCode
        }
    }
    finally {
        $env:APSCOUT_CONSTRAINTS_FILE = $PreviousConstraintsFile
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
        "scripts\build-windows.ps1"
    )

    $Git = Get-Command "git" -ErrorAction SilentlyContinue
    if ($null -eq $Git) {
        throw "Cannot verify release input cleanliness because git is not available."
    }

    Invoke-NativeChecked -Label "Inspect release input cleanliness" -Command {
        & $Git.Source -C $RepoRoot status --porcelain --untracked-files=all -- $ReleaseInputPaths
    }
    $Dirty = & $Git.Source -C $RepoRoot status --porcelain --untracked-files=all -- $ReleaseInputPaths
    if ($Dirty) {
        $Joined = ($Dirty -join [Environment]::NewLine)
        throw (
            "Refusing to build release artifacts from dirty release inputs. " +
            "Commit or revert these paths first, or rerun with -AllowDirtyReleaseInputs for a local smoke build:" +
            [Environment]::NewLine + $Joined
        )
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

function Find-InnoSetupCompiler {
    $Command = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
    if ($null -ne $Command) {
        return $Command.Source
    }

    $Candidates = @()
    if ($env:LOCALAPPDATA) {
        $Candidates += Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $Candidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    }
    if ($env:ProgramFiles) {
        $Candidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"
    }

    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) {
            return $Candidate
        }
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

if (-not $AllowDirtyReleaseInputs) {
    Assert-CleanReleaseInputs
}
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
New-VersionInfoFile -VersionText $Version -OutputPath $VersionInfoFile

if (-not $SkipChecks) {
    & (Join-Path $RepoRoot "scripts\check.ps1")
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
        --collect-all pyzbar `
        --version-file $VersionInfoFile `
        --icon $AppIcon `
        $EntryPoint
}

$AppDir = Join-Path $RepoRoot "dist\ApplicantScout"
$Exe = Join-Path $AppDir "ApplicantScout.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Build did not produce expected executable: $Exe"
}

Copy-ReleaseTextArtifacts -TargetDir $AppDir
Copy-DependencyLicenseArtifacts -TargetDir $AppDir

if (-not $SkipPortable) {
    if (Test-Path -LiteralPath $Archive) {
        Remove-Item -LiteralPath $Archive
    }
    Compress-Archive -LiteralPath $AppDir -DestinationPath $Archive -Force
}

if (-not $SkipInstaller) {
    $Iscc = Find-InnoSetupCompiler
    if (-not $Iscc) {
        throw "Missing Inno Setup compiler (iscc.exe). Install Inno Setup 6.x, or rerun with -SkipInstaller for a portable ZIP only."
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
        $InstallerHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLowerInvariant()
        "$InstallerHash  $(Split-Path -Leaf $Installer)" | Set-Content -LiteralPath $InstallerChecksum -Encoding ASCII
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
