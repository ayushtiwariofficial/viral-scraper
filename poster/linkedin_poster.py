# ============================================================
#  poster/linkedin_poster.py  —  Phase 4: Post to LinkedIn
#
#  Uses Playwright browser automation since LinkedIn has no free
#  posting API (unchanged as of 2026 — still partner/enterprise
#  only). This automates the LinkedIn web UI directly.
#
#  IMPORTANT — read before using:
#  - This goes against LinkedIn's Terms of Service, which prohibit
#    automated activity. Real risk exists: temporary restrictions,
#    or in rare cases, account suspension. Mitigate this by:
#      1. Posting infrequently (a few times a day, not dozens)
#      2. Only posting human-approved content (see poster/notifier.py
#         and the approval workflow — nothing posts without you
#         explicitly approving that specific post ID first)
#      3. Reusing one saved login session rather than logging in
#         fresh every run, which looks far less like a bot
#      4. Not running this on a schedule — only on your explicit
#         manual trigger, post by post
#
#  SETUP (one-time, ~5 minutes):
#    1. Run: python -m poster.linkedin_poster --login
#       This opens a real (visible) browser window for you to log
#       into LinkedIn manually — including any 2FA challenge.
#    2. Once logged in, press Enter in the terminal. Your session
#       (cookies) gets saved to data/linkedin_session.json.
#    3. That file is in .gitignore — it never gets committed.
#       For GitHub Actions, you'll need to add it as a secret
#       (base64-encoded) — see SETUP.md for the exact steps.
#    4. From then on, runs reuse that saved session — no repeated
#       logins, which is both more reliable and less bot-like.
#    5. Sessions do expire eventually (LinkedIn-side, weeks to
#       months). If posting starts failing with a "not logged in"
#       error, just redo step 1.
# ============================================================

import logging
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import LINKEDIN_SESSION_PATH
from data.database import (
    init_db, get_content_by_id, set_approval_status,
    mark_posted_linkedin, log_run,
)
from datetime import datetime

logger = logging.getLogger(__name__)

LINKEDIN_POST_URL = "https://www.linkedin.com/feed/"


class LinkedInPostError(Exception):
    """Raised when posting fails for a reason that should stop and be investigated,
    rather than silently logged and skipped — posting is a one-shot, irreversible
    action, so failures here deserve more visibility than a scraper hiccup would."""
    pass


# ── One-time interactive login ──────────────────────────────────

def interactive_login():
    """
    Opens a real, visible browser window for you to log into LinkedIn
    manually. Saves the resulting session (cookies) to disk for reuse.
    Run this once via: python -m poster.linkedin_poster --login
    """
    from playwright.sync_api import sync_playwright

    print("\nOpening a browser window — please log into LinkedIn manually.")
    print("Complete any 2FA / verification steps, then come back here")
    print("and press Enter once you're fully logged in and see your feed.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.linkedin.com/login")

        input("Press Enter once you're logged in and viewing your feed... ")

        os.makedirs(os.path.dirname(LINKEDIN_SESSION_PATH), exist_ok=True)
        context.storage_state(path=LINKEDIN_SESSION_PATH)
        browser.close()

    print(f"\n✓ Session saved to {LINKEDIN_SESSION_PATH}")
    print("  This file is in .gitignore — never commit it.")
    print("  For GitHub Actions, see SETUP.md for how to add it as a secret.\n")


# ── Posting ───────────────────────────────────────────────────

def post_to_linkedin(text: str, max_retries: int = 2) -> str | None:
    """
    Post text content to LinkedIn using the saved session.
    Returns the post URL on success, or None on failure.
    Raises LinkedInPostError if the session is missing/expired —
    that needs your attention, not a silent skip.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

    if not os.path.exists(LINKEDIN_SESSION_PATH):
        raise LinkedInPostError(
            f"No saved LinkedIn session found at {LINKEDIN_SESSION_PATH}. "
            f"Run 'python -m poster.linkedin_poster --login' first."
        )

    for attempt in range(1, max_retries + 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(storage_state=LINKEDIN_SESSION_PATH)
                page = context.new_page()

                # IMPORTANT: don't wait for "networkidle" here. LinkedIn's feed
                # page has continuous background network activity (real-time
                # notification polling, presence pings, infinite-scroll
                # prefetching) that never actually goes idle — Playwright's own
                # docs warn "pages with continuous polling... may never reach
                # network idle." Waiting for it just guarantees a 30s timeout
                # on every single run, valid session or not (confirmed this
                # was misdiagnosing healthy sessions as "expired", because the
                # timeout fires mid-navigation while LinkedIn is still doing
                # its normal auth-refresh redirect dance).
                #
                # Instead: wait only for domcontentloaded (fast, reliable),
                # then wait for a concrete element that proves we're actually
                # on a logged-in feed page.
                page.goto(LINKEDIN_POST_URL, wait_until="domcontentloaded", timeout=30000)

                try:
                    # "Start a post" button only exists on the logged-in feed.
                    # If this appears, we know for certain the session is valid
                    # and the page is genuinely ready to interact with —
                    # far more reliable than checking page.url or networkidle.
                    page.get_by_role("button", name="Start a post").wait_for(
                        state="visible", timeout=15000
                    )
                except PlaywrightTimeout:
                    # Capture a screenshot BEFORE closing the browser — this is
                    # the single most useful piece of debugging info we're
                    # missing right now. It tells us definitively whether
                    # we're looking at a real login page, a LinkedIn security
                    # checkpoint/CAPTCHA (common when automating from a
                    # datacenter IP like GitHub Actions', even with valid
                    # cookies), or something else entirely.
                    screenshot_path = "logs/linkedin_failure.png"
                    try:
                        os.makedirs("logs", exist_ok=True)
                        page.screenshot(path=screenshot_path, full_page=True)
                        logger.warning(f"Saved failure screenshot to {screenshot_path}")
                    except Exception as screenshot_error:
                        logger.warning(f"Could not capture failure screenshot: {screenshot_error}")

                    current_url = page.url
                    browser.close()
                    if "login" in current_url or "checkpoint" in current_url:
                        raise LinkedInPostError(
                            f"LinkedIn session appears expired or blocked (redirected to "
                            f"{current_url}). This can mean the session genuinely expired, "
                            f"OR that LinkedIn is showing a security checkpoint because it "
                            f"doesn't recognize this IP/device — common when posting from "
                            f"GitHub Actions' datacenter IPs even with valid cookies. Check "
                            f"the uploaded screenshot artifact to see which. "
                            f"Re-run 'python -m poster.linkedin_poster --login' if it's a "
                            f"genuine expiry."
                        )
                    raise LinkedInPostError(
                        f"Could not find 'Start a post' button after loading the feed "
                        f"(current URL: {current_url}). LinkedIn may have changed their page "
                        f"layout, or the page loaded unusually slowly. Check the screenshot "
                        f"artifact, or re-run 'python -m poster.linkedin_poster --login' "
                        f"if this persists."
                    )

                # Open the post composer
                start_post_button = page.get_by_role("button", name="Start a post")
                start_post_button.click(timeout=10000)

                # Type into the post text area
                editor = page.locator(".ql-editor[contenteditable='true']")
                editor.wait_for(state="visible", timeout=10000)
                editor.click()
                editor.type(text, delay=15)   # delay = more human-like typing pace

                time.sleep(1)   # brief pause before posting, again for a less bot-like pattern

                # Click the actual "Post" button
                post_button = page.get_by_role("button", name="Post", exact=True)
                post_button.click(timeout=10000)

                # Wait for the composer to close, confirming the post went through
                page.wait_for_selector(".ql-editor", state="hidden", timeout=15000)

                # Best-effort: grab the profile URL as a stand-in "posted" link,
                # since LinkedIn doesn't make it trivial to grab the exact new
                # post's permalink right after posting.
                profile_url = page.url

                browser.close()
                return profile_url

        except LinkedInPostError:
            raise   # don't retry session errors — they won't fix themselves
        except PlaywrightTimeout as e:
            logger.warning(f"LinkedIn posting timed out (attempt {attempt}): {e}")
            if attempt == max_retries:
                return None
            time.sleep(5)
        except Exception as e:
            logger.warning(f"LinkedIn posting failed (attempt {attempt}): {e}")
            if attempt == max_retries:
                return None
            time.sleep(5)

    return None


# ── Approval + posting workflow ──────────────────────────────────

def approve_and_post(content_id: int) -> dict:
    """
    The core Phase 4 safety gate: given a content_queue ID, verify
    it's still pending approval, mark it approved, post it to
    LinkedIn, and record the result. This is the ONLY path that
    posts content — there is no automatic, unattended posting.
    """
    started_at = datetime.utcnow().isoformat()
    init_db()

    row = get_content_by_id(content_id)
    if row is None:
        msg = f"No content found with id={content_id}"
        logger.error(msg)
        return {"source": "linkedin_poster", "posted": False, "error": msg}

    row = dict(row)

    if row["posted_linkedin"]:
        msg = f"Post #{content_id} was already posted to LinkedIn on {row['posted_at']}"
        logger.warning(msg)
        return {"source": "linkedin_poster", "posted": False, "error": msg}

    if row["approval_status"] == "rejected":
        msg = f"Post #{content_id} was previously rejected — not posting"
        logger.warning(msg)
        return {"source": "linkedin_poster", "posted": False, "error": msg}

    # Build the final text: post body + hashtags
    text = row["linkedin_post"]
    if row["hashtags"]:
        tags = " ".join(f"#{t.strip()}" for t in row["hashtags"].split(",") if t.strip())
        text = f"{text}\n\n{tags}"

    logger.info(f"Posting #{content_id} to LinkedIn ({len(text)} chars)...")
    set_approval_status(content_id, "approved")

    try:
        post_url = post_to_linkedin(text)
    except LinkedInPostError as e:
        msg = str(e)
        logger.error(msg)
        log_run(source="linkedin_poster", posts_found=1, posts_new=0, started_at=started_at, error=msg)
        return {"source": "linkedin_poster", "posted": False, "error": msg}

    if post_url is None:
        msg = f"Post #{content_id} failed to post after retries — check logs above for details"
        logger.error(msg)
        log_run(source="linkedin_poster", posts_found=1, posts_new=0, started_at=started_at, error=msg)
        return {"source": "linkedin_poster", "posted": False, "error": msg}

    mark_posted_linkedin(content_id, post_url=post_url)
    logger.info(f"✓ Post #{content_id} successfully posted to LinkedIn")
    log_run(source="linkedin_poster", posts_found=1, posts_new=1, started_at=started_at)

    return {"source": "linkedin_poster", "posted": True, "post_url": post_url}


def reject_post(content_id: int) -> dict:
    """Mark a post as rejected — it will never be posted, and won't
    be suggested again."""
    init_db()
    row = get_content_by_id(content_id)
    if row is None:
        return {"source": "linkedin_poster", "rejected": False, "error": f"No content found with id={content_id}"}

    set_approval_status(content_id, "rejected")
    logger.info(f"Post #{content_id} marked as rejected")
    return {"source": "linkedin_poster", "rejected": True}


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LinkedIn poster — Phase 4")
    parser.add_argument("--login", action="store_true", help="One-time interactive login to save your session")
    parser.add_argument("--approve", type=int, metavar="ID", help="Approve and post content_queue ID")
    parser.add_argument("--reject", type=int, metavar="ID", help="Reject content_queue ID (never post it)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.login:
        interactive_login()
    elif args.approve:
        result = approve_and_post(args.approve)
        print(result)
        # Without this, the CLI always exits 0 even when posting genuinely
        # fails — which is exactly why the GitHub Actions workflow showed a
        # green checkmark on a run that never actually posted anything.
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
    main()
