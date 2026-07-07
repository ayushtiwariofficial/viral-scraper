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
    reconcile_orphaned_rewrites,
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
RATE_LIMIT_DELAY = 7.0   # Gemini free tier: 10 RPM = 1 req/6s minimum.
                          # 7s gives comfortable headroom (≈8.5 RPM) and
                          # reduces daily quota usage. Was 2.0s which caused
                          # persistent 429s (4/5 rewrites failing every run).


# ── Prompt ────────────────────────────────────────────────────

REWRITE_PROMPT = """You are an elite LinkedIn ghostwriter and social media strategist — the kind
top creators pay for — rewriting trending content in someone's own voice for their
personal Twitter and LinkedIn accounts. Your posts are known for stopping the scroll
and getting genuine engagement, not just impressions.

YOUR VOICE / STYLE:
{voice}

ORIGINAL CONTENT (for inspiration only — do not copy verbatim, extract the core insight and retell it):
---
{content}
---

Create TWO versions.

1. TWITTER THREAD: A hook tweet (under 200 chars, must grab attention in the first
   line) followed by 2-3 short follow-up tweets that build on the idea. HARD LIMIT:
   every tweet under 260 characters — count carefully before finalizing.

2. LINKEDIN POST — follow this exact structure, used by high-performing LinkedIn
   creators:

   a) HOOK LINE (critical): The very first line must work as a standalone
      scroll-stopper, because LinkedIn truncates posts after ~3 lines behind
      a "see more" button on mobile — most readers decide whether to expand
      based on this line alone. Use a bold claim, a surprising number, a
      relatable tension, or a direct question. Never start with "I've been
      thinking about..." or "Just read an article..." — too soft, gets scrolled past.

   b) FORMAT FOR SCANNING, NOT READING: Real LinkedIn posts are NOT solid
      paragraphs. Break every 1-3 sentences into its own short paragraph,
      separated by a blank line (use \\n\\n between paragraphs in the JSON
      string). Never write more than 3 sentences without a break. Short,
      punchy, one-idea-per-paragraph. This is non-negotiable — a wall of
      text is the #1 reason posts get ignored.

   c) BODY: Deliver the actual insight/story in 3-5 short paragraphs following
      the hook. Include one concrete, specific detail (a number, a result, a
      moment) — specificity is what makes a post feel authentic instead of
      generic AI-written content.

   d) SEO-CONSCIOUS LANGUAGE: Naturally include the plain-language terms
      people actually search for on this topic (e.g. "AI agents", "SaaS
      pricing", "build in public" — whatever fits the actual topic), instead
      of only clever/jargon phrasing. This helps the post surface in
      LinkedIn's own search and in Google results that index public posts.
      Never force keywords unnaturally — it must still read like a human wrote it.

   e) ENDING: Close with ONE clear, specific, easy-to-answer question that
      invites a real reply (not a generic "thoughts?"). A good ending
      question is the single biggest driver of comments, and comments are
      what LinkedIn's algorithm rewards most.

   Total length: 120-250 words for the body (not counting hashtags).

3. HASHTAGS: 3-5 hashtags, WITHOUT the # symbol in the JSON (just the word,
   e.g. "AI" not "#AI" — the # gets added automatically later). Mix ONE
   broad/high-volume tag (e.g. "AI", "SaaS") with 2-4 more specific niche
   tags relevant to the actual topic (e.g. "AIagents", "IndieHackers",
   "BuildInPublic") — broad tags for reach, niche tags for the right audience.

IMPORTANT: This is a fresh rewrite in a new voice, not a copy. Extract the
underlying idea/insight and re-express it naturally. Do not reuse distinctive
phrases from the original. Keep the total response compact — do not pad with
extra commentary or alternate versions.

Respond with ONLY a JSON object, no other text, no markdown formatting. The
linkedin_post value MUST contain literal \\n\\n between paragraphs:
{{
  "twitter_thread": ["tweet 1 text", "tweet 2 text", "tweet 3 text"],
  "linkedin_post": "Hook line here.\\n\\nShort paragraph two.\\n\\nShort paragraph three.\\n\\nClosing question here?",
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
            "maxOutputTokens": 3072,   # was 2048 — still not enough for longer/denser
                                        # source articles. Post 288 (a detailed piece on
                                        # AI video production costs) kept truncating even
                                        # at 2048 across 3 retries. 3072 gives more
                                        # consistent headroom; see also the input-length
                                        # cap below, which reduces how much the model
                                        # tends to write in response.
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
                wait = 15 * attempt   # 15s, 30s, 45s — gives the 10 RPM window
                                       # time to clear before retrying
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
    Returns False only if the structure is fundamentally broken —
    missing fields, wrong types. Returns True for fixable issues
    (oversized tweets), since sanitize_rewrite() handles those.
    """
    if not isinstance(result, dict):
        return False

    thread = result.get("twitter_thread")
    post   = result.get("linkedin_post")
    tags   = result.get("hashtags")

    if not isinstance(thread, list) or len(thread) < 1:
        return False
    if not all(isinstance(t, str) and len(t.strip()) > 0 for t in thread):
        return False   # empty or non-string tweets can't be salvaged
    if not isinstance(post, str) or len(post) < 50:
        return False
    if not isinstance(tags, list):
        return False

    return True


def sanitize_rewrite(result: dict) -> dict:
    """
    Fix up minor, recoverable issues in an otherwise-valid rewrite
    rather than discarding the whole thing:
      - Tweets over 280 chars get trimmed to fit, breaking at the
        last whole word and adding an ellipsis, instead of failing
        the entire post over one oversized line. Gemini's "under
        260 chars" instruction in the prompt is a strong nudge, not
        a hard guarantee, so this is the actual enforcement point.
    """
    MAX_TWEET_LEN = 280

    fixed_thread = []
    for tweet in result["twitter_thread"]:
        tweet = tweet.strip()
        if len(tweet) > MAX_TWEET_LEN:
            # Trim to fit, breaking at the last full word before the limit
            truncated = tweet[:MAX_TWEET_LEN - 1]
            last_space = truncated.rfind(" ")
            if last_space > MAX_TWEET_LEN * 0.6:   # don't cut too aggressively
                truncated = truncated[:last_space]
            tweet = truncated.rstrip(",.;: ") + "…"
            logger.info(f"  Trimmed oversized tweet ({len(result['twitter_thread'])} chars -> {len(tweet)})")
        fixed_thread.append(tweet)

    result["twitter_thread"] = fixed_thread
    return result


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

    # Self-healing safety net: repair any posts left stuck from a previous
    # run where the content saved successfully but the status update to
    # 'rewritten' failed transiently. Cheap — only touches truly
    # inconsistent rows, a no-op the vast majority of runs.
    repaired = reconcile_orphaned_rewrites()
    if repaired:
        logger.info(f"Self-healed {repaired} post(s) stuck from a previous run")

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

        result = sanitize_rewrite(result)   # trim any oversized tweets rather than discarding

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
