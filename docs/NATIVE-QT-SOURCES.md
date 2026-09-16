# Qt, PyQt and SIP sources

This record covers PyQt6 6.11.0, PyQt6-Qt6 6.11.2 and PyQt6-sip 13.12.0 on
Windows x64. It identifies upstream source archives, build inputs and the
official Qt binaries used by the inspected application payload. It does not
claim a successful source rebuild or byte-for-byte reproducibility. See
[Native library sources](NATIVE-SOURCES.md) for the release checks and other
native dependencies.

## Binding and runtime versions

| Component | Evidence from the installed wheel |
| --- | --- |
| PyQt6 6.11.0 | `cp310-abi3-win_amd64`; WHEEL generator `pyqtbuild 1.19.1`; GPL-3.0-only |
| PyQt6-Qt6 6.11.2 | `py3-none-win_amd64`; WHEEL generator `pyqt-qt-wheel`; LGPLv3 metadata |
| PyQt6-sip 13.12.0 | `cp313-cp313-win_amd64`; WHEEL generator `setuptools 83.0.0`; BSD-2-Clause |

`QT_VERSION_STR` is 6.11.0, the Qt version used to compile the bindings;
`qVersion()` is 6.11.2, the loaded Qt runtime. These are different inputs.
`QLibraryInfo.isDebugBuild()` is false.

The installed `PyQt6/bindings/QtCore/QtCore.toml` records SIP generator 6.15.3,
SIP ABI 13.8, module tags `Qt_6_11_0` and `Windows`, and no disabled features.
`QtGui.toml` additionally disables `PyQt_OpenGL_ES2`, `PyQt_XCB`,
`PyQt_Wayland` and `PyQt_Vulkan`. The SIP runtime reports generator version
6.16.0 for its own build; that does not replace the binding generator pin.

The following SHA-256 values identify the inspected wheels:

| Wheel | SHA-256 |
| --- | --- |
| [PyQt6](https://files.pythonhosted.org/packages/6f/85/dd9f03d78d87460e109e0121cd6201c5802bdd655656bf2780e964870fea/pyqt6-6.11.0-cp310-abi3-win_amd64.whl) | `bd11b459c54dca068e988a42cf838303334f0d441b9d16d92ae6719fcb5ac6ba` |
| [PyQt6-Qt6](https://files.pythonhosted.org/packages/f6/56/62457dd9b5738f65b9fb4ac6b3120bc0bfa0bc335857fdd456b94b0a6079/pyqt6_qt6-6.11.2-py3-none-win_amd64.whl) | `4b424ed1babbef07133eb2a4174c56c848d518767bca8e759aca2c8c9c313636` |
| [PyQt6-sip](https://files.pythonhosted.org/packages/2d/63/f74d1de19822c69a5a6d0e9c59e06f05ab37e4502dbb17cea93d799ac0b6/pyqt6_sip-13.12.0-cp313-cp313-win_amd64.whl) | `99a9016b9f6f23e4446703ba2c2271b35a3ff2fd5115e607b9fac675774abea9` |

## Source archives

These links identify publisher-hosted source distributions. PyQt6,
PyQt-builder, SIP and PyQt6-sip archive bytes were checked against their PyPI
hashes; the complete Qt archive was checked against Qt's published SHA-256.

| Source | SHA-256 |
| --- | --- |
| [PyQt6 6.11.0](https://files.pythonhosted.org/packages/8b/47/b25c13eca5bebc6505394d0223e46d7ebf0c57dcac2ed908d7d19b18ab6b/pyqt6-6.11.0.tar.gz) | `45dd60aa69976de1918b5ced6b4e7b6a25abd2a919ecef5fd5826ecc76718889` |
| [PyQt-builder 1.19.1](https://files.pythonhosted.org/packages/61/f6/f3b504b4d55a7c4d3393cb90378501f1f5fc7f233bd85c0375674f84d2af/pyqt_builder-1.19.1.tar.gz) | `6af6646ba29668751b039bfdced51642cb510e300796b58a4d68b7f956a024d8` |
| [SIP 6.15.3](https://files.pythonhosted.org/packages/02/b1/79bff1c49a9e19ffe0211cb8905cc514c3f6b8f3f7ae55a40403d346c076/sip-6.15.3.tar.gz) | `bb2516983f9f716d321e5157c00d0de0c12422eba73b8f43a44610a0f6622438` |
| [PyQt6-sip 13.12.0](https://files.pythonhosted.org/packages/d1/23/16c583dbb6b53e0494dfcf7d1a44778c82e324edda62326727b37f1a5b34/pyqt6_sip-13.12.0.tar.gz) | `a7ad45c1e3cec3a2473d37ea9870b6c3baeccc560298623c8eb59265714c06e2` |
| [Qt 6.11.2 complete source](https://download.qt.io/archive/qt/6.11/6.11.2/single/qt-everywhere-src-6.11.2.tar.xz) | `6dcfbca271d76a6502741a2c0dc6fc98ef7dd0b7b4cfd0abcebb285a86a26f33` |

Qt publishes the [source SHA-256 file](https://download.qt.io/archive/qt/6.11/6.11.2/single/qt-everywhere-src-6.11.2.tar.xz.sha256).
The complete Qt archive was downloaded and hash-checked, but not rebuilt.
Retain it with the source materials for a release.
PyPI source metadata is available for [PyQt6](https://pypi.org/pypi/PyQt6/6.11.0/json),
[PyQt-builder](https://pypi.org/pypi/PyQt-builder/1.19.1/json),
[SIP](https://pypi.org/pypi/sip/6.15.3/json) and
[PyQt6-sip](https://pypi.org/pypi/PyQt6-sip/13.12.0/json).

## Official Qt binary archives

All 27 Qt, Qt plugin and software OpenGL DLLs in the inspected application's
`PyQt6/Qt6` directory match files in these official Qt installer archives.
Archive SHA-1 values were checked against the publisher's adjacent `.sha1`
files; the SHA-256 values below were calculated from downloaded archive bytes.
This establishes binary origin, not a source rebuild.

| ID | Official archive | SHA-256 |
| --- | --- | --- |
| base | [qtbase](https://download.qt.io/online/qtsdkrepository/windows_x86/desktop/qt6_6112/qt6_6112_msvc2022_64/qt.qt6.6112.win64_msvc2022_64/6.11.2-0-202608131017qtbase-Windows-Windows_11_24H2-MSVC2022-Windows-Windows_11_24H2-X86_64.7z) | `fd984b7264361b4dd3fd2a417702ca1258e4086268f2ee6a69b9a393d9c3f6bb` |
| svg | [qtsvg](https://download.qt.io/online/qtsdkrepository/windows_x86/desktop/qt6_6112/qt6_6112_msvc2022_64/qt.qt6.6112.win64_msvc2022_64/6.11.2-0-202608131017qtsvg-Windows-Windows_11_24H2-MSVC2022-Windows-Windows_11_24H2-X86_64.7z) | `417f44499c835b2303f3ff78043179bb23442f33d5d2816fbf7e8dbf275b1c89` |
| imageformats | [qtimageformats](https://download.qt.io/online/qtsdkrepository/windows_x86/desktop/qt6_6112/qt6_6112_msvc2022_64/qt.qt6.6112.addons.qtimageformats.win64_msvc2022_64/6.11.2-0-202608131017qtimageformats-Windows-Windows_11_24H2-MSVC2022-Windows-Windows_11_24H2-X86_64.7z) | `d6822c2160ebb675525173deb84bbfb68b4be046ee7f126af74a400a285b933d` |
| pdf | [qtpdf](https://download.qt.io/online/qtsdkrepository/windows_x86/extensions/qtpdf/6112/msvc2022_64/extensions.qtpdf.6112.win64_msvc2022_64/6.11.2-0-202608131017qtpdf-Windows-Windows_11_24H2-MSVC2022-Windows-Windows_11_24H2-X86_64.7z) | `83306fe90cdbb2a365bdea1188be9352772ad8f1aea94ef327758f1635c86908` |
| mesa | [Mesa software OpenGL](https://download.qt.io/online/qtsdkrepository/windows_x86/desktop/qt6_6112/qt6_6112_msvc2022_64/qt.qt6.6112.win64_msvc2022_64/6.11.2-0-202608131017opengl32sw-64-mesa_11_2_2-signed_sha256.7z) | `59a086716cfd4bdec035698d501ba45526f8d2ecb5c0c428a356d8be0b5d2c79` |

The [desktop installer metadata](https://download.qt.io/online/qtsdkrepository/windows_x86/desktop/qt6_6112/qt6_6112_msvc2022_64/Updates.xml)
identifies package version `6.11.2-0-202608131017` and Qt source revision
`713a36536903d172f9e6737584d428753c119496`. The Qt `v6.11.2` tag resolves to
that same commit. Its module revisions include:

| Module | Source commit |
| --- | --- |
| qtbase | `ef55f427f2c8b410d34f8a7681020a3000cf6866` |
| qtsvg | `17ca512f903f935282ebeca496aac5d11ba4199a` |
| qtimageformats | `47b6139dda3b84d1d3ec15caf8d04eff8d744c8d` |
| qtwebengine, including QtPdf | `a33fa2a897e5ee58e385b3f88dc247d99fca56db` |

Use the complete Qt source, including QtPdf's third-party dependencies. A
qtbase-only source archive does not cover this payload. The base and PDF
installer archives contain SPDX/CycloneDX SBOM files under `sbom/`; these
identify additional component versions, licenses and source locations.

## Build inputs

The [Qt CI platform configuration at the matching source commit](https://github.com/qt/qt5/blob/713a36536903d172f9e6737584d428753c119496/coin/platform_configs/cmake_platforms.yaml)
contains the `windows-11_24H2-msvc2022` configuration. It uses the
`qtci-windows-11_24H2-x86_64-73` environment, MSVC2022, packaging, debug and
release builds, `UseConfigure` and SBOM generation/verification. Its configure
arguments are:

```text
-debug-and-release -force-debug-info -headersclean -nomake examples -qt-zlib
```

Its additional CMake arguments enable `FEATURE_msvc_obj_debug_info` and supply
OpenSSL and PostgreSQL roots. Non-qtbase arguments supply `FFMPEG_DIR`, enable
`QT_DEPLOY_FFMPEG`, disable `FEATURE_clangcpp` and enable `INPUT_headersclean`.
Use the linked configuration for its exact environment-variable expansion.

The official base archive provides further build evidence:

- `mkspecs/qconfig.pri`: MSVC 19.44.35227, shared libraries,
  `debug_and_release`, OpenSSL 3 support without linked OpenSSL.
- `mkspecs/qmodule.pri`: enabled and disabled Qt features.
- `lib/cmake/Qt6BuildInternals/QtBuildInternalsExtra.cmake`: Ninja MultiConfig,
  RelWithDebInfo and Debug configurations, shared libraries, tests and examples
  disabled, precompiled headers disabled.
- `sbom/qtbase-6.11.2.spdx.json`: qtbase source revision and third-party inputs.

The source commit's provisioning scripts pin CMake 3.30.5 and Ninja 1.10.2 for
x64. The Ninja provisioning script also modifies its Windows manifest with
`mt`; the upstream executable alone is not the entire provisioned tool input.
The archives do not contain a complete `CMakeCache.txt`, `config.summary` or
original compiler command log.

For a new source build, follow [Qt's Windows build instructions](https://doc.qt.io/qt-6/windows-building.html):
configure a separate build directory, run `cmake --build`, then
`cmake --install`. Pin the source above and record the actual compiler,
dependencies, patches and configure output. Rebuilding the complete shipped
module set also requires QtPdf/QtWebEngine's build prerequisites; the generic
qtbase build instructions alone do not cover those prerequisites.

### Building the Python bindings

PyQt6's source contains `pyproject.toml`, `project.py`, SIP definitions and the
build configuration. Its declared build backend is `sipbuild.api`; supported
build dependency ranges are SIP `>=6.15,<7` and PyQt-builder `>=1.19,<2`.
The installed binding metadata narrows those inputs to SIP 6.15.3 and
PyQt-builder 1.19.1.

[Riverbank's source installation instructions](https://www.riverbankcomputing.com/static/Docs/PyQt6/installation.html)
document `sip-install` and the explicit qmake selection. In an isolated build
environment, install those pinned build tools, unpack the pinned PyQt6 sdist,
and run this from its source directory with the selected Qt development SDK:

```powershell
sip-install --confirm-license --qmake C:\Qt\6.11.0\msvc2022_64\bin\qmake.exe
```

The example selects Qt 6.11.0 to match the original bindings' compile-time
version. This is a documented source-build route, not a command verified here
to reproduce the publisher's wheel. Building against a different Qt SDK must
be recorded and tested as a replacement build. Preserve the selected SDK's
headers, libraries and source route alongside the binding source.

PyQt6-sip 13.12.0 has its own sdist and build configuration; its wheel records
setuptools 83.0.0. It is distinct from the SIP 6.15.3 tool that generated the
PyQt bindings.

PyQt-builder's `pyqtbuild/bundle/qt_wheel.py` and `qt_wheel_main.py` implement
`pyqt-qt-wheel --qt-dir <installed-Qt> PyQt6`. This command packages an existing
Qt installation; it does not compile Qt. Its options cover MSVC runtime and
OpenSSL inclusion, excluded modules and subwheels. The Qt wheel's generator
string alone does not establish which PyQt-builder version packaged it.

The same-publisher PyQt source and its build scripts are available. No missing
PyQt source patch was identified. The original compiler invocation and any
unrecorded local publisher changes have not been independently established;
that limits reproduction claims and does not by itself establish a missing
source obligation or require a different UI library.

## Software OpenGL and other dependencies

`opengl32sw.dll` identifies Mesa 11.2.2 and LLVM 3.6.2 in its embedded strings.
It is a separate component, not source supplied by qtbase. Qt's
[Mesa provisioning script](https://github.com/qt/qt5/blob/713a36536903d172f9e6737584d428753c119496/coin/provisioning/common/windows/mesa_llvmpipe.ps1)
references an [older prebuilt archive](https://download.qt.io/development_releases/prebuilt/llvmpipe/windows/opengl32sw-64-mesa_11_2_2-signed_sha256.7z)
with SHA-1 `58f948746696b17a594b2f542e87b0e831b28dc3`. The installer archive
listed above establishes the inspected DLL's binary origin. The original
Mesa/LLVM patch set and complete build recipe were not recovered.

The upstream source archives were downloaded and hash-checked:

| Source | SHA-256 |
| --- | --- |
| [Mesa 11.2.2](https://archive.mesa3d.org/older-versions/11.x/11.2.2/mesa-11.2.2.tar.xz) | `40e148812388ec7c6d7b6657d5a16e2e8dabba8b97ddfceea5197947647bdfb4` |
| [LLVM 3.6.2](https://releases.llvm.org/3.6.2/llvm-3.6.2.src.tar.xz) | `f60dc158bfda6822de167e87275848969f0558b3134892ff54fced87e4667b94` |

The source archives contain their build scripts. Qt's [llvmpipe build guide](https://wiki.qt.io/MesaLlvmpipe) describes the Windows build route, but its current instructions target newer versions and are not evidence of this binary's original flags. Source notices are retained under `licenses/native/` in the application payload. The source map there documents their coverage and limits.

Unknown historical compiler flags are a reproducibility
limit; they do not by themselves establish a release-blocking source
requirement for permissively licensed code. This document makes no blanket
license-compliance determination.

The payload also includes MSVC runtime DLLs. Those have a separate
redistribution/license route and are not covered by the Qt source archive.
Qt SBOMs and source license files must also be checked for the actual shipped
third-party components, including QtPdf dependencies.

## Verified DLL mapping

Paths are relative to the packaged `PyQt6/Qt6` directory. Archive IDs refer to
the table above. Each row matched the downloaded archive payload byte for byte.

| Path | Archive | SHA-256 |
| --- | --- | --- |
| `bin/Qt6Core.dll` | base | `22c113a5644b875b2c85482723cd5d2e6a3a4029169778fcc78adde08ef3b6aa` |
| `bin/Qt6Gui.dll` | base | `19265a4e76efc12ef5f5bcf59be6c6a5b9b59ec4c89660797738bd2da7f719cc` |
| `bin/Qt6Network.dll` | base | `75ff0896a618f4f5b602f391eb4917a55fd738b2e751d2cf18244a0de3619184` |
| `bin/Qt6Pdf.dll` | pdf | `c68882fca099e0647455e8612f6e8e28f584badd58977553687ea6b9ebea86bb` |
| `bin/Qt6Svg.dll` | svg | `1052c17a9df0eb82023e0e4c369bbd0af58e4c5fea3c27a4b5ee83b60c015492` |
| `bin/Qt6Widgets.dll` | base | `e6d0b7e2e697de6ad985b97f9ed8b381c1c2172fcae87b6ab08455b3b7330f32` |
| `bin/opengl32sw.dll` | mesa | `b04de4541863bc7d8879040a78889c4849c1b1da2784c4630f734c146c2998ce` |
| `plugins/generic/qtuiotouchplugin.dll` | base | `a0b971771417d60c6c2dedf66d06f6e99bd3236aa1aa6949c7e4e08d51fb69f9` |
| `plugins/iconengines/qsvgicon.dll` | svg | `7879ba1d2f293ad09c515de736add8c1023b0a4e80e2301d1283555efc2e8828` |
| `plugins/imageformats/qgif.dll` | base | `de1c1ea34cbc4156ce452023470e2145af4855128756d081d56e9a0997d0dda8` |
| `plugins/imageformats/qicns.dll` | imageformats | `2a5651332d5bc3d47bd171ad68feb5bb9d14471cf9223227cac557706df11771` |
| `plugins/imageformats/qico.dll` | base | `af98cacb134525fab15cf2146a4e6b90266f14493e72e4d51df89a5caadea157` |
| `plugins/imageformats/qjpeg.dll` | base | `a8854ac38d2bb83e8018bdcbfc93420a876251b3d2a79933e1a3c9a8ed2a24cb` |
| `plugins/imageformats/qpdf.dll` | pdf | `3983e4dd27ef4cedfec35212eb8e1a28a38f1b909bcadbe040776d15ca836ffc` |
| `plugins/imageformats/qsvg.dll` | svg | `486b395e23e258d03d54cdba045f129e9b1321f9ff12f99fe58293e0351b8caf` |
| `plugins/imageformats/qtga.dll` | imageformats | `a9a4e7031f7dc4b57888dd2a7571c01091fa291ba99dc9344957b84a24c747a2` |
| `plugins/imageformats/qtiff.dll` | imageformats | `000a6ed1cd0b9f0fea0e2778c1ff42064aa1c68bb6dd40a3bbd6fda8afe3ba77` |
| `plugins/imageformats/qwbmp.dll` | imageformats | `b3bfd939b254b6159e04e5043e348d0be3b8d2c8da8301ec5f8cc7276a9ef479` |
| `plugins/imageformats/qwebp.dll` | imageformats | `8f8f6614fa95ed25aee9e7f7cde64716171028f7cd06cce9b1ad5854e13446ed` |
| `plugins/networkinformation/qnetworklistmanager.dll` | base | `2f7c43bb4ec05e9f23a326ac69e84a6df071cdf7cc0bf87cde98f81958ad9bae` |
| `plugins/platforms/qminimal.dll` | base | `3a41a14b6f168db68fb441621a787f894b275e57f667d0b00e3b1e7fa6952fe9` |
| `plugins/platforms/qoffscreen.dll` | base | `95391476bba1d0b50cc822b93d7509341024cc7c18cd615f82e471a3e3a61caa` |
| `plugins/platforms/qwindows.dll` | base | `d94c584a18829d11fbf3aefd07e7b1e189e0cf063a0515826960037efa7465df` |
| `plugins/styles/qmodernwindowsstyle.dll` | base | `f6b88d403d7175f5b830d4958264690e5c8bbaf0148e1ccf42d52704b0480792` |
| `plugins/tls/qcertonlybackend.dll` | base | `ff0f1a60ec60fc23ec74ff5d325ddef05c1ed68fc89466bffc1d874c7c544adb` |
| `plugins/tls/qopensslbackend.dll` | base | `0248155d03aa5722e2c644393d1cc7ff1ada42a6ddd9808db7a0823d55f87adf` |
| `plugins/tls/qschannelbackend.dll` | base | `761178263b47a5f05e151806bb5854d9e5c9cbc67d08187724d80f9e317471f8` |
