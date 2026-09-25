# Qt for Python native sources

This record covers the Windows x64 payload built from PySide6 6.11.2,
PySide6-Essentials 6.11.2, PySide6-Addons 6.11.2, and shiboken6 6.11.2.
The exact DLL and PYD paths and SHA-256 hashes are in
[`packaging/native-source-provenance.json`](../packaging/native-source-provenance.json).
That manifest was checked against the installed wheels and an isolated
PyInstaller payload. Hash equality establishes which wheel files were frozen;
it is not a source rebuild or a claim of reproducibility.

## Source archives

| Source | SHA-256 |
| --- | --- |
| [Qt 6.11.2 complete source](https://download.qt.io/archive/qt/6.11/6.11.2/single/qt-everywhere-src-6.11.2.tar.xz) | `6dcfbca271d76a6502741a2c0dc6fc98ef7dd0b7b4cfd0abcebb285a86a26f33` |
| [Qt for Python 6.11.2 source](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.tar.xz) | `cba47efbaad1bedd529725cbc14e21f156c7a19366f07b3edfbb076ffd7afdf8` |
| [Mesa 11.2.2](https://archive.mesa3d.org/older-versions/11.x/11.2.2/mesa-11.2.2.tar.xz) | `40e148812388ec7c6d7b6657d5a16e2e8dabba8b97ddfceea5197947647bdfb4` |
| [LLVM 3.6.2](https://releases.llvm.org/3.6.2/llvm-3.6.2.src.tar.xz) | `f60dc158bfda6822de167e87275848969f0558b3134892ff54fced87e4667b94` |

The Qt for Python source archive was downloaded and checked against
[Qt's published SHA-256](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.tar.xz.sha256).
The Qt, Mesa, and LLVM source hashes were established during the earlier
native-source review. The source archives contain build configuration and
license texts. Preserve their exact bytes with a release's source materials.

## Mapping and build route

The frozen `PySide6/` and `shiboken6/` inventory contains Qt runtime DLLs
and plugins, PySide binding PYDs, Shiboken PYDs and a software OpenGL DLL.
Each recorded binary matched the file owned by its installed wheel.
`importlib.metadata` identified the owning distribution for each file;
`PySide6` is a meta-package and owns none of the recorded native files.
The bundled `opengl32sw.dll` contains Mesa 11.2.2 and LLVM 3.6.2 version
strings. The manifest assigns that DLL to a separate source record.
PyInstaller's generic QtGui hook would also collect the unused Qt Virtual
Keyboard plugin and its Qt QML/Quick dependencies. The application-specific
hook excludes that plugin, and the frozen-runtime verifier rejects Virtual
Keyboard files if they reappear. The other Qt modules remain recorded by
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
