"""How the REST client obtains an access token.

Two grants, chosen by whether a refresh token is configured.

**Client credentials is the better fit and is now the default.** Zoho's own
guidance is that Self Client suits "a stand-alone application that performs only
back-end jobs like data-sync (without any manual intervention)", which is
exactly the poller. It needs only the client id and secret, so there is no
10-minute one-time code to exchange — a step that has failed twice here, once
because the code was pasted into ZOHO_REFRESH_TOKEN and once because it expired
before it was used.

The refresh-token grant stays supported, because existing deployments have one
configured and changing their auth silently would be worse than carrying both.

There were no tests for this file before. It is the only code that can lock the
agent out of Zoho entirely.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from pagerduty_triage.settings import load_settings
from pagerduty_triage.zoho.rest import RestZohoDeskClient, ZohoError


class _Resp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def capture(monkeypatch):
    """Record the token request body instead of making one."""
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return _Resp({"access_token": "at-1", "expires_in": 3600})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def _settings(monkeypatch, **env):
    for k in ("ZOHO_REFRESH_TOKEN", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_ORG_ID"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_settings()


def test_refresh_token_grant_when_one_is_configured(monkeypatch, capture):
    s = _settings(
        monkeypatch,
        ZOHO_REFRESH_TOKEN="1000.refresh",
        ZOHO_CLIENT_ID="cid",
        ZOHO_CLIENT_SECRET="sec",
        ZOHO_ORG_ID="123",
    )
    assert RestZohoDeskClient(s)._access_token() == "at-1"
    assert capture["body"]["grant_type"] == "refresh_token"
    assert capture["body"]["refresh_token"] == "1000.refresh"


def test_client_credentials_when_no_refresh_token(monkeypatch, capture):
    """Only id and secret needed. No code, nothing to exchange, nothing to
    paste into the wrong variable."""
    s = _settings(
        monkeypatch, ZOHO_CLIENT_ID="cid", ZOHO_CLIENT_SECRET="sec", ZOHO_ORG_ID="123"
    )
    assert RestZohoDeskClient(s)._access_token() == "at-1"

    body = capture["body"]
    assert body["grant_type"] == "client_credentials"
    assert body["client_id"] == "cid"
    assert "refresh_token" not in body
    # Zoho scopes the token to one Desk org with `soid`; without it the token
    # authenticates and then sees nothing.
    assert body["soid"] == "ZohoDesk.123"
    assert "Desk.tickets.READ" in body["scope"]


def test_client_credentials_needs_an_org_id(monkeypatch, capture):
    """`soid` is not optional. A token minted without it would authenticate and
    then return empty ticket lists — a misconfiguration that looks like a quiet
    support queue."""
    s = _settings(monkeypatch, ZOHO_CLIENT_ID="cid", ZOHO_CLIENT_SECRET="sec")
    with pytest.raises(ZohoError, match="ZOHO_ORG_ID"):
        RestZohoDeskClient(s)._access_token()


def test_the_token_is_cached_between_calls(monkeypatch, capture):
    s = _settings(
        monkeypatch, ZOHO_CLIENT_ID="cid", ZOHO_CLIENT_SECRET="sec", ZOHO_ORG_ID="123"
    )
    c = RestZohoDeskClient(s)
    c._access_token()
    capture.clear()
    assert c._access_token() == "at-1"
    assert capture == {}, "a cached token must not re-mint"


def test_an_error_payload_raises_even_on_http_200(monkeypatch):
    """Zoho answers a bad grant with HTTP 200 and an `error` key."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _Resp({"error": "invalid_client"}),
    )
    s = _settings(
        monkeypatch, ZOHO_CLIENT_ID="cid", ZOHO_CLIENT_SECRET="sec", ZOHO_ORG_ID="123"
    )
    with pytest.raises(ZohoError, match="invalid_client"):
        RestZohoDeskClient(s)._access_token()
