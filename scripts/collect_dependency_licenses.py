from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
import re
import shutil
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


LICENSE_FILE_TOKENS = ("license", "copying", "notice")
DEFAULT_RELEASE_EXTRAS = ("dev",)


@dataclass(frozen=True)
class LicenseOverride:
    version: str
    source_url: str
    rationale: str
    notice_text: str


def load_license_overrides(path: Path) -> dict[str, LicenseOverride]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"schema_version", "overrides"}:
        raise ValueError(
            "License override manifest must contain only schema_version and overrides"
        )
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("License override manifest schema_version must be integer 1")

    raw_overrides = data["overrides"]
    if not isinstance(raw_overrides, dict):
        raise ValueError("License override manifest overrides must be a table")

    root = path.parent.resolve()
    overrides: dict[str, LicenseOverride] = {}
    for package, raw_entry in raw_overrides.items():
        canonical_name = str(canonicalize_name(package))
        if package != canonical_name:
            raise ValueError(
                f"License override key must be canonical: {package!r} -> {canonical_name!r}"
            )
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "version",
            "source_url",
            "rationale",
            "notice_file",
        }:
            raise ValueError(
                f"License override for {package} must contain exactly version, "
                "source_url, rationale, and notice_file"
            )

        version = raw_entry["version"]
        source_url = raw_entry["source_url"]
        rationale = raw_entry["rationale"]
        notice_file = raw_entry["notice_file"]
        if not isinstance(version, str) or not version or version.strip() != version:
            raise ValueError(f"License override version for {package} must be non-empty")
        if not isinstance(source_url, str):
            raise ValueError(f"License override source_url for {package} must be a string")
        parsed_source = urlparse(source_url)
        if parsed_source.scheme != "https" or not parsed_source.netloc:
            raise ValueError(
                f"License override source_url for {package} must be an HTTPS URL"
            )
        if (
            not isinstance(rationale, str)
            or not rationale
            or rationale.strip() != rationale
        ):
            raise ValueError(
                f"License override rationale for {package} must be non-empty"
            )
        if not isinstance(notice_file, str) or not notice_file:
            raise ValueError(
                f"License override notice_file for {package} must be a relative path"
            )
        relative_notice = Path(notice_file)
        if (
            relative_notice.is_absolute()
            or ".." in relative_notice.parts
            or not relative_notice.parts
            or relative_notice.parts[0] != "dependency-license-notices"
        ):
            raise ValueError(
                f"License override notice_file for {package} must stay inside "
                f"{root / 'dependency-license-notices'}"
            )
        notice_path = (root / relative_notice).resolve()
        if not notice_path.is_relative_to(root) or not notice_path.is_file():
            raise ValueError(
                f"License override notice_file for {package} is missing or outside {root}"
            )
        notice_text = notice_path.read_text(encoding="utf-8")
        if not notice_text.strip():
            raise ValueError(f"License override notice_file for {package} is empty")
        overrides[package] = LicenseOverride(
            version=version,
            source_url=source_url,
            rationale=rationale,
            notice_text=notice_text,
        )
    return overrides


def parse_exact_constraints(path: Path) -> list[str]:
    packages: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==.+", line)
        if match is None:
            raise ValueError(f"Malformed release constraint: {line}")
        packages.append(match.group(1))
    return packages


def _requirement_name(requirement: str) -> str:
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement as exc:
        raise ValueError(f"Malformed pyproject dependency: {requirement}") from exc
    return str(canonicalize_name(parsed.name))


def _string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"pyproject.toml {field} must be a list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"pyproject.toml {field} entries must be strings")
        items.append(item)
    return items


def release_dependency_names_from_pyproject(
    path: Path,
    *,
    extras: Sequence[str] = DEFAULT_RELEASE_EXTRAS,
) -> list[str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    names: set[str] = set()

    build_system = data.get("build-system", {})
    if isinstance(build_system, dict) and "requires" in build_system:
        for requirement in _string_list(
            build_system["requires"],
            field="build-system.requires",
        ):
            names.add(_requirement_name(requirement))

    project = data.get("project", {})
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml project must be a table")
    for requirement in _string_list(
        project.get("dependencies", []),
        field="project.dependencies",
    ):
        names.add(_requirement_name(requirement))

    optional = project.get("optional-dependencies", {})
    if optional is None:
        optional = {}
    if not isinstance(optional, dict):
        raise ValueError("pyproject.toml project.optional-dependencies must be a table")
    for extra in extras:
        if extra not in optional:
            continue
        for requirement in _string_list(
            optional[extra],
            field=f"project.optional-dependencies.{extra}",
        ):
            names.add(_requirement_name(requirement))

    return sorted(names)


def missing_pyproject_constraints(
    pyproject: Path,
    constraints: Path,
    *,
    extras: Sequence[str] = DEFAULT_RELEASE_EXTRAS,
) -> list[str]:
    constrained = {
        str(canonicalize_name(name)) for name in parse_exact_constraints(constraints)
    }
    required = release_dependency_names_from_pyproject(pyproject, extras=extras)
    return [name for name in required if name not in constrained]


def collect_dependency_license_artifacts(
    packages: Iterable[str],
    dest: Path,
    *,
    overrides: Mapping[str, LicenseOverride] | None = None,
) -> None:
    configured_overrides = dict(overrides or {})
    used_overrides: set[str] = set()
    dest.mkdir(parents=True, exist_ok=True)
    for package in packages:
        try:
            dist = metadata.distribution(package)
        except metadata.PackageNotFoundError as exc:
            raise ValueError(f"Pinned distribution is not installed: {package}") from exc
        name = dist.metadata.get("Name", package)
        if (
            not isinstance(name, str)
            or re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?",
                name,
            )
            is None
        ):
            raise ValueError(f"Installed distribution has an unsafe name: {name!r}")
        canonical_name = str(canonicalize_name(name))
        requested_name = str(canonicalize_name(package))
        if canonical_name != requested_name:
            raise ValueError(
                f"Installed distribution identity mismatch: requested {requested_name}, "
                f"metadata names {canonical_name}"
            )
        dest_root = dest.resolve()
        package_dest = (dest_root / name).resolve()
        if package_dest.parent != dest_root:
            raise ValueError(
                f"Installed distribution output escapes license destination: {name!r}"
            )
        copied = False
        for file in dist.files or ():
            rel = Path(str(file))
            if not any(token in rel.name.lower() for token in LICENSE_FILE_TOKENS):
                continue
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(
                    f"Installed distribution {name} exposes an unsafe license path: {rel}"
                )
            source = Path(str(dist.locate_file(file)))
            if not source.is_file() or source.stat().st_size <= 0:
                continue
            target = (package_dest / rel).resolve()
            if not target.is_relative_to(package_dest.resolve()):
                raise ValueError(
                    f"Installed distribution {name} license path escapes its output: {rel}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied = True
        if not copied:
            override = configured_overrides.get(canonical_name)
            if override is None:
                raise ValueError(
                    f"Installed distribution {name} {dist.version} exposes no "
                    "license-like file and has no reviewed override"
                )
            if override.version != dist.version:
                raise ValueError(
                    f"License override for {canonical_name} targets {override.version}, "
                    f"but installed version is {dist.version}"
                )
            package_dest.mkdir(parents=True, exist_ok=True)
            notice_text = override.notice_text
            if not notice_text.endswith("\n"):
                notice_text += "\n"
            (package_dest / "REVIEWED-LICENSE-OVERRIDE.txt").write_text(
                f"Reviewed license override for {name} {dist.version}\n"
                f"Source: {override.source_url}\n\n"
                f"Rationale: {override.rationale}\n\n"
                f"{notice_text}",
                encoding="utf-8",
            )
            used_overrides.add(canonical_name)

    unused_overrides = sorted(set(configured_overrides) - used_overrides)
    if unused_overrides:
        raise ValueError(
            "License overrides are stale or unnecessary: " + ", ".join(unused_overrides)
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--constraints", required=True, type=Path)
    parser.add_argument("--pyproject", type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--overrides", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.pyproject is not None:
        missing = missing_pyproject_constraints(args.pyproject, args.constraints)
        if missing:
            for package in missing:
                print(
                    f"missing release constraint for pyproject dependency: {package}",
                    file=sys.stderr,
                )
            return 1

    packages = parse_exact_constraints(args.constraints)
    overrides = load_license_overrides(args.overrides)
    collect_dependency_license_artifacts(packages, args.dest, overrides=overrides)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
