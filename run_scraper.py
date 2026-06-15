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


def show_stats() -> None:
    stats = get_stats()
    print("\n── Database stats ───────────────────────────")
    print(f"  Total posts stored  : {stats['total_posts']}")
    print(f"  Scraped today       : {stats['scraped_today']}")
    print(f"  In AI queue         : {stats['in_queue']}")
    print(f"  Posted to platforms : {stats['posted']}")
    print("\n── Last 5 scraper runs ──────────────────────")
    for run in stats["recent_runs"]:
        status = f"❌ {run['error'][:50]}" if run.get("error") else "✓"
        print(f"  {run['started_at'][:16]}  {run['source']:<12}  "
              f"found={run['posts_found']:>4}  new={run['posts_new']:>4}  {status}")
    print()


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Viral content scraper")
    parser.add_argument("--source",  help="Run a single source (twitter/reddit/rss/linkedin)")
    parser.add_argument("--stats",   action="store_true", help="Show DB stats and exit")
    args = parser.parse_args()

    # Always init DB first
    init_db()

    if args.stats:
        show_stats()
        return

    sources = [args.source] if args.source else None
    run_all(sources)

    # Always print stats at the end of a full run
    if not args.source:
        show_stats()


if __name__ == "__main__":
    main()
