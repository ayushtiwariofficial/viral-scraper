# Viral AI Content Scraper — Setup Guide

## Phase 1: Scraper + SQLite Storage

---

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

---

## 2. Run the scraper

```bash
# Run all sources at once
python run_scraper.py

# Run a single source
python run_scraper.py --source twitter
python run_scraper.py --source reddit
python run_scraper.py --source rss
python run_scraper.py --source linkedin

# Check what's been collected
python run_scraper.py --stats
```

---

## 3. Customise your feed (config/settings.py)

Open `config/settings.py` and edit:
- `KEYWORDS` — add your niche topics
- `TWITTER_ACCOUNTS` — accounts to follow
- `REDDIT_SUBREDDITS` — subreddits to monitor
- `RSS_FEEDS` — newsletters to track

---

## 4. Set up LinkedIn scraping (5 min, free)

LinkedIn is the hardest source. Do this once:

1. Go to **https://rss.app** → sign up free
2. Search for a LinkedIn profile or hashtag
3. Copy the generated RSS URL
4. Paste it into `RSS_APP_FEEDS` in `scrapers/linkedin_scraper.py`

Free tier gives you 3 feeds, each updating every 12 hours.

---

## 5. Run every 2 hours automatically (crontab)

```bash
# Open crontab editor
crontab -e

# Add this line (adjust the path to your project):
0 */2 * * * cd /home/youruser/viral-scraper && python run_scraper.py >> logs/cron.log 2>&1
```

---

## 6. Deploy on Oracle Cloud (free, always-on)

Oracle Cloud's free tier gives you 2 ARM VMs that run forever at no cost.

```bash
# 1. Sign up at cloud.oracle.com (free, needs a credit card for verification only)
# 2. Create an Always Free VM (Ampere ARM, 1 OCPU, 1GB RAM, Ubuntu 22.04)
# 3. SSH into your VM
ssh ubuntu@YOUR_VM_IP

# 4. Install Python + pip
sudo apt update && sudo apt install python3 python3-pip git -y

# 5. Clone or upload your project
git clone https://github.com/YOURUSERNAME/viral-scraper.git
cd viral-scraper

# 6. Install dependencies
pip3 install -r requirements.txt

# 7. Test run
python3 run_scraper.py --stats

# 8. Set up crontab
crontab -e
# Add: 0 */2 * * * cd /home/ubuntu/viral-scraper && python3 run_scraper.py >> logs/cron.log 2>&1
```

---

## 7. Alternative: GitHub Actions (no server needed)

If you don't want a VM, use GitHub Actions free tier (2,000 min/month free):

Create `.github/workflows/scraper.yml`:

```yaml
name: Viral Scraper
on:
  schedule:
    - cron: '0 */2 * * *'   # every 2 hours
  workflow_dispatch:          # manual trigger

jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python run_scraper.py
      - uses: actions/upload-artifact@v4
        with:
          name: database
          path: data/posts.db
```

Note: GitHub Actions resets the filesystem each run, so the DB won't persist
between runs unless you commit it back or use artifact caching. For a persistent
DB, use the Oracle Cloud VM approach instead.

---

## Project structure

```
viral-scraper/
├── config/
│   └── settings.py          ← edit this to customise
├── data/
│   ├── database.py          ← SQLite helpers
│   └── posts.db             ← created on first run
├── logs/
│   └── scraper.log          ← all logs here
├── scrapers/
│   ├── twitter_scraper.py   ← Nitter RSS (no API key)
│   ├── reddit_scraper.py    ← Reddit JSON API (no key)
│   ├── rss_scraper.py       ← Newsletters + HN
│   └── linkedin_scraper.py  ← Google News + rss.app
├── run_scraper.py           ← main entry point
└── requirements.txt
```

---

## What comes next (Phase 2)

Phase 2 adds AI scoring — each post gets a virality score 1–10
using Groq's free LLM API. The top 5 posts per run get flagged
for rewriting. No changes to this Phase 1 code needed.
