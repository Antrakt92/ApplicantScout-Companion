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
| PySide6, PySide6-Essentials and PySide6-Addons | Qt for Python bindings and Qt runtime for the desktop UI | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only, with component-specific Qt terms |
| shiboken6 | Qt for Python binding support | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| pyzbar / zbar | QR decoding and native zbar library | MIT / LGPL-2.1 |
| libiconv | Character-set conversion used by ZBar | GNU Library GPL v2 or later |
| Mesa / LLVM | Software OpenGL fallback supplied by Qt | MIT / University of Illinois NCSA and component notices |
| Pillow | Image loading for screenshots | HPND-style Pillow license |
| httpx / httpcore / anyio | HTTP client stack | BSD/MIT-style licenses |
| certifi | CA certificate bundle | MPL-2.0 |
| watchdog | Filesystem watcher | Apache-2.0 |
| python-dotenv | Developer/backcompat env-file parsing | BSD-3-Clause |
| PyInstaller | Windows packaging tool | GPLv2-or-later with PyInstaller exception |

The PySide6 and Shiboken wheel metadata lists LGPL-3.0-only, GPL-2.0-only and
GPL-3.0-only alternatives. The verified Qt for Python source archive supplies
the license texts copied to `licenses/native/QtForPython-LICENSE-*.txt`. Qt libraries
and their embedded components also have their own notices and conditions.
The build excludes Qt Virtual Keyboard, which this application does not use.

## Source access

The next Windows build uses the open-source PySide6 wheels from PyPI. Earlier
published builds may use PyQt; consult each release's tagged notices. No
commercial Qt license is asserted. The MIT license covers ApplicantScout's own source;
it does not replace the terms applying to the combined binary and its libraries.

For a released application, use its matching tag in the
[ApplicantScout source repository](https://github.com/Antrakt92/ApplicantScout-Companion/tags),
not the moving main branch. That tag contains the build scripts and
`constraints-release.txt`; its release manifest records the packaged files.
[CONTRIBUTING.md](https://github.com/Antrakt92/ApplicantScout-Companion/blob/main/CONTRIBUTING.md)
explains the development and packaging steps. Use the corresponding tagged
instructions when rebuilding a release that includes them.

The following routes identify source for the current dependency pins. For an
older release, use its tagged constraints and source notice.

| Component | Source route |
| --- | --- |
| PySide6 6.11.2, Essentials, Addons and shiboken6 | [Qt for Python 6.11.2 source archive](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.tar.xz) and [Qt source evidence](docs/NATIVE-QT-SOURCES.md). |
| Qt 6.11.2 | [Complete Qt 6.11.2 source archive](https://download.qt.io/archive/qt/6.11/6.11.2/single/qt-everywhere-src-6.11.2.tar.xz). |
| pyzbar 0.1.9 | [Tagged wrapper source](https://github.com/NaturalHistoryMuseum/pyzbar/tree/v0.1.9). Its [build script](https://github.com/NaturalHistoryMuseum/pyzbar/blob/v0.1.9/build.sh) identifies the Windows DLL download. |
| ZBar Windows DLL | [ZBarWin64 source commit](https://github.com/NaturalHistoryMuseum/ZBarWin64/tree/720e4577eedf6d6173c146f8494db75583503d94), whose v0.10 release wheel contains the same DLL bytes; includes the VS2013/x64 build project. |
| libiconv Windows DLL | [Retained GNU sources, generated configuration and build receipt](packaging/native/libiconv/README.md) for the controlled Windows build. |

For other Python packages, use the exact version in `constraints-release.txt`
and that version's PyPI source distribution or upstream source tag. The bundled
`licenses/` directory records the runtime license texts; build-only packages are
not all included in the application.

### Native-library source records

The [native source record](docs/NATIVE-SOURCES.md) provides binary hashes, pinned
source archives and build instructions. [Qt evidence](docs/NATIVE-QT-SOURCES.md)
maps bundled Qt and PySide files to the installed wheels and publisher source
archives, including known build limits. Copies of
these documents are included under `licenses/` in new Windows builds.

The original pyzbar libiconv DLL has been replaced in new Windows packages by
a build from retained GNU sources and explicit Windows configuration. The
source checker binds that replacement to its build input and packaged bytes.
Incomplete or changed records block release and publication; license texts
alone do not satisfy the check.

Qt component, QtPdf, Mesa, LLVM and libiconv source notices are included under
`licenses/native/`. Mesa/LLVM's original
compiler flags and PySide's original compiler invocation remain reproduction
limits, as described in the Qt evidence. No byte-for-byte source rebuild is
claimed.
