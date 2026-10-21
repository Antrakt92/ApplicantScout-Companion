# Native library notices

These files preserve licenses and attribution for the native libraries bundled
with the Windows application. They supplement the Python packages' own license
files. Build tooling copies this directory into the packaged `licenses/` tree.

| Files | Source and scope |
| --- | --- |
| `Mesa-11.2.2-LICENSE.txt` | Default MIT license and component table from `docs/license.html`. |
| `Mesa-11.2.2-COMPONENT-NOTICES.txt` | Copyright and permission blocks from Mesa's Windows software-renderer source components and headers. Includes the generated Bison parsers' skeleton exception, C11 thread emulation's Boost terms, and component-specific attributions. |
| `LLVM-3.6.2-LICENSE.txt` | Upstream `LICENSE.TXT`: University of Illinois/NCSA license. |
| `LLVM-3.6.2-ADDITIONAL-NOTICES.txt` | Support library attribution, OpenBSD regex notices, Todd Miller's string-copy code, Solar Designer's MD5 implementation, Unicode conversion code, and the ARM contribution grant. |
| `libiconv-1.14-COPYING.LIB.txt` | Upstream Library GPL version 2 license text. |
| `libiconv-1.14-NOTICES.txt` | GNU library copyright and version-2-or-later notices from the conversion, relocation and locale-support sources. |
| `Qt-6.12.0-NOTICES.txt` | 89 source attribution records from QtBase, QtSvg and QtImageFormats, with their 50 referenced license files. This conservatively includes optional platform, test and build components. |
| `QtForPython-LICENSE-LGPL-3.0-only.txt`, `QtForPython-LICENSE-GPL-2.0-only.txt`, `QtForPython-LICENSE-GPL-3.0-only.txt` | Verbatim open-source license alternatives from the verified Qt for Python 6.12.0 source archive's `LICENSES/` directory; apply to the PySide6 and Shiboken wheels as their metadata states. |

The upstream archives used for these notices are:

| Archive | SHA-256 |
| --- | --- |
| [Mesa 11.2.2](https://archive.mesa3d.org/older-versions/11.x/11.2.2/mesa-11.2.2.tar.xz) | `40e148812388ec7c6d7b6657d5a16e2e8dabba8b97ddfceea5197947647bdfb4` |
| [LLVM 3.6.2](https://releases.llvm.org/3.6.2/llvm-3.6.2.src.tar.xz) | `f60dc158bfda6822de167e87275848969f0558b3134892ff54fced87e4667b94` |
| [GNU libiconv 1.14](https://ftp.gnu.org/pub/gnu/libiconv/libiconv-1.14.tar.gz) | `72b24ded17d687193c3366d0ebe7cde1e6b18f0df8c55438ac95be39e8a30613` |
| [Qt 6.12.0](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz) | `98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165` |
| [Qt for Python 6.12.0](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz) | `099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751` |

The Mesa collection follows the source groups referenced by
`src/gallium/targets/libgl-gdi/SConscript` and `src/mesa/SConscript`: Mesa core,
GLSL/compiler, GL dispatch, utilities, Gallium auxiliary code, llvmpipe,
softpipe, trace/rbug, WGL and software window-system support. It also retains
notices from adjacent headers and optional code in those groups. Listed source
paths identify each notice's origin; they are not a claim that every file was
linked into the shipped DLL. The ARM grant is retained on the same conservative
basis. LLVM's build tools and unit-test licenses are outside this runtime
collection.

The component collections contain license comments, not implementation code.
Identical comment blocks within each collection group are combined, with their
source paths listed. Notice wording is preserved; trailing whitespace and line
endings are normalized, and legacy text encodings are represented as UTF-8. The Bison exception stays alongside
its license notice. Mesa's historical `docs/COPYING` is not used to assign a
single license to the whole renderer.

The Qt collection preserves the source `qt_attribution.json` records from
QtBase, QtSvg and QtImageFormats and each referenced license file. It includes
optional platforms, test support and build tooling conservatively; it does not
claim that all source components were linked into the shipped libraries.
The general LGPL license in the Qt wheel does not replace component notices.
The binary and source mapping is documented in
[Native Qt sources](../../docs/NATIVE-QT-SOURCES.md).

Qt PDF and Qt WebEngine are excluded from this application payload. Their
historical notices remain available in the matching older source tags.

This software uses the [FreeType Project](https://www.freetype.org/).
This software is based in part on the work of the Independent JPEG Group.

These notices do not establish corresponding-source access or a reproduced
vendor build. In particular, the GNU libiconv source notices do not resolve the
missing patched sources for its historical Windows DLL. See the accompanying
native source documentation for binary hashes and remaining evidence limits.
