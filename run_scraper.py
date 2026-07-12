#!/usr/bin/env python3
# ============================================================
#  run_scraper.py  —  Main entry point
#
#  Usage:
#    python run_scraper.py              # run all scrapers
#    python run_scraper.py --source twitter
#    python run_scraper.py --source reddit
#    python run_scraper.py --source rss
#    python run_scraper.py --source linkedin
#    python run_scraper.py --stats      # show DB stats
# ============================================================

import sys
import os
import logging
import argparse
from datetime import datetime

# ── Logging setup (file + console) ───────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config.settings import LOG_PATH

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt  = "%Y-%m-%d %H:%M:%S",
    handlers = [
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("main")

# ── Imports ───────────────────────────────────────────────────
from data.database import init_db, get_stats
from scrapers.twitter_scraper  import scrape_twitter
from scrapers.reddit_scraper   import scrape_reddit
from scrapers.rss_scraper      import scrape_rss
from scrapers.linkedin_scraper import scrape_linkedin
from ai.scorer import score_posts, build_queue
from ai.rewriter import rewrite_posts
from poster.notifier import notify_ready_content


# ── Runner ────────────────────────────────────────────────────

SCRAPERS = {
    "twitter":  scrape_twitter,
    "reddit":   scrape_reddit,
    "rss":      scrape_rss,
    "linkedin": scrape_linkedin,
}


def run_all(sources: list[str] = None) -> None:
    sources = sources or list(SCRAPERS.keys())

    logger.info("=" * 60)
    logger.info(f"Scraper run started — sources: {', '.join(sources)}")
    logger.info("=" * 60)

    results = {}
    for name in sources:
        if name not in SCRAPERS:
            logger.warning(f"Unknown source '{name}' — skipping")
            continue
        try:
            logger.info(f"── Scraping {name} ──")
            results[name] = SCRAPERS[name]()
        except Exception as e:
            logger.error(f"Scraper '{name}' crashed: {e}", exc_info=True)
            results[name] = {"error": str(e), "posts_found": 0, "posts_new": 0}

    # ── Summary table ────────────────────────────────────────
    logger.info("")
    logger.info("── Run summary ──────────────────────────────")
    total_found = 0
    total_new   = 0
    for name, r in results.items():
        f = r.get("posts_found", 0)
        n = r.get("posts_new",   0)
        e = len(r.get("errors",  []))
        total_found += f
        total_new   += n
        logger.info(f"  {name:<12}  found={f:>4}  new={n:>4}  errors={e}")

    logger.info(f"  {'TOTAL':<12}  found={total_found:>4}  new={total_new:>4}")
    logger.info("─────────────────────────────────────────────")
    logger.info("")


def run_scoring() -> None:
    """Run Phase 2: AI scoring + queue building."""
    logger.info("=" * 60)
    logger.info("AI scoring started")
    logger.info("=" * 60)

    score_result = score_posts()
    logger.info(
        f"Scoring: {score_result.get('scored', 0)} scored, "
        f"{score_result.get('skipped', 0)} skipped"
    )

    queue_result = build_queue()
    logger.info(f"Queue: {queue_result.get('queued', 0)} posts ready for rewriting")
    logger.info("")


def run_rewriting() -> None:
    """Run Phase 3: AI content rewriting for Twitter + LinkedIn."""
    logger.info("=" * 60)
    logger.info("AI content rewriting started")
    logger.info("=" * 60)

    rewrite_result = rewrite_posts()
    logger.info(
        f"Rewriting: {rewrite_result.get('rewritten', 0)} rewritten, "
        f"{rewrite_result.get('failed', 0)} failed"
    )
    logger.info("")


def run_notifications() -> None:
    """
    Run Phase 4 (notification half): tell the user about freshly
    rewritten content. Twitter drafts are copy-paste ready; LinkedIn
    posts need explicit approval (see poster/linkedin_poster.py).
    This never posts anything itself — it only notifies.
    """
    logger.info("=" * 60)
    logger.info("Notifying about ready content")
    logger.info("=" * 60)

    result = notify_ready_content()
    logger.info(f"Notifications: {result.get('notified', 0)} sent")
    logger.info("")


def show_stats() -> None:
    from data.database import get_scoring_stats, get_rewrite_stats

    stats = get_stats()
    print("\n── Database stats ───────────────────────────")
    print(f"  Total posts stored  : {stats['total_posts']}")
    print(f"  Scraped today       : {stats['scraped_today']}")
    print(f"  In AI queue         : {stats['in_queue']}")
    print(f"  Posted to platforms : {stats['posted']}")

    scoring = get_scoring_stats()
    print("\n── AI scoring breakdown ──────────────────────")
    print(f"  Raw (unscored)      : {scoring['raw']}")
    print(f"  Scored              : {scoring['scored']}")
    print(f"  Queued for rewrite  : {scoring['queued']}")
    print(f"  Skipped (low score) : {scoring['skipped']}")
    print(f"  Average total score : {scoring['avg_score']}")

    rewrites = get_rewrite_stats()
    print("\n── AI rewriting breakdown ────────────────────")
    print(f"  Awaiting rewrite    : {rewrites['queued_awaiting_rewrite']}")
    print(f"  Rewritten           : {rewrites['rewritten']}")
    print(f"  Gave up (failed)    : {rewrites['rewrite_failed']}")
    print(f"  In content queue    : {rewrites['total_in_content_queue']}")
    print(f"  Ready to post       : {rewrites['ready_to_post']}")

    from data.database import get_posting_status
    posting = get_posting_status()
    print("\n── Posting status (Phase 4) ──────────────────")
    print(f"  LinkedIn pending approval : {posting['pending_approval']}")
    print(f"  LinkedIn posted           : {posting['posted_linkedin']}")
    print(f"  LinkedIn rejected         : {posting['rejected']}")
    print(f"  Twitter posted (manual)   : {posting['posted_twitter']}")

    print("\n── Last 5 scraper runs ──────────────────────")
    for run in stats["recent_runs"]:
        status = f"❌ {run['error'][:50]}" if run.get("error") else "✓"
        print(f"  {run['started_at'][:16]}  {run['source']:<12}  "
              f"found={run['posts_found']:>4}  new={run['posts_new']:>4}  {status}")
    print()


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Viral content scraper")
    parser.add_argument("--source",        help="Run a single scraper source (twitter/reddit/rss/linkedin)")
    parser.add_argument("--stats",         action="store_true", help="Show DB stats and exit")
    parser.add_argument("--score-only",    action="store_true", help="Run AI scoring only, skip scraping")
    parser.add_argument("--rewrite-only",  action="store_true", help="Run AI rewriting only, skip scraping + scoring")
    parser.add_argument("--skip-scoring",  action="store_true", help="Run scrapers only, skip AI scoring step")
    parser.add_argument("--skip-rewrite",  action="store_true", help="Skip AI rewriting step")
    parser.add_argument("--skip-notify",   action="store_true", help="Skip sending notifications about ready content")
    parser.add_argument("--notify-only",   action="store_true", help="Run notifications only, skip everything else")
    parser.add_argument(
        "--list", action="store_true",
        help="List all posts in the content queue with their IDs, status, and a short preview"
    )
    parser.add_argument(
        "--preview", type=int, metavar="ID",
        help="Show the full content of a content_queue ID (useful before approving LinkedIn posts)"
    )
    parser.add_argument(
        "--mark-twitter-posted", type=int, metavar="ID",
        help="Mark a content_queue ID as manually posted to Twitter (Twitter posting is manual — see settings.py)"
    )
    parser.add_argument(
        "--audit-queue", action="store_true",
        help="Re-check all pending (unposted) content against the CURRENT validation "
             "rules and auto-reject anything that fails. Use this after a validation "
             "fix ships, since older content generated before the fix was never "
             "automatically re-checked against it."
    )
    args = parser.parse_args()

    # Always init DB first
    init_db()

    if args.audit_queue:
        from data.database import get_pending_unposted_content, set_approval_status
        from ai.rewriter import validate_rewrite
        import json as _json

        rows = get_pending_unposted_content()
        print(f"Auditing {len(rows)} pending post(s) against current validation rules...\n")

        rejected_count = 0
        for row in rows:
            try:
                thread = _json.loads(row.get("twitter_thread") or "[]")
            except Exception:
                thread = []
            candidate = {
                "twitter_thread": thread,
                "linkedin_post": row.get("linkedin_post", ""),
                "hashtags": [t.strip() for t in (row.get("hashtags") or "").split(",") if t.strip()],
            }
            if not validate_rewrite(candidate):
                set_approval_status(row["id"], "rejected")
                rejected_count += 1
                preview = (row.get("linkedin_post") or "")[:70]
                print(f"  ✗ Rejected #{row['id']}: {preview}...")

        print(f"\nDone — {rejected_count} of {len(rows)} pending post(s) rejected for failing "
              f"current validation (incomplete content, missing hashtags, etc.)")
        print("Everything else in the queue passed and is still available for approval.")
        return

    if args.list:
        from data.database import get_all_content_queue
        import json as _json
        rows = get_all_content_queue()
        if not rows:
            print("Content queue is empty.")
            return
        print(f"\n{'ID':<5} {'Source':<20} {'Approval':<10} {'LI':<5} {'TW':<5} {'Created':<12}  Preview")
        print("-" * 100)
        for r in rows:
            try:
                thread = _json.loads(r.get("twitter_thread") or "[]")
                preview = thread[0][:60] if thread else (r.get("linkedin_post") or "")[:60]
            except Exception:
                preview = (r.get("linkedin_post") or "")[:60]
            source = f"{r.get('source','?')}/{r.get('author') or '?'}"[:20]
            created = (r.get("created_at") or "")[:10]
            li = "✓" if r.get("posted_linkedin") else ("rej" if r.get("approval_status") == "rejected" else "pend")
            tw = "✓" if r.get("posted_twitter") else "-"
            print(f"{r.get('id'):<5} {source:<20} {r.get('approval_status','?'):<10} {li:<5} {tw:<5} {created:<12}  {preview}...")
        print(f"\nTotal: {len(rows)} posts  |  Run: python run_scraper.py --preview <ID>  to see full content")
        print()
        return

    if args.preview:
        from data.database import get_content_by_id
        import json as _json
        row = get_content_by_id(args.preview)
        if not row:
            print(f"No content found with ID #{args.preview}")
            return
        row = dict(row)

        # twitter_thread is stored as a JSON-encoded list in the DB
        raw_thread = row.get("twitter_thread") or "[]"
        try:
            thread = _json.loads(raw_thread)
            if not isinstance(thread, list):
                thread = [str(thread)]
        except Exception:
            thread = [raw_thread]

        # hashtags is stored as a comma-separated string
        hashtags = row.get("hashtags") or ""
        tag_list = [t.strip() for t in hashtags.split(",") if t.strip()]

        print(f"\n{'='*60}")
        print(f"Post #{row['id']}  |  {row.get('original_source','?')}/{row.get('original_author','?')}")
        print(f"Status: {row.get('approval_status','?')}  |  LinkedIn posted: {'✓' if row.get('posted_linkedin') else 'No'}  |  Twitter posted: {'✓' if row.get('posted_twitter') else 'No'}")
        print(f"{'='*60}")
        print(f"\n🐦 TWITTER THREAD ({len(thread)} tweets):\n")
        for i, tweet in enumerate(thread, 1):
            print(f"  [{i}/{len(thread)}]")
            print(f"  {tweet}")
            print()
        print(f"{'─'*60}")
        print(f"\n💼 LINKEDIN POST:\n")
        print(row.get("linkedin_post", "(none)"))
        print(f"\n{'─'*60}")
        if tag_list:
            print(f"\n🏷  HASHTAGS: {'  '.join('#'+t for t in tag_list)}")
        src_url = row.get("original_url")
        if src_url:
            print(f"🔗 Source: {src_url}")
        print()
        return

    if args.mark_twitter_posted:
        from data.database import mark_posted_twitter
        mark_posted_twitter(args.mark_twitter_posted)
        print(f"✓ Marked content #{args.mark_twitter_posted} as posted to Twitter")
        return

    if args.stats:
        show_stats()
        return

    if args.notify_only:
        run_notifications()
        show_stats()
        return

    if args.rewrite_only:
        run_rewriting()
        if not args.skip_notify:
            run_notifications()
        show_stats()
        return

    if args.score_only:
        run_scoring()
        show_stats()
        return

    sources = [args.source] if args.source else None
    run_all(sources)

    # Chain AI scoring + rewriting + notifications right after scraping
    # (unless explicitly skipped or only one source was requested via --source)
    if not args.skip_scoring and not args.source:
        run_scoring()

    if not args.skip_rewrite and not args.source:
        run_rewriting()

    if not args.skip_notify and not args.source:
        run_notifications()

    show_stats()


if __name__ == "__main__":
    main()
