# ============================================================
#  scrapers/reddit_scraper.py — Scrape Reddit via public RSS
#
#  Reddit's .rss endpoints survived the 2023 API pricing changes
#  and remain publicly accessible in 2026 — confirmed working.
#  Just append .rss to any subreddit URL:
#    https://www.reddit.com/r/MachineLearning/hot.rss?limit=25
#
#  No API key. No OAuth. No account. No credentials to manage
#  or rotate. Uses feedparser which is already in requirements.txt.
#
#  Why not OAuth? Reddit now blocks new app registrations for
#  accounts that don't meet their "Responsible Builder Policy"
#  threshold (account too new or insufficient karma) — so we
#  can't reliably tell users to set up OAuth credentials.
#  The RSS approach works for anyone immediately.
# ============================================================

import feedparser
import logging
import time
import re
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    REDDIT_SUBREDDITS, KEYWORDS, MIN_ENGAGEMENT,
    REQUEST_DELAY,
)
from data.database import save_post, log_run

logger = logging.getLogger(__name__)

REDDIT_RSS_BASE = "https://www.reddit.com/r/{sub}/hot.rss?limit=25"
REDDIT_HEADERS  = {
    "User-Agent": "viral-scraper/1.0 (educational project; uses public RSS only)"
}


# ── Helpers ───────────────────────────────────────────────────

def contains_keyword(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in KEYWORDS)


def clean_html(text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_score_from_summary(summary: str) -> int:
    """
    Reddit RSS summary contains upvote count in the text:
    "submitted by /u/username ... [score hidden]" or "... 1234 points".
    Parse it best-effort; default 0 if not found.
    """
    match = re.search(r"(\d+)\s+point", summary or "", re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 0


def build_content(title: str, summary: str) -> str:
    """
    Reddit RSS gives us the title + a truncated summary.
    Combine them cleanly for storage and keyword matching.
    """
    title   = clean_html(title).strip()
    summary = clean_html(summary).strip()

    # Summary often starts with "submitted by /u/..." boilerplate — strip it
    summary = re.sub(
        r"^submitted by\s+/u/\S+\s*(\[link\])?\s*(\[comments\])?",
        "", summary, flags=re.IGNORECASE
    ).strip()

    if summary and len(summary) > 20:
        return f"{title}\n\n{summary[:600]}"
    return title


# ── Main scraper function ─────────────────────────────────────

def scrape_reddit() -> dict:
    """
    Scrape hot posts from all configured subreddits via Reddit's
    public RSS feeds. No authentication required.
    """
    started_at  = datetime.utcnow().isoformat()
    total_found = 0
    total_new   = 0
    errors      = []

    for subreddit in REDDIT_SUBREDDITS:
        url = REDDIT_RSS_BASE.format(sub=subreddit)
        try:
            feed = feedparser.parse(
                url,
                request_headers=REDDIT_HEADERS,
            )

            if feed.bozo and not feed.entries:
                raise ValueError(f"Bad feed: {feed.bozo_exception}")

            logger.info(f"r/{subreddit}: {len(feed.entries)} entries")

            for entry in feed.entries:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")
                content = build_content(title, summary)

                if not contains_keyword(content):
                    continue
                if len(content.strip()) < 30:
                    continue

                # Extract author — Reddit RSS gives "u/username"
                author = entry.get("author", "")
                author = re.sub(r"^/u/", "", author).strip()

                # Score from summary text (best-effort)
                score = extract_score_from_summary(summary)
                if score < MIN_ENGAGEMENT:
                    continue

                total_found += 1
                row_id = save_post(
                    source     = "reddit",
                    platform   = f"r/{subreddit}",
                    content    = content,
                    author     = author,
                    title      = clean_html(title),
                    url        = entry.get("link", ""),
                    engagement = score,
                )
                if row_id:
                    total_new += 1
                    logger.debug(f"  ✓ Saved r/{subreddit} post (score={score}) id={row_id}")

            time.sleep(REQUEST_DELAY)

        except Exception as e:
            msg = f"r/{subreddit} error: {e}"
            logger.error(msg)
            errors.append(msg)

    summary_dict = {
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

    logger.info(f"Reddit RSS scrape done — found {total_found}, new {total_new}")
    return summary_dict
