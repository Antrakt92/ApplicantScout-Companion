from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest

from applicant_scout import wcl
from test_wcl import _FakeAuth


@pytest.mark.parametrize("header, expected", [
    ("900", 900.0),
    ("0", 0.0),
    ("864000", 86400.0),
    ("9" * 5000, 300.0),
    ("-10", 300.0),
    ("1.5", 300.0),
    ("nan", 300.0),
    ("garbage", 300.0),
    (None, 300.0),
    (format_datetime(datetime.fromtimestamp(1900, timezone.utc), usegmt=True), 900.0),
    (format_datetime(datetime.fromtimestamp(900, timezone.utc), usegmt=True), 0.0),
])
def test_graphql_rate_limit_obeys_bounded_retry_after(monkeypatch, header, expected):
    monkeypatch.setattr(wcl.time, "time", lambda: 1000.0)
    monkeypatch.setattr(wcl, "monotonic", lambda: 1000.0)
    client = wcl.WCLClient(_FakeAuth(), region="EU")
    client._http.close()
    headers = {} if header is None else {"Retry-After": header}
    client._http = httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(429, headers=headers, json={})
    ))
    try:
        with pytest.raises(wcl.WCLApiError) as error:
            client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)
        assert error.value.error_kind == wcl.WCL_ERROR_RATE_LIMITED
        assert client.retry_block_remaining_seconds(now=1000.0) == expected
    finally:
        client.close()


def test_later_429_does_not_shorten_an_existing_longer_cooldown(monkeypatch):
    monkeypatch.setattr(wcl, "monotonic", lambda: 1000.0)
    client = wcl.WCLClient(_FakeAuth(), region="EU")
    try:
        client._set_retry_block_if_current(0, wcl.WCL_ERROR_RATE_LIMITED, retry_seconds=900.0)
        client._set_retry_block_if_current(0, wcl.WCL_ERROR_RATE_LIMITED, retry_seconds=300.0)
        assert client.retry_block_remaining_seconds(now=1000.0) == 900.0
        client.reconfigure_auth(_FakeAuth())
        client._set_retry_block_if_current(0, wcl.WCL_ERROR_RATE_LIMITED, retry_seconds=900.0)
        assert client.retry_block_remaining_seconds(now=1000.0) == 0.0
    finally:
        client.close()


def test_retry_and_quota_remaining_ignore_wall_clock_corrections(monkeypatch):
    wall_clock = [1_000.0]
    elapsed_clock = [10.0]
    monkeypatch.setattr(wcl.time, "time", lambda: wall_clock[0])
    monkeypatch.setattr(wcl, "monotonic", lambda: elapsed_clock[0])
    client = wcl.WCLClient(_FakeAuth(), region="EU")
    try:
        client._set_retry_block_if_current(0, wcl.WCL_ERROR_RATE_LIMITED, retry_seconds=30.0)
        client._record_quota_snapshot(wcl.RateLimitInfo(100.0, 20.0, 60.0))
        elapsed_clock[0] += 5.0
        for wall_time in (5_000.0, 500.0):
            wall_clock[0] = wall_time
            assert client.retry_block_remaining_seconds() == 25.0
            assert client.quota_reset_remaining_seconds() == 55.0
            assert (
                client.fetch_character_ranks("Scout", "ravencrest", spec_id=71).error_kind
                == wcl.WCL_ERROR_RATE_LIMITED
            )
            with pytest.raises(wcl.WCLApiError, match="retrying in 25s"):
                client.fetch_character_raid_boss_details("Scout", "ravencrest", spec_id=71)
        elapsed_clock[0] += 55.0
        assert client.retry_block_remaining_seconds() == 0.0
        assert client.quota_reset_remaining_seconds() == 0.0
    finally:
        client.close()


def test_oauth_rate_limit_sets_cooldown_from_retry_after(monkeypatch, tmp_path):
    monkeypatch.setattr(wcl, "monotonic", lambda: 10.0)
    client = wcl.WCLClient(wcl.WCLAuth("client", "secret", tmp_path))
    called = []

    def respond(request):
        called.append(request)
        assert str(request.url) == wcl.WCL_OAUTH_URL
        return httpx.Response(429, headers={"Retry-After": "900"}, json={})

    oauth_http = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(wcl.httpx, "Client", lambda **_kwargs: oauth_http)
    try:
        with pytest.raises(wcl.WCLAuthError, match="HTTP 429"):
            client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)
        assert client.retry_block_remaining_seconds() == 900.0
        assert (
            client.fetch_character_ranks("Scout", "ravencrest", spec_id=71).error_kind
            == wcl.WCL_ERROR_RATE_LIMITED
        )
        assert len(called) == 1
    finally:
        client.close()
