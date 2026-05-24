# Deploying Grant Matcher on Azure Web App

---

## What You'll Need

- An **Azure account** — portal.azure.com (free tier available)
- A **GitHub account** with the repo already pushed (done)
- A **SendGrid account** — sendgrid.com (free tier: 100 emails/day)

---

## PART 1 — Set Up SendGrid

1. Go to https://sendgrid.com and create a free account
2. Verify your sender email address:
   - Go to **Settings → Sender Authentication → Single Sender Verification**
   - Click **Create a Sender** and fill in your details
   - Check your inbox and click the verification link
3. Create an API key:
   - Go to **Settings → API Keys → Create API Key**
   - Name: `Grant Matcher`, Permission: **Restricted** → enable **Mail Send**
   - Click **Create & View** — copy the key immediately (it won't be shown again)

---

## PART 2 — Create the Azure Web App

1. Go to https://portal.azure.com
2. Click **Create a resource** → search **Web App** → click **Create**
3. Fill in the form:
   - **Subscription:** your subscription
   - **Resource Group:** create new, e.g. `grant-matcher-rg`
   - **Name:** e.g. `som-grant-matcher` *(must be globally unique — this becomes your URL)*
   - **Publish:** `Container`
   - **Operating System:** `Linux`
   - **Region:** East US (or nearest to you)
4. Click **Next: Container**
5. Under **Image Source**, select `GitHub Container Registry` (or leave as Other — we'll configure this via GitHub Actions)
   - For now you can leave the image fields blank and configure after first deploy
6. Click **Review + create** → **Create**

---

## PART 3 — Add Application Settings (Environment Variables)

In Azure, secrets are stored as **Application Settings** (equivalent to Railway Variables).

1. Go to your new Web App in the Azure portal
2. In the left menu, click **Configuration** → **Application settings**
3. Click **+ New application setting** for each of the following:

| Name | Value | Required |
|---|---|---|
| `SENDGRID_API_KEY` | your SendGrid API key | ✅ |
| `SENDGRID_FROM_EMAIL` | your verified sender address in SendGrid | ✅ |
| `DAILY_RECIPIENTS` | comma-separated emails for the **daily** 6am digest, e.g. `you@org.edu` | one of daily/weekly |
| `WEEKLY_RECIPIENTS` | comma-separated emails for the **Tuesday** 6am 7-day roundup | one of daily/weekly |
| `DIAGNOSTIC_RECIPIENTS` | comma-separated admin emails for the diagnostic report (defaults to first daily/weekly address) | optional |
| `RESTART_RECIPIENTS` | comma-separated admin emails that get a **health email on every restart/deploy** (the only email sent on restart) | optional |
| `MANUAL_RECIPIENTS` | default recipients for the on-demand `--send-digest` command | optional |
| `NOTIFY_TIMEZONE` | IANA timezone for the send time (default `America/New_York`) | optional |
| `NOTIFY_HOUR` | hour of day 0–23 to send (default `6`) | optional |
| `WEEKLY_WEEKDAY` | weekday for the weekly roundup, Mon=0 … Sun=6 (default `1` = Tuesday) | optional |
| `WEBSITES_PORT` | `8080` | ✅ |

> The legacy `ALERT_RECIPIENTS` setting is **retired** — if you have it, delete it and use
> `DAILY_RECIPIENTS` / `WEEKLY_RECIPIENTS` instead.

4. Click **Save** at the top

---

## PART 4 — Connect GitHub Actions for Automatic Deploys

### Step 1: Download the publish profile
1. In your Azure Web App, click **Overview**
2. Click **Download publish profile** (button near the top)
3. Open the downloaded `.PublishSettings` file in a text editor and copy all the contents

### Step 2: Add secrets to GitHub
1. Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**:
   - Name: `AZURE_WEBAPP_PUBLISH_PROFILE`
   - Value: paste the entire contents of the publish profile file
3. Click **New repository variable** (under the Variables tab):
   - Name: `AZURE_WEBAPP_NAME`
   - Value: the name you gave your Web App (e.g. `som-grant-matcher`)

### Step 3: Trigger the first deploy
Push any change to the `main` or `master` branch, or go to:
**GitHub repo → Actions → Build and Deploy to Azure Web App → Run workflow**

GitHub Actions will:
1. Build the Docker image
2. Push it to GitHub Container Registry (ghcr.io)
3. Deploy it to your Azure Web App

---

## PART 5 — Add Persistent Storage (Important!)

By default, Azure Web Apps have ephemeral storage — the `data/` folder resets on restart, causing the app to re-scrape faculty and re-send duplicate emails.

To add persistent storage with Azure Files:

1. Create a **Storage Account** in Azure portal → **Storage accounts** → **Create**
   - Same resource group as your Web App
   - Name: e.g. `grantmatcherstorage`
   - Redundancy: LRS (cheapest)
2. Inside the storage account, go to **File shares** → **+ File share**
   - Name: `grant-matcher-data`
3. Go back to your Web App → **Configuration** → **Path mappings**
4. Click **+ New Azure Storage Mount**:
   - Name: `data`
   - Storage type: `Azure Files`
   - Storage account: `grantmatcherstorage`
   - Share name: `grant-matcher-data`
   - Mount path: `/app/data`
5. Click **OK** → **Save**

Now `data/faculty_profiles.json` and `data/seen_grants.json` will persist across restarts and redeployments.

---

## PART 6 — Verify It's Working

### Check logs
1. In your Web App → left menu → **Log stream**
2. You should see output like:
```
Starting UMSOM Grant Matcher Dashboard
Grant Matcher background thread started
Scheduler started — daily digest at 06:00 America/New_York; weekly roundup on Tuesday. No email on startup/restart.
Next scheduled run: 2026-05-24T06:00:00-04:00 (18.2h away)
```
(The pipeline does **not** run at startup — it waits for the next scheduled fire time. Use the
test-email step below to verify mail delivery immediately.)

### Send a test email
1. Go to **Configuration** → **General settings**
2. Set **Startup Command** to: `python main.py --test-email`
3. Click **Save** — the app will restart, send a test email via SendGrid, then exit
4. Check your inbox, then **remove the startup command** and save again to resume normal operation

### Send a manual digest (last 24h / N days)
Use this to send recent grants & matches to an ad-hoc address on demand. It reads
already-stored results, so it won't disturb the scheduled emails or the seen-grants tracker.
1. Go to **Configuration** → **General settings**
2. Set **Startup Command** to: `python main.py --send-digest --to someone@org.edu`
   (add `--days 7` for the last week; omit `--to` to use `MANUAL_RECIPIENTS`)
3. Click **Save** — the app restarts, sends the digest, then exits
4. **Remove the startup command** and save again to resume normal scheduling

#### Manual digest cheat-sheet
- **Window:** default last 24h; add `--days N` for a longer window.
- **Recipients (precedence):** `--to a@b.com,c@d.com` → else `MANUAL_RECIPIENTS` → else `DAILY_RECIPIENTS` → else nothing is sent.
- **`MANUAL_RECIPIENTS` is just the default address list** — setting it never sends anything by itself; you must run the `--send-digest` command.
- **No matches in the window → no email is sent** (it logs and exits).
- ⚠️ **Always clear the Startup Command in step 4** — until you do, the app keeps running the one-off digest instead of the scheduler.
- ⚠️ This restarts the app, so `RESTART_RECIPIENTS` (if set) will also get a restart email.

| You want… | Startup Command |
|---|---|
| Last 24h to `MANUAL_RECIPIENTS` | `python main.py --send-digest` |
| Last 24h to a specific person | `python main.py --send-digest --to dean@org.edu` |
| Last 7 days to two people | `python main.py --send-digest --days 7 --to a@org.edu,b@org.edu` |

### Restart notifications
If you set `RESTART_RECIPIENTS`, those admins get a short health email **every time the app
restarts** (including each deploy) confirming it came back up and showing the next scheduled
run time — a quick way to verify a commit deployed successfully.

---

## Day-to-Day Operation

Once deployed, the app runs automatically:

```
Daily at 6am ET (NOTIFY_HOUR / NOTIFY_TIMEZONE):
  ✓ Checks 30+ sources for newly posted grants
  ✓ Matches them against faculty research keywords (keyword + AI)
  ✓ If matches found → sends the daily digest to DAILY_RECIPIENTS
  ✓ Sends the diagnostic report to the admin

Tuesdays at 6am ET (WEEKLY_WEEKDAY):
  ✓ Additionally sends a 7-day roundup to WEEKLY_RECIPIENTS

Every 7 days:
  ✓ Re-scrapes UMSOM faculty profiles (cache-driven)
```

**No email is sent on startup or restart** — the scheduler only fires at the configured
wall-clock time. If the app is down at 6am, that day is skipped (grants surface on the next run).

Every push to `main`/`master` on GitHub triggers an automatic redeploy.

---

## Updating Recipients

Go to Azure portal → your Web App → **Configuration** → **Application settings** → edit
`DAILY_RECIPIENTS` (daily 6am) and/or `WEEKLY_RECIPIENTS` (Tuesday 6am roundup), then **Save**.
The change takes effect on the next scheduled run. To shift the send time or weekday, edit
`NOTIFY_HOUR`, `NOTIFY_TIMEZONE`, or `WEEKLY_WEEKDAY`.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| "Failed to send email via SendGrid" | Check `SENDGRID_API_KEY` in Application Settings; verify sender address is verified in SendGrid |
| App keeps restarting | Check Log stream for Python errors |
| No emails after first run | Normal if no new grants matched — check logs for "No keyword matches" |
| GitHub Actions deploy fails | Check that `AZURE_WEBAPP_PUBLISH_PROFILE` secret is set correctly |

---

## Cost Estimate

| Item | Cost |
|---|---|
| Azure App Service (B1 Basic) | ~$13/month |
| Azure App Service (F1 Free) | $0 (60 CPU min/day limit — may be sufficient) |
| Azure Storage (for persistent data) | ~$0.02/month |
| **Estimated total** | **$0–15/month** |

The F1 free tier is often sufficient for a lightweight background app like this.
