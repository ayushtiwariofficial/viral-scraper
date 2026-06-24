# ============================================================
#  ai/scorer.py  —  Phase 2: AI virality scoring via Groq
#
#  Model: qwen/qwen3.6-27b with reasoning_effort="none" and
#  strict json_schema structured outputs.
#
#  Why this combo (and not openai/gpt-oss-20b):
#  - gpt-oss-20b is a reasoning model that burns hidden "thinking"
#    tokens even for simple tasks, which blew through its 200K
#    tokens-per-day (TPD) budget in a single day of normal use.
#  - gpt-oss-20b's json_object mode also produced "json_validate_failed"
#    400 errors on a meaningful fraction of calls — a known issue
#    reported by multiple developers on Groq's community forum.
#  - qwen3.6-27b supports reasoning_effort="none" (skips reasoning
#    tokens entirely for simple tasks like this) AND supports
#    strict:true json_schema mode, which Groq's own docs describe
#    as "never errors or produces invalid JSON" because the model
#    is constrained at the token level, not just prompted to comply.
#
#  Sign up at: https://console.groq.com
# ============================================================

import json
import logging
import time
import re

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import TOP_POSTS_PER_RUN, REQUEST_DELAY, NICHE_DESCRIPTION, SCORING_BATCH_SIZE
from data.database import (
    get_unscored_posts, save_score, mark_skipped,
    get_top_scored_posts, mark_queued, log_run,
)
from datetime import datetime

logger = logging.getLogger(__name__)

# Read directly from environment — works the same locally (via .env loaded
# by python-dotenv, if you use it) and in GitHub Actions (via secrets.GROQ_API_KEY
# passed as an env var in the workflow). No extra config file needed.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "qwen/qwen3.6-27b"   # non-reasoning mode avoids gpt-oss-20b's TPD/JSON issues

# Strict JSON schema — Groq guarantees the output always matches this
# exactly when strict=True, eliminating the json_validate_failed errors
# we saw with the older json_object mode.
SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "virality":   {"type": "number"},
        "relevance":  {"type": "number"},
        "uniqueness": {"type": "number"},
        "reason":     {"type": "string"},
    },
    "required": ["virality", "relevance", "uniqueness", "reason"],
    "additionalProperties": False,
}


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
"""


# ── Groq API call ─────────────────────────────────────────────

class QuotaExhausted(Exception):
    """Raised when Groq's daily token budget (TPD) is used up for the day."""
    pass


def call_groq(prompt: str, max_retries: int = 3) -> dict | None:
    """
    Call the Groq API with a scoring prompt.
    Returns parsed JSON dict, or None if it fails after retries.
    Raises QuotaExhausted if the daily token budget is used up —
    that's a multi-hour wait, not something worth retrying in a CI run.
    """
    import httpx

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,            # low temperature = more consistent scoring
        "max_tokens": 150,             # plenty — no hidden reasoning tokens to budget for
        "reasoning_effort": "none",    # qwen3.6-27b: skip reasoning entirely for this
                                        # simple scoring task — saves tokens + avoids the
                                        # TPD exhaustion we hit with a reasoning model
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "post_score",
                "strict": True,         # constrained decoding — output always matches
                "schema": SCORE_SCHEMA, # the schema below, so json_validate_failed can't happen
            },
        },
    }

    for attempt in range(1, max_retries + 1):
        text = None
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(GROQ_API_URL, headers=headers, json=payload)

            if resp.status_code == 429:
                try:
                    err = resp.json().get("error", {})
                    err_msg = err.get("message", "")
                except Exception:
                    err_msg = ""

                # TPD (tokens-per-day) exhaustion means "wait hours", not seconds —
                # retrying in this run is pointless and would just eat the whole
                # GitHub Actions timeout. Raise immediately so the caller can stop
                # the whole batch instead of retrying post after post.
                if "tokens per day" in err_msg.lower() or "tpd" in err_msg.lower():
                    raise QuotaExhausted(err_msg)

                wait = 5 * attempt
                match = re.search(r"try again in ([\d.]+)s", err_msg)
                if match:
                    wait = min(float(match.group(1)) + 1, 60)   # cap at 60s — don't hang CI
                logger.warning(f"Groq rate limited — waiting {wait:.0f}s (attempt {attempt})")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]

            return json.loads(text)

        except json.JSONDecodeError:
            preview = text[:100] if text else "(no response body)"
            logger.warning(f"Groq returned invalid JSON (attempt {attempt}): {preview}")
        except QuotaExhausted:
            raise   # let this propagate — don't swallow it in the generic except below
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

    posts = get_unscored_posts(limit=SCORING_BATCH_SIZE)
    logger.info(f"Found {len(posts)} unscored posts to process")

    scored_count  = 0
    skipped_count = 0
    errors        = []

    for post in posts:
        content = post["content"]

        # Truncate very long posts to save tokens
        content_for_prompt = content[:1200]
        prompt = SCORING_PROMPT.format(niche=NICHE_DESCRIPTION, content=content_for_prompt)

        try:
            result = call_groq(prompt)
        except QuotaExhausted as e:
            # Daily token budget is gone — there's no point trying the
            # remaining posts in this batch, they'll all fail the same way.
            # Stop here and let the next scheduled run (after the quota
            # resets at midnight Pacific) pick up where we left off.
            msg = f"Groq daily token quota exhausted — stopping batch early: {e}"
            logger.warning(msg)
            errors.append(msg)
            break

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
