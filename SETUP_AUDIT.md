# UMSOM Grant Matcher — Setup Audit

_Audit date: 2026-05-24. Covers the application code, the GitHub repository/CI, and
the Azure resources as far as they can be confirmed from the repo + GitHub API._

**Legend:** ✅ confirmed from code/GitHub · 🔶 inferred (verify in Azure portal) · ⚠️ problem/risk

---

## 1. Executive summary

- The application **persists all of its data on the local filesystem** (JSON files +
  a local SQLite file under `data/`). ✅
- It **does not use PostgreSQL or MySQL at all** — no driver, no connection code, no
  reference to `AZURE_POSTGRESQL_CONNECTIONSTRING` anywhere in any branch or commit. ✅
- The **two database servers** in the resource group and the `AZURE_POSTGRESQL_CONNECTIONSTRING`
  app setting are **orphaned infrastructure** — provisioned/linked via Azure's Service
  Connector but never consumed by the code. 🔶→ verify with the checks in §6.
- Because the deployed container has **no persistent `/app/data` mount**, all data is
  **wiped on every restart**. The old code masked this by re-scraping on every boot; the
  new scheduler does not run on boot, so the empty dashboard is now visible. ⚠️
- There are **two Azure Web Apps** (production `SOMGrantMatcher` and dev `somgrantmatcher-dev`)
  and a pile of **leftover GitHub secrets/variables** from earlier deploy attempts that the
  current workflow no longer uses. 🔶/⚠️

**The core decision:** keep the filesystem model (add a storage mount, delete the unused DBs)
**or** migrate the app to the PostgreSQL you already pay for. See §8.

---

## 2. Architecture & runtime

- **App:** a Flask dashboard + a background "matcher" thread, launched by `run.py`
  (Docker `CMD ["python","-u","run.py"]`). ✅
- **Matcher loop:** `main.run_scheduler()` fires once/day at `NOTIFY_HOUR` (default 06:00)
  in `NOTIFY_TIMEZONE` (default `America/New_York`); weekly roundup on `WEEKLY_WEEKDAY`
  (default Tuesday). **No grant email on startup/restart**; a restart/health email is the
  only thing sent on boot. ✅
- **Pipeline:** scrape UMSOM faculty (+ enrich from PubMed/ORCID/etc.) → fetch grants
  (Grants.gov + external scrapers) → hybrid keyword+semantic matching → SendGrid emails. ✅
- **Email:** SendGrid API only (not SMTP). ✅

---

## 3. Data & persistence (the crux)

All runtime data is written to the **container filesystem**, root `data/` and `logs/`:

| File | Written by | Purpose |
|---|---|---|
| `data/faculty_profiles.json` | faculty_scraper | Faculty cache + keywords + embeddings (the expensive one) |
| `data/seen_grants.json` | grants_poller | Dedup tracker — prevents re-emailing the same grant |
| `data/match_results.json` | matcher | Rolling match history (capped 5000) — feeds dashboard + weekly roundup |
| `data/run_stats.json` | matcher | Authoritative last-run/scrape stats — feeds dashboard |
| `data/scraper_health.json` | foundation_scraper | Per-source scraper health |
| `data/matcher.db` | matcher (SQLite) | Match rows for the diagnostic script |
| `logs/grant_matcher.log` | logging | Rotating app log |

- **Dashboard** reads `faculty_profiles.json`, `match_results.json`, `run_stats.json`,
  and the log file from `DATA_DIR` (default `data/`). ✅
- **Only database the code opens is SQLite** (`sqlite3.connect("data/matcher.db")`). ✅
- ⚠️ **None of this survives a restart without a persistent mount at `/app/data`.**

### Postgres / MySQL — confirmed NOT used
- No `psycopg2`/`asyncpg`/`sqlalchemy`/`pymysql` in `requirements.txt`. ✅
- No `create_engine`, no connection string read, no `AZURE_POSTGRESQL_CONNECTIONSTRING`
  reference — in **any branch or any of the 30 commits**. ✅
- The single "Postgres" mention is one **docstring comment** in
  `app/grant_matcher_diagnostics.py`. ✅
- Proof the running app is this code: the Azure log line
  `Scheduler started — daily digest at 06:00 America/New_York` is from the current repo. ✅

---

## 4. Environment-variable contract

### Read by the running app (set these in Azure → Configuration → Application settings)
| Var | Required | Purpose |
|---|---|---|
| `SENDGRID_API_KEY` | ✅ | SendGrid key |
| `SENDGRID_FROM_EMAIL` | ✅ | Verified sender |
| `DAILY_RECIPIENTS` | one of these | Daily 6am digest list |
| `WEEKLY_RECIPIENTS` | one of these | Tuesday roundup list |
| `DIAGNOSTIC_RECIPIENTS` | optional | Admin diagnostic (defaults to first daily/weekly) |
| `RESTART_RECIPIENTS` | optional | Health email on restart |
| `MANUAL_RECIPIENTS` | optional | Default for `--send-digest` |
| `NOTIFY_TIMEZONE` / `NOTIFY_HOUR` / `WEEKLY_WEEKDAY` | optional | Schedule (defaults: America/New_York / 6 / Tue) |
| `DASHBOARD_USER` / `DASHBOARD_PASS` | should set | Dashboard login (default `admin`/`changeme` ⚠️) |
| `SECRET_KEY` | should set | Flask session key (random if unset → logins drop on restart) |
| `DASHBOARD_URL` | optional | Link shown in emails |
| `WEBSITES_PORT` / `PORT` | platform | Bind port (8080) |
| `APP_ENV` | optional | `dev` shows the red dev banner |
| `MODEL_CACHE_DIR` | optional | Embedding model cache (default `/mnt/data/model_cache`) |
| `DATA_DIR` / `LOG_FILE` / `CONFIG_FILE` | optional | Path overrides |

### ⚠️ Set in Azure but NOT used by the app
- `AZURE_POSTGRESQL_CONNECTIONSTRING` — injected by Service Connector; code never reads it.
- `ALERT_RECIPIENTS` (if still present) — **retired**; replaced by DAILY/WEEKLY. Delete it.

### Stale references in non-running helper scripts (not used in production)
- `scraper_diagnostic.py` still uses **`GMAIL_SENDER` / `GMAIL_APP_PASSWORD`** (old SMTP path)
  and `ALERT_RECIPIENTS`. It's a standalone manual diagnostic, not imported by the app.
- `app/grant_matcher_diagnostics.py` uses `MATCHER_REPORT_DIR` / `MATCHER_LOG_DIR` /
  `MATCHER_DB_PATH` — also a standalone CLI tool, not wired into the running app.

---

## 5. GitHub repository & CI

### Branches
- `dev` — working branch; pushes build the `:dev` image. ✅
- `azure` — production branch (default/HEAD); pushes build the `:latest` image. ✅
- `main` — ⚠️ **stale/divergent**: only commit not on azure is "Add files via upload"; it is
  missing all recent work. **But `main` is still in the deploy workflow trigger list**, so a
  push to `main` would build a `:latest` image from stale code. Risk — see §7.

### Workflows (`.github/workflows/`)
- **`azure-deploy.yml`** ("Build and Deploy to Azure Web App") — active. Triggers on push to
  `main`, `azure`, `dev`. Builds the Docker image and **pushes to GHCR**
  (`ghcr.io/somis-ai/som-grant-matcher:dev` or `:latest`). ✅
  - ⚠️ **Despite its name, it does NOT deploy to Azure** — there is no deploy step. It only
    publishes the image. Getting it onto a Web App is a separate pull (manual restart or
    Azure continuous-deployment).
- `azure-container-webapp.yml` — **deleted** (was failing: missing `packages:write` + blocked
  publish-profile deploy). ✅
- "Dependency Graph" — GitHub default, harmless.

### Secrets & variables (names only — values not readable)
| Name | Used by current workflow? | Notes |
|---|---|---|
| `AZURE_WEBAPP_PUBLISH_PROFILE` | ❌ no | Leftover from the deleted deploy workflow |
| `AZURE_WEBAPP_PUBLISH_PROFILE_DEV` | ❌ no | Leftover |
| `AZURE_WEBHOOK_URL` | ❌ no | Leftover from the removed webhook-trigger step |
| `AZURE_WEBHOOK_URL_DEV` | ❌ no | Leftover |
| `AZURE_WEBAPP_NAME` = `SOMGrantMatcher` | ❌ no | Confirms the **prod** Web App name |
| `AZURE_WEBAPP_NAME_DEV` = `somgrantmatcher-dev` | ❌ no | Confirms the **dev** Web App name |

➡️ **Confirms two Web Apps exist.** None of these secrets/variables are referenced by the
current `azure-deploy.yml`; they're inert leftovers from earlier deploy mechanisms.

---

## 6. Azure resources (inventory + how to verify)

| Resource | Status | Evidence / how to verify |
|---|---|---|
| Web App **SOMGrantMatcher** (prod, `:latest`) | 🔶 in use | GitHub var `AZURE_WEBAPP_NAME`; serves prod |
| Web App **somgrantmatcher-dev** (dev, `:dev`) | 🔶 in use | GitHub var `AZURE_WEBAPP_NAME_DEV` |
| PostgreSQL flexible server `somgrantmatcher-server` | 🔶 **orphaned** | Verify: Metrics → "Active Connections" ≈ 0; Storage ≈ empty; `\dt` shows no app tables |
| MySQL flexible server `somgrantmatcher-server` | 🔶 **orphaned** | Same checks; code has no MySQL driver either |
| `AZURE_POSTGRESQL_CONNECTIONSTRING` app setting | ⚠️ unused | Injected by Service Connector; code never reads it |
| Azure Files mount at `/app/data` | ⚠️ **MISSING** | Configuration → Path mappings shows no storage mount |
| Persistent storage account / file share | 🔶 unknown | Needed for the mount; may need creating |

### Decisive checks to confirm the DBs are unused
1. PostgreSQL server → **Monitoring → Metrics → "Active Connections"** over 7 days → expect ≈ 0.
2. PostgreSQL server → **Storage used** → expect near-empty.
3. Connect via Cloud Shell `psql` (or pgAdmin) → `\dt` → expect **no application tables**.

---

## 7. Problems & risks (prioritized)

1. ⚠️ **No `/app/data` persistence** → all data lost on every restart/redeploy. _This is the
   "cleared stats" cause._ (Fix: §8 option A or B.)
2. ⚠️ **`FORCE_SCRAPE` is currently a no-op** — regression introduced when `run.py` was moved to
   the scheduler; `run_scheduler` never reads it. (Easy fix, pending.)
3. ⚠️ **`main` branch is stale but still a deploy trigger** — a push to `main` would build a
   `:latest` image from outdated code and could overwrite prod. (Fix: remove `main` from the
   workflow trigger, or delete/sync `main`.)
4. ⚠️ **Default dashboard credentials** (`admin`/`changeme`) and **random `SECRET_KEY`** if unset
   — set `DASHBOARD_USER`/`DASHBOARD_PASS`/`SECRET_KEY` in both Web Apps.
5. ⚠️ **Two Web Apps must use separate file shares / data** — never share one `data/` between
   prod and dev, or `seen_grants`/match history will collide.
6. 🔶 **Paying for two unused DB servers** — flexible servers aren't free; ~$15–30+/mo each.
7. Cosmetic: leftover GitHub secrets/variables; stale `GMAIL_*`/`ALERT_RECIPIENTS` refs in
   standalone helper scripts; "deploy" workflow name is misleading (build-only).

---

## 8. Recommendations & the persistence decision

### Option A — Keep the filesystem model (fast)
1. Create a Storage Account + File Share; mount it at **`/app/data`** on **each** Web App
   (separate shares for prod vs dev). _Resolves "cleared stats."_
2. Delete/stop the **two orphaned DB servers** + remove `AZURE_POSTGRESQL_CONNECTIONSTRING`
   (after confirming unused per §6). Stops the cost.
3. Apply the `FORCE_SCRAPE` fix + a `--refresh` command (pending) to repopulate on demand.

_Pros:_ matches the code as written; minimal change. _Cons:_ App Service file mounts can be
slow/finicky; SQLite on a network share is not ideal for concurrent access.

### Option B — Migrate to PostgreSQL (better long-term)
Use the managed Postgres you already pay for. Replace the JSON/SQLite layer with PG tables
(faculty, seen_grants, matches, run_stats). _Pros:_ durable, no fragile mounts, concurrency-safe,
uses paid resource. _Cons:_ real engineering effort (driver, data-access layer, migration);
adds a dependency.

> Recommendation: if the DBs are confirmed orphaned and you want the simplest reliable fix now,
> do **A** and delete the DBs. If you want the architecturally correct long-term setup and are
> willing to invest, do **B** and keep one Postgres server (delete the MySQL one regardless).

### Cleanup checklist (independent of A/B)
- [ ] Fix `FORCE_SCRAPE` no-op; add `--refresh` (no-email repopulate).
- [ ] Remove `main` from the deploy trigger (or delete/sync `main`).
- [ ] Delete `ALERT_RECIPIENTS` app setting; set `DAILY_/WEEKLY_RECIPIENTS`.
- [ ] Set `DASHBOARD_USER`/`DASHBOARD_PASS`/`SECRET_KEY` on both Web Apps.
- [ ] Prune leftover GitHub secrets/variables (publish profiles, webhook URLs) if not reused.
- [ ] Decide whether the **dev** Web App is still needed; if not, decommission it + its image build.

---

## 9. Open questions for you to confirm in Azure
1. Do the two DB servers show ~0 connections / near-empty storage (i.e., orphaned)? (§6)
2. Are both Web Apps (`SOMGrantMatcher`, `somgrantmatcher-dev`) actually in use, or is dev retired?
3. How does each Web App currently get a new image — manual restart, or "Continuous deployment"
   enabled against the GHCR tag? (Determines whether pushes auto-deploy.)
4. Persistence direction: **Option A (filesystem mount)** or **Option B (migrate to Postgres)**?
