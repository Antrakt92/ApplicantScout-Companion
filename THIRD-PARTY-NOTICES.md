# Third-Party Notices

ApplicantScout Companion source code is distributed under the MIT License.
Windows builds bundle third-party Python packages, native libraries, and Qt
runtime files. Their licenses remain with their respective copyright holders.

Release builds copy dependency license files from the active Python environment
into the bundled `licenses/` directory. Treat that directory as part of the
portable ZIP and installer payload. A dependency that exposes no license-like
file fails the release build unless a reviewed override is bound to its exact
installed version, an HTTPS provenance source, a rationale, and a tracked
non-empty notice. Missing-license placeholders are never accepted as release
license coverage.

## Key Runtime Dependencies

| Component | Purpose | License surface |
|---|---|---|
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
