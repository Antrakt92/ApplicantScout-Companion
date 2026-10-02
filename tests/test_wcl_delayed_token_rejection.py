import time

import pytest

from applicant_scout import wcl
from test_wcl import _FakeResponse, _SequenceHTTP, _character_with_empty_mplus, _wcl_payload


def test_delayed_401_preserves_the_already_refreshed_token(monkeypatch, tmp_path):
    auth = wcl.WCLAuth("synthetic-client", "synthetic-secret", tmp_path)
    refreshes = []

    def request_token():
        refreshes.append(True)
        return wcl._Token("fresh-token", time.time() + 3600, auth._client_fingerprint)

    monkeypatch.setattr(auth, "_request_token", request_token)
    auth._token = wcl._Token("rejected-token", time.time() + 3600, auth._client_fingerprint)
    client = wcl.WCLClient(auth, region="EU")
    client._http.close()

    class DelayedRejectionHTTP(_SequenceHTTP):
        def post(self, url, *, json, headers):
            if not self.calls:
                # Another request refreshed while this request was in flight.
                auth.invalidate()
                assert auth.get_token() == "fresh-token"
            return super().post(url, json=json, headers=headers)

    http = DelayedRejectionHTTP([
        _FakeResponse({}, status_code=401),
        _FakeResponse(_wcl_payload(_character_with_empty_mplus())),
    ])
    client._http = http
    try:
        result = client.fetch_character_ranks("Scout", "ravencrest", spec_id=71)
        assert not result.error
        assert len(refreshes) == 1
        assert http.calls[0]["headers"]["Authorization"] == "Bearer rejected-token"
        assert http.calls[1]["headers"]["Authorization"] == "Bearer fresh-token"
        assert auth.get_token() == "fresh-token"
        assert auth._token_path.exists()
    finally:
        client.close()


@pytest.mark.parametrize("rejected", ["current-token", None])
def test_current_or_explicit_invalidation_still_clears_token(tmp_path, rejected):
    auth = wcl.WCLAuth("synthetic-client", "synthetic-secret", tmp_path)
    auth._token = wcl._Token("current-token", time.time() + 3600, auth._client_fingerprint)
    auth._save_cached(auth._token)
    auth.invalidate(rejected)
    assert auth._token is None
    assert not auth._token_path.exists()
