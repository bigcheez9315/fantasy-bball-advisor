"""Login plumbing: pulling the code out of whatever the user pastes."""
from __future__ import annotations

import pytest

from fantasy.auth import authorize_url, extract_code


def test_extracts_code_from_full_redirect_url():
    url = "https://localhost:8080/?code=abc123xyz&state=none"
    assert extract_code(url) == "abc123xyz"


def test_extracts_code_when_browser_shows_an_error_page_url():
    # Browsers often keep the full URL even when the connection failed.
    assert extract_code("https://localhost:8080/?code=q7w8e9") == "q7w8e9"


def test_accepts_a_bare_code():
    assert extract_code("  xk29fj  ") == "xk29fj"


def test_strips_quotes_from_shell_pasting():
    assert extract_code("'https://localhost:8080/?code=abc'") == "abc"


def test_url_without_a_code_explains_the_likely_cause():
    with pytest.raises(SystemExit, match="redirect URI"):
        extract_code("https://localhost:8080/?error=invalid_request")


def test_empty_paste_aborts():
    with pytest.raises(SystemExit):
        extract_code("   ")


def test_authorize_url_carries_the_redirect_and_code_response_type():
    url = authorize_url("my-client-id", "https://localhost:8080")
    assert url.startswith("https://api.login.yahoo.com/oauth2/request_auth?")
    assert "client_id=my-client-id" in url
    assert "response_type=code" in url
    # Redirect must be URL-encoded, not raw.
    assert "redirect_uri=https%3A%2F%2Flocalhost%3A8080" in url


def test_authorize_url_requests_the_fantasy_read_scope():
    """Without this, Yahoo issues a token that 403s on every Fantasy call."""
    url = authorize_url("cid", "oob")
    assert "scope=fspt-r" in url


def test_scope_can_be_overridden_or_omitted():
    assert "scope=openid" in authorize_url("cid", "oob", "openid")
    assert "scope=" not in authorize_url("cid", "oob", "")
