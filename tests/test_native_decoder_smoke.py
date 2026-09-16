from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/smoke_native_decoder.py"
spec = importlib.util.spec_from_file_location("native_decoder_smoke", SCRIPT)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.fixture
def inputs(tmp_path):
    directory = tmp_path / "space & Кириллица ' paths"
    directory.mkdir()
    paths = [directory / name for name in ("candidate.dll", "zbar.dll", "qrencode.lua", "lua.exe")]
    for index, path in enumerate(paths):
        path.write_bytes(f"fixture {index}".encode())
    return paths


@pytest.fixture
def isolated_run(monkeypatch):
    # The unit-level subprocess double never loads these deliberately fake DLLs.
    monkeypatch.setattr(smoke, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(smoke, "render_cases", lambda *_args: None)
    seen = []

    def run(command, **kwargs):
        assert isinstance(command, list)
        assert command[:2] == [sys.executable, "-I"]
        assert command[2:4] == [str(SCRIPT), "--child-stage"]
        assert kwargs.get("shell", False) is False
        assert kwargs["timeout"] == 120
        stage = Path(command[4])
        assert kwargs["cwd"] == stage
        result = {"passed": True, "loaded": {
            name: {"path": str(stage / name), "sha256": smoke.digest(stage / name)}
            for name in ("libiconv.dll", "libzbar-64.dll")}, "conversions": [], "qr": []}
        seen.append((stage, result))
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    return seen, run


def test_isolated_staging_preserves_inputs_and_cleans_temporary_files(inputs, isolated_run):
    before = [path.read_bytes() for path in inputs]
    result = smoke.run_smoke(*inputs)
    assert result["passed"] is True
    assert len(result["runs"]) == 2
    seen, _ = isolated_run
    assert len(seen) == 2
    assert all(not stage.exists() for stage, _ in seen)
    assert [path.read_bytes() for path in inputs] == before


@pytest.mark.parametrize("index", range(4))
def test_missing_input_rejected_before_subprocess(inputs, index, isolated_run):
    inputs[index].unlink()
    with pytest.raises(FileNotFoundError):
        smoke.run_smoke(*inputs)
    assert not isolated_run[0]


@pytest.mark.parametrize("index", range(4))
def test_empty_input_rejected(inputs, index, isolated_run):
    inputs[index].write_bytes(b"")
    with pytest.raises(smoke.SmokeError, match="nonempty file"):
        smoke.run_smoke(*inputs)
    assert not isolated_run[0]


def test_same_library_input_rejected(inputs, isolated_run):
    inputs[1] = inputs[0]
    with pytest.raises(smoke.SmokeError, match="different files"):
        smoke.run_smoke(*inputs)
    assert not isolated_run[0]


def test_preloaded_iconv_fails_before_loading_candidate(tmp_path, monkeypatch):
    def handle(_name):
        return 123

    fake = SimpleNamespace(
        sizeof=lambda _type: 8, c_void_p=object,
        c_wchar_p=object, WinDLL=lambda *_args, **_kwargs: SimpleNamespace(GetModuleHandleW=handle),
        CDLL=lambda _path: pytest.fail("A preloaded library must stop loading"),
    )
    monkeypatch.setattr(smoke, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(smoke, "ctypes", fake)
    with pytest.raises(smoke.SmokeError, match="Unexpected preloaded libiconv"):
        smoke.load_pair(tmp_path)


def test_module_path_outside_stage_fails(tmp_path, monkeypatch):
    def handle(_name):
        return None

    fake = SimpleNamespace(
        sizeof=lambda _type: 8, c_void_p=object,
        c_wchar_p=object, WinDLL=lambda *_args, **_kwargs: SimpleNamespace(GetModuleHandleW=handle),
        CDLL=lambda _path: SimpleNamespace(_handle=123),
    )
    monkeypatch.setattr(smoke, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(smoke, "ctypes", fake)
    monkeypatch.setattr(smoke, "module_path", lambda _handle: tmp_path / "elsewhere/libiconv.dll")
    with pytest.raises(smoke.SmokeError, match="outside isolated directory"):
        smoke.load_pair(tmp_path)


@pytest.mark.parametrize("runs", (0, 6, -1))
def test_run_count_bounded(inputs, isolated_run, runs):
    with pytest.raises(smoke.SmokeError, match="between 1 and 5"):
        smoke.run_smoke(*inputs, runs=runs)
    assert not isolated_run[0]


@pytest.mark.parametrize("field,value", (("sha256", "0" * 64), ("path", "C:/wrong/libiconv.dll")))
def test_wrong_loaded_library_identity_fails(inputs, isolated_run, monkeypatch, field, value):
    _, original = isolated_run

    def changed(command, **kwargs):
        result = original(command, **kwargs)
        data = json.loads(result.stdout)
        data["loaded"]["libiconv.dll"][field] = value
        result.stdout = json.dumps(data)
        return result

    monkeypatch.setattr(smoke.subprocess, "run", changed)
    with pytest.raises(smoke.SmokeError, match="unexpected libiconv"):
        smoke.run_smoke(*inputs)


@pytest.mark.parametrize("returncode,stdout", ((-1073741819, ""), (1, '{"error":"load failed"}'),
                                              (0, '{"passed":false}'), (0, "[]"), (0, "not JSON")))
def test_crash_and_malformed_child_results_fail(inputs, isolated_run, monkeypatch, returncode, stdout):
    monkeypatch.setattr(smoke.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, returncode, stdout, "native diagnostic"))
    with pytest.raises((smoke.SmokeError, ValueError)):
        smoke.run_smoke(*inputs)
    assert not isolated_run[0]


def test_hung_child_times_out_and_cleans_stage(inputs, isolated_run, monkeypatch):
    stages = []

    def timeout(command, **kwargs):
        stages.append(kwargs["cwd"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(smoke.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        smoke.run_smoke(*inputs)
    assert all(not stage.exists() for stage in stages)
    assert not isolated_run[0]


def test_render_uses_literal_arguments_and_validates_matrix(inputs, tmp_path, monkeypatch):
    from PIL import Image

    def generate(command, **kwargs):
        assert command[0] == str(inputs[3])
        assert command[2] == str(inputs[2])
        assert Path(command[3]).read_bytes() == b"payload\x00bytes"
        assert kwargs.get("shell", False) is False
        return subprocess.CompletedProcess(command, 0, "10\n01\n", "")

    monkeypatch.setattr(smoke, "qr_cases", lambda: [("demo", b"payload\x00bytes", b"expected", "exact")])
    monkeypatch.setattr(smoke.subprocess, "run", generate)
    smoke.render_cases(tmp_path, inputs[3], inputs[2])
    records = json.loads((tmp_path / "qr-cases.json").read_text())
    assert len(records) == 2
    assert records[0]["expected_hex"] == b"expected".hex()
    with Image.open(tmp_path / records[0]["image"]) as image:
        assert image.size == (40, 40)
        assert image.getpixel((16, 16)) == 0
        assert image.getpixel((20, 16)) == 255


@pytest.mark.parametrize("code,stdout", ((1, ""), (0, ""), (0, "12\n00\n"), (0, "111\n00\n")))
def test_bad_lua_output_fails(inputs, tmp_path, monkeypatch, code, stdout):
    monkeypatch.setattr(smoke, "qr_cases", lambda: [("demo", b"x", b"x", "exact")])
    monkeypatch.setattr(smoke.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, code, stdout, "Lua diagnostic"))
    with pytest.raises(smoke.SmokeError, match="QR"):
        smoke.render_cases(tmp_path, inputs[3], inputs[2])


def test_wire_fixtures_parse_as_real_snapshots_and_fragments():
    from applicant_scout.screenshot import _try_parse_appscout_candidate

    cases = smoke.qr_cases()
    wire_cases = [(name, bytes.fromhex(payload.decode())) for name, payload, _, _ in cases if name.startswith("aps-")]
    assert {payload[4] for _, payload in wire_cases} == {8, 9, 10, 11}
    for name, payload in wire_cases:
        result, error = _try_parse_appscout_candidate(payload)
        assert error is None, (name, error)
        assert result is not None
    large, error = _try_parse_appscout_candidate(dict(wire_cases)["aps-large-cyrillic-hex"])
    assert error is None
    assert len(large.applicants) == 24
    assert large.applicants[0].name == "Игрок0-Гордунни"


def test_cli_missing_required_arguments():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "--libiconv and --zbar are required" in result.stderr


def test_cli_missing_file_is_json_failure(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--libiconv", str(tmp_path / "missing & ' .dll"),
                             "--zbar", str(tmp_path / "another.dll")],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert json.loads(result.stdout)["passed"] is False


@pytest.mark.skipif(os.name != "nt", reason="Real DLL baseline needs Windows")
def test_retained_replacement_matches_real_installed_baseline():
    import importlib.metadata

    package = importlib.metadata.distribution("pyzbar")
    iconv = Path(package.locate_file("pyzbar/libiconv.dll"))
    zbar = Path(package.locate_file("pyzbar/libzbar-64.dll"))
    encoder = ROOT.parent / "ApplicantScout-Addon/libs/qrencode.lua"
    lua = shutil.which("lua5.1")
    if not iconv.is_file() or not zbar.is_file() or not encoder.is_file() or lua is None:
        pytest.skip("Installed wheel DLLs, paired addon and Lua 5.1 required")
    before = (smoke.digest(iconv), smoke.digest(zbar))
    report = smoke.run_smoke(iconv, zbar, encoder, Path(lua))
    assert report["passed"] is True
    assert len(report["runs"]) == 2
    assert len(report["runs"][0]["qr"]) >= 20
    assert report["runs"][0]["loaded"]["libiconv.dll"]["sha256"] == before[0]
    assert (smoke.digest(iconv), smoke.digest(zbar)) == before
    replacement = ROOT / "packaging/native/libiconv/libiconv.dll"
    assert replacement.is_file()
    replacement_hash = smoke.digest(replacement)
    rebuilt = smoke.run_smoke(replacement, zbar, encoder, Path(lua))
    assert rebuilt["runs"][0]["conversions"] == report["runs"][0]["conversions"]
    assert rebuilt["runs"][0]["qr"] == report["runs"][0]["qr"]
    assert smoke.digest(replacement) == replacement_hash
    assert (smoke.digest(iconv), smoke.digest(zbar)) == before
