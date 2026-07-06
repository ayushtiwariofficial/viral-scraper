# ============================================================
#  poster/linkedin_oauth.py  —  LinkedIn OAuth 2.0 flow
#
#  Replaces Playwright browser automation entirely. LinkedIn's
#  official "Share on LinkedIn" (w_member_social) permission is
#  self-serve, no approval needed — this is the same mechanism
#  Buffer/Hootsuite/Taplio use. Because it's a sanctioned API call
#  (not a scraped browser session), it does NOT trigger the
#  datacenter-IP fraud detection that blocked Playwright posting
#  from GitHub Actions.
#
#  One-time setup (run locally, not in CI — needs a real browser
#  to complete the LinkedIn consent screen):
#      python -m poster.linkedin_oauth --login
#
#  This opens LinkedIn's consent page, waits for the redirect,
#  exchanges the code for tokens, and saves them to Supabase.
#  After that, poster/linkedin_poster.py reads tokens from
#  Supabase and refreshes them automatically as needed — no
#  more manual session files, no more base64 secrets to update.
# ============================================================

import http.server
import logging
import os
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.database import save_linkedin_tokens, get_linkedin_tokens

logger = logging.getLogger(__name__)

LINKEDIN_CLIENT_ID     = os.getenv("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET", "")

# Must exactly match a redirect URL registered in your LinkedIn app's
# Auth settings (developer.linkedin.com -> your app -> Auth tab).
REDIRECT_URI = os.getenv("LINKEDIN_REDIRECT_URI", "http://localhost:8765/callback")
CALLBACK_PORT = 8765

AUTH_URL     = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL    = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

# openid + profile: identify the user. w_member_social: permission to
# post on their behalf. All three are self-serve, no LinkedIn approval needed.
SCOPES = "openid profile w_member_social"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches LinkedIn's OAuth redirect and extracts the authorization code."""
    received_code = None
    received_error = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _CallbackHandler.received_code = params["code"][0]
            body = "<h2>✓ Logged in — you can close this tab and return to the terminal.</h2>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        elif "error" in params:
            # A genuine OAuth failure — LinkedIn itself reported an error
            # (e.g. you clicked "Cancel" on the consent screen).
            _CallbackHandler.received_error = params.get(
                "error_description", params.get("error", ["Unknown error"])
            )[0]
            body = f"<h2>✗ LinkedIn login failed: {_CallbackHandler.received_error}</h2>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        else:
            # Anything else — most commonly the browser's automatic
            # /favicon.ico request right after rendering the success page.
            # This is NOT an OAuth error; it has neither "code" nor "error"
            # because it's not a callback at all. Silently return 404 and,
            # critically, do NOT touch received_code/received_error — doing
            # so previously caused a race where this spurious request
            # stamped "Unknown error" milliseconds after a perfectly valid
            # code had already been received, incorrectly failing logins
            # that had actually succeeded.
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass   # suppress default request logging — keeps terminal output clean


def _wait_for_callback(timeout: int = 120) -> str:
    """
    Run a tiny local HTTP server just long enough to catch the single
    OAuth redirect from LinkedIn, then shut down immediately.
    """
    _CallbackHandler.received_code = None
    _CallbackHandler.received_error = None

    with socketserver.TCPServer(("localhost", CALLBACK_PORT), _CallbackHandler) as httpd:
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        waited = 0
        while _CallbackHandler.received_code is None and _CallbackHandler.received_error is None:
            time.sleep(0.5)
            waited += 0.5
            if waited >= timeout:
                httpd.shutdown()
                raise TimeoutError(
                    f"No callback received within {timeout}s. Did you complete "
                    f"the LinkedIn login in the browser window?"
                )

        httpd.shutdown()

    # Check for a valid code FIRST — if we got one, use it, even if some
    # later spurious request (e.g. a stray favicon fetch) also happened to
    # set received_error. A real authorization code is definitive proof
    # the login succeeded; nothing that arrives after it can retroactively
    # invalidate that.
    if _CallbackHandler.received_code:
        return _CallbackHandler.received_code

    if _CallbackHandler.received_error:
        raise RuntimeError(f"LinkedIn login failed: {_CallbackHandler.received_error}")

    raise RuntimeError("No authorization code or error received from LinkedIn.")


def _exchange_code_for_tokens(code: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": LINKEDIN_CLIENT_ID,
            "client_secret": LINKEDIN_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _get_person_urn(access_token: str) -> str:
    """Fetch the LinkedIn member's ID via the OpenID Connect userinfo endpoint."""
    resp = httpx.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    sub = resp.json()["sub"]
    return f"urn:li:person:{sub}"


def refresh_access_token(refresh_token: str) -> dict:
    """
    Exchange a refresh token for a new access token. LinkedIn access
    tokens last 60 days; refresh tokens last 365 days. Called
    automatically by poster/linkedin_poster.py when the stored token
    is close to expiring.
    """
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": LINKEDIN_CLIENT_ID,
            "client_secret": LINKEDIN_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def interactive_login():
    """
    One-time interactive OAuth flow. Run this locally (needs a real
    browser to show LinkedIn's consent screen) — not in CI.
    """
    if not LINKEDIN_CLIENT_ID or not LINKEDIN_CLIENT_SECRET:
        print(
            "\nERROR: LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET must be set "
            "in your .env file first.\nGet these from developer.linkedin.com → "
            "your app → Auth tab.\n"
        )
        return

    params = {
        "response_type": "code",
        "client_id": LINKEDIN_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": "viral-scraper-login",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    print("\nOpening a browser window — log into LinkedIn and approve access.")
    print(f"If it doesn't open automatically, visit:\n{auth_url}\n")
    print(f"Waiting for you to complete login (listening on {REDIRECT_URI})...\n")

    webbrowser.open(auth_url)

    try:
        code = _wait_for_callback(timeout=120)
    except TimeoutError as e:
        print(f"\n✗ {e}")
        return
    except RuntimeError as e:
        print(f"\n✗ {e}")
        return

    print("✓ Got authorization code, exchanging for tokens...")
    tokens = _exchange_code_for_tokens(code)

    access_token  = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    expires_in    = tokens.get("expires_in", 5184000)          # ~60 days default
    refresh_expires_in = tokens.get("refresh_token_expires_in", 31536000)  # ~365 days default

    person_urn = _get_person_urn(access_token)

    now = datetime.now(timezone.utc)
    access_expires_at  = (now + timedelta(seconds=expires_in)).isoformat()
    refresh_expires_at = (now + timedelta(seconds=refresh_expires_in)).isoformat()

    save_linkedin_tokens(
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=access_expires_at,
        refresh_token_expires_at=refresh_expires_at,
        person_urn=person_urn,
    )

    print(f"\n✓ Connected as {person_urn}")
    print(f"✓ Access token saved to Supabase (expires in ~{expires_in // 86400} days)")
    print(f"✓ Refresh token saved (expires in ~{refresh_expires_in // 86400} days)")
    print("\nYou're all set — no more manual session files or secrets to update.")
    print("Tokens will refresh automatically when needed.\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    interactive_login()
