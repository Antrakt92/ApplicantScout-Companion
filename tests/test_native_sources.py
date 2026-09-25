from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_native_sources.py"
spec = importlib.util.spec_from_file_location("native_sources", SCRIPT)
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = checker
spec.loader.exec_module(checker)

BINARY_BYTES = b"native binary fixture"
DIGEST = hashlib.sha256(BINARY_BYTES).hexdigest()


@pytest.fixture
def evidence(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/build.md").write_text("Build the pinned source with the recorded patch.\n", encoding="utf-8")
    return {
        "schema_version": 1,
        "components": [{
            "name": "Demo native library", "package": "Demo_Package", "version": "1.2.3",
            "binaries": [{"path": "demo/native.dll", "sha256": DIGEST}],
            "sources": [{"url": "https://example.org/source-1.2.3.tar.gz", "sha256": "a" * 64}],
            "build_instructions": "docs/build.md", "unresolved": [],
        }],
    }


def validate(evidence, tmp_path, **kwargs):
    return checker.validate_manifest(evidence, {"demo-package": "1.2.3"}, repo_root=tmp_path, **kwargs)


def mock_installed(monkeypatch, tmp_path, *, version="1.2.3", escape=False):
    root = tmp_path / "site-packages"
    (root / "demo").mkdir(parents=True)
    path = root / "demo/native.dll"
    path.write_bytes(BINARY_BYTES)

    class Distribution:
        def locate_file(self, relative):
            if relative and escape:
                return tmp_path / "outside.dll"
            return root / relative

    monkeypatch.setattr(checker.metadata, "version", lambda _package: version)
    monkeypatch.setattr(checker.metadata, "distribution", lambda _package: Distribution())
    return path


def test_complete_evidence_needs_no_environment_or_network(evidence, tmp_path, monkeypatch):
    def forbidden(_package):
        raise AssertionError("metadata must not be read without --installed")

    monkeypatch.setattr(checker.metadata, "version", forbidden)
    monkeypatch.setattr(checker.metadata, "distribution", forbidden)
    assert validate(evidence, tmp_path, require_complete=True) == ()


def test_installed_bytes_and_pin_match(evidence, tmp_path, monkeypatch):
    mock_installed(monkeypatch, tmp_path)
    assert validate(evidence, tmp_path, installed=True, require_complete=True) == ()


@pytest.mark.parametrize("failure", ["modified", "missing", "version", "escape", "distribution"])
def test_installed_mismatch_fails_even_nonstrict(evidence, tmp_path, monkeypatch, failure):
    path = mock_installed(monkeypatch, tmp_path, version="9" if failure == "version" else "1.2.3", escape=failure == "escape")
    if failure == "modified":
        path.write_bytes(BINARY_BYTES + b"changed")
    elif failure == "missing":
        path.unlink()
    elif failure == "distribution":
        def missing(_package):
            raise checker.metadata.PackageNotFoundError("demo-package")
        monkeypatch.setattr(checker.metadata, "version", missing)
    with pytest.raises(checker.NativeSourceError):
        validate(evidence, tmp_path, installed=True)


@pytest.mark.parametrize("failure", ["unresolved", "sources", "null_doc", "absent_doc", "missing_doc", "empty_doc"])
def test_incomplete_research_is_visible_and_blocks_strict(evidence, tmp_path, failure):
    record = evidence["components"][0]
    if failure == "unresolved":
        record["unresolved"] = ["Exact native patches are not identified"]
    elif failure == "sources":
        record["sources"] = []
    elif failure == "null_doc":
        record["build_instructions"] = None
    elif failure == "absent_doc":
        del record["build_instructions"]
    elif failure == "missing_doc":
        (tmp_path / "docs/build.md").unlink()
    else:
        (tmp_path / "docs/build.md").write_text(" \n", encoding="utf-8")
    assert validate(evidence, tmp_path) == ("Demo native library",)
    with pytest.raises(checker.IncompleteSourceError) as caught:
        validate(evidence, tmp_path, require_complete=True)
    assert "Demo native library" in str(caught.value)
    if failure == "unresolved":
        assert "Exact native patches are not identified" in str(caught.value)


def test_repaired_evidence_passes_strict(evidence, tmp_path):
    record = evidence["components"][0]
    source = record["sources"].pop()
    record["unresolved"] = ["Source archive not verified"]
    with pytest.raises(checker.IncompleteSourceError):
        validate(evidence, tmp_path, require_complete=True)
    record["sources"].append(source)
    record["unresolved"] = []
    assert validate(evidence, tmp_path, require_complete=True) == ()


def test_multiple_components_can_share_package_and_source(evidence, tmp_path):
    other = copy.deepcopy(evidence["components"][0])
    other["name"] = "Second native library"
    other["binaries"][0]["path"] = "demo/second.dll"
    evidence["components"].append(other)
    assert validate(evidence, tmp_path, require_complete=True) == ()


def test_build_doc_symlink_may_not_escape_repository(evidence, tmp_path):
    target = tmp_path.parent / (tmp_path.name + "-outside.md")
    target.write_text("Build instructions", encoding="utf-8")
    link = tmp_path / "docs/linked.md"
    try:
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation is unavailable on this host")
        evidence["components"][0]["build_instructions"] = "docs/linked.md"
        with pytest.raises(checker.NativeSourceError, match="escapes"):
            validate(evidence, tmp_path)
    finally:
        target.unlink()


def test_unreadable_doc_is_not_reported_as_verified(evidence, tmp_path):
    (tmp_path / "docs/build.md").write_bytes(b"\xff")
    with pytest.raises(checker.NativeSourceError, match="UTF-8"):
        validate(evidence, tmp_path)


@pytest.mark.parametrize("url", [
    "http://example.org/a", "file:///tmp/a", "https:///a", "https://user:secret@example.org/a",
    "https://example.org/a#fragment", "https://example.org:bad/a", "https://example.org/a\n",
    "https://example.org/a b", "https://example.org\\a", "https://[broken/a",
])
def test_malformed_source_url_fails_ordinary_mode(evidence, tmp_path, url):
    evidence["components"][0]["sources"][0]["url"] = url
    with pytest.raises(checker.NativeSourceError):
        validate(evidence, tmp_path)


@pytest.mark.parametrize("field", ["binaries", "sources"])
@pytest.mark.parametrize("digest", [None, 123, "", "a" * 63, "g" * 64, "a" * 64 + "\n"])
def test_invalid_hash_fails_ordinary_mode(evidence, tmp_path, field, digest):
    evidence["components"][0][field][0]["sha256"] = digest
    with pytest.raises(checker.NativeSourceError):
        validate(evidence, tmp_path)


@pytest.mark.parametrize("field", ["binary", "instructions"])
@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "foo\\bar", "./a", "a//b", "a/../b", "a/", "a./b", "a\x00b", ""])
def test_unsafe_paths_fail_without_installed_check(evidence, tmp_path, field, path):
    record = evidence["components"][0]
    if field == "binary":
        record["binaries"][0]["path"] = path
    else:
        record["build_instructions"] = path
    with pytest.raises(checker.NativeSourceError):
        validate(evidence, tmp_path)


@pytest.mark.parametrize("failure", ["name", "binary", "source", "cross_component_binary"])
def test_duplicates_fail(evidence, tmp_path, failure):
    record = evidence["components"][0]
    if failure == "name":
        evidence["components"].append(copy.deepcopy(record))
    elif failure == "binary":
        duplicate = copy.deepcopy(record["binaries"][0])
        duplicate["path"] = duplicate["path"].upper()
        record["binaries"].append(duplicate)
    elif failure == "source":
        record["sources"].append(copy.deepcopy(record["sources"][0]))
    else:
        duplicate = copy.deepcopy(record)
        duplicate["name"] = "Another component"
        evidence["components"].append(duplicate)
    with pytest.raises(checker.NativeSourceError):
        validate(evidence, tmp_path)


@pytest.mark.parametrize("value", [None, {}, {"schema_version": True, "components": []},
    {"schema_version": 2, "components": []}, {"schema_version": 1, "components": []},
    {"schema_version": 1, "components": [None]}])
def test_bad_root_schema(value, tmp_path):
    with pytest.raises(checker.NativeSourceError):
        validate(value, tmp_path)


@pytest.mark.parametrize("field,value", [("version", "1.2.4"), ("package", "other"),
    ("binaries", []), ("binaries", [None]), ("sources", None), ("sources", [None]),
    ("unresolved", "pending"), ("unresolved", [""]), ("unresolved", [False])])
def test_bad_component_schema_fails_ordinary(evidence, tmp_path, field, value):
    evidence["components"][0][field] = value
    with pytest.raises(checker.NativeSourceError):
        validate(evidence, tmp_path)


@pytest.mark.parametrize("text", ["", "demo>=1", "demo==1\nDemo==2", "demo==1.*", "demo==1; platform_system=='Windows'"])
def test_constraints_require_unambiguous_exact_pins(tmp_path, text):
    path = tmp_path / "constraints.txt"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(checker.NativeSourceError):
        checker.read_constraints(path)


def test_cli_exit_codes_and_reasons(evidence, tmp_path):
    manifest = tmp_path / "manifest.json"
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("# comment\nDemo_Package==1.2.3\n", encoding="utf-8")
    args = [sys.executable, str(SCRIPT), "--manifest", str(manifest), "--constraints", str(constraints), "--repo-root", str(tmp_path)]

    def run(strict=False):
        return subprocess.run(args + (["--require-complete"] if strict else []), capture_output=True, text=True, check=False)

    manifest.write_text(json.dumps(evidence), encoding="utf-8")
    assert run(True).returncode == 0
    evidence["components"][0]["unresolved"] = ["Missing exact build patches"]
    manifest.write_text(json.dumps(evidence), encoding="utf-8")
    assert run().returncode == 0
    result = run(True)
    assert result.returncode == 1
    assert "Missing exact build patches" in result.stderr
    evidence["components"][0]["binaries"][0]["sha256"] = "broken"
    manifest.write_text(json.dumps(evidence), encoding="utf-8")
    assert run().returncode == 2
    manifest.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    assert run().returncode == 2
    manifest.write_text("not json", encoding="utf-8")
    assert run().returncode == 2
    manifest.unlink()
    assert run().returncode == 2


@pytest.fixture
def payload(evidence, tmp_path):
    root = tmp_path / "dist/App/_internal"
    relative = "PySide6/Qt6Core.dll"
    evidence["components"][0]["binaries"][0]["path"] = relative
    binary = root / relative
    binary.parent.mkdir(parents=True)
    binary.write_bytes(BINARY_BYTES)
    return root


def test_payload_recorded_bytes_pass_without_installed_metadata(evidence, tmp_path, payload, monkeypatch):
    def forbidden(_package):
        raise AssertionError("payload checks must not require the installed environment")
    monkeypatch.setattr(checker.metadata, "distribution", forbidden)
    monkeypatch.setattr(checker.metadata, "version", forbidden)
    assert validate(evidence, tmp_path, payload_root=payload, require_complete=True) == ()


@pytest.mark.parametrize("failure", ["missing", "modified", "missing_root", "wrong_root"])
def test_payload_binary_must_exist_and_match(evidence, tmp_path, payload, failure):
    binary = payload / evidence["components"][0]["binaries"][0]["path"]
    root = payload
    if failure == "missing":
        binary.unlink()
    elif failure == "modified":
        binary.write_bytes(b"wrong binary")
    elif failure == "missing_root":
        root = tmp_path / "does-not-exist"
    else:
        root = payload.parent
    with pytest.raises(checker.NativeSourceError):
        validate(evidence, tmp_path, payload_root=root)


@pytest.mark.parametrize("relative", [
    "PySide6/plugins/new/new.dll", "PySide6/other.PYD", "shiboken6/new.pyd",
    "pyzbar/libextra.dll", "pyzbar/vcruntime140.dll", "PySide6/vcruntime140_new.dll",
])
def test_payload_rejects_unrecorded_native_files_in_scopes(evidence, tmp_path, payload, relative):
    extra = payload / relative
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"new dependency")
    with pytest.raises(checker.NativeSourceError, match="unrecorded native"):
        validate(evidence, tmp_path, payload_root=payload)


def test_payload_rejects_removed_component_while_other_component_remains(evidence, tmp_path, payload):
    extra = payload / "pyzbar/libiconv.dll"
    extra.parent.mkdir()
    extra.write_bytes(BINARY_BYTES)
    with pytest.raises(checker.NativeSourceError, match="unrecorded native.*libiconv"):
        validate(evidence, tmp_path, payload_root=payload)


def test_two_packages_cannot_claim_the_same_payload_path(evidence, tmp_path, payload):
    other = copy.deepcopy(evidence["components"][0])
    other["name"] = "Another package's native binary"
    other["package"] = "other-package"
    evidence["components"].append(other)
    with pytest.raises(checker.NativeSourceError, match="duplicate payload binary"):
        checker.validate_manifest(
            evidence, {"demo-package": "1.2.3", "other-package": "1.2.3"},
            repo_root=tmp_path, payload_root=payload,
        )


def test_payload_excludes_only_reviewed_microsoft_paths(evidence, tmp_path, payload):
    for relative in checker._PAYLOAD_RUNTIME_EXCLUSIONS:
        scope, filename = relative.split("/", 1)
        target = payload / ({"pyside6": "PySide6", "shiboken6": "shiboken6"}[scope]) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"Microsoft redistributable")
    assert validate(evidence, tmp_path, payload_root=payload) == ()


def test_recorded_excluded_runtime_still_requires_hash_match(evidence, tmp_path, payload):
    relative = "PySide6/MSVCP140.dll"
    (payload / relative).write_bytes(b"modified runtime")
    evidence["components"][0]["binaries"].append({"path": relative, "sha256": DIGEST})
    with pytest.raises(checker.NativeSourceError, match="SHA-256"):
        validate(evidence, tmp_path, payload_root=payload)


def test_payload_does_not_claim_coverage_of_other_packages(evidence, tmp_path, payload):
    for relative in ("PIL/new.dll", "python313.dll", "PySide6/data.txt"):
        path = payload / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"outside the native-source scope")
    assert validate(evidence, tmp_path, payload_root=payload) == ()


@pytest.mark.parametrize("kind", ["file", "directory", "internal_directory"])
def test_payload_symlinks_cannot_hide_native_files(evidence, tmp_path, payload, kind):
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "hidden.dll").write_bytes(BINARY_BYTES)
    link = payload / "PySide6/extra"
    if kind == "file":
        link = link.with_suffix(".dll")
        target = outside / "hidden.dll"
    elif kind == "internal_directory":
        target = payload / "PySide6/plugins"
    else:
        target = outside
    try:
        link.symlink_to(target, target_is_directory=kind != "file")
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(checker.NativeSourceError, match="escapes|aliases"):
        validate(evidence, tmp_path, payload_root=payload)


def test_recorded_payload_symlink_cannot_escape_root(evidence, tmp_path, payload):
    relative = evidence["components"][0]["binaries"][0]["path"]
    path = payload / relative
    outside = tmp_path / "outside.dll"
    outside.write_bytes(BINARY_BYTES)
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(checker.NativeSourceError, match="escapes"):
        validate(evidence, tmp_path, payload_root=payload)


def test_payload_cli_failure_is_nonzero(evidence, tmp_path, payload):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(evidence), encoding="utf-8")
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("demo-package==1.2.3\n", encoding="utf-8")
    args = [sys.executable, str(SCRIPT), "--manifest", str(manifest), "--constraints", str(constraints),
            "--repo-root", str(tmp_path), "--payload-root", str(payload)]
    assert subprocess.run(args, capture_output=True, check=False).returncode == 0
    (payload / "PySide6/extra.pyd").write_bytes(b"unrecorded")
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "unrecorded native payload" in result.stderr


@pytest.fixture
def replacement(evidence, tmp_path):
    relative = "packaging/native/demo/native.dll"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"reviewed replacement build")
    evidence["components"][0]["binaries"][0].update({
        "build_input": relative,
        "installed_sha256": DIGEST,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })
    return path


def test_override_checks_original_wheel_and_replacement_payload_separately(
    evidence, tmp_path, replacement, monkeypatch,
):
    installed = mock_installed(monkeypatch, tmp_path)
    payload = tmp_path / "dist/_internal"
    (payload / "demo").mkdir(parents=True)
    (payload / "demo/native.dll").write_bytes(replacement.read_bytes())
    assert validate(evidence, tmp_path, installed=True, payload_root=payload, require_complete=True) == ()
    assert installed.read_bytes() == BINARY_BYTES


@pytest.mark.parametrize("failure", ["missing", "modified", "empty", "directory"])
@pytest.mark.parametrize("strict", [False, True])
def test_override_build_bytes_are_required_without_installed_mode(evidence, tmp_path, replacement, failure, strict):
    if failure == "missing":
        replacement.unlink()
    elif failure == "modified":
        replacement.write_bytes(b"different build")
    elif failure == "empty":
        replacement.write_bytes(b"")
        evidence["components"][0]["binaries"][0]["sha256"] = hashlib.sha256(b"").hexdigest()
    else:
        replacement.unlink()
        replacement.mkdir()
    with pytest.raises(checker.NativeSourceError, match="build input"):
        validate(evidence, tmp_path, require_complete=strict)


def test_override_ordinary_validation_does_not_read_installed_metadata(evidence, tmp_path, replacement, monkeypatch):
    assert replacement.is_file()

    def forbidden(_package):
        raise AssertionError("override source validation must not require installed packages")

    monkeypatch.setattr(checker.metadata, "version", forbidden)
    monkeypatch.setattr(checker.metadata, "distribution", forbidden)
    assert validate(evidence, tmp_path, require_complete=True) == ()


@pytest.mark.parametrize("field", ["build_input", "installed_sha256"])
def test_override_fields_must_be_declared_together(evidence, tmp_path, replacement, field):
    assert replacement.is_file()
    del evidence["components"][0]["binaries"][0][field]
    with pytest.raises(checker.NativeSourceError, match="together"):
        validate(evidence, tmp_path)


@pytest.mark.parametrize("value", [None, 123, "", "a" * 63, "g" * 64, "a" * 64 + "\n"])
def test_override_installed_hash_must_be_valid_in_ordinary_mode(evidence, tmp_path, replacement, value):
    assert replacement.is_file()
    evidence["components"][0]["binaries"][0]["installed_sha256"] = value
    with pytest.raises(checker.NativeSourceError, match="SHA-256"):
        validate(evidence, tmp_path)


@pytest.mark.parametrize("path", [None, "../escape", "/absolute", "C:/escape", "foo\\bar", "./a", "a//b",
                                  "a/../b", "a/", "a./b", "a\x00b", ""])
def test_override_build_path_must_be_safe_in_ordinary_mode(evidence, tmp_path, replacement, path):
    assert replacement.is_file()
    evidence["components"][0]["binaries"][0]["build_input"] = path
    with pytest.raises(checker.NativeSourceError):
        validate(evidence, tmp_path)


def test_override_build_input_symlink_cannot_escape_repository(evidence, tmp_path, replacement):
    outside = tmp_path.parent / (tmp_path.name + "-outside.dll")
    outside.write_bytes(replacement.read_bytes())
    replacement.unlink()
    try:
        try:
            replacement.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is unavailable on this host")
        with pytest.raises(checker.NativeSourceError, match="escapes"):
            validate(evidence, tmp_path)
    finally:
        outside.unlink()


def test_override_does_not_allow_installed_wheel_replacement(evidence, tmp_path, replacement, monkeypatch):
    installed = mock_installed(monkeypatch, tmp_path)
    installed.write_bytes(replacement.read_bytes())
    with pytest.raises(checker.NativeSourceError, match="installed binary SHA-256"):
        validate(evidence, tmp_path, installed=True)


def test_override_does_not_allow_old_wheel_bytes_in_payload(evidence, tmp_path, replacement):
    assert replacement.is_file()
    root = tmp_path / "dist/_internal"
    (root / "demo").mkdir(parents=True)
    (root / "demo/native.dll").write_bytes(BINARY_BYTES)
    with pytest.raises(checker.NativeSourceError, match="payload binary SHA-256"):
        validate(evidence, tmp_path, payload_root=root)


def test_override_cli_checks_build_bytes_without_environment(evidence, tmp_path, replacement):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(evidence), encoding="utf-8")
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("demo-package==1.2.3\n", encoding="utf-8")
    args = [sys.executable, str(SCRIPT), "--manifest", str(manifest), "--constraints", str(constraints),
            "--repo-root", str(tmp_path), "--require-complete"]
    assert subprocess.run(args, capture_output=True, check=False).returncode == 0
    replacement.write_bytes(b"changed candidate")
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "build input SHA-256" in result.stderr


@pytest.fixture
def local_archive(evidence, tmp_path):
    relative = "packaging/native/demo/source.tar.gz"
    archive = tmp_path / relative
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"retained source archive")
    evidence["components"][0]["sources"][0].update({
        "local_path": relative, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    })
    return archive


def test_local_archive_passes_without_installed_environment(evidence, tmp_path, local_archive, monkeypatch):
    assert local_archive.is_file()

    def forbidden(_package):
        raise AssertionError("local archive validation must not inspect installed distributions")

    monkeypatch.setattr(checker.metadata, "distribution", forbidden)
    monkeypatch.setattr(checker.metadata, "version", forbidden)
    assert validate(evidence, tmp_path, require_complete=True) == ()


@pytest.mark.parametrize("failure", ["missing", "modified", "empty", "directory"])
@pytest.mark.parametrize("strict", [False, True])
def test_local_archive_must_exist_and_match_without_installed_mode(evidence, tmp_path, local_archive, failure, strict):
    if failure == "missing":
        local_archive.unlink()
    elif failure == "modified":
        local_archive.write_bytes(b"wrong source archive")
    elif failure == "empty":
        local_archive.write_bytes(b"")
        evidence["components"][0]["sources"][0]["sha256"] = hashlib.sha256(b"").hexdigest()
    else:
        local_archive.unlink()
        local_archive.mkdir()
    with pytest.raises(checker.NativeSourceError, match="local source archive"):
        validate(evidence, tmp_path, require_complete=strict)


@pytest.mark.parametrize("path", [None, "../escape", "/absolute", "C:/escape", "foo\\bar", "./a", "a//b",
                                  "a/../b", "a/", "a./b", "a\x00b", ""])
def test_local_archive_path_must_be_safe(evidence, tmp_path, local_archive, path):
    assert local_archive.is_file()
    evidence["components"][0]["sources"][0]["local_path"] = path
    with pytest.raises(checker.NativeSourceError):
        validate(evidence, tmp_path)


def test_local_archive_symlink_cannot_escape_repository(evidence, tmp_path, local_archive):
    outside = tmp_path.parent / (tmp_path.name + "-outside.tar.gz")
    outside.write_bytes(local_archive.read_bytes())
    local_archive.unlink()
    try:
        try:
            local_archive.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is unavailable on this host")
        with pytest.raises(checker.NativeSourceError, match="escapes"):
            validate(evidence, tmp_path)
    finally:
        outside.unlink()
