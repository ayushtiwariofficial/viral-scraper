# ============================================================
#  ai/rewriter.py  —  Phase 3: AI Content Rewriting via Gemini
#
#  Takes queued posts (top scorers from Phase 2) and rewrites
#  each into:
#    - A Twitter thread (hook tweet + 3-4 follow-ups)
#    - A LinkedIn post (150-300 words, narrative tone)
#    - Relevant hashtags for each platform
#
#  Model: gemini-2.5-flash — confirmed free tier, stable as of
#  June 2026. (gemini-2.0-flash was deprecated/shutdown Jun 2026 —
#  do NOT use it.)
#
#  Sign up at: https://aistudio.google.com/app/apikey
# ============================================================

import json
import logging
import time
import re

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import REWRITE_BATCH_SIZE, YOUR_VOICE, GEMINI_MODEL
from data.database import (
    get_queued_posts_without_content, save_rewritten_content,
    get_rewrite_stats, record_rewrite_failure, log_run,
)
from datetime import datetime

logger = logging.getLogger(__name__)

# Read directly from environment — same pattern as scorer.py.
# Works locally (.env) and in GitHub Actions (secrets.GEMINI_API_KEY).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

MAX_RETRIES = 3
RATE_LIMIT_DELAY = 2.0   # Gemini 2.5 Flash free tier: ~10 RPM, so pace gently


# ── Prompt ────────────────────────────────────────────────────

REWRITE_PROMPT = """You are a ghostwriter helping someone rewrite trending content in their own voice for their personal Twitter and LinkedIn accounts.

YOUR VOICE / STYLE:
{voice}

ORIGINAL CONTENT (for inspiration only — do not copy verbatim, extract the core insight and retell it):
---
{content}
---

Create TWO versions:

1. TWITTER THREAD: A hook tweet (under 200 chars, must grab attention in first line) followed by 2-4 short follow-up tweets that build on the idea. Each tweet under 270 chars.

2. LINKEDIN POST: 150-250 words, more narrative and reflective tone, can include a personal angle ("I've been thinking about this..." or "This made me reconsider..."), ends with a question or call to engage.

3. HASHTAGS: 3-5 relevant hashtags that fit both platforms.

IMPORTANT: This is a fresh rewrite in a new voice, not a copy. Extract the underlying idea/insight and re-express it naturally. Do not reuse distinctive phrases from the original.

Respond with ONLY a JSON object, no other text, no markdown formatting:
{{
  "twitter_thread": ["tweet 1 text", "tweet 2 text", "tweet 3 text"],
  "linkedin_post": "full linkedin post text here",
  "hashtags": ["AI", "BuildInPublic", "SaaS"]
}}
"""


# ── Gemini API call ──────────────────────────────────────────

def call_gemini(prompt: str, max_retries: int = MAX_RETRIES) -> dict | None:
    """
    Call the Gemini API with a rewriting prompt.
    Returns parsed JSON dict, or None if it fails after retries.
    """
    import httpx

    headers = {"Content-Type": "application/json"}
    params  = {"key": GEMINI_API_KEY}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.8,        # higher than scoring — we want creative variety
            "maxOutputTokens": 2048,   # was 800 — too low. A Twitter thread + LinkedIn
                                        # post + hashtags routinely needs 900-1400 tokens
                                        # once you include JSON structure overhead (quotes,
                                        # escaped newlines, brackets). At 800 the response
                                        # was getting cut off mid-string every time, which
                                        # is exactly why every single rewrite failed with
                                        # "invalid JSON" — it wasn't malformed, it was
                                        # truncated. 2048 gives real headroom.
            "responseMimeType": "application/json",   # forces valid JSON back
        },
    }

    for attempt in range(1, max_retries + 1):
        text = None
        finish_reason = None
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(GEMINI_API_URL, headers=headers, params=params, json=payload)

            if resp.status_code == 429:
                wait = 10 * attempt
                logger.warning(f"Gemini rate limited — waiting {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                # Log Gemini's actual error body instead of letting raise_for_status()
                # hide it behind a generic "400/503 ... " message with no detail.
                try:
                    err_body = resp.json().get("error", {})
                    err_msg = err_body.get("message", resp.text[:300])
                except Exception:
                    err_msg = resp.text[:300]
                logger.warning(f"Gemini {resp.status_code} (attempt {attempt}): {err_msg}")
                time.sleep(2)
                continue

            data = resp.json()

            # Check why generation stopped BEFORE trying to parse — if it was
            # cut off due to the token limit, json.loads will fail anyway, but
            # this tells us *why* immediately instead of just "invalid JSON".
            finish_reason = data.get("candidates", [{}])[0].get("finishReason")

            # Gemini's response shape: candidates[0].content.parts[0].text
            text = data["candidates"][0]["content"]["parts"][0]["text"]

            # Defensive: strip markdown fences if the model adds them anyway
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            return json.loads(cleaned)

        except json.JSONDecodeError:
            preview = text[:150] if text else "(no response body)"
            if finish_reason == "MAX_TOKENS":
                logger.warning(
                    f"Gemini response truncated by maxOutputTokens (attempt {attempt}) — "
                    f"increase maxOutputTokens further if this keeps happening. Preview: {preview}"
                )
            else:
                logger.warning(
                    f"Gemini returned invalid JSON (attempt {attempt}, "
                    f"finish_reason={finish_reason}): {preview}"
                )
        except (KeyError, IndexError) as e:
            # Usually means content was blocked by safety filters, or empty response
            preview = str(data)[:200] if 'data' in dir() else "(no data)"
            logger.warning(f"Gemini response missing expected fields (attempt {attempt}): {e} — {preview}")
        except Exception as e:
            logger.warning(f"Gemini call failed (attempt {attempt}): {e}")
            time.sleep(2)

    return None


def validate_rewrite(result: dict) -> bool:
    """
    Sanity-check the AI's output before saving it.
    Returns False if the structure is malformed — better to skip
    a bad rewrite than post broken content to your real accounts.
    """
    if not isinstance(result, dict):
        return False

    thread = result.get("twitter_thread")
    post   = result.get("linkedin_post")
    tags   = result.get("hashtags")

    if not isinstance(thread, list) or len(thread) < 1:
        return False
    if not all(isinstance(t, str) and len(t) <= 280 for t in thread):
        return False   # a tweet over 280 chars would fail to post later
    if not isinstance(post, str) or len(post) < 50:
        return False
    if not isinstance(tags, list):
        return False

    return True


# ── Main rewriting function ──────────────────────────────────

def rewrite_posts() -> dict:
    """
    Rewrite all queued posts that don't yet have content_queue entries.
    Returns a summary dict with counts.
    """
    started_at = datetime.utcnow().isoformat()

    if not GEMINI_API_KEY:
        msg = "GEMINI_API_KEY not set — copy .env.example to .env and add your key"
        logger.error(msg)
        log_run(source="ai_rewrite", posts_found=0, posts_new=0,
                started_at=started_at, error=msg)
        return {"source": "ai_rewrite", "rewritten": 0, "failed": 0, "errors": [msg]}

    posts = get_queued_posts_without_content(limit=REWRITE_BATCH_SIZE)
    logger.info(f"Found {len(posts)} queued posts to rewrite")

    rewritten_count = 0
    failed_count    = 0
    errors          = []

    for post in posts:
        content = post["content"]
        prompt  = REWRITE_PROMPT.format(voice=YOUR_VOICE, content=content[:1500])

        result = call_gemini(prompt)

        if result is None:
            failed_count += 1
            record_rewrite_failure(post["id"])
            errors.append(f"Post {post['id']} from {post['source']}: Gemini call failed")
            logger.error(f"  ✗ Post {post['id']}: rewrite failed after retries")
            continue

        if not validate_rewrite(result):
            failed_count += 1
            record_rewrite_failure(post["id"])
            errors.append(f"Post {post['id']} from {post['source']}: malformed AI response")
            logger.error(f"  ✗ Post {post['id']}: AI response failed validation — {str(result)[:150]}")
            continue

        save_rewritten_content(
            raw_post_id    = post["id"],
            twitter_thread = result["twitter_thread"],
            linkedin_post  = result["linkedin_post"],
            hashtags       = result["hashtags"],
        )
        rewritten_count += 1

        logger.info(
            f"  ✓ Post {post['id']} [{post['source']}] rewritten — "
            f"{len(result['twitter_thread'])} tweets, "
            f"{len(result['linkedin_post'])} char LinkedIn post"
        )

        time.sleep(RATE_LIMIT_DELAY)

    summary = {
        "source":    "ai_rewrite",
        "rewritten": rewritten_count,
        "failed":    failed_count,
        "errors":    errors,
    }

    log_run(
        source      = "ai_rewrite",
        posts_found = len(posts),
        posts_new   = rewritten_count,
        started_at  = started_at,
        error       = f"{failed_count} posts failed rewriting" if failed_count else None,
    )

    logger.info(f"Rewriting done — {rewritten_count} rewritten, {failed_count} failed")
    return summary
