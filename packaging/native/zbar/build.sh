#!/bin/sh
set -eu

export DEBIAN_FRONTEND=noninteractive
sed -i 's|http://deb.debian.org/debian|https://deb.debian.org/debian|g' /etc/apt/sources.list.d/debian.sources
apt-get update -qq
apt-get -q --no-install-recommends install -y \
    gcc-mingw-w64-x86-64-win32=14.2.0-19+27+b1 \
    g++-mingw-w64-x86-64-win32=14.2.0-19+27+b1 \
    binutils-mingw-w64-x86-64=2.44-3+12+b1 \
    mingw-w64-common=12.0.0-5 mingw-w64-x86-64-dev=12.0.0-5 \
    meson=1.7.0-1 ninja-build=1.12.1-1 > /output/toolchain-install.log

python -I - <<'PY'
import hashlib
from pathlib import Path
from urllib.request import urlopen

url = "https://codeload.github.com/mchehab/zbar/tar.gz/dc19fcc7da58e8cf84ba1ec417e24b04609feb23"
expected = "ad6949bfa9287cba1dc2f4b4ed8c1402ceec3feffe8a74dc8d1c76e3d45f24fa"
with urlopen(url, timeout=45) as response:
    archive = response.read(16 * 1024 * 1024 + 1)
if hashlib.sha256(archive).hexdigest() != expected:
    raise SystemExit("ZBar source archive hash mismatch")
Path("/tmp/zbar-source.tar.gz").write_bytes(archive)
PY

mkdir /tmp/zbar
tar -xzf /tmp/zbar-source.tar.gz -C /tmp/zbar --strip-components=1
python -I /recipe/prepare-zbar.py
cp /libiconv/generated/iconv.h /usr/x86_64-w64-mingw32/include/iconv.h
x86_64-w64-mingw32-dlltool -D libiconv.dll -d /libiconv/libiconv.def \
    -l /usr/x86_64-w64-mingw32/lib/libiconv.dll.a

cat > /tmp/windows.ini <<'EOF'
[binaries]
c = 'x86_64-w64-mingw32-gcc-win32'
cpp = 'x86_64-w64-mingw32-g++-win32'
ar = 'x86_64-w64-mingw32-ar'
strip = 'x86_64-w64-mingw32-strip'
windres = 'x86_64-w64-mingw32-windres'

[host_machine]
system = 'windows'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'

[built-in options]
c_link_args = ['-static-libgcc', '-Wl,--no-insert-timestamp', '/usr/x86_64-w64-mingw32/lib/libwinpthread.a']
EOF

meson setup /tmp/out /tmp/zbar --cross-file /tmp/windows.ini --wrap-mode=nodownload --buildtype=release \
    -Dwerror=true -Dcodes=qrcode -Dlibrary_type=shared -Dpthread=false -Dvideo=false -Ddoc=false \
    -Dgtk=no -Dqt=false -Dqt6=false -Djava=disabled -Dgir=false -Dpython=no \
    -Ddbus=disabled -Djpeg=disabled -Dimagemagick=disabled -Dgraphicsmagick=disabled \
    -Dbuild_tests=false -Dnls=disabled -Dx11=disabled -Dxshm=disabled -Dxv=disabled > /output/configure.log
meson compile -C /tmp/out > /output/build.log

# Check the generated configuration and imported libraries before publishing an output.
config=/tmp/out/config.h
test "$(grep -E '^#define ENABLE_[A-Z0-9_]+ 1$' "$config")" = '#define ENABLE_QRCODE 1'
dll=/tmp/out/zbar/libzbar-0.dll
x86_64-w64-mingw32-objdump -p "$dll" | awk '/DLL Name:/ {print tolower($3)}' | sort > /output/imports.txt
printf '%s\n' gdi32.dll kernel32.dll libiconv.dll msvcrt.dll user32.dll | cmp - /output/imports.txt

cp "$config" /output/config.h
cp "$dll" /output/libzbar-64.dll
sha256sum /output/libzbar-64.dll
dpkg-query -W gcc-mingw-w64-x86-64-win32 g++-mingw-w64-x86-64-win32 \
    binutils-mingw-w64-x86-64 mingw-w64-x86-64-dev meson ninja-build > /output/toolchain.txt
