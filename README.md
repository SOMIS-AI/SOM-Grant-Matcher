# UMSOM Grant Matcher 🎓

Automatically monitors [Grants.gov](https://www.grants.gov) for new funding opportunities and alerts you when a grant's keywords match a faculty member's research interests at the [University of Maryland School of Medicine](https://www.medschool.umaryland.edu/faculty/faculty-profiles/).

---

## How It Works

```
Every 24 hours:
  1. Scrape UMSOM faculty profiles → extract research keywords per faculty member
     (cached for 7 days to be polite to the server)
  2. Fetch newly posted grants from Grants.gov (free public API, no key needed)
  3. Match grant titles/descriptions against faculty keywords
  4. Send one HTML digest email listing all matches
```

---

## Quick Start

### Prerequisites
- Python 3.11+ (or Docker)
- A Gmail account with an App Password

### 1. Set Up Gmail App Password

Gmail requires an "App Password" (not your regular password) for SMTP access:

1. Go to your Google Account → **Security**
2. Enable **2-Step Verification** if not already on
3. Go to **Security → App passwords**
4. Click "Select app" → choose **Mail**
5. Click "Select device" → choose **Other** → type "Grant Matcher"
6. Click **Generate** — copy the 16-character password (e.g. `abcd efgh ijkl mnop`)

### 2. Configure the App

Edit `config/config.yaml`:

```yaml
email:
  sender: "your-gmail@gmail.com"
  app_password: "abcd efgh ijkl mnop"   # The 16-char app password from above
  recipients:
    - "you@yourinstitution.edu"
    - "colleague@yourinstitution.edu"
```

You can add/remove recipients at any time by editing this file. The change takes effect on the next run.

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

## What the Email Looks Like

Each email digest contains:
- **Grant title** with a direct link to the Grants.gov listing
- **Agency, grant number, deadline, award ceiling**
- **Synopsis snippet**
- **Table of matched faculty** with their department, email, match score, and the specific keywords that triggered the match

Grants with more keyword overlaps get a higher **match score** and appear first.

---

## Configuration Reference

All settings are in `config/config.yaml`:

| Setting | Default | Description |
|---|---|---|
| `email.sender` | — | Gmail address to send from |
| `email.app_password` | — | Gmail App Password |
| `email.recipients` | — | List of email addresses to notify |
| `faculty.rescrape_interval_hours` | 168 (7 days) | How often to re-scrape faculty profiles |
| `grants.check_interval_hours` | 24 | How often to poll Grants.gov |
| `grants.max_results_per_check` | 100 | Max grants fetched per run |
| `grants.statuses` | `[posted, forecasted]` | Grant statuses to include |
| `matching.min_keyword_length` | 4 | Minimum keyword character length |
| `matching.stop_words` | (list) | Words to exclude from keyword matching |

---

## Useful Commands

```bash
# Force re-scrape faculty profiles on next run
python main.py --run-once --scrape

# Send a test email to verify Gmail works
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

**"Gmail authentication failed"**
→ Make sure you're using an App Password, not your regular Gmail password. 2-Step Verification must be enabled.

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
│   └── emailer.py           # HTML email builder + Gmail sender
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
