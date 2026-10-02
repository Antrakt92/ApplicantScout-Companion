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
    monkeypatch.setattr(wcl.time, "time", lambda: 1000.0)
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
