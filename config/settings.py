# ============================================================
#  config/settings.py  —  All settings in one place
#  Edit this file to customise your scraper
# ============================================================

# ── Your niche keywords (posts must contain at least one) ───
KEYWORDS = [
    "AI", "artificial intelligence", "LLM", "ChatGPT", "Claude",
    "SaaS", "startup", "building in public", "indie hacker",
    "machine learning", "automation", "productivity", "solopreneur",
    "side project", "bootstrapped", "MRR", "open source",
]

# ── How many top posts to keep per scraping run ─────────────
TOP_POSTS_PER_RUN = 5

# ── Minimum engagement to be considered (likes/upvotes) ─────
MIN_ENGAGEMENT = 10

# ── Nitter instances (public mirrors of Twitter, free) ───────
#    If one goes down, the next is tried automatically.
#    IMPORTANT: Nitter as a platform has become structurally unreliable
#    in 2026 — X/Twitter removed the guest-account API Nitter relied on,
#    so public instances now depend on rotating real-account credentials
#    that frequently get banned. This list reflects instances confirmed
#    working via https://nitter.net/ (status: https://status.d420.de/)
#    as of June 2026, in rough order of reliability. Even so, expect
#    this list to need periodic updates — check the status page above
#    if Twitter scraping starts failing consistently again.
NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://xcancel.com",
    "https://nitter.privacyredirect.com",
    "https://nitter.tiekoetter.com",
]

# ── Twitter accounts to scrape via Nitter ───────────────────
TWITTER_ACCOUNTS = [
    "levelsio",         # indie hacker, building in public
    "marc_louvion",     # SaaS builder
    "tibo_maker",       # building in public
    "gregisenberg",     # community / startup
    "naval",            # startup philosophy
    "paulg",            # YC / startup
    "sama",             # OpenAI / AI
    "karpathy",         # AI / ML
    "swyx",             # AI engineering
    "bentossell",       # no-code / AI tools
]

# ── Reddit subreddits to scrape ──────────────────────────────
REDDIT_SUBREDDITS = [
    "MachineLearning",
    "artificial",
    "SaaS",
    "indiehackers",
    "ChatGPT",
    "LocalLLaMA",
    "startups",
    "SideProject",
]

# Reddit OAuth credentials — required as of May 2026, when Reddit
# deprecated unauthenticated .json endpoint access entirely (it now
# returns a hard 403 for all unauthenticated requests, regardless of
# headers or IP — this isn't a bug we can work around, it's a policy
# change). Free OAuth "script" apps still get 60-100 req/min for free,
# no business justification needed at this volume.
#
# To get these (5 minutes, free):
#   1. Go to https://www.reddit.com/prefs/apps
#   2. Click "create another app..." at the bottom
#   3. Select "script" as the app type
#   4. Set redirect URI to http://localhost:8080 (required but unused)
#   5. Click "create app"
#   6. Copy the string under your app's name (that's the CLIENT_ID)
#   7. Copy the "secret" field (that's the CLIENT_SECRET)
#   8. Add REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME,
#      REDDIT_PASSWORD to your .env file and as GitHub Actions secrets
# Read via config/settings.py below — actual values come from os.getenv()

# ── RSS feeds (newsletters & blogs) ─────────────────────────
RSS_FEEDS = [
    "https://www.bensbites.com/feed",                        # AI newsletter
    "https://tldr.tech/ai/rss",                              # TLDR AI
    "https://www.deeplearning.ai/the-batch/feed/",           # The Batch
    "https://feeds.feedburner.com/oreilly/radar/atom",       # O'Reilly radar
    "https://hackernoon.com/feed",                           # Hacker Noon
    "https://news.ycombinator.com/rss",                      # Hacker News
]

# ── LinkedIn public profiles to scrape (via Google RSS hack) ─
LINKEDIN_PROFILES = [
    "lara-acosta-oficial",    # personal branding / LinkedIn growth
    "justinwelsh",            # creator economy
    "gregisenberg",           # startup / community
]

# ── Database path ────────────────────────────────────────────
import os
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, "data", "posts.db")
LOG_PATH = os.path.join(BASE_DIR, "logs", "scraper.log")

# ── HTTP request settings ────────────────────────────────────
REQUEST_TIMEOUT  = 10    # seconds
REQUEST_DELAY    = 2     # seconds between requests (be polite)
MAX_RETRIES      = 3

# Hard ceiling on total wall-clock time the Twitter scraper is allowed
# to spend across ALL accounts/instances combined. This is a circuit
# breaker — if Nitter instances are degraded (slow but not outright
# erroring), per-request timeouts alone don't help, because 10 accounts
# x 4 instances x 10s timeout = 400s worst case, which still eats most
# of a CI job's budget. This caps the whole scraper at a sane ceiling
# regardless, so one bad Nitter day can't time out the whole workflow.
TWITTER_SCRAPE_TIME_BUDGET = 180   # seconds (3 minutes)

# ── Scraper run interval (used in crontab comment only) ──────
RUN_EVERY_HOURS  = 2

# ── Phase 2: AI Scoring settings ─────────────────────────────
GROQ_MODEL          = "qwen/qwen3.6-27b"   # NOTE: ai/scorer.py defines its own GROQ_MODEL constant and
                                             # does not read this value — kept here for reference only.
                                             # Switched from openai/gpt-oss-20b (reasoning model) because
                                             # it exhausted its 200K tokens-per-day budget in a single day
                                             # and had unreliable JSON output (json_validate_failed errors).
SCORING_BATCH_SIZE  = 50      # max posts to score per run (50/run x 12 runs/day = 600/day capacity)
SCORING_MIN_TOTAL   = 5.0     # posts below this total score get marked 'skipped'

# Your niche description — used in the scoring prompt so the AI
# knows what "relevant" means for YOUR audience specifically.
NICHE_DESCRIPTION = (
    "A student building in public, sharing AI tools, SaaS insights, "
    "automation projects, and indie hacker / startup content. "
    "Audience: developers, indie hackers, AI enthusiasts, students "
    "interested in building side projects and startups."
)

# ── Phase 3: AI Content Rewriting settings ───────────────────
GEMINI_MODEL         = "gemini-2.5-flash"   # free tier, stable (gemini-2.0-flash deprecated/shutdown Jun 2026)
REWRITE_BATCH_SIZE   = 10     # max posts to rewrite per run (Gemini free tier: 250 req/day on 2.5 Flash)

# Your personal voice/brand — used so rewrites sound like YOU, not
# a generic AI. Edit this to match your actual writing style.
YOUR_VOICE = (
    "A student building AI/SaaS side projects in public. Casual but "
    "informative tone, no corporate jargon, occasional self-deprecating "
    "humor, genuinely curious about the topic rather than salesy. "
    "Uses simple words. Not afraid to share specific numbers or results."
)

# ── Phase 4: Notification + Posting settings ──────────────────
# Max notifications sent per run — keeps you from getting spammed if
# a backlog of unnotified content builds up after downtime.
MAX_NOTIFICATIONS_PER_RUN = 5

# Where the LinkedIn browser session (cookies) gets saved after the
# one-time interactive login. This file is in .gitignore — it
# contains your actual logged-in session and should never be
# committed. See poster/linkedin_poster.py --login for setup.
LINKEDIN_SESSION_PATH = os.path.join(BASE_DIR, "data", "linkedin_session.json")

# NOTE on Twitter/X posting: as of February 6, 2026, X discontinued
# free API access for new developer accounts — posting now costs
# $0.015/post ($0.20/post if it contains a link) under the pay-per-use
# model. Since this project commits to staying genuinely free, Twitter
# posting is manual: the notifier sends you a copy-paste-ready draft,
# and you post it yourself. See poster/notifier.py.
