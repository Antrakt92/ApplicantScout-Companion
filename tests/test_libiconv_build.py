from __future__ import annotations

import importlib.util
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
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
