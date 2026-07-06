# ============================================================
#  poster/linkedin_poster.py  —  Post to LinkedIn via official API
#
#  Replaces the old Playwright browser-automation approach.
#  That approach failed because LinkedIn applies step-up security
#  challenges to sensitive actions (like posting) from datacenter
#  IPs — confirmed via a captured screenshot showing a "Welcome
#  back, re-enter password" prompt triggered mid-click, even with
#  valid session cookies. No amount of session-file juggling fixes
#  this, because it's a live server-side risk decision.
#
#  The fix: use LinkedIn's official Posts API (Community Management
#  API) via OAuth instead of scraping a browser. This is a sanctioned,
#  authenticated API call — the same mechanism Buffer/Hootsuite/Taplio
#  use — so it does not trigger the datacenter-IP fraud detection at
#  all. It also removes the ToS-violation concern entirely, since
#  this is LinkedIn's own supported integration path.
#
#  One-time setup: python -m poster.linkedin_oauth --login
#  (see poster/linkedin_oauth.py)
# ============================================================

import logging
import os
import sys
import argparse
from datetime import datetime, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.database import (
    get_content_by_id, set_approval_status, mark_posted_linkedin,
    get_linkedin_tokens, save_linkedin_tokens, log_run,
)
from poster.linkedin_oauth import refresh_access_token

logger = logging.getLogger(__name__)

POSTS_API_URL = "https://api.linkedin.com/rest/posts"

# LinkedIn versions its API by calendar month (YYYYMM). Bump this
# periodically — LinkedIn deprecates old versions after ~1 year, but
# any version from the last several months works fine.
LINKEDIN_API_VERSION = "202506"


class LinkedInPostError(Exception):
    """Raised when a LinkedIn post could not be created."""
    pass


def _get_valid_access_token() -> tuple[str, str]:
    """
    Fetch the stored LinkedIn tokens, refreshing the access token if
    it's expired or close to expiring. Returns (access_token, person_urn).
    """
    tokens = get_linkedin_tokens()
    if not tokens:
        raise LinkedInPostError(
            "No LinkedIn account connected. Run 'python -m poster.linkedin_oauth "
            "--login' locally first."
        )

    expires_at = datetime.fromisoformat(tokens["access_token_expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    buffer_seconds = 86400   # refresh a day early rather than cutting it exactly

    if (expires_at - now).total_seconds() > buffer_seconds:
        return tokens["access_token"], tokens["person_urn"]

    # Access token expired or expiring soon — refresh it
    logger.info("LinkedIn access token expiring soon, refreshing...")
    if not tokens.get("refresh_token"):
        raise LinkedInPostError(
            "LinkedIn access token expired and no refresh token is stored. "
            "Run 'python -m poster.linkedin_oauth --login' again."
        )

    try:
        new_tokens = refresh_access_token(tokens["refresh_token"])
    except httpx.HTTPStatusError as e:
        raise LinkedInPostError(
            f"Failed to refresh LinkedIn token ({e.response.status_code}): "
            f"{e.response.text[:200]}. Re-run 'python -m poster.linkedin_oauth --login'."
        )

    from datetime import timedelta
    new_access_token = new_tokens["access_token"]
    new_refresh_token = new_tokens.get("refresh_token", tokens["refresh_token"])
    expires_in = new_tokens.get("expires_in", 5184000)
    new_expires_at = (now + timedelta(seconds=expires_in)).isoformat()

    save_linkedin_tokens(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        access_token_expires_at=new_expires_at,
        refresh_token_expires_at=tokens.get("refresh_token_expires_at"),
        person_urn=tokens["person_urn"],
    )
    logger.info("✓ LinkedIn access token refreshed successfully")

    return new_access_token, tokens["person_urn"]


def post_to_linkedin(text: str) -> str:
    """
    Publish a text post to LinkedIn via the official Posts API.
    Returns the new post's URN (e.g. 'urn:li:share:12345').
    Raises LinkedInPostError on failure.
    """
    access_token, person_urn = _get_valid_access_token()

    if len(text) > 3000:
        # LinkedIn's commentary field has a hard 3000-char limit
        text = text[:2997] + "..."
        logger.warning("Post text exceeded 3000 chars — truncated to fit LinkedIn's limit")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": LINKEDIN_API_VERSION,
    }
    payload = {
        "author": person_urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }

    try:
        resp = httpx.post(POSTS_API_URL, headers=headers, json=payload, timeout=20)
    except Exception as e:
        raise LinkedInPostError(f"Request to LinkedIn API failed: {e}")

    if resp.status_code != 201:
        raise LinkedInPostError(
            f"LinkedIn API returned {resp.status_code}: {resp.text[:300]}"
        )

    post_urn = resp.headers.get("x-restli-id", "unknown")
    logger.info(f"✓ Posted to LinkedIn — {post_urn}")
    return post_urn


# ── Approval workflow functions (unchanged interface) ─────────

def approve_and_post(content_id: int) -> dict:
    """
    Approve and immediately post a piece of content to LinkedIn.
    Returns a result dict with 'posted': True/False.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    row = get_content_by_id(content_id)

    if not row:
        msg = f"No content found with id={content_id}"
        logger.error(msg)
        return {"source": "linkedin_poster", "posted": False, "error": msg}

    if row.get("approval_status") == "rejected":
        msg = f"Post #{content_id} was previously rejected — not posting"
        logger.warning(msg)
        return {"source": "linkedin_poster", "posted": False, "error": msg}

    if row.get("posted_linkedin"):
        msg = f"Post #{content_id} was already posted to LinkedIn"
        logger.warning(msg)
        return {"source": "linkedin_poster", "posted": False, "error": msg}

    linkedin_text = row.get("linkedin_post", "")
    logger.info(f"Posting #{content_id} to LinkedIn ({len(linkedin_text)} chars)...")

    set_approval_status(content_id, "approved")

    try:
        post_urn = post_to_linkedin(linkedin_text)
    except LinkedInPostError as e:
        logger.error(str(e))
        log_run(source="linkedin_poster", posts_found=1, posts_new=0,
                started_at=started_at, error=str(e))
        return {"source": "linkedin_poster", "posted": False, "error": str(e)}

    mark_posted_linkedin(content_id, post_url=post_urn)
    log_run(source="linkedin_poster", posts_found=1, posts_new=1, started_at=started_at)

    return {"source": "linkedin_poster", "posted": True, "url": post_urn}


def reject_post(content_id: int) -> dict:
    """Mark a piece of content as rejected — it will never be posted."""
    row = get_content_by_id(content_id)
    if not row:
        return {"source": "linkedin_poster", "rejected": False, "error": f"No content found with id={content_id}"}

    set_approval_status(content_id, "rejected")
    logger.info(f"Post #{content_id} marked as rejected")
    return {"source": "linkedin_poster", "rejected": True}


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LinkedIn posting (via official API)")
    parser.add_argument("--approve", type=int, metavar="ID", help="Approve and post a content_queue ID")
    parser.add_argument("--reject", type=int, metavar="ID", help="Reject a content_queue ID")
    args = parser.parse_args()

    if args.approve:
        result = approve_and_post(args.approve)
        print(result)
        if not result.get("posted"):
            sys.exit(1)
    elif args.reject:
        result = reject_post(args.reject)
        print(result)
        if not result.get("rejected"):
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
