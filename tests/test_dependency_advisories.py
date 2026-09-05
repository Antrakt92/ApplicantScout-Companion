from __future__ import annotations

import importlib.util
from http.client import IncompleteRead
from pathlib import Path
import sys
from urllib.error import URLError

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_dependency_advisories.py"
spec = importlib.util.spec_from_file_location("dependency_advisories", SCRIPT)
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = checker
spec.loader.exec_module(checker)


def payload(name="demo", version="1.0", records=None):
    return {
        "info": {"name": name, "version": version},
        "vulnerabilities": [] if records is None else records,
    }


@pytest.mark.parametrize(
    "contents",
    ["", "# comment", "demo>=1", "-r other.txt", "demo==1.*", "demo==1; python_version>'3'",
     "demo @ https://example.com/file.whl", "demo==1\nDemo==1", "a_b==1\na-b==2",
     "demo==1/path", "demo==1 # ignored comment"],
)
def test_constraints_reject_incomplete_or_ambiguous_inputs(tmp_path, contents):
    path = tmp_path / "constraints.txt"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(checker.AdvisoryCheckError):
        checker.read_pins(path)


def test_constraints_preserve_exact_versions_and_normalize_names(tmp_path):
    path = tmp_path / "constraints.txt"
    path.write_text("# release pins\nPillow==12.3.0\ntyping_extensions==4.16.0\n", encoding="utf-8")
    assert [pin.label for pin in checker.read_pins(path)] == [
        "pillow==12.3.0", "typing-extensions==4.16.0",
    ]


def test_active_and_withdrawn_advisories_are_distinct():
    records = [
        {"id": "PYSEC-2026-2", "withdrawn": None},
        {"id": "PYSEC-2026-1", "withdrawn": "2026-01-01T00:00:00Z"},
        {"id": "PYSEC-2026-2", "withdrawn": None},
    ]
    assert checker.active_advisories(checker.Pin("demo", "1.0"), payload(records=records)) == (
        "PYSEC-2026-2",
    )


@pytest.mark.parametrize(
    "response",
    [None, {}, {"info": {"name": "demo", "version": "1.0"}},
     payload(name="different"), payload(version="2.0"),
     payload(records={}), payload(records=[None]), payload(records=[{}]),
     payload(records=[{"id": "PYSEC-1"}]),
     payload(records=[{"id": "PYSEC-1", "withdrawn": True}]),
     payload(records=[{"id": "PYSEC-1", "withdrawn": "unknown"}]),
     payload(records=[{"id": "PYSEC-1", "withdrawn": "2026-01-01"}]),
     payload(records=[{"id": "PYSEC-1\n::notice::bad", "withdrawn": None}])],
)
def test_unknown_metadata_never_becomes_a_clean_result(response):
    result = checker.check_pin(checker.Pin("demo", "1.0"), lambda _pin: response)
    assert result.error


@pytest.mark.parametrize(("body", "expected_error"), [
    (b'{"info":{"name":"demo","version":"1.0"},"vulnerabilities":[]}', ""),
    (b'{"info":', "registry returned invalid JSON"),
    (b'\xff', "registry returned invalid JSON"),
    (b'[' * 2000, "registry returned invalid JSON"),
    (b' ' * (checker.MAX_RESPONSE_BYTES + 1), "registry response exceeded the size limit"),
], ids=["valid", "truncated-json", "invalid-encoding", "deep-json", "oversized"])
def test_transport_uses_release_endpoint_and_bounded_request(monkeypatch, body, expected_error):
    observed = {}

    class Response:
        status = 200

        class headers:
            @staticmethod
            def get_content_type():
                return "application/json"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, size):
            observed["size"] = size
            return body

    class Opener:
        def open(self, request, timeout):
            observed.update(url=request.full_url, timeout=timeout)
            assert "Authorization" not in request.headers
            return Response()

    monkeypatch.setattr(checker, "build_opener", lambda *_args: Opener())
    assert checker.check_pin(checker.Pin("demo", "1.0")).error == expected_error
    assert observed == {
        "url": "https://pypi.org/pypi/demo/1.0/json",
        "timeout": checker.REQUEST_TIMEOUT_SECONDS,
        "size": checker.MAX_RESPONSE_BYTES + 1,
    }


def test_network_failure_does_not_echo_untrusted_error_text(monkeypatch):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise URLError("private\n::notice::injected")

    monkeypatch.setattr(checker, "build_opener", lambda *_args: Opener())
    result = checker.check_pin(checker.Pin("demo", "1.0"))
    assert result.error == "registry request unavailable"


def test_truncated_http_body_is_an_unavailable_result(monkeypatch):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise IncompleteRead(b"private\n::notice::injected")

    monkeypatch.setattr(checker, "build_opener", lambda *_args: Opener())
    result = checker.check_pin(checker.Pin("demo", "1.0"))
    assert result.error == "registry request unavailable"


def test_default_constraints_resolve_from_repository_not_working_directory(tmp_path, monkeypatch):
    observed = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(checker, "read_pins", lambda path: observed.append(path) or [])
    assert checker.main([]) == 0
    assert observed == [SCRIPT.parent.parent / "constraints-release.txt"]


def test_redirects_fail_closed():
    with pytest.raises(checker.AdvisoryCheckError, match="redirect"):
        checker._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test")


@pytest.mark.parametrize(("affected", "unavailable", "expected"), [(False, False, 0), (True, False, 1), (False, True, 2), (True, True, 2)])
def test_cli_does_not_report_success_for_advisories_or_partial_results(
    tmp_path, monkeypatch, capsys, affected, unavailable, expected
):
    path = tmp_path / "constraints.txt"
    path.write_text("first==1\nsecond==2", encoding="utf-8")

    def check(pin):
        return checker.Result(
            pin,
            advisory_ids=("PYSEC-1",) if affected and pin.name == "first" else (),
            error="registry request unavailable" if unavailable and pin.name == "second" else "",
        )

    monkeypatch.setattr(checker, "check_pin", check)
    assert checker.main(["--constraints", str(path)]) == expected
    output = capsys.readouterr().out
    assert "2 exact pins" in output
    assert f"{int(affected)} affected packages" in output
    assert f"{int(unavailable)} unavailable results" in output
