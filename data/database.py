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
    status        TEXT    DEFAULT 'raw',     -- 'raw'|'scored'|'queued'|'rewritten'|'posted'|'skipped'|'rewrite_failed'
    rewrite_attempts INTEGER DEFAULT 0       -- tracks retries to avoid infinite loops
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
    posted_at       TEXT,
    posted_linkedin_url TEXT,                -- profile/post URL after successful LinkedIn post
    approval_status TEXT    DEFAULT 'pending', -- 'pending' | 'approved' | 'rejected'
    notified        INTEGER DEFAULT 0         -- 0 = no notification sent yet, 1 = sent
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
    """Create all tables if they don't exist, and migrate older DBs safely."""
    with get_db() as conn:
        conn.executescript(SCHEMA)

        # Migrations for columns added after the initial schema — safe to run
        # every time, since SQLite raises "duplicate column" if it already
        # exists and we just ignore that specific error.
        migrations = [
            "ALTER TABLE raw_posts ADD COLUMN rewrite_attempts INTEGER DEFAULT 0",
            "ALTER TABLE content_queue ADD COLUMN posted_linkedin_url TEXT",
            "ALTER TABLE content_queue ADD COLUMN approval_status TEXT DEFAULT 'pending'",
            "ALTER TABLE content_queue ADD COLUMN notified INTEGER DEFAULT 0",
        ]
        for stmt in migrations:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    logger.info(f"Database ready at {DB_PATH}")


# ── Core helpers (Phase 1) ────────────────────────────────────

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


# ── Phase 2: AI scoring helpers ───────────────────────────────

def get_unscored_posts(limit: int = 50):
    """Fetch raw posts not yet scored, highest engagement first."""
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM raw_posts WHERE status = 'raw' ORDER BY engagement DESC LIMIT ?",
            (limit,)
        ).fetchall()


def save_score(
    raw_post_id: int,
    virality_score: float,
    relevance_score: float,
    uniqueness_score: float,
) -> int:
    """
    Save an AI-generated score for a post and mark it as 'scored'.
    total_score is a weighted average — virality matters most,
    since that's what drives growth.
    """
    total_score = round(
        (virality_score * 0.5) + (relevance_score * 0.3) + (uniqueness_score * 0.2),
        2
    )

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO scored_posts
                (raw_post_id, virality_score, relevance_score, uniqueness_score, total_score, scored_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (raw_post_id, virality_score, relevance_score, uniqueness_score,
             total_score, datetime.utcnow().isoformat())
        )
        conn.execute(
            "UPDATE raw_posts SET status = 'scored' WHERE id = ?",
            (raw_post_id,)
        )
    return cursor.lastrowid


def mark_skipped(raw_post_id: int, reason: str = "") -> None:
    """Mark a post as skipped (e.g. AI scoring failed, or it was junk)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE raw_posts SET status = 'skipped' WHERE id = ?",
            (raw_post_id,)
        )
    if reason:
        logger.debug(f"Post {raw_post_id} skipped: {reason}")


def get_top_scored_posts(limit: int = 5, min_score: float = 6.0):
    """
    Fetch the highest-scoring posts that haven't been queued yet.
    Joins raw_posts + scored_posts, ordered by total_score descending.
    """
    with get_db() as conn:
        return conn.execute(
            """
            SELECT
                r.id, r.source, r.platform, r.author, r.title,
                r.content, r.url, r.engagement,
                s.virality_score, s.relevance_score, s.uniqueness_score, s.total_score
            FROM raw_posts r
            JOIN scored_posts s ON s.raw_post_id = r.id
            WHERE r.status = 'scored' AND s.total_score >= ?
            ORDER BY s.total_score DESC
            LIMIT ?
            """,
            (min_score, limit)
        ).fetchall()


def mark_queued(raw_post_id: int) -> None:
    """Mark a post as queued for rewriting (Phase 3)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE raw_posts SET status = 'queued' WHERE id = ?",
            (raw_post_id,)
        )


def get_scoring_stats() -> dict:
    """Quick breakdown of the scoring pipeline status."""
    with get_db() as conn:
        raw     = conn.execute("SELECT COUNT(*) FROM raw_posts WHERE status='raw'").fetchone()[0]
        scored  = conn.execute("SELECT COUNT(*) FROM raw_posts WHERE status='scored'").fetchone()[0]
        queued  = conn.execute("SELECT COUNT(*) FROM raw_posts WHERE status='queued'").fetchone()[0]
        skipped = conn.execute("SELECT COUNT(*) FROM raw_posts WHERE status='skipped'").fetchone()[0]
        avg_score = conn.execute("SELECT AVG(total_score) FROM scored_posts").fetchone()[0]
    return {
        "raw": raw, "scored": scored, "queued": queued, "skipped": skipped,
        "avg_score": round(avg_score, 2) if avg_score else 0,
    }


# ── Phase 3: Content rewriting helpers ────────────────────────

def get_queued_posts_without_content(limit: int = 10, max_attempts: int = 3):
    """
    Fetch posts marked 'queued' that don't yet have a content_queue
    entry — these are ready for Phase 3 rewriting. Excludes posts
    that have already failed max_attempts times, so we don't burn
    API quota retrying something that's permanently broken.
    """
    with get_db() as conn:
        return conn.execute(
            """
            SELECT r.* FROM raw_posts r
            WHERE r.status = 'queued'
              AND r.rewrite_attempts < ?
              AND r.id NOT IN (SELECT raw_post_id FROM content_queue)
            ORDER BY r.id
            LIMIT ?
            """,
            (max_attempts, limit)
        ).fetchall()


def record_rewrite_failure(raw_post_id: int, max_attempts: int = 3) -> None:
    """
    Increment a post's rewrite_attempts counter. Once it hits
    max_attempts, mark it 'rewrite_failed' so it stops being
    picked up by future runs.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE raw_posts SET rewrite_attempts = rewrite_attempts + 1 WHERE id = ?",
            (raw_post_id,)
        )
        row = conn.execute(
            "SELECT rewrite_attempts FROM raw_posts WHERE id = ?",
            (raw_post_id,)
        ).fetchone()
        if row and row["rewrite_attempts"] >= max_attempts:
            conn.execute(
                "UPDATE raw_posts SET status = 'rewrite_failed' WHERE id = ?",
                (raw_post_id,)
            )
            logger.warning(f"Post {raw_post_id} gave up after {max_attempts} failed rewrite attempts")


def save_rewritten_content(
    raw_post_id: int,
    twitter_thread: list[str],
    linkedin_post: str,
    hashtags: list[str],
) -> int:
    """
    Save AI-rewritten content for a post and mark it 'rewritten'.
    twitter_thread is stored as a JSON array; hashtags as comma-separated.
    """
    import json as _json

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO content_queue
                (raw_post_id, twitter_thread, linkedin_post, hashtags, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                raw_post_id,
                _json.dumps(twitter_thread),
                linkedin_post,
                ", ".join(hashtags),
                datetime.utcnow().isoformat(),
            )
        )
        conn.execute(
            "UPDATE raw_posts SET status = 'rewritten' WHERE id = ?",
            (raw_post_id,)
        )
    return cursor.lastrowid


def get_unposted_content(limit: int = 10):
    """Fetch rewritten content not yet posted to either platform (for Phase 4)."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT cq.*, r.source AS original_source, r.author AS original_author, r.url AS original_url
            FROM content_queue cq
            JOIN raw_posts r ON r.id = cq.raw_post_id
            WHERE cq.posted_twitter = 0 OR cq.posted_linkedin = 0
            ORDER BY cq.created_at
            LIMIT ?
            """,
            (limit,)
        ).fetchall()


def get_rewrite_stats() -> dict:
    """Quick breakdown of the rewriting pipeline status."""
    with get_db() as conn:
        queued_unwritten = conn.execute(
            "SELECT COUNT(*) FROM raw_posts WHERE status='queued'"
        ).fetchone()[0]
        rewritten = conn.execute(
            "SELECT COUNT(*) FROM raw_posts WHERE status='rewritten'"
        ).fetchone()[0]
        rewrite_failed = conn.execute(
            "SELECT COUNT(*) FROM raw_posts WHERE status='rewrite_failed'"
        ).fetchone()[0]
        total_in_queue = conn.execute(
            "SELECT COUNT(*) FROM content_queue"
        ).fetchone()[0]
        ready_to_post = conn.execute(
            "SELECT COUNT(*) FROM content_queue WHERE posted_twitter=0 AND posted_linkedin=0"
        ).fetchone()[0]
    return {
        "queued_awaiting_rewrite": queued_unwritten,
        "rewritten": rewritten,
        "rewrite_failed": rewrite_failed,
        "total_in_content_queue": total_in_queue,
        "ready_to_post": ready_to_post,
    }


# ── Phase 4: notification + LinkedIn approval/posting helpers ────

def get_unnotified_content(limit: int = 10):
    """
    Fetch content_queue rows that haven't been notified about yet —
    these are fresh rewrites the user hasn't seen a draft/approval
    request for. Joins raw_posts for source/author context, same
    pattern as get_unposted_content().
    """
    with get_db() as conn:
        return conn.execute(
            """
            SELECT cq.*, r.source AS original_source, r.author AS original_author, r.url AS original_url
            FROM content_queue cq
            JOIN raw_posts r ON r.id = cq.raw_post_id
            WHERE cq.notified = 0
            ORDER BY cq.created_at
            LIMIT ?
            """,
            (limit,)
        ).fetchall()


def mark_notified(content_id: int) -> None:
    """Mark a content_queue row as having been notified about."""
    with get_db() as conn:
        conn.execute(
            "UPDATE content_queue SET notified = 1 WHERE id = ?",
            (content_id,)
        )


def get_content_by_id(content_id: int):
    """Fetch a single content_queue row by ID, with source context joined in."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT cq.*, r.source AS original_source, r.author AS original_author, r.url AS original_url
            FROM content_queue cq
            JOIN raw_posts r ON r.id = cq.raw_post_id
            WHERE cq.id = ?
            """,
            (content_id,)
        ).fetchone()


def set_approval_status(content_id: int, status: str) -> None:
    """Set a content_queue row's approval_status: 'pending' | 'approved' | 'rejected'."""
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"Invalid approval_status: {status!r}")
    with get_db() as conn:
        conn.execute(
            "UPDATE content_queue SET approval_status = ? WHERE id = ?",
            (status, content_id)
        )


def mark_posted_linkedin(content_id: int, post_url: str = None) -> None:
    """Mark a content_queue row as successfully posted to LinkedIn."""
    with get_db() as conn:
        conn.execute(
            """
            UPDATE content_queue
            SET posted_linkedin = 1, posted_at = ?, posted_linkedin_url = ?
            WHERE id = ?
            """,
            (datetime.utcnow().isoformat(), post_url, content_id)
        )


def mark_posted_twitter(content_id: int) -> None:
    """
    Mark a content_queue row as posted to Twitter. Since Twitter is
    manual (the API isn't free for new developers as of Feb 2026),
    this is set by the user confirming they posted the draft manually
    — see run_scraper.py --mark-twitter-posted.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE content_queue SET posted_twitter = 1, posted_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), content_id)
        )
