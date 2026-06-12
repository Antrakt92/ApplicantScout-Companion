from __future__ import annotations

import argparse
from importlib import metadata
import re
import shutil
import sys
import tomllib
from pathlib import Path
from collections.abc import Iterable, Sequence

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


LICENSE_FILE_TOKENS = ("license", "copying", "notice")
DEFAULT_RELEASE_EXTRAS = ("dev",)


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
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for package in packages:
        try:
            dist = metadata.distribution(package)
        except metadata.PackageNotFoundError:
            continue
        name = dist.metadata.get("Name", package)
        package_dest = dest / name
        copied = False
        for file in dist.files or ():
            rel = Path(str(file))
            if not any(token in rel.name.lower() for token in LICENSE_FILE_TOKENS):
                continue
            source = Path(str(dist.locate_file(file)))
            if not source.is_file():
                continue
            target = package_dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied = True
        if not copied:
            package_dest.mkdir(parents=True, exist_ok=True)
            (package_dest / "NO-LICENSE-FILE-FOUND.txt").write_text(
                f"No license-like file was exposed by installed metadata for {name}.\n",
                encoding="utf-8",
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--constraints", required=True, type=Path)
    parser.add_argument("--pyproject", type=Path)
    parser.add_argument("--dest", required=True, type=Path)
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
    collect_dependency_license_artifacts(packages, args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
