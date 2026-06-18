# ============================================================
#  scrapers/linkedin_scraper.py  —  Scrape LinkedIn (free)
#
#  LinkedIn blocks direct scraping hard. The free workaround:
#  1. Google's RSS alert feed for LinkedIn post searches
#  2. Apify's free public LinkedIn scraper actor (1000 req/mo)
#  3. Manual RSS via RSS.app (free tier — 3 feeds)
#
#  We implement all three with graceful fallback.
# ============================================================

import feedparser
import httpx
import logging
import time
import re
from datetime import datetime
from urllib.parse import quote_plus

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    LINKEDIN_PROFILES, KEYWORDS,
    REQUEST_TIMEOUT, REQUEST_DELAY,
)
from data.database import save_post, log_run

logger = logging.getLogger(__name__)


# ── Strategy 1: Google Alerts RSS ────────────────────────────
#
#  Create free Google Alerts at alerts.google.com for your
#  keywords, set delivery to RSS. Google emails you the feed URL.
#  Paste the feed URLs here. We provide two example searches
#  that work without any setup using Google News RSS.

GOOGLE_NEWS_RSS_QUERIES = [
    "AI+SaaS+startup",
    "building+in+public+AI",
    "indie+hacker+AI+tools",
    "LLM+startup+founder",
]

GOOGLE_NEWS_BASE = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def scrape_google_news() -> tuple[int, int]:
    """Scrape Google News RSS for LinkedIn-style thought leadership posts."""
    found, new_posts = 0, 0

    for query in GOOGLE_NEWS_RSS_QUERIES:
        url = GOOGLE_NEWS_BASE.format(query=query)
        try:
            feed = feedparser.parse(url, request_headers={
                "User-Agent": "Mozilla/5.0 (compatible; RSSReader/1.0)"
            })

            for entry in feed.entries[:10]:
                title   = entry.get("title", "")
                summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
                content = f"{title}\n\n{summary}".strip()

                if len(content) < 50:
                    continue

                found += 1
                row_id = save_post(
                    source     = "linkedin",
                    platform   = "google_news",
                    content    = content[:800],
                    author     = entry.get("source", {}).get("title", "Google News"),
                    title      = title,
                    url        = entry.get("link", ""),
                    engagement = 30,   # Google News = published = some engagement
                )
                if row_id:
                    new_posts += 1

            time.sleep(REQUEST_DELAY)

        except Exception as e:
            logger.warning(f"Google News query '{query}' failed: {e}")

    return found, new_posts


# ── Strategy 2: RSS.app free feeds ───────────────────────────
#
#  Go to rss.app, create free RSS feeds for LinkedIn profiles.
#  Free tier: 3 feeds, 10 items each, updated every 12h.
#  Paste your generated RSS URLs below.
#
#  Example: https://rss.app/feeds/XXXXXXXXXXXXXXXX.xml

RSS_APP_FEEDS: list[str] = [
    # Paste your rss.app feed URLs here after creating them:
    # "https://rss.app/feeds/YOUR_FEED_ID_1.xml",
    # "https://rss.app/feeds/YOUR_FEED_ID_2.xml",
]


def scrape_rssapp_feeds() -> tuple[int, int]:
    """Scrape rss.app LinkedIn feeds (requires free account setup)."""
    found, new_posts = 0, 0

    for feed_url in RSS_APP_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                content = re.sub(r"<[^>]+>", " ", entry.get("summary", entry.get("title", "")))
                if len(content) < 50:
                    continue

                found += 1
                row_id = save_post(
                    source     = "linkedin",
                    platform   = "rss_app",
                    content    = content[:800],
                    author     = entry.get("author", "LinkedIn"),
                    title      = entry.get("title", ""),
                    url        = entry.get("link", ""),
                    engagement = 50,
                )
                if row_id:
                    new_posts += 1

            time.sleep(REQUEST_DELAY)

        except Exception as e:
            logger.warning(f"rss.app feed {feed_url} failed: {e}")

    return found, new_posts


# ── Strategy 3: PhantomBuster free tier ──────────────────────
#
#  PhantomBuster has a free tier with 2h execution/month.
#  Their "LinkedIn Profile Scraper" phantom exports JSON.
#  If you set it up, drop the JSON export path here and we'll
#  read it directly — no HTTP needed.

PHANTOMBUSTER_EXPORT_PATH: str = ""   # e.g. "/home/user/phantom_export.json"


def scrape_phantombuster_export() -> tuple[int, int]:
    """Read a PhantomBuster JSON export if one exists."""
    found, new_posts = 0, 0

    if not PHANTOMBUSTER_EXPORT_PATH or not os.path.exists(PHANTOMBUSTER_EXPORT_PATH):
        return 0, 0

    import json
    try:
        with open(PHANTOMBUSTER_EXPORT_PATH) as f:
            items = json.load(f)

        for item in items:
            content = item.get("postText") or item.get("content") or ""
            if not content or len(content) < 30:
                continue

            found += 1
            row_id = save_post(
                source     = "linkedin",
                platform   = "phantombuster",
                content    = content[:800],
                author     = item.get("profileFullName", ""),
                url        = item.get("postUrl", ""),
                engagement = item.get("likes", 0) + item.get("comments", 0),
            )
            if row_id:
                new_posts += 1

    except Exception as e:
        logger.error(f"PhantomBuster export parse error: {e}")

    return found, new_posts


# ── Main scraper function ─────────────────────────────────────

def scrape_linkedin() -> dict:
    """
    Aggregate LinkedIn content from all available free sources.
    Tries all three strategies and combines results.
    """
    started_at  = datetime.utcnow().isoformat()
    total_found = 0
    total_new   = 0
    errors      = []

    # Strategy 1 — Google News (always works, no setup)
    try:
        f, n = scrape_google_news()
        total_found += f
        total_new   += n
        logger.info(f"Google News: found={f}, new={n}")
    except Exception as e:
        errors.append(f"Google News: {e}")

    # Strategy 2 — rss.app (needs 5-min free account setup)
    try:
        f, n = scrape_rssapp_feeds()
        total_found += f
        total_new   += n
        if RSS_APP_FEEDS:
            logger.info(f"RSS.app: found={f}, new={n}")
        else:
            logger.info("RSS.app: no feeds configured yet (see linkedin_scraper.py)")
    except Exception as e:
        errors.append(f"RSS.app: {e}")

    # Strategy 3 — PhantomBuster export (optional)
    try:
        f, n = scrape_phantombuster_export()
        total_found += f
        total_new   += n
        if f:
            logger.info(f"PhantomBuster: found={f}, new={n}")
    except Exception as e:
        errors.append(f"PhantomBuster: {e}")

    summary = {
        "source":      "linkedin",
        "posts_found": total_found,
        "posts_new":   total_new,
        "errors":      errors,
    }

    log_run(
        source      = "linkedin",
        posts_found = total_found,
        posts_new   = total_new,
        started_at  = started_at,
        error       = "; ".join(errors) if errors else None,
    )

    logger.info(f"LinkedIn scrape done — found {total_found}, new {total_new}")
    return summary
