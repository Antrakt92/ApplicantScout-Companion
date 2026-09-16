from __future__ import annotations

import importlib.util
from importlib import metadata
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_libiconv.py"
spec = importlib.util.spec_from_file_location("prepare_libiconv", SCRIPT)
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def templates():
    config = "/* Preserve upstream notice. */\n" + "".join(
        f"#undef {name}\n" for name in (
            "EILSEQ", "ICONV_CONST", "ENABLE_EXTRA", "ENABLE_RELOCATABLE", "ENABLE_NLS",
            "HAVE_WCHAR_T", "HAVE_MBSTATE_T", "HAVE_MBRTOWC", "HAVE_WCRTOMB", "HAVE_LANGINFO_CODESET",
        )
    )
    iconv = (
        "/* Preserve upstream notice. */\n"
        "extern @DLL_VARIABLE@ int _libiconv_version;\n"
        "#ifndef EILSEQ\n#define EILSEQ @EILSEQ@\n#endif\n"
        "size_t iconv(iconv_t, @ICONV_CONST@ char **, size_t *, char **, size_t *);\n"
        "#if @USE_MBSTATE_T@\n#endif\n#if @USE_MBSTATE_T@\n#endif\n"
        "#if @BROKEN_WCHAR_H@\n#endif\n#if @HAVE_WCHAR_T@\n#endif\n"
    )
    return config, iconv, "/* charset notice */\nextern const char *locale_charset(void);\n"


def test_headers_preserve_notices_and_crt_errno_without_source_edits(templates):
    headers = builder.generated_headers(*templates)
    assert headers["config.h"].startswith("/* Preserve upstream notice. */")
    assert "#undef EILSEQ" not in headers["config.h"]
    assert "#define ICONV_CONST const" in headers["config.h"]
    assert '#error "MSVC errno.h must define EILSEQ"' in headers["iconv.h"]
    assert "#define EILSEQ" not in headers["iconv.h"]
    assert "const char **" in headers["iconv.h"]
    assert headers["localcharset.h"] == templates[2]
    assert headers["configmake.h"] == '#define LIBDIR ""\n'


@pytest.mark.parametrize("change", ["missing", "duplicate", "new_token"])
def test_template_drift_is_rejected_instead_of_silently_generating_headers(templates, change):
    config, iconv, charset = templates
    if change == "missing":
        config = config.replace("#undef ICONV_CONST\n", "")
    elif change == "duplicate":
        iconv += "@ICONV_CONST@\n"
    else:
        charset += "@NEW_AUTOCONF_OPTION@\n"
    with pytest.raises(builder.PreparationError):
        builder.generated_headers(config, iconv, charset)


def tar_bytes(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "../../outside"
            member.size = 0
            archive.addfile(member, io.BytesIO())
    return data.getvalue()


@pytest.mark.parametrize("name,kind", [
    ("../outside", tarfile.REGTYPE),
    ("libiconv-1.14/../../outside", tarfile.REGTYPE),
    ("/libiconv-1.14/absolute", tarfile.REGTYPE),
    ("libiconv-1.14/drive:stream", tarfile.REGTYPE),
    ("libiconv-1.14/path\\escape", tarfile.REGTYPE),
    ("libiconv-1.14/CON.txt", tarfile.REGTYPE),
    ("libiconv-1.14/dot.", tarfile.REGTYPE),
    ("libiconv-1.14/link", tarfile.SYMTYPE),
    ("libiconv-1.14/hardlink", tarfile.LNKTYPE),
])
def test_archive_rejects_unsafe_windows_paths_and_links(name, kind):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes([(name, kind)]))) as archive:
        with pytest.raises(builder.PreparationError, match="unsafe"):
            builder.checked_members(archive)


@pytest.mark.parametrize("entries", [
    [("libiconv-1.14/A", tarfile.REGTYPE), ("libiconv-1.14/a", tarfile.REGTYPE)],
    [("libiconv-1.14/a", tarfile.REGTYPE), ("libiconv-1.14/a/b", tarfile.REGTYPE)],
])
def test_archive_rejects_case_collisions_and_files_as_parents(entries):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes(entries))) as archive:
        with pytest.raises(builder.PreparationError):
            builder.checked_members(archive)


def test_wrong_source_hash_leaves_output_absent(tmp_path):
    output = tmp_path / "candidate"
    with pytest.raises(builder.PreparationError, match="SHA-256"):
        builder.prepare(b"wrong source", output)
    assert not output.exists()


def test_candidate_path_cannot_overwrite_current_package_or_old_experiment(tmp_path):
    for requested in (Path(".venv/Lib/site-packages/pyzbar"), Path("dist"), tmp_path.parent / "outside"):
        with pytest.raises(builder.PreparationError, match="under"):
            builder.candidate_output(requested, tmp_path)
    output = tmp_path / "build/native-decoder"
    output.mkdir(parents=True)
    (output / "existing.dll").write_bytes(b"preserve")
    with pytest.raises(builder.PreparationError, match="already exists"):
        builder.candidate_output(output, tmp_path)
    assert (output / "existing.dll").read_bytes() == b"preserve"


def test_fresh_candidate_child_is_allowed(tmp_path):
    assert builder.candidate_output(Path("build/native-decoder/experiment-2"), tmp_path) == (
        tmp_path / "build/native-decoder/experiment-2"
    )


def test_preparation_retains_stock_c_and_records_generated_inputs(tmp_path, monkeypatch, templates):
    contents = {
        "config.h.in": templates[0].encode(),
        "include/iconv.h.in": templates[1].encode(),
        "libcharset/include/localcharset.h.in": templates[2].encode(),
        "COPYING": b"upstream license fixture\n",
        "COPYING.LIB": b"upstream library license fixture\n",
        "lib/iconv.c": b"/* stock conversion implementation */\n",
        "libcharset/lib/localcharset.c": b'#include "configmake.h"\n',
        "lib/relocatable.c": b"/* stock relocation implementation */\n",
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, value in contents.items():
            member = tarfile.TarInfo(f"libiconv-1.14/{name}")
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    payload = data.getvalue()
    monkeypatch.setattr(builder, "SOURCE_SHA256", hashlib.sha256(payload).hexdigest())
    output = tmp_path / "candidate"
    builder.prepare(payload, output)
    identity = json.loads((output / "artifact/source-identity.json").read_text())
    for name in builder.SOURCES:
        assert (output / "source/libiconv-1.14" / name).read_bytes() == contents[name]
        assert identity["source_files"][name] == hashlib.sha256(contents[name]).hexdigest()
    assert identity["implementation_patches"] == []
    for name, digest in identity["generated_files"].items():
        assert hashlib.sha256((output / "artifact" / name).read_bytes()).hexdigest() == digest
    assert (output / "artifact/licenses/libiconv/COPYING.LIB").read_bytes() == contents["COPYING.LIB"]
    assert "_libiconv_version @1 DATA" in (output / "artifact/libiconv.def").read_text()
    with pytest.raises(builder.PreparationError, match="already exists"):
        builder.prepare(payload, output)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows application lookup contract")
def test_build_resolvers_select_one_executable_when_path_contains_two(tmp_path):
    pwsh = shutil.which("pwsh")
    assert pwsh is not None, "PowerShell 7 is required for the Windows build regression"
    first = tmp_path / "first tools"
    second = tmp_path / "second tools"
    for directory in (first, second):
        directory.mkdir()
        for filename in ("python.exe", "cl.exe", "link.exe", "dumpbin.exe"):
            (directory / filename).write_bytes(b"not executed: command lookup fixture")
    probe = tmp_path / "resolve.ps1"
    probe.write_text(r'''
param([string]$BuildScript, [string]$FirstDir, [string]$SecondDir)
$ErrorActionPreference = "Stop"
$env:PATH = $FirstDir + [IO.Path]::PathSeparator + $SecondDir
$env:PATHEXT = ".EXE"
$Tokens = $null
$ParseErrors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($BuildScript, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count) { throw "Build script has parse errors." }
$Python = "python"
$Resolved = @{}
foreach ($Name in @("PythonCommand", "Compiler", "Linker", "Dumpbin")) {
    $Assignments = @($Ast.FindAll({
        param($Node)
        $Node -is [Management.Automation.Language.AssignmentStatementAst] -and
            $Node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
            $Node.Left.VariablePath.UserPath -eq $Name
    }, $true))
    if ($Assignments.Count -ne 1) { throw "Expected one resolver assignment for $Name." }
    # Execute the real assignment, not a test copy of its Get-Command pipeline.
    . ([scriptblock]::Create($Assignments[0].Extent.Text))
    $Value = Get-Variable -Name $Name -ValueOnly
    if ($Value -isnot [string]) { throw "$Name resolved to multiple commands instead of one string." }
    $Resolved[$Name] = $Value
}
$AllPython = @(Get-Command python -CommandType Application -ErrorAction Stop)
if ($AllPython.Count -ne 2) { throw "Fixture did not create two Python application matches." }
$Resolved | ConvertTo-Json -Compress
''', encoding="utf-8")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(probe), str(SCRIPT.with_name("build-libiconv.ps1")),
         str(first), str(second)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    resolved = json.loads(result.stdout)
    assert resolved == {
        name: str(first / filename) for name, filename in (
            ("PythonCommand", "python.exe"), ("Compiler", "cl.exe"),
            ("Linker", "link.exe"), ("Dumpbin", "dumpbin.exe"),
        )
    }


@pytest.fixture
def retained_build():
    directory = SCRIPT.parents[1] / "packaging/native/libiconv"
    receipt = json.loads((directory / "build-receipt.json").read_text(encoding="utf-8"))
    return directory, receipt


def test_retained_build_files_match_receipt_bytes_and_sizes(retained_build):
    directory, receipt = retained_build
    expected = {
        "COPYING", "COPYING.LIB", "generated/config.h", "generated/configmake.h",
        "generated/iconv.h", "generated/localcharset.h", "libiconv-1.14.tar.gz",
        "libiconv.def", "libiconv.dll",
    }
    assert set(receipt["retained_files"]) == expected
    for name, identity in receipt["retained_files"].items():
        data = (directory / name).read_bytes()
        assert len(data) == identity["size_bytes"], name
        assert hashlib.sha256(data).hexdigest() == identity["sha256"], name
    for section, path_key in (("binary", "path"), ("source", "archive")):
        identity = receipt[section]
        retained = receipt["retained_files"][identity[path_key]]
        assert identity["sha256"] == retained["sha256"]
        assert identity["size_bytes"] == retained["size_bytes"]


def test_retained_headers_and_export_definition_reproduce_from_pinned_source(retained_build):
    directory, receipt = retained_build
    source = receipt["source"]
    assert receipt["version"] == "1.14"
    assert source["url"] == builder.SOURCE_URL
    data = (directory / source["archive"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == source["sha256"] == builder.SOURCE_SHA256
    assert source["implementation_patches"] == []
    assert set(source["compiled_source_files"]) == set(builder.SOURCES)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        builder.checked_members(archive)

        def upstream(name):
            stream = archive.extractfile(f"{builder.SOURCE_ROOT}/{name}")
            assert stream is not None, name
            return stream.read()

        for name, digest in source["compiled_source_files"].items():
            assert hashlib.sha256(upstream(name)).hexdigest() == digest, name
        for name in ("COPYING", "COPYING.LIB"):
            assert (directory / name).read_bytes() == upstream(name)
        generated = builder.generated_headers(
            upstream("config.h.in").decode("utf-8"),
            upstream("include/iconv.h.in").decode("utf-8"),
            upstream("libcharset/include/localcharset.h.in").decode("utf-8"),
        )
    assert set(generated) == {"config.h", "configmake.h", "iconv.h", "localcharset.h"}
    for name, text in generated.items():
        assert (directory / "generated" / name).read_bytes() == text.encode("utf-8"), name
    definition = "LIBRARY libiconv\nEXPORTS\n" + "".join(
        f"    {entry}\n" for entry in builder.EXPORTS
    )
    assert (directory / "libiconv.def").read_bytes() == definition.encode("ascii")


def test_retained_build_recipe_matches_current_code_after_checkout_eol_normalization(retained_build):
    _, receipt = retained_build
    recipes = receipt["build"]["recipe_files"]
    assert set(recipes) == {"scripts/build-libiconv.ps1", "scripts/prepare_libiconv.py"}
    for name, identity in recipes.items():
        normalized = (SCRIPT.parents[1] / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(normalized).hexdigest() == identity["lf_normalized_sha256"], (
            f"{name} changed after the retained DLL build; review/rebuild and update its evidence"
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Pinned pyzbar Windows binary ABI")
def test_retained_dll_preserves_the_pinned_zbar_import_abi(retained_build):
    # pefile is already a pinned Windows build dependency; this never loads DLLs.
    import pefile

    directory, receipt = retained_build
    distribution = metadata.distribution("pyzbar")
    zbar_path = Path(distribution.locate_file("pyzbar/libzbar-64.dll"))
    zbar_data = zbar_path.read_bytes()
    assert hashlib.sha256(zbar_data).hexdigest() == receipt["validation"]["zbar_sha256"]
    with pefile.PE(data=zbar_data) as zbar, pefile.PE(data=(directory / "libiconv.dll").read_bytes()) as iconv:
        assert zbar.FILE_HEADER.Machine == iconv.FILE_HEADER.Machine == 0x8664
        imports = [entry for entry in zbar.DIRECTORY_ENTRY_IMPORT if entry.dll.lower() == b"libiconv.dll"]
        assert len(imports) == 1
        required = {symbol.name for symbol in imports[0].imports}
        assert required == {b"libiconv", b"libiconv_open", b"libiconv_close"}
        exports = {symbol.name: symbol for symbol in iconv.DIRECTORY_ENTRY_EXPORT.symbols}
        assert {name: symbol.ordinal for name, symbol in exports.items()} == {
            b"_libiconv_version": 1, b"iconv_canonicalize": 2, b"libiconv": 3,
            b"libiconv_close": 4, b"libiconv_open": 5, b"libiconv_open_into": 6,
            b"libiconvctl": 7, b"libiconvlist": 8, b"locale_charset": 9,
        }
        assert required <= exports.keys()
        assert all(symbol.forwarder is None for symbol in exports.values())
        assert {entry.dll.lower() for entry in iconv.DIRECTORY_ENTRY_IMPORT} == {b"kernel32.dll"}
        version_rva = exports[b"_libiconv_version"].address
        assert struct.unpack("<I", iconv.get_data(version_rva, 4))[0] == 0x010E
        section = iconv.get_section_by_rva(version_rva)
        assert section is not None and not section.Characteristics & 0x20000000
