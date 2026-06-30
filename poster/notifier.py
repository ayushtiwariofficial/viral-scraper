# ============================================================
#  poster/notifier.py  —  Phase 4: Notify you about ready content
#
#  Sends a free push notification (via ntfy.sh) whenever new
#  content is ready in the queue:
#    - Twitter content: full copy-paste-ready draft included in
#      the notification — you post it manually (see settings.py
#      for why: X discontinued free API access for new devs in
#      Feb 2026, posting now costs $0.015-0.20/tweet).
#    - LinkedIn content: a summary + instructions for how to
#      approve it, since LinkedIn posting is automated but
#      gated behind your explicit approval.
#
#  ntfy.sh is a free, no-signup push notification service —
#  you just need the app (or browser) subscribed to your topic.
# ============================================================

import json
import logging
import os

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import MAX_NOTIFICATIONS_PER_RUN
from data.database import get_unnotified_content, mark_notified, log_run
from datetime import datetime

logger = logging.getLogger(__name__)

NTFY_TOPIC  = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"


def send_ntfy(title: str, message: str, priority: str = "default", tags: str = "") -> bool:
    """
    Send a single push notification via ntfy.sh.
    Returns True on success, False if NTFY_TOPIC isn't configured
    or the request fails — this should never crash the pipeline,
    a missed notification is not worth failing the whole run over.
    """
    if not NTFY_TOPIC:
        logger.debug("NTFY_TOPIC not set — skipping notification (content is still saved in the DB)")
        return False

    try:
        import httpx
        resp = httpx.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": tags,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"ntfy notification failed (non-fatal): {e}")
        return False


def format_twitter_notification(content_row: dict) -> tuple[str, str]:
    """
    Build a copy-paste-ready Twitter thread notification.
    Returns (title, message).
    """
    thread = json.loads(content_row["twitter_thread"])
    hashtags = content_row["hashtags"] or ""

    title = f"🐦 Twitter draft ready (post #{content_row['id']})"

    lines = [f"Source: {content_row['original_source']}/{content_row['original_author']}", ""]
    for i, tweet in enumerate(thread, 1):
        lines.append(f"[{i}/{len(thread)}] {tweet}")
        lines.append("")
    if hashtags:
        lines.append(f"Hashtags: {hashtags}")
    lines.append("")
    lines.append("This is a DRAFT — copy and post it yourself on Twitter/X.")

    return title, "\n".join(lines)


def format_linkedin_notification(content_row: dict) -> tuple[str, str]:
    """
    Build a LinkedIn approval-request notification.
    Returns (title, message).
    """
    title = f"💼 LinkedIn post ready for approval (post #{content_row['id']})"

    post_preview = content_row["linkedin_post"][:300]
    if len(content_row["linkedin_post"]) > 300:
        post_preview += "..."

    lines = [
        f"Source: {content_row['original_source']}/{content_row['original_author']}",
        "",
        post_preview,
        "",
        f"Hashtags: {content_row['hashtags'] or '(none)'}",
        "",
        "To approve and post this to LinkedIn:",
        "1. Open your repo's Actions tab",
        "2. Run the 'Approve & Post to LinkedIn' workflow",
        f"3. Enter post ID: {content_row['id']}",
        "",
        "If you don't approve it, it just stays in the queue — nothing",
        "posts automatically.",
    ]

    return title, "\n".join(lines)


# ── Main notification function ───────────────────────────────

def notify_ready_content() -> dict:
    """
    Check for new ready content and send notifications.
    Marks each notified post so we don't repeat the same
    notification on the next run.
    """
    started_at = datetime.utcnow().isoformat()
    rows = get_unnotified_content(limit=MAX_NOTIFICATIONS_PER_RUN)

    if not rows:
        logger.info("No new content to notify about")
        return {"source": "notifier", "notified": 0, "errors": []}

    notified_count = 0
    errors = []

    for row in rows:
        row = dict(row)
        try:
            has_twitter = bool(row.get("twitter_thread"))
            has_linkedin = bool(row.get("linkedin_post"))

            sent_any = False

            if has_twitter:
                title, message = format_twitter_notification(row)
                if send_ntfy(title, message, priority="default", tags="bird"):
                    sent_any = True

            if has_linkedin:
                title, message = format_linkedin_notification(row)
                if send_ntfy(title, message, priority="high", tags="briefcase"):
                    sent_any = True

            # Mark notified even if ntfy isn't configured — the content is
            # still visible via `python run_scraper.py --stats` either way,
            # and we don't want to keep re-attempting a notification that
            # has no destination configured.
            mark_notified(row["id"])
            if sent_any:
                notified_count += 1
                logger.info(f"  ✓ Notified about post #{row['id']}")

        except Exception as e:
            msg = f"Notification for post #{row['id']} failed: {e}"
            logger.error(msg)
            errors.append(msg)

    summary = {
        "source": "notifier",
        "notified": notified_count,
        "errors": errors,
    }

    log_run(
        source      = "notifier",
        posts_found = len(rows),
        posts_new   = notified_count,
        started_at  = started_at,
        error       = "; ".join(errors) if errors else None,
    )

    logger.info(f"Notification check done — {notified_count} notified")
    return summary
