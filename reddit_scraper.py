# ============================================================
#  scrapers/reddit_scraper.py  —  Scrape Reddit free JSON API
#
#  Reddit exposes a free JSON endpoint for any subreddit:
#  reddit.com/r/{sub}/hot.json  — no API key, no OAuth.
# ============================================================

import httpx
import logging
import time
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    REDDIT_SUBREDDITS, KEYWORDS, MIN_ENGAGEMENT,
    REQUEST_TIMEOUT, REQUEST_DELAY,
)
from data.database import save_post, log_run

logger = logging.getLogger(__name__)

REDDIT_BASE   = "https://www.reddit.com"
HEADERS       = {"User-Agent": "viral-scraper/1.0 (educational project)"}
POSTS_PER_SUB = 25   # fetch top 25 hot posts per subreddit


# ── Helpers ───────────────────────────────────────────────────

def contains_keyword(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in KEYWORDS)


def build_content(post: dict) -> str:
    """Combine title + selftext into a single content string."""
    title    = post.get("title", "").strip()
    selftext = post.get("selftext", "").strip()

    # Truncate very long posts — we only need the core idea
    if len(selftext) > 800:
        selftext = selftext[:800] + "..."

    if selftext:
        return f"{title}\n\n{selftext}"
    return title


# ── Main scraper function ─────────────────────────────────────

def scrape_reddit() -> dict:
    """
    Scrape hot posts from all configured subreddits.
    Uses the free Reddit JSON API — no key required.
    """
    started_at  = datetime.utcnow().isoformat()
    total_found = 0
    total_new   = 0
    errors      = []

    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=HEADERS) as client:
        for subreddit in REDDIT_SUBREDDITS:
            try:
                url  = f"{REDDIT_BASE}/r/{subreddit}/hot.json?limit={POSTS_PER_SUB}"
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()

                posts = data.get("data", {}).get("children", [])
                logger.info(f"r/{subreddit}: fetched {len(posts)} posts")

                for item in posts:
                    p = item.get("data", {})

                    # Skip removed / deleted / stickied posts
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
                msg = f"r/{subreddit} HTTP {e.response.status_code}"
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
