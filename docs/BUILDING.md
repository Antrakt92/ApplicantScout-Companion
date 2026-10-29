# Building ApplicantScout Companion

[Contributor setup](../CONTRIBUTING.md) · [Documentation](README.md)

Run commands from the repository root.

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

Public builds remain unsigned until a code-signing certificate is configured.
If you have a suitable
certificate installed, set `APSCOUT_SIGNING_CERT_SHA1` to its thumbprint; the
build signs the installer before generating its checksum. This does not enable
signing for other contributors or imply a certificate is provided.

Follow [RELEASE_CHECKLIST.md](../RELEASE_CHECKLIST.md) for paired publication and
[THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md#source-access) for dependency source
routes and unresolved native-library provenance. Building from the same source
and pinned packages does not by itself prove byte-for-byte reproduction of an
existing installer.
