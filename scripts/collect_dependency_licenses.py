from __future__ import annotations

import argparse
from importlib import metadata
import re
import shutil
from pathlib import Path
from collections.abc import Iterable, Sequence


LICENSE_FILE_TOKENS = ("license", "copying", "notice")


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
    parser.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args(argv)

    packages = parse_exact_constraints(args.constraints)
    collect_dependency_license_artifacts(packages, args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
