# ============================================================
#  scrapers/twitter_scraper.py  —  Scrape Twitter via Nitter
#
#  Nitter is an open-source Twitter frontend that exposes
#  RSS feeds for any public account — zero API key needed.
#
#  IMPORTANT CONTEXT (June 2026): Nitter has become structurally
#  unreliable as a platform. X/Twitter removed the guest-account
#  API that public Nitter instances relied on, so instances now
#  depend on rotating real-account credentials that get banned
#  routinely. Some instances hang for 60-120+ seconds before
#  timing out rather than failing fast. This file enforces hard
#  timeouts via httpx (NOT feedparser's built-in fetching, which
#  uses urllib and doesn't reliably respect timeouts on all
#  platforms) plus a global time budget across the whole run, so
#  a bad Nitter day degrades gracefully instead of hanging the
#  entire CI job. See config/settings.py for the current instance
#  list and time budget — check https://status.d420.de/ periodically,
#  since which instances work changes over time.
# ============================================================

import feedparser
import httpx
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
    TWITTER_SCRAPE_TIME_BUDGET,
    SKIP_TWITTER_SCRAPING,
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
    """
    Fetch and parse the RSS feed for one account from one Nitter instance.

    Uses httpx with an explicit timeout to do the actual HTTP fetch,
    THEN hands the raw text to feedparser just for XML parsing. This
    is the fix for the bug that caused 10-minute CI timeouts: calling
    feedparser.parse(url) directly lets feedparser do the fetching
    internally via urllib, which does NOT reliably honor timeouts on
    all platforms — so a slow/hanging Nitter instance could block for
    minutes per account with no way to bound it. httpx's timeout is
    enforced at the socket level and raises predictably.
    """
    url = f"{nitter_base.rstrip('/')}/{account}/rss"
    logger.debug(f"Fetching {url}")

    resp = httpx.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RSSReader/1.0)"},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()

    feed = feedparser.parse(resp.text)

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
    Stops early if TWITTER_SCRAPE_TIME_BUDGET is exceeded, so a
    widespread Nitter outage can't hang the whole CI job — we'd
    rather scrape 3 accounts successfully and move on to scoring/
    rewriting than time out the entire workflow trying to scrape 10.
    Returns a summary dict.
    """
    started_at    = datetime.utcnow().isoformat()

    if SKIP_TWITTER_SCRAPING:
        logger.info(
            "Twitter scraping skipped (SKIP_TWITTER_SCRAPING=True in settings.py). "
            "Nitter is structurally dead as of July 2026 — all public instances "
            "403 from GitHub Actions IPs. Twitter content is posted manually."
        )
        return {"source": "twitter", "posts_found": 0, "posts_new": 0, "errors": []}

    run_start_ts  = time.monotonic()
    total_found   = 0
    total_new     = 0
    errors        = []
    accounts_done = 0

    for account in TWITTER_ACCOUNTS:
        elapsed = time.monotonic() - run_start_ts
        if elapsed > TWITTER_SCRAPE_TIME_BUDGET:
            msg = (
                f"Twitter scrape time budget ({TWITTER_SCRAPE_TIME_BUDGET}s) exceeded "
                f"after {accounts_done}/{len(TWITTER_ACCOUNTS)} accounts — stopping early "
                f"so the rest of the pipeline (scoring, rewriting) still gets to run this cycle."
            )
            logger.warning(msg)
            errors.append(msg)
            break

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

        accounts_done += 1

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

    logger.info(
        f"Twitter scrape done — found {total_found}, new {total_new}, "
        f"{accounts_done}/{len(TWITTER_ACCOUNTS)} accounts processed"
    )
    return summary
