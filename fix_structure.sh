#!/bin/bash
# ============================================================
#  fix_structure.sh
#
#  Run this ONCE inside your cloned repo folder to fix the
#  flat file structure into the correct folder structure.
#
#  Usage:
#    git clone https://github.com/YOUR_USERNAME/viral-scraper
#    cd viral-scraper
#    chmod +x fix_structure.sh
#    ./fix_structure.sh
# ============================================================

echo "🔧 Fixing repo structure..."

# Create the required folders
mkdir -p config data scrapers logs .github/workflows

# Move files into correct folders
# (only moves if file exists in root — safe to run multiple times)

[ -f "settings.py" ]         && git mv settings.py config/settings.py         && echo "  ✓ settings.py → config/"
[ -f "database.py" ]         && git mv database.py data/database.py           && echo "  ✓ database.py → data/"
[ -f "twitter_scraper.py" ]  && git mv twitter_scraper.py scrapers/twitter_scraper.py  && echo "  ✓ twitter_scraper.py → scrapers/"
[ -f "reddit_scraper.py" ]   && git mv reddit_scraper.py scrapers/reddit_scraper.py    && echo "  ✓ reddit_scraper.py → scrapers/"
[ -f "rss_scraper.py" ]      && git mv rss_scraper.py scrapers/rss_scraper.py          && echo "  ✓ rss_scraper.py → scrapers/"
[ -f "linkedin_scraper.py" ] && git mv linkedin_scraper.py scrapers/linkedin_scraper.py && echo "  ✓ linkedin_scraper.py → scrapers/"
[ -f "SETUP.md" ]            && git mv SETUP.md docs/SETUP.md 2>/dev/null || true

# Create __init__.py files so Python treats folders as packages
touch config/__init__.py
touch data/__init__.py
touch scrapers/__init__.py
git add config/__init__.py data/__init__.py scrapers/__init__.py

# Create logs folder placeholder (git doesn't track empty folders)
touch logs/.gitkeep
git add logs/.gitkeep

echo ""
echo "📁 New structure:"
find . -not -path './.git/*' -not -name '*.pyc' -not -path './__pycache__/*' | sort

echo ""
echo "✅ Done! Now run:"
echo "   git commit -m 'fix: reorganize into correct folder structure'"
echo "   git push"
