# ============================================================
#  scrapers/reddit_scraper.py  —  Scrape Reddit via OAuth API
#
#  IMPORTANT: As of May 2026, Reddit deprecated unauthenticated
#  .json endpoint access entirely — it now returns a hard 403
#  for every unauthenticated request, regardless of User-Agent
#  or IP. This isn't a transient block, it's a permanent policy
#  change (confirmed via Reddit's own developer announcements).
#  This scraper uses OAuth2 "script" app credentials instead,
#  which remain free for personal/non-commercial use at this
#  volume (60-100 requests/minute, we use ~8 every 2 hours).
#
#  Setup (5 minutes, free, no business justification needed):
#    1. https://www.reddit.com/prefs/apps -> "create another app..."
#    2. App type: "script"
#    3. Redirect URI: http://localhost:8080 (required but unused)
#    4. Copy the Client ID (under the app name) and Secret
#    5. Add to .env / GitHub Actions secrets:
#       REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET,
#       REDDIT_USERNAME, REDDIT_PASSWORD
#       (username/password = your Reddit login — "script" apps
#       authenticate this way, it's the standard OAuth flow for
#       this app type, not a workaround)
# ============================================================

import httpx
import logging
import time
import os
from datetime import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    REDDIT_SUBREDDITS, KEYWORDS, MIN_ENGAGEMENT,
    REQUEST_TIMEOUT, REQUEST_DELAY,
)
from data.database import save_post, log_run

logger = logging.getLogger(__name__)

REDDIT_OAUTH_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API_BASE  = "https://oauth.reddit.com"
POSTS_PER_SUB    = 25

# OAuth credentials — read directly from environment, same pattern
# as GROQ_API_KEY / GEMINI_API_KEY elsewhere in this project.
REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME      = os.getenv("REDDIT_USERNAME", "")
REDDIT_PASSWORD      = os.getenv("REDDIT_PASSWORD", "")

# Reddit requires a descriptive User-Agent identifying your app —
# generic/missing User-Agents get flagged and rate-limited harder.
USER_AGENT = "viral-scraper/1.0 (by /u/{})".format(REDDIT_USERNAME or "unknown")


# ── Helpers ───────────────────────────────────────────────────

def contains_keyword(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in KEYWORDS)


def build_content(post: dict) -> str:
    """Combine title + selftext into a single content string."""
    title    = post.get("title", "").strip()
    selftext = post.get("selftext", "").strip()

    if len(selftext) > 800:
        selftext = selftext[:800] + "..."

    if selftext:
        return f"{title}\n\n{selftext}"
    return title


def get_access_token() -> str | None:
    """
    Authenticate with Reddit's OAuth2 password grant flow.
    Returns a bearer token valid for 1 hour, or None if auth fails.
    This is the standard flow for "script" type Reddit apps —
    not a workaround, this is how Reddit's docs say to do it.
    """
    if not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD]):
        logger.error(
            "Reddit OAuth credentials missing. Set REDDIT_CLIENT_ID, "
            "REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD "
            "(see scrapers/reddit_scraper.py header for setup steps)."
        )
        return None

    try:
        resp = httpx.post(
            REDDIT_OAUTH_URL,
            auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            data={
                "grant_type": "password",
                "username": REDDIT_USERNAME,
                "password": REDDIT_PASSWORD,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            logger.error(f"Reddit OAuth response missing access_token: {resp.json()}")
            return None
        return token

    except httpx.HTTPStatusError as e:
        # Surface Reddit's actual error body instead of a generic status code —
        # common causes: wrong password, 2FA enabled (script apps can't use 2FA
        # accounts directly), or app type isn't "script".
        try:
            err_detail = e.response.json()
        except Exception:
            err_detail = e.response.text[:200]
        logger.error(f"Reddit OAuth failed ({e.response.status_code}): {err_detail}")
        return None
    except Exception as e:
        logger.error(f"Reddit OAuth request failed: {e}")
        return None


# ── Main scraper function ─────────────────────────────────────

def scrape_reddit() -> dict:
    """
    Scrape hot posts from all configured subreddits via Reddit's
    official OAuth API. Free tier, ~60-100 req/min — comfortably
    covers our 8 subreddits every 2 hours.
    """
    started_at  = datetime.utcnow().isoformat()
    total_found = 0
    total_new   = 0
    errors      = []

    token = get_access_token()
    if not token:
        msg = "Could not authenticate with Reddit — skipping this run"
        log_run(source="reddit", posts_found=0, posts_new=0, started_at=started_at, error=msg)
        return {"source": "reddit", "posts_found": 0, "posts_new": 0, "errors": [msg]}

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }

    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=headers) as client:
        for subreddit in REDDIT_SUBREDDITS:
            try:
                url  = f"{REDDIT_API_BASE}/r/{subreddit}/hot"
                resp = client.get(url, params={"limit": POSTS_PER_SUB})
                resp.raise_for_status()
                data = resp.json()

                posts = data.get("data", {}).get("children", [])
                logger.info(f"r/{subreddit}: fetched {len(posts)} posts")

                for item in posts:
                    p = item.get("data", {})

                    if p.get("removed_by_category") or p.get("stickied"):
                        continue

                    score = p.get("score", 0)
                    if score < MIN_ENGAGEMENT:
                        continue

                    content = build_content(p)
                    if not contains_keyword(content):
                        continue

                    if len(content.strip()) < 30:
                        continue

                    total_found += 1
                    row_id = save_post(
                        source     = "reddit",
                        platform   = f"r/{subreddit}",
                        content    = content,
                        author     = p.get("author", ""),
                        title      = p.get("title", ""),
                        url        = f"https://reddit.com{p.get('permalink', '')}",
                        engagement = score + p.get("num_comments", 0),
                    )
                    if row_id:
                        total_new += 1
                        logger.debug(f"  ✓ Saved r/{subreddit} post (score={score}) id={row_id}")

                time.sleep(REQUEST_DELAY)

            except httpx.HTTPStatusError as e:
                try:
                    err_detail = e.response.json()
                except Exception:
                    err_detail = e.response.text[:200]
                msg = f"r/{subreddit} HTTP {e.response.status_code}: {err_detail}"
                logger.error(msg)
                errors.append(msg)
            except Exception as e:
                msg = f"r/{subreddit} error: {e}"
                logger.error(msg)
                errors.append(msg)

    summary = {
        "source":      "reddit",
        "posts_found": total_found,
        "posts_new":   total_new,
        "errors":      errors,
    }

    log_run(
        source      = "reddit",
        posts_found = total_found,
        posts_new   = total_new,
        started_at  = started_at,
        error       = "; ".join(errors) if errors else None,
    )

    logger.info(f"Reddit scrape done — found {total_found}, new {total_new}")
    return summary
