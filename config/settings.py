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
#    If one goes down, the next is tried automatically
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
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

# ── Scraper run interval (used in crontab comment only) ──────
RUN_EVERY_HOURS  = 2

# ── Phase 2: AI Scoring settings ─────────────────────────────
GROQ_MODEL          = "openai/gpt-oss-20b"   # free, fast (llama-3.3-70b-versatile was deprecated Jun 2026)
SCORING_BATCH_SIZE  = 30      # max posts to score per run (stay within free limits)
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
