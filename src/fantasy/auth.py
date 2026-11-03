"""Yahoo OAuth2 login.

Yahoo rejects `localhost` and `127.0.0.1` redirect URIs outright (the authorize
step fails with `invalid redirect uri` no matter what the app has registered),
so the usual "run a callback server on :8080" OAuth dance is not available for
a desktop script. The supported path is out-of-band: Yahoo shows the user a
code to copy. That's the default here.

A real registered https redirect still works if you have one -- `extract_code`
takes either a bare code or the full redirected URL.

Tokens are written in the exact shape `yahoo_oauth` expects, so the SDK picks
them up and refreshes them on its own from then on. `callback_uri` is stored
alongside them because Yahoo requires the same redirect_uri on refresh.
"""
from __future__ import annotations

import time
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import OAUTH_FILE, ensure_oauth_file, env, read_json, save_json

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
DEFAULT_REDIRECT = "oob"


def redirect_uri() -> str:
    return env("YAHOO_REDIRECT_URI", DEFAULT_REDIRECT) or DEFAULT_REDIRECT


def authorize_url(client_id: str, redirect: str) -> str:
    return f"{AUTH_URL}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "language": "en-us",
        }
    )


def extract_code(pasted: str) -> str:
    """Accept either a bare code or the whole redirected URL."""
    pasted = (pasted or "").strip().strip('"').strip("'")
    if not pasted:
        raise SystemExit("Nothing pasted; aborting.")
    if "code=" in pasted:
        query = urlparse(pasted).query or pasted.partition("?")[2]
        codes = parse_qs(query).get("code")
        if codes and codes[0]:
            return codes[0]
        raise SystemExit(f"Could not find a code in: {pasted[:120]}")
    if pasted.startswith("http"):
        raise SystemExit(
            "That URL has no ?code= in it. If Yahoo showed an error instead of "
            "redirecting, the redirect URI in .env must match the app exactly."
        )
    return pasted


def exchange_code(code: str, redirect: str) -> dict:
    """Trade the authorization code for access + refresh tokens."""
    credentials = read_json(OAUTH_FILE, {}) or {}
    client_id = credentials["consumer_key"]
    client_secret = credentials["consumer_secret"]

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
            "code": code,
        },
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(_explain_failure(response, redirect))

    payload = response.json()
    stored = {
        "consumer_key": client_id,
        "consumer_secret": client_secret,
        # yahoo_oauth reads this back and reuses it when refreshing.
        "callback_uri": redirect,
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "token_type": payload.get("token_type", "bearer"),
        "token_time": time.time(),
    }
    if "xoauth_yahoo_guid" in payload:
        stored["guid"] = payload["xoauth_yahoo_guid"]
    save_json(OAUTH_FILE, stored)
    return stored


def _explain_failure(response: requests.Response, redirect: str) -> str:
    """Turn Yahoo's terse OAuth errors into the actual fix."""
    body = response.text[:400]
    hints = {
        "invalid_grant": (
            "The code was already used or expired. Codes are single-use and "
            "short-lived -- re-run `fantasy auth` and paste a fresh one."
        ),
        "invalid_redirect_uri": (
            f"Yahoo rejected redirect_uri={redirect!r}. It must match a Redirect "
            "URI on the app *exactly* (including https:// and port). Check "
            "YAHOO_REDIRECT_URI in .env against the Yahoo app settings."
        ),
        "invalid_client": (
            "Client id/secret rejected. Re-copy both from the Yahoo app page "
            "into .env -- the secret is easy to truncate."
        ),
        "INVALID_AUTHORIZATION_CODE": (
            "Yahoo didn't accept that code. Paste the full redirected URL "
            "rather than retyping the code by hand."
        ),
    }
    for marker, hint in hints.items():
        if marker.lower() in body.lower():
            return f"Token exchange failed ({response.status_code}).\n{hint}\n\nYahoo said: {body}"
    return f"Token exchange failed ({response.status_code}).\nYahoo said: {body}"


def login(open_browser: bool = True, pasted: str | None = None) -> dict:
    """Run the interactive consent flow and cache the tokens."""
    ensure_oauth_file()  # validates credentials exist, writes consumer key/secret
    credentials = read_json(OAUTH_FILE, {}) or {}
    redirect = redirect_uri()
    url = authorize_url(credentials["consumer_key"], redirect)

    print("1. Approve access in the browser window that just opened.")
    print("   (If it didn't open, paste this URL yourself:)\n")
    print(f"   {url}\n")
    if redirect != "oob":
        print(f"2. Yahoo will send you to {redirect} , which will show a")
        print("   connection error -- that is expected, nothing is running there.")
        print("   Copy the FULL URL out of the address bar; it contains ?code=...\n")
    else:
        print("2. Yahoo will show you a verifier code.\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if pasted is None:
        prompt = (
            "3. Paste the full redirected URL here: "
            if redirect != "oob"
            else "3. Paste the verifier code here: "
        )
        pasted = input(prompt)

    return exchange_code(extract_code(pasted), redirect)


def have_token() -> bool:
    data = read_json(OAUTH_FILE, {})
    return isinstance(data, dict) and bool(data.get("access_token"))
