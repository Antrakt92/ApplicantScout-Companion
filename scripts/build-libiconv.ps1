#Requires -Version 7.0
param(
    [string]$Archive = "",
    [switch]$Download,
    [string]$OutputDirectory = "build/native-decoder",
    [string]$Python = "python"
)

# This experiment never writes into site-packages or the application package.
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonCommand = (Get-Command $Python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
if (($Download -and $Archive) -or (-not $Download -and -not $Archive)) {
    throw "Choose exactly one of -Archive or -Download."
}
$PrepareArgs = @((Join-Path $PSScriptRoot "prepare_libiconv.py"), "--output", $OutputDirectory)
if ($Download) { $PrepareArgs += "--download" }
else { $PrepareArgs += @("--archive", (Resolve-Path -LiteralPath $Archive).Path) }
& $PythonCommand @PrepareArgs
if ($LASTEXITCODE -ne 0) { throw "Candidate source preparation failed." }

if ([IO.Path]::IsPathRooted($OutputDirectory)) { $CandidateRoot = $OutputDirectory }
else { $CandidateRoot = Join-Path $RepoRoot $OutputDirectory }
$CandidateRoot = (Resolve-Path -LiteralPath $CandidateRoot).Path
$ArtifactRoot = Join-Path $CandidateRoot "artifact"
$SourceRoot = Join-Path $CandidateRoot "source/libiconv-1.14"
$ObjectRoot = Join-Path $CandidateRoot "objects"
New-Item -ItemType Directory -Path $ObjectRoot -ErrorAction Stop | Out-Null
$RecipeRoot = Join-Path $ArtifactRoot "recipe"
New-Item -ItemType Directory -Path $RecipeRoot -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath $PSCommandPath -Destination $RecipeRoot
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "prepare_libiconv.py") -Destination $RecipeRoot

function Invoke-CandidateCommand {
    param([string]$Executable, [string[]]$Arguments)
    Write-Host ($Executable + " " + ($Arguments -join " "))
    & $Executable @Arguments 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Candidate command failed with exit code $LASTEXITCODE`: $Executable"
    }
}

Start-Transcript -Path (Join-Path $ArtifactRoot "build.log") | Out-Null
try {
    $VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio/Installer/vswhere.exe"
    if (-not (Test-Path -LiteralPath $VsWhere)) { throw "Installed VS2022 vswhere was not found." }
    $VsJson = & $VsWhere -latest -version "[17.0,18.0)" -products "*" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -format json
    if ($LASTEXITCODE -ne 0) { throw "vswhere could not inspect installed VS2022." }
    $VsInstances = @($VsJson | ConvertFrom-Json)
    if ($VsInstances.Count -ne 1) { throw "One installed VS2022 C++ toolchain is required; no tools will be downloaded." }
    $VsInstance = $VsInstances[0]
    $LaunchShell = Join-Path $VsInstance.installationPath "Common7/Tools/Launch-VsDevShell.ps1"
    & $LaunchShell -Arch amd64 -HostArch amd64 -SkipAutomaticLocation | Out-Host
    if ($env:VSCMD_ARG_TGT_ARCH -ne "x64" -or $env:VSCMD_ARG_HOST_ARCH -ne "x64") {
        throw "VS2022 did not select the x64 host and target toolchain."
    }
    $Compiler = (Get-Command cl.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $Linker = (Get-Command link.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $Dumpbin = (Get-Command dumpbin.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $VsPrefix = $VsInstance.installationPath.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    foreach ($Tool in @($Compiler, $Linker, $Dumpbin)) {
        if (-not $Tool.StartsWith($VsPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "A selected build tool is outside the chosen VS2022 installation."
        }
    }
    foreach ($OptionVariable in @("CL", "_CL_", "LINK", "_LINK_")) {
        if ([Environment]::GetEnvironmentVariable($OptionVariable)) {
            throw "Implicit $OptionVariable options are not allowed in this candidate build."
        }
    }
    $CompilerArgs = @(
        "/nologo", "/Bv", "/TC", "/O2", "/Oi", "/Gy", "/GF", "/MT", "/W3",
        "/DNDEBUG", "/D_CRT_SECURE_NO_WARNINGS",
        "/I$(Join-Path $ArtifactRoot 'generated')", "/I$(Join-Path $SourceRoot 'lib')",
        "/I$(Join-Path $SourceRoot 'libcharset/lib')", "/c",
        (Join-Path $SourceRoot "lib/iconv.c"),
        (Join-Path $SourceRoot "libcharset/lib/localcharset.c"),
        (Join-Path $SourceRoot "lib/relocatable.c")
    )
    $LinkerArgs = @(
        "/nologo", "/DLL", "/MACHINE:X64", "/INCREMENTAL:NO", "/OPT:REF", "/OPT:ICF",
        "/DYNAMICBASE", "/NXCOMPAT", "/OUT:$(Join-Path $ArtifactRoot 'libiconv.dll')",
        "/IMPLIB:$(Join-Path $ArtifactRoot 'libiconv.lib')", "/DEF:$(Join-Path $ArtifactRoot 'libiconv.def')",
        "iconv.obj", "localcharset.obj", "relocatable.obj", "kernel32.lib"
    )
    $Toolchain = [ordered]@{
        image_version = $env:ImageVersion
        visual_studio = $VsInstance.installationVersion
        vc_tools_version = $env:VCToolsVersion
        windows_sdk_version = $env:WindowsSDKVersion
        compiler = $Compiler
        compiler_sha256 = (Get-FileHash -LiteralPath $Compiler -Algorithm SHA256).Hash.ToLowerInvariant()
        linker = $Linker
        linker_sha256 = (Get-FileHash -LiteralPath $Linker -Algorithm SHA256).Hash.ToLowerInvariant()
        compiler_arguments = $CompilerArgs
        linker_arguments = $LinkerArgs
        commit = $env:GITHUB_SHA
        run_id = $env:GITHUB_RUN_ID
        run_attempt = $env:GITHUB_RUN_ATTEMPT
    }
    $Toolchain | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $ArtifactRoot "toolchain.json") -Encoding utf8
    Push-Location $ObjectRoot
    try {
        Invoke-CandidateCommand -Executable $Compiler -Arguments $CompilerArgs
        Invoke-CandidateCommand -Executable $Linker -Arguments $LinkerArgs
        foreach ($View in @("headers", "exports", "imports")) {
            $Report = & $Dumpbin "/$View" (Join-Path $ArtifactRoot "libiconv.dll") 2>&1
            if ($LASTEXITCODE -ne 0) { throw "dumpbin /$View failed." }
            $Report | Set-Content -LiteralPath (Join-Path $ArtifactRoot "$View.txt") -Encoding utf8
        }
    }
    finally { Pop-Location }
    [ordered]@{
        status = "compiled-candidate"
        dll_sha256 = (Get-FileHash -LiteralPath (Join-Path $ArtifactRoot "libiconv.dll") -Algorithm SHA256).Hash.ToLowerInvariant()
        runtime_acceptance = "pending; compiler success does not authorize shipping"
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ArtifactRoot "build-result.json") -Encoding utf8
}
catch {
    [ordered]@{ status = "failed"; error = $_.Exception.Message } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ArtifactRoot "build-result.json") -Encoding utf8
    throw
}
finally {
    Stop-Transcript | Out-Null
    $Sums = Get-ChildItem -LiteralPath $ArtifactRoot -File -Recurse | Sort-Object FullName | ForEach-Object {
        $Relative = [IO.Path]::GetRelativePath($ArtifactRoot, $_.FullName).Replace('\', '/')
        (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() + "  " + $Relative
    }
    $Sums | Set-Content -LiteralPath (Join-Path $ArtifactRoot "SHA256SUMS") -Encoding ascii
}
