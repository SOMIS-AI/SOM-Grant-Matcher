# Deploying Grant Matcher on Azure Web App

---

## What You'll Need

- An **Azure account** — portal.azure.com (free tier available)
- A **GitHub account** with the repo already pushed (done)
- A **Gmail account** with an App Password

---

## PART 1 — Set Up Gmail App Password

1. Go to https://myaccount.google.com → **Security**
2. Enable **2-Step Verification** if not already on
3. Go to **Security → App passwords**
4. App name: `Grant Matcher` → click **Create**
5. Copy the 16-character password — you'll need it in Part 3

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

| Name | Value |
|---|---|
| `GMAIL_SENDER` | your-gmail@gmail.com |
| `GMAIL_APP_PASSWORD` | your 16-character app password (no spaces) |
| `ALERT_RECIPIENTS` | comma-separated emails, e.g. `you@org.edu,colleague@org.edu` |
| `WEBSITES_PORT` | `8080` |

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
Starting grant matching cycle
Step 1/3 — Loading faculty profiles...
```

### Send a test email
1. Go to **Configuration** → **General settings**
2. Set **Startup Command** to: `python main.py --test-email`
3. Click **Save** — the app will restart and send a test email
4. Check your inbox, then **remove the startup command** and save again

---

## Day-to-Day Operation

Once deployed, the app runs automatically:

```
Every 24 hours:
  ✓ Checks Grants.gov for newly posted grants
  ✓ Compares them against faculty research keywords
  ✓ If matches found → sends HTML email digest

Every 7 days:
  ✓ Re-scrapes UMSOM faculty profiles
```

Every push to `main`/`master` on GitHub triggers an automatic redeploy.

---

## Updating Recipients

Go to Azure portal → your Web App → **Configuration** → **Application settings** → edit `ALERT_RECIPIENTS`

---

## Troubleshooting

| Problem | Solution |
|---|---|
| "Gmail authentication failed" | Check `GMAIL_APP_PASSWORD` in Application Settings — no spaces |
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
