# Contributing

Report bugs with the companion/addon versions, Windows version, expected result
and steps to reproduce. Redact credentials and player data using the
[diagnostic guide](docs/REFERENCE.md#trust-and-local-data). For vulnerabilities,
use [private security reporting](SECURITY.md).

## Set up a Windows checkout

Use Git, Python 3.13 (64-bit), PowerShell and Lua 5.1. Keep both repositories as
siblings; the companion gate checks their shared transport and release contracts.

```powershell
git clone https://github.com/Antrakt92/ApplicantScout-Companion.git
git clone https://github.com/Antrakt92/ApplicantScout-Addon.git
cd ApplicantScout-Companion
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r constraints-release.txt
.\.venv\Scripts\python -m pip install -e '.[dev]' -c constraints-release.txt
```

Install Lua 5.1 before running the gate. CI uses the pinned LuaBinaries package
below through Chocolatey; run it only on a machine with Chocolatey configured.
The checksum pins the downloaded 64-bit archive.

```powershell
choco install lua51 --version=5.1.5 -y --no-progress --checksum64=5f34cf7d40a20a587ea351482a4207d93b92ef6f1983e910a13338253819fe93 --checksumtype64=sha256
```

The gate needs `lua5.1.exe` and `luac5.1.exe` on PATH or in Chocolatey's
`lua51\tools` directory. Addon Lua language-server checks use a separate lock;
follow the [addon contributor guide](https://github.com/Antrakt92/ApplicantScout-Addon/blob/main/CONTRIBUTING.md)
and its `scripts/tool-version-locks.json`. A random Lua language server on PATH
is not equivalent to the pinned version.

Run from the companion checkout:

```powershell
.\scripts\check.ps1 -AddonRoot ..\ApplicantScout-Addon
.\.venv\Scripts\applicant-scout.exe
```

The check runs Python tests, Ruff, Pyright, visual baseline checks, public media
consistency, addon Lua syntax and paired contract tests. Pass `-AddonRoot` for
another checkout location. See the [visual checks](docs/README.md) before
changing UI baselines. Use the focused tests for your change first, then the
full gate. Do not refresh images merely to make a failed comparison pass.

Warcraft Logs credentials are only needed to use live lookups. Offline tests do
not need them. Configure live runs through Settings or the optional
[environment variables](docs/REFERENCE.md#settings); never commit credentials.
Seasonal WCL verification is opt-in because it spends API quota.

## Build Windows artifacts

```powershell
.\scripts\build-windows.ps1
```

Installer builds require **Inno Setup 6.7.1 exactly**, installed with its normal
installer in a supported user or Program Files directory. CI uses
`choco install innosetup --version=6.7.1 -y --no-progress`. The build verifies the
registered version, compiler signatures and a pinned preprocessor file.
Putting an arbitrary `iscc.exe` on PATH is insufficient. The current version and
checks live in `scripts/build-windows.ps1::Find-InnoSetupCompiler`.

Builds require committed release inputs by default. For a local exploratory
build, `-AllowDirtyReleaseInputs` opts into using uncommitted inputs; such a build
is not a publication candidate. Use `-SkipInstaller` for a portable-only build.
The full build writes the installer, its `.exe.sha256` sidecar and the portable
ZIP to `dist/`. It checks dependency advisories before packaging and requires
network access for that check.

ApplicantScout has a signing-ready release pipeline. Public builds remain
unsigned until a code-signing certificate is configured. If you have a suitable
certificate installed, set `APSCOUT_SIGNING_CERT_SHA1` to its thumbprint; the
build signs the installer before generating its checksum. This does not enable
signing for other contributors or imply a certificate is provided.

Follow [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) for paired publication and
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md#source-access) for dependency source
routes and unresolved native-library provenance. Building from the same source
and pinned packages does not by itself prove byte-for-byte reproduction of an
existing installer.

## Keep changes reviewable

Keep each change focused, describe the user-visible result and include the
checks you ran. Preserve the full release history and third-party notices.
Avoid runtime, dependency or generated-image changes in a documentation-only
pull request. Do not include logs, screenshots or caches containing private
player data or credentials.
