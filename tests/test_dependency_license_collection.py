from __future__ import annotations

from pathlib import Path

import pytest

from scripts.collect_dependency_licenses import (
    collect_dependency_license_artifacts,
    load_license_overrides,
    missing_pyproject_constraints,
    parse_exact_constraints,
    release_dependency_names_from_pyproject,
)


def _write_override_manifest(
    tmp_path: Path,
    *,
    version: str = "1.2.3",
    notice_text: str = "Reviewed license text.\n",
) -> Path:
    notice_dir = tmp_path / "dependency-license-notices"
    notice_dir.mkdir()
    (notice_dir / "no-license-wheel.txt").write_text(
        notice_text,
        encoding="utf-8",
    )
    manifest = tmp_path / "overrides.toml"
    manifest.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[overrides.no-license-wheel]",
                f'version = "{version}"',
                'source_url = "https://example.test/no-license-wheel/LICENSE"',
                'rationale = "Wheel metadata omits the reviewed upstream notice."',
                'notice_file = "dependency-license-notices/no-license-wheel.txt"',
            ]
        ),
        encoding="utf-8",
    )
    return manifest


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


def test_pyproject_release_dependency_names_include_runtime_dev_and_build_system(
    tmp_path: Path,
):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools>=68", "wheel>=1"]',
                "",
                "[project]",
                'dependencies = ["Alpha-Pkg>=1", "beta_pkg[fast]>=2"]',
                "",
                "[project.optional-dependencies]",
                'dev = ["Gamma.Pkg>=3"]',
                'docs = ["Docs-Pkg>=4"]',
            ]
        ),
        encoding="utf-8",
    )

    assert release_dependency_names_from_pyproject(pyproject) == [
        "alpha-pkg",
        "beta-pkg",
        "gamma-pkg",
        "setuptools",
        "wheel",
    ]


def test_missing_pyproject_constraints_reports_unpinned_release_dependency(
    tmp_path: Path,
):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools>=68"]',
                "",
                "[project]",
                'dependencies = ["Alpha-Pkg>=1", "beta_pkg>=2"]',
                "",
                "[project.optional-dependencies]",
                'dev = ["Gamma.Pkg>=3"]',
            ]
        ),
        encoding="utf-8",
    )
    constraints = tmp_path / "constraints-release.txt"
    constraints.write_text(
        "\n".join(["Alpha-Pkg==1.0", "Gamma.Pkg==3.0", "setuptools==82.0.1"]),
        encoding="utf-8",
    )

    assert missing_pyproject_constraints(pyproject, constraints) == ["beta-pkg"]


def test_dependency_license_collection_fails_closed_when_no_license_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Distribution:
        files: tuple[()] = ()
        metadata = {"Name": "No-License-Wheel"}
        version = "1.2.3"

        def locate_file(self, file: object) -> Path:
            raise AssertionError("no files should be located")

    monkeypatch.setattr(
        "scripts.collect_dependency_licenses.metadata.distribution",
        lambda _package: Distribution(),
    )

    dest = tmp_path / "licenses"
    with pytest.raises(ValueError, match="has no reviewed override"):
        collect_dependency_license_artifacts(["no-license-wheel"], dest)

    assert not (dest / "No-License-Wheel" / "NO-LICENSE-FILE-FOUND.txt").exists()


def test_dependency_license_collection_uses_exact_reviewed_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Distribution:
        files: tuple[()] = ()
        metadata = {"Name": "No-License-Wheel"}
        version = "1.2.3"

        def locate_file(self, file: object) -> Path:
            raise AssertionError("no files should be located")

    monkeypatch.setattr(
        "scripts.collect_dependency_licenses.metadata.distribution",
        lambda _package: Distribution(),
    )
    manifest = _write_override_manifest(tmp_path)

    dest = tmp_path / "licenses"
    collect_dependency_license_artifacts(
        ["no-license-wheel"],
        dest,
        overrides=load_license_overrides(manifest),
    )

    output = dest / "No-License-Wheel" / "REVIEWED-LICENSE-OVERRIDE.txt"
    assert output.read_text(encoding="utf-8") == (
        "Reviewed license override for No-License-Wheel 1.2.3\n"
        "Source: https://example.test/no-license-wheel/LICENSE\n\n"
        "Rationale: Wheel metadata omits the reviewed upstream notice.\n\n"
        "Reviewed license text.\n"
    )


def test_dependency_license_collection_rejects_stale_override_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Distribution:
        files: tuple[()] = ()
        metadata = {"Name": "No-License-Wheel"}
        version = "2.0.0"

    monkeypatch.setattr(
        "scripts.collect_dependency_licenses.metadata.distribution",
        lambda _package: Distribution(),
    )
    manifest = _write_override_manifest(tmp_path)

    with pytest.raises(ValueError, match="targets 1.2.3, but installed version is 2.0.0"):
        collect_dependency_license_artifacts(
            ["no-license-wheel"],
            tmp_path / "licenses",
            overrides=load_license_overrides(manifest),
        )


def test_dependency_license_collection_rejects_missing_distribution(tmp_path: Path):
    with pytest.raises(ValueError, match="Pinned distribution is not installed"):
        collect_dependency_license_artifacts(
            ["definitely-not-an-installed-distribution"],
            tmp_path / "licenses",
        )


def test_license_override_rejects_empty_notice(tmp_path: Path):
    manifest = _write_override_manifest(tmp_path, notice_text="   \n")

    with pytest.raises(ValueError, match="notice_file.*is empty"):
        load_license_overrides(manifest)


def test_repository_license_override_manifest_is_valid_and_empty():
    manifest = (
        Path(__file__).resolve().parents[1]
        / "packaging"
        / "dependency-license-overrides.toml"
    )

    assert load_license_overrides(manifest) == {}


def test_repository_release_dependencies_collect_without_placeholders(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    packages = parse_exact_constraints(repo_root / "constraints-release.txt")
    overrides = load_license_overrides(
        repo_root / "packaging" / "dependency-license-overrides.toml"
    )
    dest = tmp_path / "licenses"

    collect_dependency_license_artifacts(packages, dest, overrides=overrides)

    files = [path for path in dest.rglob("*") if path.is_file()]
    assert files
    assert all(path.stat().st_size > 0 for path in files)
    assert not list(dest.rglob("NO-LICENSE-FILE-FOUND.txt"))


def test_dependency_license_collection_copies_non_empty_native_license(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "native-LICENSE.txt"
    source.write_text("Native license text.\n", encoding="utf-8")

    class Distribution:
        files = (Path("Native-Wheel.dist-info/LICENSE.txt"),)
        metadata = {"Name": "Native-Wheel"}
        version = "1.0.0"

        def locate_file(self, _file: object) -> Path:
            return source

    monkeypatch.setattr(
        "scripts.collect_dependency_licenses.metadata.distribution",
        lambda _package: Distribution(),
    )

    dest = tmp_path / "licenses"
    collect_dependency_license_artifacts(["native-wheel"], dest)

    copied = dest / "Native-Wheel" / "Native-Wheel.dist-info" / "LICENSE.txt"
    assert copied.read_text(encoding="utf-8") == "Native license text.\n"


def test_dependency_license_collection_rejects_unused_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "native-LICENSE.txt"
    source.write_text("Native license text.\n", encoding="utf-8")

    class Distribution:
        files = (Path("no-license-wheel.dist-info/LICENSE.txt"),)
        metadata = {"Name": "No-License-Wheel"}
        version = "1.2.3"

        def locate_file(self, _file: object) -> Path:
            return source

    monkeypatch.setattr(
        "scripts.collect_dependency_licenses.metadata.distribution",
        lambda _package: Distribution(),
    )

    with pytest.raises(ValueError, match="stale or unnecessary"):
        collect_dependency_license_artifacts(
            ["no-license-wheel"],
            tmp_path / "licenses",
            overrides=load_license_overrides(_write_override_manifest(tmp_path)),
        )


def test_dependency_license_collection_rejects_unsafe_metadata_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Distribution:
        files = (Path("../LICENSE.txt"),)
        metadata = {"Name": "Unsafe-Wheel"}
        version = "1.0.0"

    monkeypatch.setattr(
        "scripts.collect_dependency_licenses.metadata.distribution",
        lambda _package: Distribution(),
    )

    with pytest.raises(ValueError, match="unsafe license path"):
        collect_dependency_license_artifacts(
            ["unsafe-wheel"],
            tmp_path / "licenses",
        )


def test_dependency_license_collection_rejects_unsafe_distribution_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Distribution:
        files = (Path("LICENSE.txt"),)
        metadata = {"Name": ".."}
        version = "1.0.0"

    monkeypatch.setattr(
        "scripts.collect_dependency_licenses.metadata.distribution",
        lambda _package: Distribution(),
    )

    with pytest.raises(ValueError, match="unsafe name"):
        collect_dependency_license_artifacts(
            ["unsafe-wheel"],
            tmp_path / "licenses",
        )


def test_dependency_license_collection_rejects_distribution_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Distribution:
        files: tuple[()] = ()
        metadata = {"Name": "Different-Wheel"}
        version = "1.0.0"

    monkeypatch.setattr(
        "scripts.collect_dependency_licenses.metadata.distribution",
        lambda _package: Distribution(),
    )

    with pytest.raises(ValueError, match="identity mismatch"):
        collect_dependency_license_artifacts(
            ["expected-wheel"],
            tmp_path / "licenses",
        )
