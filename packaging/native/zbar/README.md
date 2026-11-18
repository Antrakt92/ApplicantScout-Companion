# ZBar 0.23.95 Windows QR decoder

ApplicantScout packages this x86-64 DLL in the location expected by pyzbar. The build enables QR decoding and excludes all other symbol decoders. It uses the I/O-independent scanner API and links to the controlled `libiconv.dll` in the same application directory.

- [Pinned ZBar source](https://codeload.github.com/mchehab/zbar/tar.gz/dc19fcc7da58e8cf84ba1ec417e24b04609feb23) has SHA-256 `ad6949bfa9287cba1dc2f4b4ed8c1402ceec3feffe8a74dc8d1c76e3d45f24fa`.
- `prepare-zbar.py` applies the Windows x64 pointer-width and iconv input-type adjustments to that exact source.
- The recipe uses Meson 1.7.0, Ninja 1.12.1 and the Debian Windows x64 MinGW packages pinned in `build.sh`. It statically links MinGW's Winpthreads clock support from `mingw-w64-x86-64-dev` 12.0.0-5.
- The retained DLL has SHA-256 `c444e165a20cb72e545008f311378a4e830bf0e950141a39369cc044e7a40feb`.

## Rebuild

From the repository root, run this in PowerShell with Docker in Linux container mode. The recipe writes its DLL and configure, compiler and import reports into a new temporary directory.

```powershell
$zbarRecipe = (Resolve-Path packaging/native/zbar).Path
$zbarIconv = (Resolve-Path packaging/native/libiconv).Path
$zbarOutput = Join-Path ([IO.Path]::GetTempPath()) ("applicantscout-zbar-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $zbarOutput | Out-Null
docker run --rm --platform linux/amd64 `
    --mount "type=bind,source=$zbarRecipe,target=/recipe,readonly" `
    --mount "type=bind,source=$zbarIconv,target=/libiconv,readonly" `
    --mount "type=bind,source=$zbarOutput,target=/output" `
    docker.io/library/python@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f sh /recipe/build.sh
```

The recipe checks that only QR decoding is enabled and that the DLL imports only the expected Windows system libraries and `libiconv.dll`. Compare its output with the retained DLL and run the Windows decoder smoke before updating the recorded hash.

```powershell
.\.venv\Scripts\python scripts/smoke_native_decoder.py `
    --libiconv packaging/native/libiconv/libiconv.dll `
    --zbar (Join-Path $zbarOutput libzbar-64.dll)
```

Source and license records for ZBar and the linked native components are listed in [the native source guide](../../../docs/NATIVE-SOURCES.md) and included in the application's license directory.
