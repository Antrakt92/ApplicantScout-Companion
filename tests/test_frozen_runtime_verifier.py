from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

import pytest

from scripts.verify_frozen_runtime import (
    AMD64_MACHINE,
    FORBIDDEN_FROZEN_MODULE_ROOTS,
    FrozenRuntimeVerificationError,
    pe_machine,
    verify_application_import_warnings,
    verify_collect_membership,
    verify_no_bundled_icu,
    verify_no_payload_redirection,
    verify_no_forbidden_modules,
    verify_packaged_extras,
    verify_toc_provenance,
)


def _write_pe(path: Path, machine: int) -> None:
    contents = bytearray(0x80)
    contents[0:2] = b"MZ"
    struct.pack_into("<I", contents, 0x3C, 0x40)
    contents[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", contents, 0x44, machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)


def test_pe_machine_accepts_amd64_and_rejects_malformed(tmp_path: Path):
    amd64 = tmp_path / "amd64.dll"
    _write_pe(amd64, AMD64_MACHINE)
    assert pe_machine(amd64) == AMD64_MACHINE

    malformed = tmp_path / "malformed.dll"
    malformed.write_bytes(b"MZ")
    with pytest.raises(FrozenRuntimeVerificationError, match="Truncated PE header"):
        pe_machine(malformed)


def test_toc_provenance_rejects_prefix_collision_outside_allowed_root(tmp_path: Path):
    allowed = tmp_path / "trusted"
    allowed.mkdir()
    trusted_source = allowed / "module.py"
    trusted_source.write_text("", encoding="utf-8")
    toc = tmp_path / "Analysis-00.toc"
    toc.write_text(
        repr((("module", str(trusted_source), "PYMODULE"),)), encoding="utf-8"
    )
    verify_toc_provenance((toc,), allowed_roots=(allowed,))

    collision = tmp_path / "trusted-evil" / "module.py"
    collision.parent.mkdir()
    collision.write_text("", encoding="utf-8")
    toc.write_text(repr((("module", str(collision), "PYMODULE"),)), encoding="utf-8")
    with pytest.raises(FrozenRuntimeVerificationError, match="untrusted origin"):
        verify_toc_provenance((toc,), allowed_roots=(allowed,))


def test_toc_provenance_allows_only_named_generated_files(tmp_path: Path):
    generated = tmp_path / "build" / "ApplicantScout.exe"
    generated.parent.mkdir()
    generated.write_text("", encoding="utf-8")
    toc = tmp_path / "Analysis-00.toc"
    toc.write_text(repr((str(generated),)), encoding="utf-8")
    verify_toc_provenance(
        (toc,), allowed_roots=(tmp_path / "src",), allowed_files=(generated,)
    )

    foreign = generated.parent / "foreign.dll"
    foreign.write_text("", encoding="utf-8")
    toc.write_text(repr((str(foreign),)), encoding="utf-8")
    with pytest.raises(FrozenRuntimeVerificationError, match="untrusted origin"):
        verify_toc_provenance(
            (toc,), allowed_roots=(tmp_path / "src",), allowed_files=(generated,)
        )


def test_toc_provenance_rejects_malformed_or_empty_files(tmp_path: Path):
    malformed = tmp_path / "bad.toc"
    malformed.write_text("not valid [", encoding="utf-8")
    with pytest.raises(FrozenRuntimeVerificationError, match="Could not parse"):
        verify_toc_provenance((malformed,), allowed_roots=(tmp_path,))

    empty = tmp_path / "empty.toc"
    empty.write_text("()", encoding="utf-8")
    with pytest.raises(FrozenRuntimeVerificationError, match="no absolute origins"):
        verify_toc_provenance((empty,), allowed_roots=(tmp_path,))


def test_toc_provenance_rejects_global_site_packages_inside_allowed_base(
    tmp_path: Path,
):
    base_prefix = tmp_path / "Python"
    rogue = base_prefix / "Lib" / "site-packages" / "rogue.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text("", encoding="utf-8")
    toc = tmp_path / "Analysis-00.toc"
    toc.write_text(repr((("rogue", str(rogue), "PYMODULE"),)), encoding="utf-8")

    with pytest.raises(FrozenRuntimeVerificationError, match="forbidden site-packages"):
        verify_toc_provenance(
            (toc,),
            allowed_roots=(base_prefix,),
            forbidden_roots=(base_prefix / "Lib" / "site-packages",),
        )


def test_bundled_icu_is_rejected_recursively(tmp_path: Path):
    app_dir = tmp_path / "ApplicantScout"
    icu = app_dir / "_internal" / "foreign" / "icuuc.dll"
    _write_pe(icu, AMD64_MACHINE)
    with pytest.raises(FrozenRuntimeVerificationError, match="foreign/icuuc.dll"):
        verify_no_bundled_icu(app_dir)

    icu.unlink()
    verify_no_bundled_icu(app_dir)


def test_build_and_test_only_modules_are_rejected_from_frozen_pyz(tmp_path: Path):
    pyz = tmp_path / "PYZ-00.toc"
    pyz.write_text(
        repr(
            (
                str(tmp_path / "PYZ-00.pyz"),
                [
                    ("applicant_scout", str(tmp_path / "app.py"), "PYMODULE"),
                    ("numpy.testing", str(tmp_path / "numpy.py"), "PYMODULE"),
                    ("_pytest.outcomes", str(tmp_path / "pytest.py"), "PYMODULE"),
                ],
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        FrozenRuntimeVerificationError,
        match=r"build/test-only modules: _pytest, numpy",
    ):
        verify_no_forbidden_modules(pyz)

    pyz.write_text(
        repr(
            (
                str(tmp_path / "PYZ-00.pyz"),
                [("applicant_scout", str(tmp_path / "app.py"), "PYMODULE")],
            )
        ),
        encoding="utf-8",
    )
    verify_no_forbidden_modules(pyz)


def test_every_reviewed_non_runtime_root_is_rejected_from_analysis(tmp_path: Path):
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(
        repr(
            tuple(
                (f"{root}.nested", str(tmp_path / f"{root}.py"), "PYMODULE")
                for root in sorted(FORBIDDEN_FROZEN_MODULE_ROOTS)
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(FrozenRuntimeVerificationError) as exc_info:
        verify_no_forbidden_modules(analysis)

    for root in FORBIDDEN_FROZEN_MODULE_ROOTS:
        assert root in str(exc_info.value)


def test_application_import_warnings_use_a_narrow_reviewed_allowlist(tmp_path: Path):
    warnings = tmp_path / "warn-ApplicantScout.txt"
    warnings.write_text(
        "\n".join(
            (
                "missing module named 'collections.abc' - imported by "
                "applicant_scout.__main__ (top-level)",
                "missing module named optional_platform_api - imported by "
                "third_party.module (optional)",
            )
        ),
        encoding="utf-8",
    )
    verify_application_import_warnings(warnings)

    warnings.write_text(
        "missing module named 'required_runtime' - imported by "
        "applicant_scout.overlay (top-level)\n",
        encoding="utf-8",
    )
    with pytest.raises(
        FrozenRuntimeVerificationError,
        match="missing application imports: required_runtime",
    ):
        verify_application_import_warnings(warnings)


def test_collect_membership_rejects_untracked_or_missing_payload(tmp_path: Path):
    app_dir = tmp_path / "dist" / "ApplicantScout"
    executable = app_dir / "ApplicantScout.exe"
    runtime = app_dir / "_internal" / "runtime.dll"
    _write_pe(executable, AMD64_MACHINE)
    _write_pe(runtime, AMD64_MACHINE)
    collect = tmp_path / "COLLECT-00.toc"
    collect.write_text(
        repr(
            (
                [
                    (
                        "ApplicantScout.exe",
                        str(tmp_path / "build" / "ApplicantScout.exe"),
                        "EXECUTABLE",
                    ),
                    (
                        "runtime.dll",
                        str(tmp_path / "trusted" / "runtime.dll"),
                        "BINARY",
                    ),
                ],
            )
        ),
        encoding="utf-8",
    )
    verify_collect_membership(app_dir, collect)

    (app_dir / "_internal" / "foreign.dll").write_bytes(b"foreign")
    with pytest.raises(
        FrozenRuntimeVerificationError, match="unexpected: _internal/foreign.dll"
    ):
        verify_collect_membership(app_dir, collect)

    (app_dir / "_internal" / "foreign.dll").unlink()
    runtime.unlink()
    with pytest.raises(
        FrozenRuntimeVerificationError, match="missing: _internal/runtime.dll"
    ):
        verify_collect_membership(app_dir, collect)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_collect_membership_rejects_junction_before_traversing_external_tree(
    tmp_path: Path,
):
    app_dir = tmp_path / "dist" / "ApplicantScout"
    executable = app_dir / "ApplicantScout.exe"
    _write_pe(executable, AMD64_MACHINE)
    external = tmp_path / "external"
    external.mkdir()
    (external / "foreign.dll").write_bytes(b"must stay outside payload")
    junction = app_dir / "_internal" / "redirect"
    junction.parent.mkdir(parents=True)
    completed = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(external)],
        check=False,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    collect = tmp_path / "COLLECT-00.toc"
    collect.write_text(
        repr(
            (
                [
                    (
                        "ApplicantScout.exe",
                        str(tmp_path / "build" / "ApplicantScout.exe"),
                        "EXECUTABLE",
                    ),
                ],
            )
        ),
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            FrozenRuntimeVerificationError,
            match="filesystem redirection",
        ):
            verify_collect_membership(app_dir, collect)
    finally:
        junction.rmdir()

    assert (external / "foreign.dll").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
@pytest.mark.parametrize("redirected_part", ("dist", "dist/ApplicantScout"))
def test_payload_root_rejects_junction_ancestry_before_resolution(
    tmp_path: Path,
    redirected_part: str,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("outside", encoding="utf-8")
    junction = repo_root / Path(redirected_part)
    junction.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(external)],
        check=False,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    app_dir = junction / "ApplicantScout" if redirected_part == "dist" else junction
    try:
        with pytest.raises(
            FrozenRuntimeVerificationError, match="filesystem redirection"
        ):
            verify_no_payload_redirection(app_dir, trusted_root=repo_root)
    finally:
        junction.rmdir()

    assert (external / "sentinel.txt").read_text(encoding="utf-8") == "outside"


def test_packaged_extras_are_exact_text_only_and_collect_can_reverify_them(
    tmp_path: Path,
):
    app_dir = tmp_path / "dist" / "ApplicantScout"
    executable = app_dir / "ApplicantScout.exe"
    _write_pe(executable, AMD64_MACHINE)
    collect = tmp_path / "COLLECT-00.toc"
    collect.write_text(
        repr(
            (
                [
                    (
                        "ApplicantScout.exe",
                        str(tmp_path / "build" / "ApplicantScout.exe"),
                        "EXECUTABLE",
                    ),
                ],
            )
        ),
        encoding="utf-8",
    )
    for name, contents in {
        ".apscout-payload-version": "1.2.3\n",
        "LICENSE": "project license\n",
        "RELEASE_NOTES.md": "release notes\n",
        "THIRD-PARTY-NOTICES.md": "third party notices\n",
    }.items():
        (app_dir / name).write_text(contents, encoding="utf-8")
    dependency_license = app_dir / "licenses" / "example" / "LICENSE.txt"
    dependency_license.parent.mkdir(parents=True)
    dependency_license.write_text("dependency license\n", encoding="utf-8")

    extras = verify_packaged_extras(app_dir)
    verify_collect_membership(app_dir, collect, allowed_extra_files=extras)

    injected = app_dir / "licenses" / "example" / "LICENSE.dll"
    injected.write_bytes(b"MZ unsafe")
    with pytest.raises(
        FrozenRuntimeVerificationError,
        match="Unexpected packaged license artifact",
    ):
        verify_packaged_extras(app_dir)


def test_packaged_extras_require_stable_version_marker(tmp_path: Path):
    app_dir = tmp_path / "ApplicantScout"
    app_dir.mkdir()
    for name in ("LICENSE", "RELEASE_NOTES.md", "THIRD-PARTY-NOTICES.md"):
        (app_dir / name).write_text("present\n", encoding="utf-8")
    (app_dir / ".apscout-payload-version").write_text("1.2.3-rc1\n", encoding="utf-8")
    license_path = app_dir / "licenses" / "example" / "LICENSE"
    license_path.parent.mkdir(parents=True)
    license_path.write_text("present\n", encoding="utf-8")

    with pytest.raises(FrozenRuntimeVerificationError, match="stable version"):
        verify_packaged_extras(app_dir)
