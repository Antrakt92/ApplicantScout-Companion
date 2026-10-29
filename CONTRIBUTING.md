# Contributing to ApplicantScout Companion

[Player guide](README.md) · [Build instructions](docs/BUILDING.md) · [Security](SECURITY.md)

Report bugs with both versions, your Windows version, reproduction steps and
expected result. Redact credentials and player data using the
[diagnostic guide](docs/REFERENCE.md#trust-and-local-data).
Report vulnerabilities through [private security reporting](SECURITY.md).

## Set up a Windows checkout

Use Git, Python 3.14 (64-bit), PowerShell and Lua 5.1. Keep both repositories as
siblings; the companion gate checks their shared transport and release contracts.

```powershell
git clone https://github.com/Antrakt92/ApplicantScout-Companion.git
git clone https://github.com/Antrakt92/ApplicantScout-Addon.git
cd ApplicantScout-Companion
py -3.14 -m venv .venv
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
and its `scripts/tool-version-locks.json`. Use the pinned Lua language-server version.

Run from the companion checkout:

```powershell
.\scripts\check.ps1 -AddonRoot ..\ApplicantScout-Addon
.\.venv\Scripts\applicant-scout.exe
```

The check runs Python tests, Ruff, Pyright, visual checks, addon syntax and
paired contracts. Use focused tests first, then the full check. See the
[visual-check instructions](docs/README.md) before changing image baselines.

Warcraft Logs credentials are only needed to use live lookups. Offline tests do
not need them. Configure live runs through Settings or the optional
[environment variables](docs/REFERENCE.md#settings); never commit credentials.
Seasonal WCL verification is opt-in because it spends API quota.

## Submit a change

Keep changes focused and include the checks you ran. Preserve release history
and third-party notices. Keep credentials, player data and generated builds out
of commits. The [build instructions](docs/BUILDING.md) cover installers, native
dependencies and signing; [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) covers publication.
