# Phase 4 Setup — Posting

## Important context first

**Twitter/X posting is manual.** As of February 6, 2026, X discontinued free
API access for new developer accounts — posting now costs $0.015–$0.20 per
post under their pay-per-use model. Since this project is committed to
staying genuinely free, Twitter posting works like this instead:

1. The pipeline drafts a Twitter thread and saves it to the queue
2. You get a notification (via ntfy) with the full, copy-paste-ready thread
3. You paste it into Twitter yourself
4. Run `python run_scraper.py --mark-twitter-posted <ID>` so the system
   doesn't keep reminding you about a post you already published

**LinkedIn posting is automated, but gated behind your explicit approval.**
LinkedIn still has no free posting API (unchanged since this project
started), so this uses browser automation (Playwright) instead — which goes
against LinkedIn's Terms of Service. To manage that risk:
- Nothing posts without you explicitly approving that specific post ID
- It reuses one saved login session rather than logging in fresh each time
- You're in control of how often it posts (there's no autonomous schedule)

---

## Setup steps

### 1. Notifications (optional but recommended) — ntfy.sh, free, no signup

1. Pick a random, hard-to-guess topic name, e.g. `viral-scraper-yourname-8273`
   (anyone who knows your topic name can read your notifications, so don't
   use something guessable)
2. Install the **ntfy** app (iOS/Android) or use the web client at
   https://ntfy.sh/app — subscribe to your topic name
3. Add it to `.env`:
   ```
   NTFY_TOPIC=viral-scraper-yourname-8273
   ```
4. Add the same value as a GitHub Actions secret (Settings → Secrets →
   Actions → New repository secret → `NTFY_TOPIC`)

Without this, content still lands in the database — you just won't get a
push notification about it. `python run_scraper.py --stats` always shows
what's waiting.

---

### 2. LinkedIn session (required for LinkedIn auto-posting)

This is a one-time, ~5 minute setup done **on your own machine**, not in CI
(GitHub Actions can't show you a browser window to log in).

```bash
# Install Playwright's browser binary (one-time)
pip install -r requirements.txt
playwright install chromium

# Run the interactive login
python -m poster.linkedin_poster --login
```

A real browser window opens. Log into LinkedIn manually, including any 2FA
step. Once you see your feed, go back to the terminal and press Enter.

This saves your session to `data/linkedin_session.json` — a file that
contains live login cookies. **It's already in `.gitignore` and must never
be committed.**

#### Getting that session into GitHub Actions

GitHub Actions runs on a fresh machine each time, so it needs its own copy
of that session file, passed in as a secret:

```bash
# On your machine, base64-encode the session file
base64 -i data/linkedin_session.json | tr -d '\n' > session_b64.txt

# Copy the contents of session_b64.txt
cat session_b64.txt
```

Then: GitHub repo → Settings → Secrets and variables → Actions → New
repository secret → name it `LINKEDIN_SESSION_B64` → paste the base64 string.

Delete `session_b64.txt` locally afterward — you don't need it once it's
saved as a secret.

**Sessions expire** (LinkedIn-side, typically weeks to months). If LinkedIn
posting starts failing with a "session expired" error, just redo this
section — login again, re-encode, update the secret.

---

## Day-to-day usage

### Reviewing and approving content

1. Get a notification, or run `python run_scraper.py --stats` and look at
   "Posting status (Phase 4)" → "LinkedIn pending approval"
2. To see the actual content for a specific ID, query the database directly,
   or check the ntfy notification you received for it
3. Go to your repo's **Actions** tab → **Approve & Post to LinkedIn** →
   **Run workflow**
4. Enter the content ID, choose `approve` or `reject`, run it

If you approve, it posts within the same workflow run (under a minute
typically). If you reject, it's marked permanently rejected and won't be
suggested again.

### Posting to Twitter manually

1. Get a notification with the full thread text, or check `--stats` /
   query the database for `content_queue` rows where `posted_twitter = 0`
2. Copy the thread, paste it into Twitter/X yourself
3. Run:
   ```bash
   python run_scraper.py --mark-twitter-posted <content_id>
   ```
   (You can also do this via a GitHub Actions `workflow_dispatch` if you'd
   rather not run it locally — ask if you want that workflow added.)

---

## Safety notes

- **LinkedIn automation violates LinkedIn's ToS.** The approval gate and
  session-reuse pattern reduce — but don't eliminate — the risk of
  temporary restrictions or, in rare cases, account action. Posting
  infrequently (a few times a day, not dozens) further reduces risk.
- **Nothing posts automatically.** Every LinkedIn post requires you to
  explicitly run the approval workflow with that specific content ID.
  There's no cron job, no scheduled auto-approval, nothing that posts
  without you taking an action first.
- **Rejected content stays rejected.** Once you reject a post ID, it's
  permanently marked and will never be picked up again, even if you
  re-run scoring/rewriting.
