# UMSOM Grant Matcher 🎓

Automatically monitors [Grants.gov](https://www.grants.gov) for new funding opportunities and alerts you when a grant's keywords match a faculty member's research interests at the [University of Maryland School of Medicine](https://www.medschool.umaryland.edu/faculty/faculty-profiles/).

> **Changing how matching behaves?** Record it in **[TUNING_LOG.md](TUNING_LOG.md)** —
> thresholds, filters, scoring and vocabulary changes, with why they were tried and
> whether they actually worked. It also lists approaches already known to fail, so
> we stop re-trying them. Measure outcomes with `python tools/diag_trend.py <Diag Files folder>`.

---

## How It Works

```
Once a day at 6am ET (configurable):
  1. Scrape UMSOM faculty profiles → extract & enrich research keywords per faculty
     member (cached for 7 days to be polite to the server)
  2. Fetch newly posted grants from 30+ sources (Grants.gov + foundations, portals, etc.)
  3. Match grants to faculty using hybrid keyword + semantic (AI embedding) matching,
     scored 0–100 by confidence
  4. Email a match digest to faculty recipients (when there are matches),
     plus an admin-only diagnostic report
```

---

## Quick Start

### Prerequisites
- Python 3.11+ (or Docker)
- A [SendGrid](https://sendgrid.com) account with an API key and a verified sender

### 1. Set Up SendGrid

Email is sent via the SendGrid API (not SMTP). You need two things:

1. **An API key** — in the SendGrid dashboard go to **Settings → API Keys → Create API Key**
   (a "Restricted Access" key with **Mail Send** permission is sufficient). Copy the key — it is
   shown only once.
2. **A verified sender** — under **Settings → Sender Authentication**, verify the "from" address
   (Single Sender Verification) or authenticate your domain. The `SENDGRID_FROM_EMAIL` you use
   must match a verified sender or SendGrid will reject the message.

### 2. Configure the App

**Secrets are never stored in `config.yaml`** — they are supplied at runtime via environment
variables (in Azure: **Configuration → Application Settings**). `config.yaml` only holds
placeholders, which the app overrides from the environment on startup (`main.py:load_config`).

Set these environment variables / Application Settings:

| Variable | Required | Description |
|---|---|---|
| `SENDGRID_API_KEY` | ✅ | Your SendGrid API key |
| `SENDGRID_FROM_EMAIL` | ✅ | Verified sender address (the "from" address) |
| `DAILY_RECIPIENTS` | ✅* | Comma-separated recipients of the **daily** digest, sent every day at the scheduled time |
| `WEEKLY_RECIPIENTS` | ✅* | Comma-separated recipients of the **weekly** 7-day roundup, sent only on the configured weekday (default Tuesday) |
| `DIAGNOSTIC_RECIPIENTS` | optional | Comma-separated recipients of the admin diagnostic email. Defaults to the **first** address in the daily/weekly lists. |
| `RESTART_RECIPIENTS` | optional | Comma-separated admins who receive a **restart/health email** when the app starts (e.g. after a deploy). This is the **only** email sent on restart. |
| `MANUAL_RECIPIENTS` | optional | Default recipients for a **manually-triggered** digest (see "Manual digest" below). |
| `NOTIFY_TIMEZONE` | optional | IANA timezone for the fire time. Default `America/New_York`. |
| `NOTIFY_HOUR` | optional | Hour of day (0–23) to send. Default `6` (6am). |
| `WEEKLY_WEEKDAY` | optional | Weekday for the weekly roundup, `Mon=0 … Sun=6`. Default `1` (Tuesday). |
| `DASHBOARD_URL` | optional | Full URL to your dashboard, linked in emails |

*At least one of `DAILY_RECIPIENTS` / `WEEKLY_RECIPIENTS` should be set, or no faculty-facing email is sent. They are independent groups — a person only gets the cadence whose list they're on. (The legacy `ALERT_RECIPIENTS` variable has been **retired**.)

To run locally, export them in your shell before starting the app (PowerShell example):

```powershell
$env:SENDGRID_API_KEY    = "SG.xxxxxxxx"
$env:SENDGRID_FROM_EMAIL = "grants@yourinstitution.edu"
$env:DAILY_RECIPIENTS    = "daily-reader@yourinstitution.edu"
$env:WEEKLY_RECIPIENTS   = "weekly-reader@yourinstitution.edu,dept-head@yourinstitution.edu"
```

You can change recipients at any time by editing these variables — the change takes effect on the next scheduled run.

### 3. Deploy

#### Option A: Docker (Recommended for servers)

```bash
# Build and start
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

The container runs continuously and checks every 24 hours. It restarts automatically if the server reboots.

#### Option B: Python directly

```bash
# Install dependencies
pip install -r requirements.txt

# Test email configuration first
python main.py --test-email

# Run once (test / cron job)
python main.py --run-once

# Run continuously (server mode)
python main.py
```

#### Option C: Systemd service (Linux server without Docker)

Create `/etc/systemd/system/grant-matcher.service`:

```ini
[Unit]
Description=UMSOM Grant Matcher
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/grant-matcher
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable grant-matcher
sudo systemctl start grant-matcher
sudo systemctl status grant-matcher
```

---

## What the Emails Look Like

The app sends these emails, by audience and trigger:

| Email | Recipients | When |
|---|---|---|
| **Daily digest** | `DAILY_RECIPIENTS` | Every day at `NOTIFY_HOUR` (default 6am ET), when there are matches |
| **Weekly roundup** | `WEEKLY_RECIPIENTS` | Only on `WEEKLY_WEEKDAY` (default Tuesday) at the same hour — covers the **last 7 days** of matches |
| **Diagnostic report** | `DIAGNOSTIC_RECIPIENTS` (admin) | Every scheduled run |
| **Restart / health** | `RESTART_RECIPIENTS` (admin) | Once on app startup/restart — the only email sent on restart |
| **Manual digest** | `MANUAL_RECIPIENTS` or `--to` | On demand via `--send-digest` (see below) |

### 1. Match Digest (daily and weekly)

Sent to `DAILY_RECIPIENTS` every day (and, as a 7-day roundup, to `WEEKLY_RECIPIENTS` on Tuesdays), **only when at least one new grant matches a faculty member**. It contains:
- **Grant title** with a direct link to the listing
- **Agency, grant number, deadline, award ceiling**
- **Table of matched faculty** with their department, the **match type** (keyword / AI / keyword + AI), a **confidence score (0–100%)**, and the specific keywords that triggered the match

Matches with a higher **confidence score** are listed first. Confidence is a calibrated 0–100 score that weights rare/specific keywords (IDF) more heavily than generic ones, adds bonuses for title hits and semantic similarity, and is filtered by `matching.min_confidence_score`. (It replaced the older raw keyword-count "match score.")

### 2. Diagnostic Report (admin only)

Sent to `DIAGNOSTIC_RECIPIENTS` (or, if unset, the **first** address in the daily/weekly recipient lists) on **every completed scheduled run** — including days with no grants or no matches. It is an operational/tuning report, not a faculty-facing alert, and contains:
- **Run summary** — faculty processed, grants checked, grants skipped (non-biomedical), raw vs. post-filter match counts, run duration
- **Active parameters** — semantic threshold, min confidence, IDF floor, per-grant cap, etc.
- **Dynamic stop words** suppressed this run
- **Per-grant detail**, **confidence histograms**, and **semantic score distributions**
- **Foundation scraper health** — per-source new-grant counts and alerts for sources returning 0 results for 3+ consecutive runs

> **Tip:** the diagnostic email is designed to be forwarded to Claude for analysis and tuning recommendations.

### 3. Restart / Health Email (admin only)

Sent to `RESTART_RECIPIENTS` **once when the app starts or restarts** (e.g. after a deploy). This is the **only** email that fires on restart — it confirms the process is live and reports the environment, build/commit id, the next scheduled run time, and recipient counts. It does **not** run the grant pipeline.

### On-demand: Manual Digest

You can manually send a digest of matches from the last N days at any time — useful to share recent results with an ad-hoc address. It reads **already-stored** results, so it does not run the pipeline or affect the scheduled emails:

```bash
# Send the last 24h of matches to MANUAL_RECIPIENTS
python main.py --send-digest

# Last 7 days, to a specific address (overrides MANUAL_RECIPIENTS)
python main.py --send-digest --days 7 --to someone@org.edu,another@org.edu
```

On Azure, trigger it the same way as the test email: temporarily set the **Startup Command** to `python main.py --send-digest --to someone@org.edu`, save (the app restarts and runs it), then clear the Startup Command to resume normal scheduling.

#### Manual digest cheat-sheet

- **What it does:** sends a one-off match digest covering the last N days. Reads stored results only — never scrapes, never marks grants seen, never affects the scheduled daily/weekly emails.
- **Default window:** `--days 1` (last 24 hours). Use `--days 7` for a week, etc.
- **Who receives it (precedence):** `--to a@b.com,c@d.com`  →  else `MANUAL_RECIPIENTS`  →  else `DAILY_RECIPIENTS`  →  else nothing is sent.
- **`MANUAL_RECIPIENTS` does nothing on its own** — it is only the default recipient list for this command. Setting it never triggers a send; you must run `--send-digest`.
- **No matches in the window → no email** (it logs "no matches found in the last N days").
- **Local:** `python main.py --send-digest [--days N] [--to ...]`
- **Azure:** set **Startup Command** → save (restarts, runs, exits) → **clear the Startup Command and save again** to resume the scheduler. ⚠️ Until you clear it, the app keeps re-running the digest instead of the scheduler. ⚠️ Triggering it restarts the app, so `RESTART_RECIPIENTS` will also get a restart email.

| You want… | Command |
|---|---|
| Last 24h to the default list | `python main.py --send-digest` |
| Last 24h to a specific person | `python main.py --send-digest --to dean@org.edu` |
| Last 7 days to two people | `python main.py --send-digest --days 7 --to a@org.edu,b@org.edu` |

> **Note on timing:** the matcher fires once per day at a **fixed wall-clock time** (`NOTIFY_HOUR`, default 6am, in `NOTIFY_TIMEZONE`, default `America/New_York`). It does **not** send anything on startup or restart — it simply waits for the next fire time. If the app happens to be down at the fire time, that day is skipped (unseen grants surface on the next successful run).

---

## Configuration Reference

All settings are in `config/config.yaml`:

| Setting | Default | Description |
|---|---|---|
| `email.sender` | — | Placeholder; set the real value via the `SENDGRID_FROM_EMAIL` env var |
| `email.sendgrid_api_key` | — | Placeholder; set the real value via the `SENDGRID_API_KEY` env var |
| `email.recipients` | — | Unused placeholder; real recipients come from the `DAILY_RECIPIENTS` / `WEEKLY_RECIPIENTS` env vars |
| `email.subject_prefix` | `[Grant Match]` | Prefix for the match digest subject line |
| `faculty.rescrape_interval_hours` | 168 (7 days) | How often to re-scrape faculty profiles |
| `grants.check_interval_hours` | 24 | Legacy; no longer drives scheduling — the daily fire time is set by `NOTIFY_HOUR`/`NOTIFY_TIMEZONE` env vars |
| `grants.max_results_per_check` | 100 | Max grants fetched per run |
| `grants.statuses` | `[posted, forecasted]` | Grant statuses to include |
| `matching.min_keyword_length` | 4 | Minimum keyword character length |
| `matching.stop_words` | (list) | Words to exclude from keyword matching |

---

## Useful Commands

```bash
# Force re-scrape faculty profiles on next run
python main.py --run-once --scrape

# Send a test email to verify SendGrid is configured correctly
python main.py --test-email

# Run with a custom config file
python main.py --config /path/to/my-config.yaml

# View live logs (Docker)
docker compose logs -f

# View log file directly
tail -f logs/grant_matcher.log
```

---

## Data Files

| File | Purpose |
|---|---|
| `data/faculty_profiles.json` | Cached faculty profiles + keywords |
| `data/seen_grants.json` | IDs of already-processed grants (prevents duplicate emails) |
| `logs/grant_matcher.log` | Application logs |

---

## Troubleshooting

**"SENDGRID_API_KEY environment variable is not set"**
→ The `SENDGRID_API_KEY` (and `SENDGRID_FROM_EMAIL`) env vars / Azure Application Settings are missing. Set both and restart.

**SendGrid returns 401 / 403, or mail silently never arrives**
→ 401/403 means a bad/revoked API key, or the key lacks **Mail Send** permission. If sends succeed (2xx) but mail never arrives, the `SENDGRID_FROM_EMAIL` is likely not a **verified sender** in SendGrid — verify it under Sender Authentication.

**"No faculty profiles loaded"**
→ The UMSOM website may be temporarily down or have changed structure. Check logs for details. You can manually test with: `python -c "from src.faculty_scraper import scrape_faculty_list; import requests; print(scrape_faculty_list(requests.Session()))"`

**"No new grants found"**
→ Normal if run multiple times in one day — the `seen_grants.json` file tracks already-processed grants. Delete it to reprocess all recent grants.

**Very few keyword matches**
→ Consider reducing `matching.min_keyword_length` or removing overly broad terms from `stop_words` in the config.

---

## Cloud Hosting Options

For an always-on server, consider:
- **DigitalOcean Droplet** ($6/mo) — run with Docker
- **Linode/Akamai** ($5/mo) — run with Docker  
- **AWS EC2 t4g.nano** (~$3/mo) — run with Docker or systemd
- **Google Cloud Run** — run on a schedule (free tier likely sufficient)
- **Railway.app** — easy Docker deployment, free tier available

---

## Architecture

```
grant-matcher/
├── main.py                  # Entry point + scheduler
├── src/
│   ├── faculty_scraper.py   # Scrapes UMSOM faculty profiles
│   ├── grants_poller.py     # Polls Grants.gov API
│   ├── matcher.py           # Keyword matching engine
│   └── emailer.py           # HTML email builder + SendGrid sender
├── config/
│   └── config.yaml          # All configuration
├── data/                    # Runtime data (auto-created)
│   ├── faculty_profiles.json
│   └── seen_grants.json
├── logs/                    # Log files (auto-created)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
