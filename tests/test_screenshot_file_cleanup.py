"""Deletion owns a verified file object, including concurrent path replacements."""
import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import applicant_scout.screenshot as screenshot_mod
import applicant_scout.verified_file_cleanup as cleanup_mod


def _source(path):
    return screenshot_mod.ScreenshotWatcher._source_from_stat(path, path.stat())


@pytest.mark.skipif(os.name != "nt", reason="Native Windows sharing semantics")
def test_cleanup_preserves_replacement_between_validation_and_delete(monkeypatch, tmp_path):
    path = tmp_path / "transport.jpg"
    replacement = tmp_path / "manual.jpg"
    path.write_bytes(b"old transport")
    replacement.write_bytes(b"new manual photograph")
    expected = _source(path)
    real_stat = Path.stat
    real_fstat = os.fstat
    attempted = []

    def replace_after_inspection():
        if attempted:
            return
        attempted.append(True)
        try:
            os.replace(replacement, path)
        except PermissionError:
            pass  # A verified handle may prevent replacement until it is closed.

    def pathname_stat(self, *args, **kwargs):
        value = real_stat(self, *args, **kwargs)
        if self == path:
            replace_after_inspection()
        return value

    def handle_stat(fd):
        value = real_fstat(fd)
        replace_after_inspection()
        return value

    monkeypatch.setattr(Path, "stat", pathname_stat)
    monkeypatch.setattr(os, "fstat", handle_stat)
    screenshot_mod._unlink_if_source_matches(path, expected)
    assert attempted == [True]
    assert any(
        candidate.exists() and candidate.read_bytes() == b"new manual photograph"
        for candidate in (path, replacement)
    )


def test_cleanup_preserves_equal_metadata_different_file(tmp_path):
    path = tmp_path / "transport.jpg"
    replacement = tmp_path / "manual.jpg"
    path.write_bytes(b"old-transport")
    expected = _source(path)
    replacement.write_bytes(b"new-manual!!!")
    assert replacement.stat().st_size == expected.size
    os.utime(replacement, ns=(expected.mtime_ns, expected.mtime_ns))
    os.replace(replacement, path)
    assert screenshot_mod._unlink_if_source_matches(path, expected) is False
    assert path.read_bytes() == b"new-manual!!!"


def test_cleanup_without_generation_authority_preserves_file(tmp_path):
    path = tmp_path / "manual.jpg"
    path.write_bytes(b"manual")
    assert screenshot_mod._unlink_if_source_matches(path, None) is False
    assert path.read_bytes() == b"manual"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows sharing semantics")
def test_cleanup_blocks_inplace_write_after_handle_inspection(monkeypatch, tmp_path):
    path = tmp_path / "transport.jpg"
    path.write_bytes(b"old transport")
    expected = _source(path)
    real_fstat = os.fstat
    attempted = []

    def mutate_after_inspection(fd):
        value = real_fstat(fd)
        with pytest.raises(PermissionError):
            path.write_bytes(b"manual replacement")
        attempted.append(True)
        return value

    monkeypatch.setattr(os, "fstat", mutate_after_inspection)
    assert screenshot_mod._unlink_if_source_matches(path, expected) is True
    assert attempted == [True]
    assert not path.exists()


@pytest.mark.parametrize("identity", [None, (0, 0)])
def test_cleanup_without_object_identity_preserves_file(tmp_path, identity):
    path = tmp_path / "manual.jpg"
    path.write_bytes(b"manual")
    source = _source(path)
    from dataclasses import replace
    source = replace(source, file_identity=identity)
    assert screenshot_mod._unlink_if_source_matches(path, source) is False
    assert path.read_bytes() == b"manual"


def test_unsupported_platform_preserves_file(monkeypatch, tmp_path):
    path = tmp_path / "transport.jpg"
    path.write_bytes(b"transport")
    source = _source(path)
    monkeypatch.setattr(cleanup_mod, "os", SimpleNamespace(name="posix"))
    assert screenshot_mod._unlink_if_source_matches(path, source) is False
    assert path.read_bytes() == b"transport"


def test_source_from_another_path_cannot_authorize_deletion(tmp_path):
    path = tmp_path / "transport.jpg"
    other = tmp_path / "other.jpg"
    path.write_bytes(b"transport")
    os.link(path, other)
    assert screenshot_mod._unlink_if_source_matches(other, _source(path)) is False
    assert path.read_bytes() == other.read_bytes() == b"transport"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows sharing semantics")
@pytest.mark.parametrize("failure", ["disposition", "descriptor"])
def test_native_cleanup_failure_releases_handle_and_preserves_file(
    monkeypatch, tmp_path, failure,
):
    import msvcrt

    path = tmp_path / "transport.jpg"
    path.write_bytes(b"transport")
    source = _source(path)
    if failure == "disposition":
        api = cleanup_mod._windows_api()

        def fail_disposition(*_args):
            ctypes.set_last_error(5)
            return False

        monkeypatch.setattr(cleanup_mod, "_windows_api", lambda: SimpleNamespace(
            CreateFileW=api.CreateFileW,
            SetFileInformationByHandle=fail_disposition,
            CloseHandle=api.CloseHandle,
        ))
    else:
        def fail_descriptor(*_args):
            raise OSError("descriptor conversion failed")

        monkeypatch.setattr(msvcrt, "open_osfhandle", fail_descriptor)
    with pytest.raises(OSError):
        screenshot_mod._unlink_if_source_matches(path, source)
    assert path.read_bytes() == b"transport"
    moved = path.with_name("preserved.jpg")
    os.replace(path, moved)  # A leaked mutation-excluding handle would block this.
    moved.write_bytes(b"still writable")


@pytest.mark.skipif(os.name != "nt", reason="Native Windows sharing semantics")
def test_existing_writer_blocks_cleanup_without_pathname_fallback(tmp_path):
    path = tmp_path / "transport.jpg"
    path.write_bytes(b"transport")
    source = _source(path)
    with path.open("r+b"):
        with pytest.raises(OSError):
            screenshot_mod._unlink_if_source_matches(path, source)
    assert path.read_bytes() == b"transport"
    assert screenshot_mod._unlink_if_source_matches(path, source) is True


@pytest.mark.skipif(os.name != "nt", reason="Native Windows reparse semantics")
def test_cleanup_preserves_reparse_point_and_target(tmp_path):
    target = tmp_path / "manual.jpg"
    link = tmp_path / "transport.jpg"
    target.write_bytes(b"manual")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this Windows account")
    assert screenshot_mod._unlink_if_source_matches(link, _source(link)) is False
    assert link.is_symlink()
    assert target.read_bytes() == b"manual"
