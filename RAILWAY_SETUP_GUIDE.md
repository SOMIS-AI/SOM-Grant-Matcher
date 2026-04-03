# Step-by-Step: Deploying Grant Matcher on Railway via GitHub

---

## Before You Start — What You'll Need

- A **GitHub account** (free) → github.com
- A **Railway account** (free to start) → railway.app
- A **Gmail account** with an App Password set up
- About **30–45 minutes** for the full setup

---

## PART 1 — Set Up Gmail App Password

Your Gmail App Password is a special 16-character password that lets the app
send emails on your behalf without using your real Gmail password.

**Steps:**

1. Go to your Google Account: https://myaccount.google.com
2. Click **Security** in the left sidebar
3. Under "How you sign in to Google," click **2-Step Verification**
   - If it's not turned on, enable it first (required for App Passwords)
4. Scroll to the bottom of the 2-Step Verification page
5. Click **App passwords**
6. In the "App name" field, type: `Grant Matcher`
7. Click **Create**
8. Google shows you a 16-character password like: `abcd efgh ijkl mnop`
9. **Copy this password and save it somewhere safe** — you'll need it in Part 4

---

## PART 2 — Put the Code on GitHub

### Step 1: Create a GitHub account (skip if you have one)
Go to https://github.com and sign up for a free account.

### Step 2: Create a new repository
1. Once logged in, click the **+** icon (top right) → **New repository**
2. Fill in:
   - **Repository name:** `umsom-grant-matcher` (or any name you like)
   - **Description:** `Monitors Grants.gov and alerts UMSOM faculty of matching grants`
   - **Visibility:** Choose **Private** (recommended — keeps your config private)
3. Leave everything else as-is
4. Click **Create repository**

### Step 3: Upload the project files
GitHub will show you your empty repository. Look for the link that says
**"uploading an existing file"** — click it.

1. Open the `grant-matcher` folder on your computer
2. **Select ALL files and folders inside it** (Ctrl+A on Windows, Cmd+A on Mac)
3. Drag them into the GitHub upload area in your browser
4. Wait for all files to upload (you'll see a list appear)
5. At the bottom, in the "Commit changes" section, type a message like:
   `Initial commit — grant matcher app`
6. Click **Commit changes**

You should now see all the files listed in your GitHub repository:
```
.gitignore
Dockerfile
README.md
docker-compose.yml
main.py
railway.toml
requirements.txt
config/
  config.yaml
src/
  __init__.py
  emailer.py
  faculty_scraper.py
  grants_poller.py
  matcher.py
```

> ✅ Notice: The `data/` and `logs/` folders are NOT there — that's correct,
> they're excluded by .gitignore and will be created automatically when the app runs.

---

## PART 3 — Create a Railway Account and Project

### Step 1: Sign up for Railway
1. Go to https://railway.app
2. Click **Start a New Project**
3. Sign up using your **GitHub account** (click "Login with GitHub")
   - This automatically links Railway to GitHub — you'll need this later
4. Authorize Railway to access your GitHub account when prompted

### Step 2: Agree to the free trial
Railway gives you **$5 in free credits** when you sign up. For reference,
this app uses roughly $1–3/month on the Hobby plan ($5/month), so after
the trial you'll need to add a payment method to keep it running.

> 💡 **Hobby plan is $5/month** flat + usage. For a lightweight background
> script like this, your total bill will typically be $5–7/month.

### Step 3: Create a new project
1. From the Railway dashboard, click **New Project**
2. Click **Deploy from GitHub repo**
3. If asked to install the Railway GitHub App, click **Install Railway** and
   authorize it for your account or the specific repository
4. You'll see a list of your GitHub repositories — click on `umsom-grant-matcher`
5. Railway will start scanning the repo. It will find the `Dockerfile` and
   say it will use it to build — that's exactly what we want.
6. Click **Deploy Now**

Railway will now:
- Pull your code from GitHub
- Build the Docker container using your Dockerfile
- Try to start the app

> ⚠️ The first deployment will likely **fail** — that's expected! The app
> can't send emails yet because we haven't entered your Gmail credentials.
> We'll fix that in the next step.

---

## PART 4 — Add Your Secret Credentials to Railway

This is the important security step. Instead of putting your Gmail password
in a file on GitHub (where anyone could see it), we store it securely in
Railway's Variables system.

### Step 1: Open your service settings
1. In your Railway project, click on the service tile (it will be named
   something like `umsom-grant-matcher`)
2. Click the **Variables** tab at the top

### Step 2: Add each variable
Click **New Variable** for each of the following. Add them one at a time:

---

**Variable 1:**
- Name: `GMAIL_SENDER`
- Value: `your-actual-gmail@gmail.com`
  *(The Gmail address you want to send alerts FROM)*

---

**Variable 2:**
- Name: `GMAIL_APP_PASSWORD`
- Value: `abcdefghijklmnop`
  *(The 16-character App Password you generated in Part 1 — no spaces)*

---

**Variable 3:**
- Name: `ALERT_RECIPIENTS`
- Value: `you@yourinstitution.edu,colleague@yourinstitution.edu`
  *(Comma-separated list of everyone who should receive grant alerts)*

---

### Step 3: Apply the changes
After adding all three variables, Railway will show a banner saying
"Changes staged." Click **Deploy** to apply them and restart the app
with the new credentials.

---

## PART 5 — Verify It's Working

### Check the logs
1. Click on your service tile
2. Click the **Deployments** tab
3. Click on the most recent deployment
4. Click **View Logs** (or the **Deploy Logs** tab)

You should see output like:
```
UMSOM Grant Matcher starting up
Grants.gov check interval: 24h
Faculty re-scrape interval: 168h
Recipients: you@yourinstitution.edu
Starting grant matching cycle
Step 1/3 — Loading faculty profiles...
  Fetching faculty listing: https://www.medschool.umaryland.edu/...
  Scraping profile 1/450: Dr. John Smith
  Scraping profile 2/450: Dr. Jane Doe
  ...
```

The first run will take **15–30 minutes** to scrape all faculty profiles.
Subsequent runs will use the cache and be much faster.

### Send a test email
To confirm email is working before waiting for a real match, you can
trigger a test from Railway's interface:

1. In your service, click **Settings** tab
2. Find the **Start Command** field
3. Temporarily change it to: `python main.py --test-email`
4. Click **Deploy**
5. Check your inbox — you should receive a test email within a minute or two
6. **Change the Start Command back to blank** (so it uses the Dockerfile default)
7. Click **Deploy** again

---

## PART 6 — Add Persistent Storage (Important!)

By default, Railway's filesystem resets every time the app restarts. This
means the app would re-scrape all faculty profiles and re-send alerts for
grants it's already seen. We need to add a **Volume** so data persists.

1. In your Railway project, click **New** → **Volume**
2. Name it: `grant-matcher-data`
3. Set the **Mount Path** to: `/app/data`
4. Click **Create**

Railway will restart your service with the volume attached. Now the
`data/faculty_profiles.json` and `data/seen_grants.json` files will
survive restarts and redeployments.

---

## PART 7 — Automatic Deploys (The Best Part!)

When a new commit is pushed to the linked branch, Railway will automatically build and deploy the new code.

This means:
- If you ever need to change settings (like adding a new email recipient),
  just edit `config/config.yaml` in GitHub and commit the change
- Railway will automatically detect the change, rebuild, and redeploy
- No manual steps needed

**How to update your recipient list:**
1. Go to your GitHub repository
2. Click on `config/config.yaml`
3. Click the pencil (edit) icon
4. The `recipients` setting in the file is now just a placeholder — the real
   recipients are controlled by the `ALERT_RECIPIENTS` Railway variable
5. To add/remove recipients, go to Railway → Variables → edit `ALERT_RECIPIENTS`

---

## What Happens Now (Day-to-Day)

Once deployed, the app runs like this:

```
Every 24 hours (automatically):
  ✓ Checks Grants.gov for newly posted grants
  ✓ Compares them against faculty research keywords
  ✓ If matches found → sends you an HTML email digest
  ✓ Saves grant IDs so they won't trigger duplicate alerts

Every 7 days (automatically):
  ✓ Re-scrapes UMSOM faculty profiles to pick up new faculty
    or updated research interests
```

You don't need to do anything — it just runs.

---

## Monitoring & Troubleshooting

**To check the app is still running:**
Go to Railway → your project → service. You'll see a green "Active" badge
if it's healthy.

**To view recent activity:**
Click the service → **Deployments** → latest deployment → **View Logs**

**To see when the last email was sent:**
The logs will show lines like:
`Email sent to 2 recipient(s): you@org.edu, colleague@org.edu`

**Common issues:**

| Problem | Solution |
|---|---|
| "Gmail authentication failed" | Double-check GMAIL_APP_PASSWORD in Railway Variables — no spaces |
| App restarts frequently | Check logs for Python errors; usually a scraping issue |
| No emails after first run | Normal if no new grants matched — check logs for "No keyword matches" |
| Faculty scraping fails | UMSOM website may be temporarily down; app will retry next cycle |

---

## Costs Summary

| Item | Cost |
|---|---|
| Railway Hobby Plan | $5/month |
| Compute for this app | ~$0.50–2/month (very lightweight) |
| **Estimated total** | **~$5–7/month** |

Railway's $5 trial credit covers roughly the first month for free.

---

## Need to Make Changes Later?

**Add a new email recipient:**
Railway dashboard → Variables → edit `ALERT_RECIPIENTS`

**Change how often it checks:**
Edit `config/config.yaml` in GitHub, change `check_interval_hours`, commit

**Force a fresh faculty scrape right now:**
Railway → service → Settings → Start Command → temporarily set to
`python main.py --run-once --scrape` → Deploy → then revert

**Stop the app temporarily:**
Railway → service → Settings → scroll down → **Suspend Service**
