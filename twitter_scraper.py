# ============================================================
#  scrapers/twitter_scraper.py  —  Scrape Twitter via Nitter
#
#  Nitter is an open-source Twitter frontend that exposes
#  RSS feeds for any public account — zero API key needed.
# ============================================================

import feedparser
import logging
import time
import re
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    NITTER_INSTANCES, TWITTER_ACCOUNTS,
    KEYWORDS, MIN_ENGAGEMENT,
    REQUEST_TIMEOUT, REQUEST_DELAY,
)
from data.database import save_post, log_run

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────

def contains_keyword(text: str) -> bool:
    """Return True if text contains at least one of our niche keywords."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in KEYWORDS)


def clean_tweet(text: str) -> str:
    """Strip HTML tags and extra whitespace from Nitter RSS entries."""
    text = re.sub(r"<[^>]+>", " ", text)           # remove HTML tags
    text = re.sub(r"http\S+", "", text)             # remove URLs
    text = re.sub(r"\s+", " ", text).strip()        # normalise whitespace
    return text


def parse_engagement(entry: dict) -> int:
    """
    Nitter RSS doesn't expose like counts — we use a rough proxy:
    entries with images/videos tend to have more engagement so we
    score them slightly higher. Returns a best-effort integer.
    """
    score = 0
    summary = entry.get("summary", "")
    if "<img" in summary:
        score += 20   # has media
    if len(summary) > 200:
        score += 10   # longer tweet → more substance
    return score


def fetch_rss(nitter_base: str, account: str) -> list[dict]:
    """Fetch and parse the RSS feed for one account from one Nitter instance."""
    url = f"{nitter_base.rstrip('/')}/{account}/rss"
    logger.debug(f"Fetching {url}")

    feed = feedparser.parse(url, request_headers={
        "User-Agent": "Mozilla/5.0 (compatible; RSSReader/1.0)"
    })

    if feed.bozo and not feed.entries:
        raise ValueError(f"Bad feed from {url}: {feed.bozo_exception}")

    posts = []
    for entry in feed.entries:
        text = clean_tweet(entry.get("summary", "") or entry.get("title", ""))
        if not text or len(text) < 30:
            continue
        if not contains_keyword(text):
            continue

        posts.append({
            "author":     account,
            "content":    text,
            "url":        entry.get("link", ""),
            "engagement": parse_engagement(entry),
            "published":  entry.get("published", ""),
        })

    return posts


# ── Main scraper function ─────────────────────────────────────

def scrape_twitter() -> dict:
    """
    Scrape all configured Twitter accounts via Nitter RSS.
    Tries each Nitter instance in order — falls back if one is down.
    Returns a summary dict.
    """
    started_at   = datetime.utcnow().isoformat()
    total_found  = 0
    total_new    = 0
    errors       = []

    for account in TWITTER_ACCOUNTS:
        account_saved = False

        for instance in NITTER_INSTANCES:
            try:
                posts = fetch_rss(instance, account)
                logger.info(f"@{account}: {len(posts)} relevant posts via {instance}")

                for post in posts:
                    total_found += 1
                    row_id = save_post(
                        source     = "twitter",
                        platform   = "nitter",
                        content    = post["content"],
                        author     = post["author"],
                        url        = post["url"],
                        engagement = post["engagement"],
                    )
                    if row_id:
                        total_new += 1
                        logger.debug(f"  ✓ Saved new post id={row_id} from @{account}")

                account_saved = True
                time.sleep(REQUEST_DELAY)   # be polite between requests
                break                       # success — don't try other instances

            except Exception as e:
                logger.warning(f"Instance {instance} failed for @{account}: {e}")
                continue

        if not account_saved:
            msg = f"All Nitter instances failed for @{account}"
            logger.error(msg)
            errors.append(msg)

    summary = {
        "source":      "twitter",
        "posts_found": total_found,
        "posts_new":   total_new,
        "errors":      errors,
    }

    log_run(
        source      = "twitter",
        posts_found = total_found,
        posts_new   = total_new,
        started_at  = started_at,
        error       = "; ".join(errors) if errors else None,
    )

    logger.info(f"Twitter scrape done — found {total_found}, new {total_new}")
    return summary
