# Qt for Python native sources

This record covers the Windows x64 payload built from
PySide6-Essentials 6.12.0 and shiboken6 6.12.0.
The exact DLL and PYD paths and SHA-256 hashes are in
[`packaging/native-source-provenance.json`](../packaging/native-source-provenance.json).
That manifest was checked against the installed wheels and an isolated
PyInstaller payload. Hash equality establishes which wheel files were frozen;
it is not a source rebuild or a claim of reproducibility.

## Source archives

| Source | SHA-256 |
| --- | --- |
| [Qt 6.12.0 complete source](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz) | `98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165` |
| [Qt for Python 6.12.0 source](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz) | `099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751` |
| [Mesa 11.2.2](https://archive.mesa3d.org/older-versions/11.x/11.2.2/mesa-11.2.2.tar.xz) | `40e148812388ec7c6d7b6657d5a16e2e8dabba8b97ddfceea5197947647bdfb4` |
| [LLVM 3.6.2](https://releases.llvm.org/3.6.2/llvm-3.6.2.src.tar.xz) | `f60dc158bfda6822de167e87275848969f0558b3134892ff54fced87e4667b94` |

Both Qt source archives were downloaded and their SHA-256 hashes matched
the publisher checksum files. The Qt for Python checksum is published at
[Qt's published SHA-256](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz.sha256).
The Mesa and LLVM source hashes were established during the earlier
native-source review. The source archives contain build configuration and
license texts. Preserve their exact bytes with a release's source materials.

## Mapping and build route

The frozen `PySide6/` and `shiboken6/` inventory contains Qt runtime DLLs
and plugins, PySide binding PYDs, Shiboken PYDs and a software OpenGL DLL.
Each recorded binary matched the file owned by its installed wheel.
`importlib.metadata` identified the owning distribution for each file.
Only the Essentials distribution and Shiboken are required. The PySide6
meta-package, Addons, PDF and WebEngine distributions are excluded from the
release environment and payload; no Qt PDF DLL or image plugin is shipped.
The bundled `opengl32sw.dll` contains Mesa 11.2.2 and LLVM 3.6.2 version
strings. The manifest assigns that DLL to a separate source record.
PyInstaller's generic QtGui hook would also collect the unused Qt Virtual
Keyboard plugin and its Qt QML/Quick dependencies. The application-specific
hook excludes that plugin, and the frozen-runtime verifier rejects Virtual
Keyboard files if they reappear. The remaining Qt modules are recorded by
their exact packaged hashes.

To build from source, start with the exact Qt and Qt for Python archives above,
the supported MSVC toolchain, CMake, Ninja and Python. Follow
[Qt's Windows source-build guide](https://doc.qt.io/qtforpython-6/building_from_source/windows.html)
for the `pyside-setup` build and its matching Qt installation. The Qt
archive includes module build files for the shipped Qt libraries and plugins.
Mesa and LLVM have separate build systems and source archives. Their original
wheel vendor build flags and any local changes have not been established.
The original PySide/Shiboken wheel build invocation has not been established
either. No byte-identical rebuild is claimed.

The packaged Microsoft C/C++ runtime DLLs are excluded by exact path from
this native-source inventory and checked separately by the frozen-runtime
verifier. The checker only covers `PySide6/`, `shiboken6/` and `pyzbar/`;
Pillow and other native Python dependencies remain outside that inventory.
Run both checks from the repository root:

```powershell
.\.venv\Scripts\python scripts\check_native_sources.py --installed --require-complete
.\.venv\Scripts\python scripts\check_native_sources.py --payload-root dist\ApplicantScout\_internal --require-complete
```

The PySide wheel metadata lists LGPL-3.0-only, GPL-2.0-only and GPL-3.0-only
alternatives. The source archive's verbatim license texts are packaged under
`licenses/native/QtForPython-LICENSE-*.txt`; Qt and third-party notices are described
in [the native notice map](../packaging/native-license-notices/README.md).
These files support license review but do not establish legal compliance.
