from __future__ import annotations

from pathlib import Path

import pytest

from scripts.collect_dependency_licenses import (
    collect_dependency_license_artifacts,
    parse_exact_constraints,
)


def test_dependency_license_collection_reads_every_release_constraint(tmp_path: Path):
    constraints = tmp_path / "constraints-release.txt"
    constraints.write_text(
        "\n".join(
            [
                "# comment",
                "",
                "Alpha-Pkg==1.0",
                "beta_pkg==2.0",
                "gamma.pkg==3.0",
            ]
        ),
        encoding="utf-8",
    )

    assert parse_exact_constraints(constraints) == [
        "Alpha-Pkg",
        "beta_pkg",
        "gamma.pkg",
    ]


def test_dependency_license_collection_rejects_non_exact_constraints(
    tmp_path: Path,
):
    constraints = tmp_path / "constraints-release.txt"
    constraints.write_text("Alpha-Pkg>=1.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Malformed release constraint"):
        parse_exact_constraints(constraints)


def test_dependency_license_collection_writes_placeholder_when_no_license_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Distribution:
        files: tuple[()] = ()
        metadata = {"Name": "NoLicenseWheel"}

        def locate_file(self, file: object) -> Path:
            raise AssertionError("no files should be located")

    monkeypatch.setattr(
        "scripts.collect_dependency_licenses.metadata.distribution",
        lambda _package: Distribution(),
    )

    dest = tmp_path / "licenses"
    collect_dependency_license_artifacts(["no-license-wheel"], dest)

    placeholder = dest / "NoLicenseWheel" / "NO-LICENSE-FILE-FOUND.txt"
    assert placeholder.read_text(encoding="utf-8") == (
        "No license-like file was exposed by installed metadata for NoLicenseWheel.\n"
    )
