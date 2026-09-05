"""Check exact release pins against PyPI's known release-specific advisories.

This does not install or resolve packages, execute dependency code, or establish
that a package is safe. Missing or unreadable advisory metadata fails the gate.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPException
import json
from pathlib import Path
import re
import sys
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15
MAX_WORKERS = 4
_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)")
_ADVISORY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")


class AdvisoryCheckError(ValueError):
    """The exact release's advisory result cannot be established."""


@dataclass(frozen=True)
class Pin:
    name: str
    version: str

    @property
    def label(self) -> str:
        return f"{self.name}=={self.version}"


@dataclass(frozen=True)
class Result:
    pin: Pin
    advisory_ids: tuple[str, ...] = ()
    error: str = ""


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def read_pins(path: Path) -> list[Pin]:
    pins: list[Pin] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.fullmatch(line)
        if match is None:
            raise AdvisoryCheckError(f"line {line_number}: expected one exact name==version pin")
        name, version = match.groups()
        name = normalized_name(name)
        if name in seen:
            raise AdvisoryCheckError(f"line {line_number}: duplicate package {name}")
        seen.add(name)
        pins.append(Pin(name, version))
    if not pins:
        raise AdvisoryCheckError("constraints contain no exact release pins")
    return pins


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise AdvisoryCheckError("unexpected registry redirect")


def fetch_release(pin: Pin) -> object:
    # Pin parsing excludes URL delimiters, query strings, paths, and credentials.
    request = Request(
        f"https://pypi.org/pypi/{pin.name}/{pin.version}/json",
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "ApplicantScout-dependency-advisories",
        },
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise AdvisoryCheckError("registry returned a non-success status")
            if response.headers.get_content_type() != "application/json":
                raise AdvisoryCheckError("registry returned a non-JSON response")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise AdvisoryCheckError(f"registry HTTP {exc.code}") from exc
    except (URLError, OSError, HTTPException) as exc:
        raise AdvisoryCheckError("registry request unavailable") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise AdvisoryCheckError("registry response exceeded the size limit")
    try:
        return json.loads(body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AdvisoryCheckError("registry returned invalid JSON") from exc


def active_advisories(pin: Pin, payload: object) -> tuple[str, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("info"), dict):
        raise AdvisoryCheckError("missing release identity metadata")
    info = payload["info"]
    name, version = info.get("name"), info.get("version")
    if (
        not isinstance(name, str)
        or normalized_name(name) != pin.name
        or version != pin.version
    ):
        raise AdvisoryCheckError("registry release identity does not match the exact pin")
    records = payload.get("vulnerabilities")
    if not isinstance(records, list):
        raise AdvisoryCheckError("missing or malformed vulnerabilities list")
    active: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise AdvisoryCheckError("malformed advisory record")
        advisory_id = record.get("id")
        if not isinstance(advisory_id, str) or not _ADVISORY_ID.fullmatch(advisory_id):
            raise AdvisoryCheckError("missing or malformed advisory identifier")
        if "withdrawn" not in record:
            raise AdvisoryCheckError("missing advisory withdrawal state")
        withdrawn = record["withdrawn"]
        if withdrawn is not None:
            if not isinstance(withdrawn, str):
                raise AdvisoryCheckError("malformed advisory withdrawal timestamp")
            try:
                timestamp = datetime.fromisoformat(withdrawn)
            except ValueError as exc:
                raise AdvisoryCheckError("malformed advisory withdrawal timestamp") from exc
            if timestamp.tzinfo is None:
                raise AdvisoryCheckError("advisory withdrawal timestamp has no timezone")
            continue
        active.add(advisory_id)
    return tuple(sorted(active))


def check_pin(pin: Pin, fetcher: Callable[[Pin], object] = fetch_release) -> Result:
    try:
        return Result(pin, advisory_ids=active_advisories(pin, fetcher(pin)))
    except AdvisoryCheckError as exc:
        return Result(pin, error=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--constraints", type=Path,
        default=Path(__file__).resolve().parents[1] / "constraints-release.txt",
    )
    args = parser.parse_args(argv)
    try:
        pins = read_pins(args.constraints)
    except (OSError, UnicodeError):
        print("ERROR: cannot read the constraints file", file=sys.stderr)
        return 2
    except AdvisoryCheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(check_pin, pins))
    unavailable = sum(bool(result.error) for result in results)
    affected = sum(bool(result.advisory_ids) for result in results)
    for result in results:
        if result.error:
            print(f"UNAVAILABLE {result.pin.label}: {result.error}")
        elif result.advisory_ids:
            print(f"ADVISORY {result.pin.label}: {', '.join(result.advisory_ids)}")
    print(
        f"PyPI known-advisory check: {len(results)} exact pins; "
        f"{affected} affected packages; {unavailable} unavailable results."
    )
    return 2 if unavailable else 1 if affected else 0


if __name__ == "__main__":
    raise SystemExit(main())
