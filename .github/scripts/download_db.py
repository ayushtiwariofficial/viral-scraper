#!/usr/bin/env python3
"""
Download the latest viral-scraper-db artifact from any workflow run
in this repository. Handles the GitHub Actions limitation where
download-artifact@v4 only sees artifacts from the CURRENT run.
Uses the GitHub REST API directly with the GITHUB_TOKEN.
"""
import os, sys, json, zipfile, urllib.request, urllib.error

REPO    = os.environ["GITHUB_REPOSITORY"]   # e.g. "ayushtiwariofficial/viral-scraper"
TOKEN   = os.environ["GITHUB_TOKEN"]
API     = "https://api.github.com"
NAME    = "viral-scraper-db"
OUT_DIR = os.environ.get("OUTPUT_DIR", "data")


def api_get(path):
    req = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def download_artifact(artifact_id, dest_dir):
    url = f"{API}/repos/{REPO}/actions/artifacts/{artifact_id}/zip"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
    })
    zip_path = "/tmp/db_artifact.zip"
    with urllib.request.urlopen(req) as r:
        with open(zip_path, "wb") as f:
            f.write(r.read())
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dest_dir)
        files = z.namelist()
    print(f"✓ Extracted {len(files)} file(s) to {dest_dir}/: {files}")


# ── Find the latest non-expired artifact named 'viral-scraper-db' ────────────
print(f"Searching for latest '{NAME}' artifact in {REPO}...")

data = api_get(f"/repos/{REPO}/actions/artifacts?name={NAME}&per_page=10")
artifacts = [a for a in data.get("artifacts", []) if not a.get("expired")]

if not artifacts:
    print(f"", file=sys.stderr)
    print(f"ERROR: No artifact named '{NAME}' found.", file=sys.stderr)
    print(f"", file=sys.stderr)
    print(f"This means the main scraper workflow hasn't run yet,", file=sys.stderr)
    print(f"or all its artifacts have expired (retention: 90 days).", file=sys.stderr)
    print(f"", file=sys.stderr)
    print(f"Fix: Go to Actions → Viral Content Scraper → Run workflow", file=sys.stderr)
    print(f"     Let it complete, then retry this workflow.", file=sys.stderr)
    sys.exit(1)

# Sort by creation date, newest first
artifacts.sort(key=lambda a: a["created_at"], reverse=True)
latest = artifacts[0]

print(f"Found artifact: id={latest['id']}")
print(f"  Created: {latest['created_at']}")
print(f"  Size:    {latest['size_in_bytes']:,} bytes")

download_artifact(latest["id"], OUT_DIR)
print("✓ Database restored successfully")
