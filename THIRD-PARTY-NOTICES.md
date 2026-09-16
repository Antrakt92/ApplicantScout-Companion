# Third-Party Notices

ApplicantScout Companion source code is distributed under the MIT License.
Windows builds bundle third-party Python packages, native libraries, and Qt
runtime files. Their licenses remain with their respective copyright holders.

Release builds resolve the installed runtime dependency closure, add the
PyInstaller bootloader whose code is incorporated into the executable, and copy
their license files from the active Python environment into the bundled
`licenses/` directory. Test and build-only environments are not copied into the
payload. Treat that directory as part of the portable ZIP and installer payload.
A shipped dependency that exposes no license-like file fails the release build
unless a reviewed override is bound to its exact installed version, an HTTPS
provenance source, a rationale, and a tracked non-empty notice.
Missing-license placeholders are never accepted as release license coverage.

## Key Runtime Dependencies

| Component | Purpose | License surface |
|---|---|---|
| CPython | Embedded Python interpreter and standard library | Python Software Foundation License; shipped at `licenses/CPython/LICENSE.txt` |
| PyQt6 | Qt bindings for the desktop UI | GPL v3 or Riverbank commercial license |
| PyQt6-Qt6 | Qt runtime bundled by PyQt wheels | LGPL v3 |
| PyQt6-sip | PyQt support module | BSD-2-Clause |
| pyzbar / zbar | QR decoding and native zbar library | MIT / LGPL-2.1 |
| Pillow | Image loading for screenshots | HPND-style Pillow license |
| httpx / httpcore / anyio | HTTP client stack | BSD/MIT-style licenses |
| certifi | CA certificate bundle | MPL-2.0 |
| watchdog | Filesystem watcher | Apache-2.0 |
| python-dotenv | Developer/backcompat env-file parsing | BSD-3-Clause |
| PyInstaller | Windows packaging tool | GPLv2-or-later with PyInstaller exception |

PyQt licensing is release-critical: PyQt is not LGPL. Public binary releases
using the GPL PyQt wheels must be compatible with the GPL terms, or the build
must use an appropriate commercial PyQt license.

## Source access

The published Windows build uses the GPL PyQt packages from PyPI. No commercial
PyQt license is asserted. The MIT license covers ApplicantScout's own source;
it does not replace the terms applying to the combined binary and its libraries.

For a released application, use its matching tag in the
[ApplicantScout source repository](https://github.com/Antrakt92/ApplicantScout-Companion/tags),
not the moving main branch. That tag contains the build scripts and
`constraints-release.txt`; its release manifest records the packaged files.
[CONTRIBUTING.md](https://github.com/Antrakt92/ApplicantScout-Companion/blob/main/CONTRIBUTING.md)
explains the development and packaging steps. Use the corresponding tagged
instructions when rebuilding a release that includes them.

The following routes identify source for the dependency versions pinned for
Companion 0.18.2. Check a different release's constraints before using them.

| Component | Source route |
| --- | --- |
| PyQt6 6.11.0 | [PyPI source distribution and SHA-256](https://pypi.org/project/PyQt6/6.11.0/#files); the source archive includes its build configuration. |
| Qt 6.11.2 (PyQt6-Qt6) | [Qt 6.11.2 source archives](https://download.qt.io/archive/qt/6.11/6.11.2/single/). Matching version alone does not establish the wheel publisher's exact configuration or patches. |
| PyQt6-sip 13.12.0 | [PyPI source distribution](https://pypi.org/project/PyQt6-sip/13.12.0/#files). |
| pyzbar 0.1.9 | [Tagged wrapper source](https://github.com/NaturalHistoryMuseum/pyzbar/tree/v0.1.9). Its [build script](https://github.com/NaturalHistoryMuseum/pyzbar/blob/v0.1.9/build.sh) identifies the Windows DLL download. |
| ZBar and libiconv Windows DLLs | [Upstream binary release 0.1](https://github.com/NaturalHistoryMuseum/barcode-reader-dlls/releases/tag/0.1), used by pyzbar's wheel build. This is binary provenance, not a corresponding-source archive. |

For other Python packages, use the exact version in `constraints-release.txt`
and that version's PyPI source distribution or upstream source tag. The bundled
`licenses/` directory records the runtime license texts; build-only packages are
not all included in the application.

### Remaining native-library evidence

The linked Windows DLL release does not identify exact ZBar/libiconv source
revisions, patches or a reproducible build recipe. We have not established that
an arbitrary current ZBar or libiconv release corresponds to those DLLs. The Qt
source-version link also does not document the exact wheel build configuration.
These links improve source discovery; they are not a claim that complete
corresponding-source obligations have been verified. Before another binary
release, resolve the native source/build provenance and provide the matching
source materials and instructions with the release. Do not treat the presence
of license texts alone as proof that this work is complete.
