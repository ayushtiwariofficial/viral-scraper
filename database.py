# ============================================================
#  data/database.py  —  SQLite storage layer
# ============================================================

import sqlite3
import hashlib
import logging
from datetime import datetime
from contextlib import contextmanager

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_PATH

logger = logging.getLogger(__name__)


# ── Schema ───────────────────────────────────────────────────

SCHEMA = """
-- Raw posts collected from all sources
CREATE TABLE IF NOT EXISTS raw_posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash  TEXT    UNIQUE NOT NULL,   -- SHA256 of content (dedup key)
    source        TEXT    NOT NULL,          -- 'twitter' | 'reddit' | 'rss' | 'linkedin'
    platform      TEXT    NOT NULL,          -- e.g. 'nitter', 'reddit.com'
    author        TEXT,
    title         TEXT,                      -- used for reddit/rss titles
    content       TEXT    NOT NULL,
    url           TEXT,
    engagement    INTEGER DEFAULT 0,         -- likes / upvotes / comments
    scraped_at    TEXT    NOT NULL,
    status        TEXT    DEFAULT 'raw'      -- 'raw' | 'scored' | 'queued' | 'posted' | 'skipped'
);

-- Virality scores assigned by AI (Phase 2)
CREATE TABLE IF NOT EXISTS scored_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_post_id     INTEGER REFERENCES raw_posts(id),
    virality_score  REAL,                    -- 1–10
    relevance_score REAL,                    -- 1–10
    uniqueness_score REAL,                   -- 1–10
    total_score     REAL,
    scored_at       TEXT    NOT NULL
);

-- Rewritten content ready to post (Phase 3)
CREATE TABLE IF NOT EXISTS content_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_post_id     INTEGER REFERENCES raw_posts(id),
    twitter_thread  TEXT,                    -- JSON array of tweet strings
    linkedin_post   TEXT,
    hashtags        TEXT,                    -- comma-separated
    created_at      TEXT    NOT NULL,
    scheduled_at    TEXT,                    -- when to post
    posted_twitter  INTEGER DEFAULT 0,       -- 0 = no, 1 = yes
    posted_linkedin INTEGER DEFAULT 0,
    posted_at       TEXT
);

-- Scraper run history
CREATE TABLE IF NOT EXISTS scraper_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    source       TEXT    NOT NULL,
    posts_found  INTEGER DEFAULT 0,
    posts_new    INTEGER DEFAULT 0,
    error        TEXT
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_raw_hash   ON raw_posts(content_hash);
CREATE INDEX IF NOT EXISTS idx_raw_status ON raw_posts(status);
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_posts(source);
CREATE INDEX IF NOT EXISTS idx_queue_posted ON content_queue(posted_twitter, posted_linkedin);
"""


# ── Connection helper ─────────────────────────────────────────

@contextmanager
def get_db():
    """Context manager — always closes the connection."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL") # safe for concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Init ──────────────────────────────────────────────────────

def init_db():
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript(SCHEMA)
    logger.info(f"Database ready at {DB_PATH}")


# ── Core helpers ──────────────────────────────────────────────

def make_hash(content: str) -> str:
    """SHA256 fingerprint of post content for deduplication."""
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()


def is_duplicate(content_hash: str) -> bool:
    """Return True if we've already seen this content."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM raw_posts WHERE content_hash = ?",
            (content_hash,)
        ).fetchone()
    return row is not None


def save_post(
    source: str,
    platform: str,
    content: str,
    author: str = None,
    title: str = None,
    url: str = None,
    engagement: int = 0,
) -> int | None:
    """
    Save a post to raw_posts.
    Returns the new row ID, or None if it was a duplicate.
    """
    content_hash = make_hash(content)

    if is_duplicate(content_hash):
        return None

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO raw_posts
                (content_hash, source, platform, author, title, content, url, engagement, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_hash, source, platform,
                author, title, content, url,
                engagement, datetime.utcnow().isoformat(),
            )
        )
    return cursor.lastrowid


def log_run(source: str, posts_found: int, posts_new: int,
            started_at: str, error: str = None) -> None:
    """Record a scraper run in the history table."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO scraper_runs
                (started_at, finished_at, source, posts_found, posts_new, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                datetime.utcnow().isoformat(),
                source, posts_found, posts_new, error,
            )
        )


# ── Read helpers (used later in Phase 2 + 3) ─────────────────

def get_unscored_posts(limit: int = 50):
    """Fetch raw posts not yet scored."""
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM raw_posts WHERE status = 'raw' ORDER BY engagement DESC LIMIT ?",
            (limit,)
        ).fetchall()


def get_stats() -> dict:
    """Quick summary for the status dashboard."""
    with get_db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM raw_posts").fetchone()[0]
        today   = conn.execute(
            "SELECT COUNT(*) FROM raw_posts WHERE scraped_at >= date('now')"
        ).fetchone()[0]
        queued  = conn.execute(
            "SELECT COUNT(*) FROM raw_posts WHERE status = 'queued'"
        ).fetchone()[0]
        posted  = conn.execute(
            "SELECT COUNT(*) FROM content_queue WHERE posted_twitter=1 OR posted_linkedin=1"
        ).fetchone()[0]
        runs    = conn.execute(
            "SELECT * FROM scraper_runs ORDER BY started_at DESC LIMIT 5"
        ).fetchall()
    return {
        "total_posts": total,
        "scraped_today": today,
        "in_queue": queued,
        "posted": posted,
        "recent_runs": [dict(r) for r in runs],
    }
