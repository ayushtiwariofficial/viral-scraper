# ============================================================
#  ai/scorer.py  —  Phase 2: AI virality scoring via Groq
#
#  Groq free tier: 14,400 requests/day, no credit card.
#  Model: openai/gpt-oss-20b — fast and free, perfect for
#  scoring short social posts.
#
#  Sign up at: https://console.groq.com
# ============================================================

import json
import logging
import time
import re

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.env import GROQ_API_KEY
from config.settings import TOP_POSTS_PER_RUN, REQUEST_DELAY, NICHE_DESCRIPTION
from data.database import (
    get_unscored_posts, save_score, mark_skipped,
    get_top_scored_posts, mark_queued, log_run,
)
from datetime import datetime

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "openai/gpt-oss-20b"   # fast + free; replaces deprecated llama-3.1-8b-instant (deprecated Jun 2026)

MAX_POSTS_PER_RUN = 30   # cap how many posts we score per run (stay within free limits)


# ── Prompt ────────────────────────────────────────────────────

SCORING_PROMPT = """You are a social media virality expert specializing in AI, SaaS, and tech "building in public" content.

AUDIENCE CONTEXT:
{niche}

Score the following post on three dimensions, each from 1-10:
1. VIRALITY: How likely is this to get high engagement (likes, shares, comments) if reposted today?
2. RELEVANCE: How relevant is this to the audience described above?
3. UNIQUENESS: How fresh/non-generic is this take? (Generic motivational quotes score low; specific insights, data, or contrarian takes score high)

Post to score:
---
{content}
---

Respond with ONLY a JSON object, no other text, no markdown formatting:
{{"virality": <number>, "relevance": <number>, "uniqueness": <number>, "reason": "<one short sentence>"}}
"""


# ── Groq API call ─────────────────────────────────────────────

def call_groq(prompt: str, max_retries: int = 3) -> dict | None:
    """
    Call the Groq API with a scoring prompt.
    Returns parsed JSON dict, or None if it fails after retries.
    """
    import httpx

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,     # low temperature = more consistent scoring
        "max_tokens": 150,
        "response_format": {"type": "json_object"},   # forces valid JSON back
    }

    for attempt in range(1, max_retries + 1):
        text = None
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(GROQ_API_URL, headers=headers, json=payload)

            if resp.status_code == 429:
                wait = 5 * attempt
                logger.warning(f"Groq rate limited — waiting {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]

            # Defensive: strip markdown code fences if the model adds them
            # despite being told not to (happens occasionally with some models)
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            return json.loads(cleaned)

        except json.JSONDecodeError:
            preview = text[:100] if text else "(no response body)"
            logger.warning(f"Groq returned invalid JSON (attempt {attempt}): {preview}")
        except Exception as e:
            logger.warning(f"Groq call failed (attempt {attempt}): {e}")
            time.sleep(2)

    return None


def clamp_score(value, default=5.0) -> float:
    """Ensure a score is a valid number between 1 and 10."""
    try:
        v = float(value)
        return max(1.0, min(10.0, v))
    except (TypeError, ValueError):
        return default


# ── Main scoring function ─────────────────────────────────────

def score_posts() -> dict:
    """
    Score all unscored posts using Groq.
    Returns a summary dict with counts.
    """
    started_at = datetime.utcnow().isoformat()

    if not GROQ_API_KEY:
        msg = "GROQ_API_KEY not set — copy .env.example to .env and add your key"
        logger.error(msg)
        log_run(source="ai_scoring", posts_found=0, posts_new=0,
                started_at=started_at, error=msg)
        return {"source": "ai_scoring", "scored": 0, "skipped": 0, "errors": [msg]}

    posts = get_unscored_posts(limit=MAX_POSTS_PER_RUN)
    logger.info(f"Found {len(posts)} unscored posts to process")

    scored_count  = 0
    skipped_count = 0
    errors        = []

    for post in posts:
        content = post["content"]

        # Truncate very long posts to save tokens
        content_for_prompt = content[:1200]
        prompt = SCORING_PROMPT.format(niche=NICHE_DESCRIPTION, content=content_for_prompt)

        result = call_groq(prompt)

        if result is None:
            mark_skipped(post["id"], reason="Groq scoring failed after retries")
            skipped_count += 1
            errors.append(f"Post {post['id']} from {post['source']}: scoring failed")
            continue

        virality   = clamp_score(result.get("virality"))
        relevance  = clamp_score(result.get("relevance"))
        uniqueness = clamp_score(result.get("uniqueness"))
        reason     = result.get("reason", "")

        save_score(post["id"], virality, relevance, uniqueness)
        scored_count += 1

        logger.info(
            f"  Scored post {post['id']} [{post['source']}] — "
            f"V:{virality} R:{relevance} U:{uniqueness} — {reason[:60]}"
        )

        time.sleep(0.5)   # gentle pacing — well within Groq free limits

    summary = {
        "source":  "ai_scoring",
        "scored":  scored_count,
        "skipped": skipped_count,
        "errors":  errors,
    }

    log_run(
        source      = "ai_scoring",
        posts_found = len(posts),
        posts_new   = scored_count,
        started_at  = started_at,
        error       = f"{skipped_count} posts failed scoring" if skipped_count else None,
    )

    logger.info(f"Scoring done — scored {scored_count}, skipped {skipped_count}")
    return summary


# ── Queue builder ──────────────────────────────────────────────

def build_queue(min_score: float = 6.0) -> dict:
    """
    Pick the top N scored posts and mark them as 'queued'
    for Phase 3 (content rewriting).
    """
    started_at = datetime.utcnow().isoformat()

    top_posts = get_top_scored_posts(limit=TOP_POSTS_PER_RUN, min_score=min_score)

    for post in top_posts:
        mark_queued(post["id"])
        logger.info(
            f"  Queued post {post['id']} [{post['source']}] "
            f"score={post['total_score']} — {post['content'][:60]}..."
        )

    logger.info(f"Queue built — {len(top_posts)} posts ready for rewriting")

    log_run(
        source      = "queue_builder",
        posts_found = len(top_posts),
        posts_new   = len(top_posts),
        started_at  = started_at,
    )

    return {
        "source": "queue_builder",
        "queued": len(top_posts),
        "posts":  [dict(p) for p in top_posts],
    }
