# ============================================================
#  data/database.py  —  Supabase (Postgres) storage layer
#
#  Migrated from SQLite + GitHub Actions artifacts after hitting
#  an unresolved GitHub bug: actions/upload-artifact@v4 moved to
#  Azure Blob storage, and the artifact download API redirects to
#  a SAS-signed Azure URL that rejects requests carrying an
#  Authorization header — causing persistent 401s with no fix on
#  our end (confirmed via multiple open GitHub community threads).
#
#  Supabase's free tier (500MB Postgres, generous API limits) is
#  a real always-on database — no more artifact/cache juggling
#  between workflow runs. It also sets up cleanly for the future
#  multi-user SaaS phase via Row Level Security (see the schema
#  file's note on this).
#
#  Every function below has the EXACT same name and signature as
#  the old SQLite version, so ai/scorer.py, ai/rewriter.py,
#  poster/*.py, and run_scraper.py did not need any changes.
# ============================================================

import hashlib
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

_client = None


def _get_client():
    """Lazily create and cache the Supabase client."""
    global _client
    if _client is not None:
        return _client

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise EnvironmentError(
            "SUPABASE_URL and SUPABASE_KEY must be set (as env vars locally, "
            "or as GitHub Actions secrets). See SUPABASE_SETUP.md."
        )

    from supabase import create_client
    _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def _now() -> str:
    """ISO timestamp string, timezone-aware, for timestamptz columns."""
    return datetime.now(timezone.utc).isoformat()


# ── Init ──────────────────────────────────────────────────────

def init_db():
    """
    No-op for Supabase — tables are created once via supabase_schema.sql
    in the Supabase SQL Editor, not per-run like SQLite's CREATE TABLE IF
    NOT EXISTS. This function just verifies the connection works, so
    callers get an early, clear error instead of a confusing failure
    deep inside some later query.
    """
    client = _get_client()
    try:
        client.table("raw_posts").select("id").limit(1).execute()
        logger.info(f"Connected to Supabase at {SUPABASE_URL}")
    except Exception as e:
        raise RuntimeError(
            f"Could not query Supabase — has supabase_schema.sql been run yet? "
            f"See SUPABASE_SETUP.md. Original error: {e}"
        )


# ── Core helpers (Phase 1) ────────────────────────────────────

def make_hash(content: str) -> str:
    """SHA256 fingerprint of post content for deduplication."""
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()


def is_duplicate(content_hash: str) -> bool:
    """Return True if we've already seen this content."""
    client = _get_client()
    resp = (
        client.table("raw_posts")
        .select("id")
        .eq("content_hash", content_hash)
        .limit(1)
        .execute()
    )
    return len(resp.data) > 0


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

    client = _get_client()
    resp = (
        client.table("raw_posts")
        .insert({
            "content_hash": content_hash,
            "source": source,
            "platform": platform,
            "author": author,
            "title": title,
            "content": content,
            "url": url,
            "engagement": engagement,
            "scraped_at": _now(),
        })
        .execute()
    )
    return resp.data[0]["id"] if resp.data else None


def log_run(source: str, posts_found: int, posts_new: int,
            started_at: str, error: str = None) -> None:
    """Record a scraper run in the history table."""
    client = _get_client()
    client.table("scraper_runs").insert({
        "started_at": started_at,
        "finished_at": _now(),
        "source": source,
        "posts_found": posts_found,
        "posts_new": posts_new,
        "error": error,
    }).execute()


def get_stats() -> dict:
    """Quick summary for the status dashboard."""
    client = _get_client()

    total = client.table("raw_posts").select("id", count="exact").execute()
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    today = (
        client.table("raw_posts")
        .select("id", count="exact")
        .gte("scraped_at", today_iso)
        .execute()
    )
    queued = (
        client.table("raw_posts")
        .select("id", count="exact")
        .eq("status", "queued")
        .execute()
    )
    posted = (
        client.table("content_queue")
        .select("id", count="exact")
        .or_("posted_twitter.eq.1,posted_linkedin.eq.1")
        .execute()
    )
    runs = (
        client.table("scraper_runs")
        .select("*")
        .order("started_at", desc=True)
        .limit(5)
        .execute()
    )

    return {
        "total_posts": total.count or 0,
        "scraped_today": today.count or 0,
        "in_queue": queued.count or 0,
        "posted": posted.count or 0,
        "recent_runs": runs.data,
    }


# ── Phase 2: AI scoring helpers ───────────────────────────────

def get_unscored_posts(limit: int = 50):
    """Fetch raw posts not yet scored, highest engagement first."""
    client = _get_client()
    resp = (
        client.table("raw_posts")
        .select("*")
        .eq("status", "raw")
        .order("engagement", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data


def save_score(
    raw_post_id: int,
    virality_score: float,
    relevance_score: float,
    uniqueness_score: float,
) -> int:
    """
    Save an AI-generated score for a post and mark it as 'scored'.
    total_score is a weighted average — virality matters most.
    """
    total_score = round(
        (virality_score * 0.5) + (relevance_score * 0.3) + (uniqueness_score * 0.2),
        2
    )

    client = _get_client()
    resp = (
        client.table("scored_posts")
        .insert({
            "raw_post_id": raw_post_id,
            "virality_score": virality_score,
            "relevance_score": relevance_score,
            "uniqueness_score": uniqueness_score,
            "total_score": total_score,
            "scored_at": _now(),
        })
        .execute()
    )
    client.table("raw_posts").update({"status": "scored"}).eq("id", raw_post_id).execute()
    return resp.data[0]["id"] if resp.data else None


def mark_skipped(raw_post_id: int, reason: str = "") -> None:
    """Mark a post as skipped (e.g. AI scoring failed, or it was junk)."""
    client = _get_client()
    client.table("raw_posts").update({"status": "skipped"}).eq("id", raw_post_id).execute()
    if reason:
        logger.debug(f"Post {raw_post_id} skipped: {reason}")


def get_top_scored_posts(limit: int = 5, min_score: float = 6.0):
    """
    Fetch the highest-scoring posts that haven't been queued yet.

    Deliberately avoids filtering on an embedded/joined resource's column
    (previously: .eq("raw_posts.status", "scored") on a scored_posts query
    with raw_posts!inner(...) embedded). That pattern is a known tricky
    area with PostgREST — if the filter isn't applied as strictly as
    intended, already-processed posts could keep getting re-selected as
    "top scored" candidates indefinitely, since their scored_posts row
    would still show a high total_score even after their raw_post moved
    on to 'queued' or 'rewritten'. This directly matched a real symptom:
    the same handful of posts kept getting "found" and re-queued every
    run, which then blocked genuinely new posts from ever being rewritten.

    Instead: fetch the set of raw_post ids that are CURRENTLY 'scored'
    via a simple, unambiguous single-table query, then only consider
    scored_posts rows whose raw_post_id is in that set.
    """
    client = _get_client()

    # Simple, unambiguous: which raw_posts are actually still 'scored' right now?
    still_scored = (
        client.table("raw_posts")
        .select("id, source, platform, author, title, content, url, engagement, status")
        .eq("status", "scored")
        .execute()
    )
    still_scored_map = {r["id"]: r for r in still_scored.data}

    if not still_scored_map:
        return []

    # Fetch high-scoring candidates — overfetch a bit since some will get
    # filtered out below if they're no longer actually 'scored'.
    resp = (
        client.table("scored_posts")
        .select("*")
        .gte("total_score", min_score)
        .order("total_score", desc=True)
        .limit(limit * 5 + 20)   # generous overfetch, cheap and simple
        .execute()
    )

    results = []
    for row in resp.data:
        raw_post_id = row.get("raw_post_id")
        if raw_post_id in still_scored_map:
            merged = {**still_scored_map[raw_post_id], **row}
            results.append(merged)
        if len(results) >= limit:
            break

    return results


def mark_queued(raw_post_id: int) -> None:
    """Mark a post as queued for rewriting (Phase 3)."""
    client = _get_client()
    client.table("raw_posts").update({"status": "queued"}).eq("id", raw_post_id).execute()


def get_scoring_stats() -> dict:
    """Quick breakdown of the scoring pipeline status."""
    client = _get_client()

    def count_by_status(status):
        r = client.table("raw_posts").select("id", count="exact").eq("status", status).execute()
        return r.count or 0

    raw     = count_by_status("raw")
    scored  = count_by_status("scored")
    queued  = count_by_status("queued")
    skipped = count_by_status("skipped")

    scores = client.table("scored_posts").select("total_score").execute()
    values = [r["total_score"] for r in scores.data if r["total_score"] is not None]
    avg_score = round(sum(values) / len(values), 2) if values else 0

    return {
        "raw": raw, "scored": scored, "queued": queued, "skipped": skipped,
        "avg_score": avg_score,
    }


# ── Phase 3: AI rewriting helpers ─────────────────────────────

def get_queued_posts_without_content(limit: int = 10, max_attempts: int = 3):
    """
    Fetch posts marked 'queued' that don't yet have a content_queue
    entry — these are ready for Phase 3 rewriting. Excludes posts
    that have already failed max_attempts times.
    """
    client = _get_client()

    existing = client.table("content_queue").select("raw_post_id").execute()
    existing_ids = {r["raw_post_id"] for r in existing.data if r.get("raw_post_id") is not None}

    # Fetch a generously-sized, FIXED window of queued posts rather than
    # trying to compute an exact "limit + len(existing_ids)" — that
    # calculation is fragile and easy to get subtly wrong as the table
    # grows. status='queued' should always be a small working set (it's
    # the active backlog, not the whole table), so a large fixed cap is
    # simpler and safer than trying to be precise about it.
    SAFE_FETCH_CAP = 500
    resp = (
        client.table("raw_posts")
        .select("*")
        .eq("status", "queued")
        .lt("rewrite_attempts", max_attempts)
        .order("id")
        .limit(SAFE_FETCH_CAP)
        .execute()
    )

    result = [r for r in resp.data if r["id"] not in existing_ids]
    return result[:limit]


def record_rewrite_failure(raw_post_id: int, max_attempts: int = 3) -> None:
    """
    Increment a post's rewrite_attempts counter. Once it hits
    max_attempts, mark it 'rewrite_failed'.
    """
    client = _get_client()
    row = client.table("raw_posts").select("rewrite_attempts").eq("id", raw_post_id).execute()
    current = row.data[0]["rewrite_attempts"] if row.data else 0
    new_attempts = current + 1

    update = {"rewrite_attempts": new_attempts}
    if new_attempts >= max_attempts:
        update["status"] = "rewrite_failed"
        logger.warning(f"Post {raw_post_id} gave up after {max_attempts} failed rewrite attempts")

    client.table("raw_posts").update(update).eq("id", raw_post_id).execute()


def save_rewritten_content(
    raw_post_id: int,
    twitter_thread: list[str],
    linkedin_post: str,
    hashtags: list[str],
) -> int:
    """
    Save AI-rewritten content for a post and mark it 'rewritten'.
    twitter_thread is stored as a JSON array; hashtags as comma-separated.

    This does two separate writes (insert content, then update status) —
    not a single atomic transaction, since Supabase's REST API doesn't
    expose multi-statement transactions directly. If the second write
    ever failed silently, a post's content would exist but its status
    would stay 'queued' forever, permanently miscounting it as "still
    needs rewriting" everywhere else in the system. We retry the status
    update a few times before giving up, and log loudly if it still
    fails, so this can never happen silently again.
    """
    import json as _json
    import time as _time

    client = _get_client()
    resp = (
        client.table("content_queue")
        .insert({
            "raw_post_id": raw_post_id,
            "twitter_thread": _json.dumps(twitter_thread),
            "linkedin_post": linkedin_post,
            "hashtags": ", ".join(hashtags),
            "created_at": _now(),
        })
        .execute()
    )

    # Retry the status update a few times — this is the step that, when it
    # silently failed before, left content sitting in content_queue with no
    # way for the rest of the system to know the post was actually done.
    for attempt in range(3):
        try:
            client.table("raw_posts").update({"status": "rewritten"}).eq("id", raw_post_id).execute()
            break
        except Exception as e:
            if attempt == 2:
                logger.error(
                    f"Post {raw_post_id}: content saved successfully, but marking it "
                    f"'rewritten' failed after 3 attempts ({e}). This post will be "
                    f"auto-repaired by reconcile_orphaned_rewrites() on the next run."
                )
            else:
                _time.sleep(1)

    return resp.data[0]["id"] if resp.data else None


def reconcile_orphaned_rewrites() -> int:
    """
    Self-healing safety net: finds posts that have content in content_queue
    but whose raw_posts.status never got updated to 'rewritten' (the
    failure mode save_rewritten_content's retry logic guards against, but
    can't fully eliminate — e.g. if all 3 retries hit a genuine outage).
    Fixes their status and returns how many were repaired. Cheap to run
    every cycle since it only touches truly inconsistent rows.
    """
    client = _get_client()

    content_rows = client.table("content_queue").select("raw_post_id").execute()
    content_ids = {r["raw_post_id"] for r in content_rows.data if r.get("raw_post_id")}

    if not content_ids:
        return 0

    stuck = (
        client.table("raw_posts")
        .select("id")
        .eq("status", "queued")
        .in_("id", list(content_ids))
        .execute()
    )

    for row in stuck.data:
        client.table("raw_posts").update({"status": "rewritten"}).eq("id", row["id"]).execute()

    if stuck.data:
        logger.warning(
            f"Reconciled {len(stuck.data)} post(s) that had content but were "
            f"stuck showing status='queued' — this can happen if a status "
            f"update failed transiently in a previous run."
        )

    return len(stuck.data)


def get_unposted_content(limit: int = 10):
    """Fetch rewritten content not yet posted to either platform (for Phase 4)."""
    client = _get_client()
    resp = (
        client.table("content_queue")
        .select("*, raw_posts!inner(source, author, url)")
        .eq("posted_twitter", 0)
        .eq("posted_linkedin", 0)
        .limit(limit)
        .execute()
    )
    return _flatten_raw_posts_join(resp.data)


def get_rewrite_stats() -> dict:
    """Quick breakdown of the rewriting pipeline status."""
    client = _get_client()

    def count_raw_status(status):
        r = client.table("raw_posts").select("id", count="exact").eq("status", status).execute()
        return r.count or 0

    queued_unwritten = count_raw_status("queued")
    rewritten        = count_raw_status("rewritten")
    rewrite_failed   = count_raw_status("rewrite_failed")

    total_in_queue = client.table("content_queue").select("id", count="exact").execute().count or 0
    ready_to_post = (
        client.table("content_queue")
        .select("id", count="exact")
        .eq("posted_twitter", 0)
        .eq("posted_linkedin", 0)
        .execute()
    ).count or 0

    return {
        "queued_awaiting_rewrite": queued_unwritten,
        "rewritten": rewritten,
        "rewrite_failed": rewrite_failed,
        "total_in_content_queue": total_in_queue,
        "ready_to_post": ready_to_post,
    }


# ── Phase 4: notification + LinkedIn approval/posting helpers ────

def get_unnotified_content(limit: int = 10):
    """Fetch content_queue rows that haven't been notified about yet."""
    client = _get_client()
    resp = (
        client.table("content_queue")
        .select("*, raw_posts!inner(source, author, url)")
        .eq("notified", 0)
        .order("created_at")
        .limit(limit)
        .execute()
    )
    return _flatten_raw_posts_join(resp.data)


def mark_notified(content_id: int) -> None:
    """Mark a content_queue row as having been notified about."""
    client = _get_client()
    client.table("content_queue").update({"notified": 1}).eq("id", content_id).execute()


def get_content_by_id(content_id: int):
    """Fetch a single content_queue row by ID, with source context joined in."""
    client = _get_client()
    resp = (
        client.table("content_queue")
        .select("*, raw_posts!inner(source, author, url)")
        .eq("id", content_id)
        .limit(1)
        .execute()
    )
    rows = _flatten_raw_posts_join(resp.data)
    return rows[0] if rows else None


def set_approval_status(content_id: int, status: str) -> None:
    """Set a content_queue row's approval_status: 'pending' | 'approved' | 'rejected'."""
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"Invalid approval_status: {status!r}")
    client = _get_client()
    client.table("content_queue").update({"approval_status": status}).eq("id", content_id).execute()


def mark_posted_linkedin(content_id: int, post_url: str = None) -> None:
    """Mark a content_queue row as successfully posted to LinkedIn."""
    client = _get_client()
    client.table("content_queue").update({
        "posted_linkedin": 1,
        "posted_at": _now(),
        "posted_linkedin_url": post_url,
    }).eq("id", content_id).execute()


def mark_posted_twitter(content_id: int) -> None:
    """Mark a content_queue row as posted to Twitter (manual posting)."""
    client = _get_client()
    client.table("content_queue").update({
        "posted_twitter": 1,
        "posted_at": _now(),
    }).eq("id", content_id).execute()


def get_posting_status() -> dict:
    """Quick breakdown of LinkedIn/Twitter posting status for --stats."""
    client = _get_client()

    def count_cq(**filters):
        q = client.table("content_queue").select("id", count="exact")
        for field, val in filters.items():
            q = q.eq(field, val)
        return q.execute().count or 0

    return {
        "pending_approval": count_cq(approval_status="pending", posted_linkedin=0),
        "posted_linkedin":  count_cq(posted_linkedin=1),
        "posted_twitter":   count_cq(posted_twitter=1),
        "rejected":         count_cq(approval_status="rejected"),
    }


def get_all_content_queue() -> list:
    """Fetch every content_queue row joined with source info, newest first — for --list."""
    client = _get_client()
    resp = (
        client.table("content_queue")
        .select("*, raw_posts!inner(source, author)")
        .order("id", desc=True)
        .execute()
    )
    rows = []
    for row in resp.data:
        row = dict(row)
        rp = row.pop("raw_posts", {}) or {}
        row["source"] = rp.get("source")
        row["author"] = rp.get("author")
        rows.append(row)
    return rows


# ── LinkedIn OAuth token storage (Posts API — replaces Playwright) ────

def save_linkedin_tokens(
    access_token: str,
    refresh_token: str,
    access_token_expires_at: str,
    refresh_token_expires_at: str,
    person_urn: str,
) -> None:
    """
    Save (or overwrite) the LinkedIn OAuth tokens. Single-row table for
    the MVP — always replaces row id=1. When this becomes multi-user,
    this will need a user_id and become an upsert keyed on that instead.
    """
    client = _get_client()
    existing = client.table("linkedin_oauth_tokens").select("id").execute()

    payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_expires_at": access_token_expires_at,
        "refresh_token_expires_at": refresh_token_expires_at,
        "person_urn": person_urn,
        "updated_at": _now(),
    }

    if existing.data:
        client.table("linkedin_oauth_tokens").update(payload).eq("id", existing.data[0]["id"]).execute()
    else:
        payload["created_at"] = _now()
        client.table("linkedin_oauth_tokens").insert(payload).execute()


def get_linkedin_tokens() -> dict | None:
    """Fetch the stored LinkedIn OAuth tokens, or None if never connected."""
    client = _get_client()
    resp = client.table("linkedin_oauth_tokens").select("*").limit(1).execute()
    return resp.data[0] if resp.data else None


# ── Internal helpers ─────────────────────────────────────────

def _flatten_raw_posts_join(rows: list) -> list:
    """
    Supabase's nested-select join returns {"raw_posts": {...}, ...}.
    Flatten to match the old SQLite row shape which used aliased
    columns like original_source, original_author, original_url.
    """
    flattened = []
    for row in rows:
        row = dict(row)
        rp = row.pop("raw_posts", {}) or {}
        row["original_source"] = rp.get("source")
        row["original_author"] = rp.get("author")
        row["original_url"]    = rp.get("url")
        flattened.append(row)
    return flattened
