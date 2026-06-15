# ============================================================
#  scrapers/rss_scraper.py  —  Scrape RSS feeds / newsletters
#
#  Covers: Hacker News, TLDR AI, Ben's Bites, The Batch,
#  O'Reilly Radar, Hacker Noon — all free, no key needed.
# ============================================================

import feedparser
import httpx
import logging
import time
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    RSS_FEEDS, KEYWORDS,
    REQUEST_TIMEOUT, REQUEST_DELAY,
)
from data.database import save_post, log_run

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────

def contains_keyword(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in KEYWORDS)


def clean_html(text: str) -> str:
    """Strip HTML and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)      # HTML entities
    text = re.sub(r"http\S+", "", text)         # remove URLs
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_content(entry: dict) -> str:
    """Pull the best available content from an RSS entry."""
    # Try full content first, then summary, then title
    if entry.get("content"):
        raw = entry["content"][0].get("value", "")
    elif entry.get("summary"):
        raw = entry["summary"]
    else:
        raw = entry.get("title", "")

    cleaned = clean_html(raw)

    # Truncate very long articles — we just need the key insight
    if len(cleaned) > 1000:
        cleaned = cleaned[:1000] + "..."

    return cleaned


def estimate_engagement(entry: dict) -> int:
    """
    RSS has no engagement data so we use recency as a proxy:
    entries published in the last 24h get a higher base score.
    """
    try:
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub:
            from time import mktime, time as now
            age_hours = (now() - mktime(pub)) / 3600
            if age_hours < 6:   return 100
            if age_hours < 24:  return 50
            if age_hours < 72:  return 20
    except Exception:
        pass
    return 5


def feed_name(url: str) -> str:
    """Extract a readable source name from a feed URL."""
    url = url.lower()
    if "bensbites"     in url: return "Ben's Bites"
    if "tldr"          in url: return "TLDR AI"
    if "deeplearning"  in url: return "The Batch"
    if "oreilly"       in url: return "O'Reilly Radar"
    if "hackernoon"    in url: return "Hacker Noon"
    if "ycombinator"   in url: return "Hacker News"
    # Fallback: use domain
    import urllib.parse
    return urllib.parse.urlparse(url).netloc


# ── Main scraper function ─────────────────────────────────────

def scrape_rss() -> dict:
    """
    Scrape all configured RSS feeds.
    feedparser handles the HTTP request internally.
    """
    started_at  = datetime.utcnow().isoformat()
    total_found = 0
    total_new   = 0
    errors      = []

    for feed_url in RSS_FEEDS:
        source_name = feed_name(feed_url)
        try:
            feed = feedparser.parse(
                feed_url,
                request_headers={
                    "User-Agent": "Mozilla/5.0 (compatible; RSSReader/1.0)",
                    "Accept": "application/rss+xml, application/atom+xml, */*",
                }
            )

            if not feed.entries:
                logger.warning(f"{source_name}: no entries found (feed may be down)")
                continue

            logger.info(f"{source_name}: {len(feed.entries)} entries")

            for entry in feed.entries:
                title   = clean_html(entry.get("title", ""))
                content = extract_content(entry)

                # Check keywords against title + content combined
                if not contains_keyword(f"{title} {content}"):
                    continue

                if len(content.strip()) < 50:
                    continue

                total_found += 1
                row_id = save_post(
                    source     = "rss",
                    platform   = source_name,
                    content    = content,
                    author     = entry.get("author", source_name),
                    title      = title,
                    url        = entry.get("link", ""),
                    engagement = estimate_engagement(entry),
                )
                if row_id:
                    total_new += 1
                    logger.debug(f"  ✓ Saved [{source_name}] '{title[:60]}...' id={row_id}")

            time.sleep(REQUEST_DELAY)

        except Exception as e:
            msg = f"{source_name} error: {e}"
            logger.error(msg)
            errors.append(msg)

    summary = {
        "source":      "rss",
        "posts_found": total_found,
        "posts_new":   total_new,
        "errors":      errors,
    }

    log_run(
        source      = "rss",
        posts_found = total_found,
        posts_new   = total_new,
        started_at  = started_at,
        error       = "; ".join(errors) if errors else None,
    )

    logger.info(f"RSS scrape done — found {total_found}, new {total_new}")
    return summary
