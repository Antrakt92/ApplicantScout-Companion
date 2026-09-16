# GNU libiconv for the Windows decoder

This directory contains the Windows x86-64 `libiconv.dll` built for
ApplicantScout from the unmodified GNU libiconv 1.14 implementation sources.
It replaces the historical DLL whose Windows-specific source changes could
not be traced. The upstream source archive is intentionally retained alongside
the DLL so a checkout or release source archive includes its source inputs.

| File | Purpose |
| --- | --- |
| `libiconv.dll` | Compiled Windows library. |
| `libiconv-1.14.tar.gz` | Complete, unchanged GNU source archive. |
| `generated/*.h` | The four Windows configuration/API headers used in the build. |
| `libiconv.def` | The nine exported symbols, including their ordinals. |
| `build-receipt.json` | Source, binary, generated-input and toolchain hashes; build flags and validation results. |
| `COPYING.LIB` | GNU Library General Public License version 2. The library's notices permit version 2 or later. |
| `COPYING` | GNU GPL version 3 text retained from the source archive for its separately licensed programs. |

The source archive comes from
[GNU's libiconv 1.14 distribution](https://ftp.gnu.org/pub/gnu/libiconv/libiconv-1.14.tar.gz).
Its SHA-256 is
`72b24ded17d687193c3366d0ebe7cde1e6b18f0df8c55438ac95be39e8a30613`.
The DLL's SHA-256 is
`5e684c9b6943ed5f30bc7f3a6e3de9942bfa12f68b9fb20dedb3472af8a999c6`.
Library copyright notices are also retained in
[the packaged native notices](../../native-license-notices/libiconv-1.14-NOTICES.txt).

## Build recipe

The retained DLL was produced by
[build run 35107186695](https://github.com/Antrakt92/ApplicantScout-Companion/actions/runs/35107186695)
from commit `03882887989ebedfe8d224fe99669560ccfdee47`.
[prepare_libiconv.py](../../../scripts/prepare_libiconv.py) verifies the source
archive, generates the headers and export definition, and preserves the three
compiled implementation files without patches.
[build-libiconv.ps1](../../../scripts/build-libiconv.ps1) compiles them with the
installed Visual Studio 2022 x64 toolchain. Neither script installs a compiler.

From the repository root, with Python, PowerShell 7 and Visual Studio 2022 C++
tools already installed:

```powershell
.\scripts\build-libiconv.ps1 `
    -Archive packaging/native/libiconv/libiconv-1.14.tar.gz `
    -OutputDirectory build/native-decoder/rebuild
```

Choose a fresh output directory. The script leaves the resulting DLL and
evidence under that directory; it does not replace this retained binary.
The original build used MSVC tools 14.44.35207, compiler 19.44.35228.0,
linker 14.44.35228.0 and Windows SDK 10.0.26100.0. The receipt records their
hashes and complete options, including `/MT` for the static C runtime.
Its command paths are normalized relative to the `objects/` build directory;
they are not the original machine's absolute paths. Recipe hashes include the
original artifact bytes and an LF-normalized form for checkouts using CRLF.

## Validation and limits

Two independent smoke-test processes loaded the retained DLL together with
the pinned ZBar DLL. Each passed 18 conversion cases and 22 QR cases. The
receipt identifies both loaded binaries by hash. This checks the selected
encoding and QR corpus; it does not establish every encoding's behavior or
live game capture.

The 552 compiler warnings were reviewed for the bounded ZBar QR path. Most
concern constant offsets in small generated string tables. Legacy narrowing
conversions remain unsuitable for a general-purpose input larger than
`INT_MAX`; the QR caller does not approach that size. No warnings were
suppressed and no implementation files were patched.

The source and build inputs are retained, but an independent bit-for-bit
rebuild has not been demonstrated. The original
link command did not request deterministic PE timestamps. A later build must
be verified before replacing the retained DLL or changing its recorded hash.
