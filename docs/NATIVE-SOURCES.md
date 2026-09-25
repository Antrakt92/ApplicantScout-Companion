# Native library sources

This document records the Qt, PyQt, SIP and QR-decoder native dependencies in
`constraints-release.txt`. The machine-readable record is
[`packaging/native-source-provenance.json`](https://github.com/Antrakt92/ApplicantScout-Companion/blob/main/packaging/native-source-provenance.json).
For a released build, read these files at its release tag.

The record binds dependency versions and DLL hashes to source archives and
build instructions. It distinguishes verified source routes from missing
evidence. It is not a claim that every original binary has been reproduced.

## Checking a build

From a Windows development environment with the pinned dependencies installed:

```powershell
.\.venv\Scripts\python scripts\check_native_sources.py --installed
.\.venv\Scripts\python scripts\check_native_sources.py --installed --require-complete
```

The first command checks recorded versions, paths and DLL bytes and lists any
incomplete components. The second also rejects incomplete source/build records.
The release and publication workflows require the second condition. Development
builds remain available for testing while evidence is incomplete.

Before tagging, run the strict command locally. Release builds also pass
`-RequireNativeSources` to `scripts/build-windows.ps1`; this repeats the installed
binary checks before PyInstaller runs. The source-access documents are copied
to `licenses/` in the application payload.

After PyInstaller, the build checks the recorded hashes in the packaged
`_internal` directory and rejects unrecorded DLLs or Python native modules in
`PyQt6` and `pyzbar`. Only the reviewed Microsoft runtime files in Qt's `bin`
directory are excluded from this source inventory; their provenance remains
covered by the frozen-runtime verifier. This is a check of those native
components, not a complete inventory of every dependency in the application.

The checker performs no downloads. It verifies locally retained archives when
recorded; other archive hashes are source identifiers, not a substitute for
obtaining and retaining the sources
when preparing a release. Preserve the complete source archives, license texts,
patches and referenced build instructions together.

## ZBar supplied by pyzbar 0.1.9

The Windows x64 `libzbar-64.dll` is identical to `libzbar64-0.dll` in the
[Natural History Museum ZBarWin64 v0.10 wheel](https://github.com/NaturalHistoryMuseum/ZBarWin64/releases/tag/v0.10):

```text
43ecda0a3e803ffadeab912f5ecaa2655a8a51c6af1c4286bf9ef21cc90a4b04
```

The release tag resolves to source commit
`720e4577eedf6d6173c146f8494db75583503d94`.
[Download the pinned source archive](https://codeload.github.com/NaturalHistoryMuseum/ZBarWin64/tar.gz/720e4577eedf6d6173c146f8494db75583503d94).
Its SHA-256 is
`248611c3da46a77cca2d2ddf91b1cd7f4f9512a0ddb4b32bc579fec08d3e98e6`.

The source includes the Windows changes, `zbar64.sln`, `zbar64.vcxproj`, export
definitions, headers and license. Its Release/x64 project uses Visual Studio
2013 toolset `v120`, whole-program and maximum-speed optimization, intrinsic
functions, function-level linking and COMDAT folding. It links `winmm.lib` and
the vendored x64 libiconv import library. The declared build command is:

```text
msbuild zbar64.sln /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v120
```

The output is `x64/Release/libzbar64-0.dll`. This is the path selected by the
release's `python/setup.py`; the different prebuilt DLL committed under `lib/`
is not the wheel binary. The build command above documents the upstream
project and has not been used here to reproduce the historical DLL. The project
also imports optional user property sheets, whose original contents are unknown.

The generic ZBar 0.10 tarball does not contain the same Windows source tree.
Use the pinned fork. The packaged libiconv dependency is built separately as
described below.

## libiconv 1.14: controlled Windows build

Windows packages use the DLL retained under `packaging/native/libiconv/`:

```text
5e684c9b6943ed5f30bc7f3a6e3de9942bfa12f68b9fb20dedb3472af8a999c6
```

It was built from unmodified GNU libiconv 1.14 implementation files with
explicit generated Windows headers and a nine-entry export definition. The
[source and build record](https://github.com/Antrakt92/ApplicantScout-Companion/tree/main/packaging/native/libiconv)
contains the complete GNU source archive, generated headers, licenses,
compiler/linker settings and the build receipt. These files are available in
the corresponding application's source tag.

The retained [GNU source archive](https://ftp.gnu.org/pub/gnu/libiconv/libiconv-1.14.tar.gz)
has SHA-256
`72b24ded17d687193c3366d0ebe7cde1e6b18f0df8c55438ac95be39e8a30613`.
The build uses MSVC2022 x64 with a static CRT. The receipt identifies the exact
compiler and linker versions and hashes; the scripts record fresh toolchain
identity on subsequent builds. Different toolchains are not claimed to produce
identical DLL bytes.

The pyzbar wheel still provides the original development-environment DLL, with
SHA-256 `2843567e3fe8c035884dcc6366628ece2f683380e6b1d64763bacf8fc617e17c`.
Its original patched Windows archive is unavailable. Packaging explicitly
selects the controlled DLL without rewriting the installed Python package.
The source checker verifies both the expected wheel input and the retained
replacement, then verifies that the replacement reached the frozen payload.

The replacement preserves the original nine exports and ordinals. Isolated
runtime checks cover conversion results, errors and buffer boundaries,
independent descriptors and real QR decoding with the unchanged ZBar DLL.
They do not replace the application's release and installer acceptance checks.

To repeat the decoder check in the pinned Windows development environment:

```powershell
.\.venv\Scripts\python scripts\smoke_native_decoder.py --libiconv packaging/native/libiconv/libiconv.dll --zbar .venv/Lib/site-packages/pyzbar/libzbar-64.dll
```

The script also needs the paired addon's QR encoder and Lua 5.1; pass
`--qrencode` and `--lua` if they are not in their usual locations. It stages the
DLL pair in a temporary directory and never replaces installed files.

## Qt, PyQt and SIP

See [Qt source and build evidence](https://github.com/Antrakt92/ApplicantScout-Companion/blob/main/docs/NATIVE-QT-SOURCES.md)
for the verified Qt installer archives, source revisions, compiler settings and
binding generators. The software OpenGL library is a separate Mesa/LLVM build;
matching a Qt installer binary alone does not establish its corresponding
source and build recipe.

## Pillow 12.3.0: accepted native-provenance gap

Pillow is a pinned release dependency (`Pillow==12.3.0`) used for screenshot
image loading, but its Windows wheel native modules (`PIL/*.pyd`, bundling
upstream codec builds) have no entry in
`packaging/native-source-provenance.json`: the original wheel file is not
retained, so wheel bytes and upstream source-archive hashes cannot be verified
offline, and frozen-payload coverage is limited to the `PyQt6` and `pyzbar`
scopes. License attribution is recorded in `THIRD-PARTY-NOTICES.md`. This gap
is accepted for development builds; full Pillow native provenance remains open
release-hardening work.
