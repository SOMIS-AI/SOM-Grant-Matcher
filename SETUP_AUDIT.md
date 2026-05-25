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
- `main` — **stale/divergent**: only commit not on azure is "Add files via upload"; it is
  missing all recent work. Retained intentionally as an **emergency fallback** (old code).
  As of 2026-05-25 it is **removed from the deploy-workflow auto-trigger** so a push to `main`
  no longer builds an image. A fallback build from `main` can still be done **manually** via
  the workflow's "Run workflow" (workflow_dispatch), selecting the `main` branch.

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
| `AZURE_WEBAPP_NAME` = `SOMGrantMatcher` | ❌ no | The App Service name (production slot) |
| `AZURE_WEBAPP_NAME_DEV` = `somgrantmatcher-dev` | ❌ no | The **dev deployment slot**, not a separate app |

➡️ **CORRECTION (2026-05-25): there is ONE App Service (`SOMGrantMatcher`) with TWO deployment
slots** — `somgrantmatcher` (production) and `somgrantmatcher-dev` (a slot) — not two separate
Web Apps as an earlier draft inferred from these names. None of these secrets/variables are
referenced by the current `azure-deploy.yml`; they're inert leftovers from earlier deploy
mechanisms (and `AZURE_WEBHOOK_URL*` embed publishing credentials — rotate if exposed).

---

## 6. Azure resources (inventory + how to verify)

| Resource | Status | Notes (as of 2026-05-25 review) |
|---|---|---|
| App Service **SOMGrantMatcher** — **production slot** (`somgrantmatcher`, `:latest`) | ✅ in use | The live app; serves dashboard + emails |
| App Service **dev slot** (`somgrantmatcher-dev`, `:dev`) | ⚠️ **not used → decommission** | A deployment *slot* of the same app, not a separate app. User confirmed unused. |
| PostgreSQL flexible server `somgrantmatcher-server` | ⚠️ **orphaned (confirmed)** | Private access; ~6.5 baseline connections = platform, not the app; ~9 GiB ≠ matcher data. Code has no PG driver. |
| MySQL flexible server `somgrantmatcher-server` | 🔶 **orphaned** | No MySQL driver/connection in code either |
| `AZURE_POSTGRESQL_CONNECTIONSTRING` app setting | ⚠️ unused | Injected by Service Connector; code never reads it |
| Azure Files mount at `/app/data` (production slot) | ✅ **ADDED 2026-05-25** | SMB share `grant-matcher-data`; data now persists |
| Storage account + file share | ✅ created | `grant-matcher-data` (prod). `grant-matcher-data-dev` created but unused (dev slot retired). |

### Deployment mechanism (confirmed)
- Production slot → **Deployment Center → Containers**: pulls `ghcr.io/somis-ai/som-grant-matcher:latest`,
  **Continuous deployment = On** with a registry **Webhook URL**.
- ⚠️ In practice the webhook is **not auto-called** (ghcr.io doesn't, and the CI step that called it
  was removed — "Basic Auth blocked by tenant policy"). So new images do **not** auto-deploy;
  the reliable method is to **restart the production slot** to pull the latest image.
- ⚠️ The webhook URL embeds publishing credentials — if it's ever exposed, **reset publishing
  credentials** (Deployment Center → reset, or download a fresh publish profile).

---

## 7. Problems & risks (prioritized)

1. ✅ **RESOLVED (2026-05-25): `/app/data` persistence added** (Option A — SMB Azure Files mount
   on the production slot). Was the "cleared stats" cause; data now survives restarts.
2. ✅ **RESOLVED (2026-05-25): `FORCE_SCRAPE` honored again.** `run_scheduler` now reads it and
   applies a fresh re-scrape on the first scheduled run. Also added `python main.py --refresh`
   to populate the data/dashboard on demand without sending email.
3. ✅ **RESOLVED (2026-05-25): `main` removed from the deploy auto-trigger.** `main` is a stale
   emergency-fallback branch; it no longer auto-builds. A manual fallback build is still
   possible via workflow_dispatch (select `main`). Previously a push to `main` would have built
   a `:latest` image from outdated code and could have overwritten prod.
4. ⚠️ **Default dashboard credentials** (`admin`/`changeme`) and **random `SECRET_KEY`** if unset
   — set `DASHBOARD_USER`/`DASHBOARD_PASS`/`SECRET_KEY` on the production slot.
5. ⚠️ **Unused dev slot** (`somgrantmatcher-dev`) — costs resources and caused the dev/azure
   confusion. Decommission it (user confirmed unused). If kept, it must use a *separate* file
   share from production or `seen_grants`/match history would collide.
6. 🔶 **Paying for two unused DB servers** — flexible servers aren't free; ~$15–30+/mo each.
   Stop first, watch a week, then delete (confirm nothing else uses them).
7. ⚠️ **`AZURE_WEBHOOK_URL*` secrets embed publishing credentials** — rotate publishing creds if
   ever exposed; prune these secrets (unused by current CI).
8. Cosmetic: leftover GitHub publish-profile/webhook secrets; stale `GMAIL_*`/`ALERT_RECIPIENTS`
   refs in standalone helper scripts; "deploy" workflow name is misleading (build-only).

---

## 8. Recommendations & the persistence decision

### Option A — Keep the filesystem model (fast)  ✅ CHOSEN 2026-05-25
1. ✅ Mount Azure Files at **`/app/data`** on the **production slot** (done — share
   `grant-matcher-data`). _Resolved "cleared stats."_
2. Delete/stop the **two orphaned DB servers** + remove `AZURE_POSTGRESQL_CONNECTIONSTRING`
   (after confirming unused per §6). Stops the cost. _(Pending.)_
3. ✅ `FORCE_SCRAPE` fix + `--refresh` command shipped on `dev` (pending merge to `azure`).

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
- [x] Fix `FORCE_SCRAPE` no-op; add `--refresh` (no-email repopulate). (done 2026-05-25)
- [x] Remove `main` from the deploy trigger (done 2026-05-25; kept as manual fallback).
- [ ] Delete `ALERT_RECIPIENTS` app setting; set `DAILY_/WEEKLY_RECIPIENTS`.
- [ ] Set `DASHBOARD_USER`/`DASHBOARD_PASS`/`SECRET_KEY` on the production slot.
- [ ] Prune leftover GitHub secrets/variables (publish profiles, webhook URLs) if not reused.
- [ ] Decommission the unused **dev slot** (`somgrantmatcher-dev`) + optionally stop building `:dev`.
- [ ] Delete the unused `grant-matcher-data-dev` file share.
- [ ] Rotate publishing credentials (the webhook URL with embedded creds was exposed).

---

## 9. Open questions — resolved during the 2026-05-25 Azure review
1. ✅ **DB servers orphaned?** Yes (w.r.t. this app). PostgreSQL is **Private access**
   (VNet-isolated — unreachable from Cloud Shell, and from the app, which isn't VNet-integrated).
   ~6.5 avg connections = Azure platform baseline, not the app. Storage 7.16% of 128 GiB (~9 GiB)
   — not the matcher's data (which is MB-scale); likely WAL/system baseline. Code has no PG/MySQL
   driver. ⚠️ Before *deleting* either server, confirm no *other* workload uses it (can't see
   beyond this repo) — safer to **Stop** and watch a week, then delete.
2. ✅ **Topology:** ONE App Service (`SOMGrantMatcher`) with TWO slots — `somgrantmatcher`
   (production) and `somgrantmatcher-dev` (slot). The dev slot is **not used → decommission**.
3. ✅ **Deployment:** production slot uses **Continuous Deployment** against
   `ghcr.io/...:latest` with a registry webhook — but the webhook isn't auto-called (ghcr.io
   doesn't, and the CI call step was removed due to tenant Basic-Auth policy). **Effective method:
   restart the production slot to pull a new `:latest`.**
4. ✅ **DECIDED: Option A — filesystem mount** (done on production slot). Retire the orphaned DB
   servers separately.

### Remaining open items
- Merge `dev → azure` to ship the `FORCE_SCRAPE` fix, `--refresh`, `main`-trigger removal, and
  this audit; then restart the production slot to deploy.
- Decommission the dev slot; stop/delete the two DB servers; rotate publishing credentials.
