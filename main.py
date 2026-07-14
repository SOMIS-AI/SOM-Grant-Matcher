#!/usr/bin/env python3
"""
UMSOM Grant Matcher — Main Application
=======================================
Polls Grants.gov for new opportunities, matches them to UMSOM faculty
research keywords, and sends email alerts.

Usage:
  python main.py                 # Start the daily scheduler (no email on startup except restart notice)
  python main.py --run-once      # Run the pipeline once and send immediately (testing)
  python main.py --scrape        # Force re-scrape of faculty profiles
  python main.py --test-email    # Send a synthetic test email and exit
  python main.py --send-digest [--days N] [--to a@b.com,c@d.com]
                                 # Manually send a digest of matches from the last N days (default 1)
  python main.py --refresh [--scrape]
                                 # Populate data/dashboard WITHOUT emailing (warm a fresh mount)
"""

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# Add src/ to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from faculty_scraper import get_faculty_profiles
from grants_poller import fetch_new_grants, fetch_all_sources
from matcher import find_matches, get_last_diagnostic, load_recent_matched_results
from emailer import (
    send_email,
    send_diagnostic_email,
    send_restart_email,
    get_daily_recipients,
    get_weekly_recipients,
    get_restart_recipients,
    get_manual_recipients,
    build_faculty_email,
    build_dept_admin_email,
    send_personal_email,
)
import subscriptions


# ── Config ──────────────────────────────────────────────────────────────────

def load_config(config_path: str = "config/config.yaml") -> dict:
    """
    Load config from YAML, then override sensitive values from environment
    variables if present. This allows safe deployment to Railway (or any
    cloud) without storing secrets in the GitHub repo.

    Environment variables (set in Azure Application Settings):
      SENDGRID_API_KEY    - SendGrid API key
      SENDGRID_FROM_EMAIL - Verified sender email address

    Recipients are resolved at send time from DAILY_RECIPIENTS /
    WEEKLY_RECIPIENTS / DIAGNOSTIC_RECIPIENTS (see emailer.py and
    run_scheduler below). The legacy ALERT_RECIPIENTS variable is retired.
    """
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Override email settings from environment variables if set
    if os.environ.get("SENDGRID_FROM_EMAIL"):
        config["email"]["sender"] = os.environ["SENDGRID_FROM_EMAIL"]

    if os.environ.get("SENDGRID_API_KEY"):
        config["email"]["sendgrid_api_key"] = os.environ["SENDGRID_API_KEY"]

    return config


# ── Logging ──────────────────────────────────────────────────────────────────

def setup_logging(config: dict):
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    log_file = log_config.get("log_file", "logs/grant_matcher.log")
    max_bytes = log_config.get("max_log_size_mb", 10) * 1024 * 1024
    backup_count = log_config.get("backup_count", 3)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


# ── Core Run Cycle ────────────────────────────────────────────────────────────

def run_pipeline(config: dict, force_scrape: bool = False):
    """
    Execute the data pipeline: scrape (if needed) → fetch grants → match.
    Does NOT send any email — the caller decides what to send and to whom.

    Returns:
      (matched_results, matcher_diag, scraper_health) on a completed run.
        matched_results is [] when no new grants or no matches were found.
      None when the run was aborted early (faculty unavailable or grant fetch
        failed) — in that case the caller should send nothing at all.
    """
    logger = logging.getLogger("main")
    logger.info("=" * 60)
    logger.info("Starting grant matching pipeline")

    if force_scrape:
        # Force a fresh scrape by invalidating the cache
        cache_file = Path(config["faculty"]["cache_file"])
        if cache_file.exists():
            cache_file.unlink()
            logger.info("Faculty cache cleared — forcing fresh scrape")

    # Step 1: Load faculty profiles (from cache or fresh scrape)
    logger.info("Step 1/3 — Loading faculty profiles...")
    try:
        faculty = get_faculty_profiles(config)
    except Exception as e:
        logger.error(f"Faculty scraping failed: {e}", exc_info=True)
        return None

    if not faculty:
        logger.warning("No faculty profiles loaded. Skipping run.")
        return None
    logger.info(f"  ✓ {len(faculty)} faculty profiles loaded")

    # Check if faculty embeddings need regeneration (e.g. after embedder update)
    try:
        from embedder import embed_faculty_batch, EMBEDDING_VERSION, is_available
        if is_available():
            stale = sum(1 for f in faculty if f.get("embedding_version") != EMBEDDING_VERSION)
            if stale > 0:
                logger.info(
                    f"  {stale}/{len(faculty)} faculty have stale embeddings "
                    f"(need v{EMBEDDING_VERSION}). Regenerating..."
                )
                embed_faculty_batch(faculty)
                # Save updated embeddings back to cache so we don't redo this every run.
                # The cache file is expected to be {"scraped_at": iso, "faculty": [...]}.
                # We MUST preserve the existing scraped_at (otherwise the rescrape-age
                # check resets to "very old" and forces a 3h scrape on the next run)
                # AND any faculty marked inactive (departed) that aren't in this active
                # subset. Earlier versions of this block wrote `faculty` (a bare list of
                # active+title-non-excluded only) directly to the cache, which clobbered
                # both pieces of state. Merge the new embeddings into the existing cache
                # structure instead.
                try:
                    cache_file = config["faculty"]["cache_file"]
                    cache_path = Path(cache_file)
                    cache_path.parent.mkdir(parents=True, exist_ok=True)

                    # Read the existing cache to preserve scraped_at + inactive faculty.
                    existing = None
                    if cache_path.exists():
                        try:
                            with open(cache_path) as f:
                                loaded = json.load(f)
                            if isinstance(loaded, dict) and "faculty" in loaded:
                                existing = loaded
                        except (OSError, json.JSONDecodeError) as e:
                            logger.warning(f"  Could not read existing cache before embedding-save: {e}")

                    if existing is None:
                        # No usable existing cache — skip the save to avoid corrupting things.
                        # The next pipeline run will re-scrape and rebuild cleanly.
                        logger.warning(
                            "  Existing cache missing or malformed — skipping embedding-save "
                            "to avoid wiping scraped_at / inactive faculty. Next run will re-scrape."
                        )
                    else:
                        # Index existing cache faculty by (name, email) so we can find
                        # the same person across active and inactive subsets.
                        def _key(f): return (f.get("name", "").lower().strip(),
                                             f.get("email", "").lower().strip())
                        by_key = {_key(f): f for f in existing["faculty"]}
                        updated = 0
                        for active_f in faculty:
                            slot = by_key.get(_key(active_f))
                            if slot is not None:
                                # Copy the regenerated embedding fields back; leave everything
                                # else (department, keywords, enrichment metadata) untouched.
                                slot["embedding"]         = active_f.get("embedding")
                                slot["embedding_version"] = active_f.get("embedding_version")
                                updated += 1
                        from atomic_io import atomic_write_json
                        atomic_write_json(cache_path, existing)
                        logger.info(
                            f"  ✓ Updated embeddings saved to cache "
                            f"({updated}/{len(faculty)} faculty merged; "
                            f"preserved scraped_at={existing.get('scraped_at','?')[:19]} "
                            f"and {len(existing['faculty']) - updated} non-active records)"
                        )
                except Exception as e:
                    logger.warning(f"  Could not save updated embeddings to cache: {e}")
    except ImportError:
        pass  # embedder not installed — semantic matching will be disabled

    # Step 2: Fetch new grants from ALL sources
    logger.info("Step 2/3 — Fetching new grants from all sources...")
    scraper_health = {}
    try:
        new_grants = fetch_all_sources(config)
        # Collect scraper health data for diagnostic email
        try:
            from foundation_scraper import get_last_scraper_health
            scraper_health = get_last_scraper_health()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Multi-source grant fetch failed: {e}", exc_info=True)
        return None

    # Surface Grants.gov fetch health in the diagnostic. Grants.gov is the primary
    # source but had no health visibility, so a silent fetch failure looked
    # identical to a quiet weekend (grants_checked=0). Recorded on every fetch
    # path (ok / request_failed / json_decode_error / api_errorcode_N).
    try:
        from matcher import get_last_grants_fetch_stats
        if isinstance(scraper_health, dict):
            scraper_health["grants_gov"] = get_last_grants_fetch_stats()
    except Exception:
        pass

    if not new_grants:
        logger.info("  ✓ No new grants found this run.")
        empty_diag = {
            "summary": {"faculty_count": len(faculty), "grants_checked": 0,
                        "grants_matched": 0, "raw_matches": 0,
                        "matches_after_filter": 0, "note": "No new grants this run"},
            "params": {}, "stop_words_suppressed": [], "per_grant": [],
            "semantic_score_distributions": [], "confidence_histograms": [],
            "grants_capped": [], "idf_filtered_keywords": [],
        }
        return [], empty_diag, scraper_health
    logger.info(f"  ✓ {len(new_grants)} new grants retrieved from all sources")

    # Step 3: Match grants to faculty keywords
    logger.info("Step 3/3 — Matching grants to faculty keywords...")
    matched_results = find_matches(new_grants, faculty, config=config)
    logger.info(f"  ✓ {len(matched_results)} grants with faculty matches")

    return matched_results, get_last_diagnostic(), scraper_health


def run_cycle(config: dict, force_scrape: bool = False):
    """
    Run the pipeline once and send immediately. Used by --run-once and local
    testing. The match digest goes to DAILY_RECIPIENTS; the diagnostic report
    goes to the admin (DIAGNOSTIC_RECIPIENTS). Production scheduling lives in
    run_scheduler().
    """
    logger = logging.getLogger("main")
    result = run_pipeline(config, force_scrape=force_scrape)
    if result is None:
        return
    matched_results, matcher_diag, scraper_health = result

    if matched_results:
        logger.info(f"Sending match digest for {len(matched_results)} matched grant(s)...")
        try:
            send_email(config, matched_results, recipients=get_daily_recipients())
            logger.info("  ✓ Match digest sent")
        except Exception as e:
            logger.error(f"Email sending failed: {e}", exc_info=True)
    else:
        logger.info("No matches this run — no match email sent.")

    logger.info("Sending diagnostic email...")
    try:
        send_diagnostic_email(config, matcher_diag, scraper_health)
        logger.info("  ✓ Diagnostic email sent")
    except Exception as e:
        logger.error(f"Diagnostic email failed: {e}", exc_info=True)

    logger.info("Cycle complete.")


def run_test_email(config: dict):
    """Send a test email with synthetic data to verify email setup."""
    logger = logging.getLogger("main")
    logger.info("Sending test email...")

    # Send the test to the daily list (falls back to the weekly list, then the
    # config placeholder) so a single configured group is enough to verify mail.
    recipients = get_daily_recipients() or get_weekly_recipients()

    test_results = [{
        "grant": {
            "id": "TEST-001",
            "title": "Test Grant: Advanced Research in Molecular Biology",
            "agency": "National Institutes of Health (NIH)",
            "number": "PAR-25-TEST",
            "synopsis": "This is a test grant opportunity generated by the UMSOM Grant Matcher "
                        "to verify your email configuration is working correctly.",
            "close_date": "2025-12-31",
            "open_date": "2025-01-01",
            "award_ceiling": "500000",
            "link": "https://www.grants.gov",
            "searchable_text": "test grant molecular biology"
        },
        "matches": [
            type("Match", (), {
                "faculty_name": "Dr. Jane Smith (TEST)",
                "faculty_url": "https://www.medschool.umaryland.edu/faculty/",
                "faculty_department": "Department of Biochemistry and Molecular Biology",
                "faculty_email": recipients[0] if recipients else "",
                "matched_keywords": ["molecular biology", "biochemistry", "genetics", "proteomics"],
                "match_score": 4
            })()
        ]
    }]

    send_email(config, test_results, recipients=recipients or None)
    logger.info("Test email sent! Check your inbox.")


def run_manual_digest(config: dict, recipients: list = None, days: int = 1):
    """
    Manually send a digest of grants/matches from the last `days` days to an
    ad-hoc recipient list. Reads already-stored match results (does NOT run the
    pipeline or touch seen_grants), so it's safe to trigger any time without
    affecting the scheduled emails.

    Recipients precedence: explicit `recipients` → MANUAL_RECIPIENTS → DAILY_RECIPIENTS.
    """
    logger = logging.getLogger("main")
    if recipients is None:
        recipients = get_manual_recipients() or get_daily_recipients()

    if not recipients:
        logger.warning(
            "No recipients for manual digest — set MANUAL_RECIPIENTS or pass --to. Aborting."
        )
        return

    results = load_recent_matched_results(days)
    if not results:
        logger.info(f"Manual digest: no matches found in the last {days} day(s) — nothing sent.")
        return

    total_matches = sum(len(r["matches"]) for r in results)
    logger.info(
        f"Manual digest: sending {len(results)} grant(s) / {total_matches} match(es) "
        f"from the last {days} day(s) to {len(recipients)} recipient(s)..."
    )
    try:
        send_email(config, results, recipients=recipients)
        logger.info("  ✓ Manual digest sent.")
    except Exception as e:
        logger.error(f"Manual digest failed: {e}", exc_info=True)


def run_refresh(config: dict, force_scrape: bool = False):
    """
    Run the pipeline once to (re)populate the data files and dashboard WITHOUT
    sending any email. Useful to warm a fresh /app/data mount or refresh the
    dashboard on demand. Add force_scrape=True for a full faculty re-scrape.

    Note: the pipeline marks fetched grants as "seen", so grants consumed by a
    refresh won't reappear in the next scheduled digest. Intended for one-off
    recovery/warm-up, not routine use.
    """
    logger = logging.getLogger("main")
    logger.info("Manual refresh: running pipeline to populate data (no email will be sent)...")
    result = run_pipeline(config, force_scrape=force_scrape)
    if result is None:
        logger.warning("Refresh: pipeline aborted (no faculty loaded or grant fetch failed).")
        return
    matched_results, _diag, _health = result
    n_matches = sum(len(r["matches"]) for r in matched_results)
    logger.info(
        f"Refresh complete: data written for {len(matched_results)} grant(s) / "
        f"{n_matches} match(es). No email sent."
    )


# ── Scheduler ─────────────────────────────────────────────────────────────────

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                  "Friday", "Saturday", "Sunday"]


def _scheduler_settings():
    """
    Read scheduling settings from the environment (Azure Application Settings):
      NOTIFY_TIMEZONE  - IANA tz name for the fire time (default America/New_York)
      NOTIFY_HOUR      - hour of day to fire, 0-23 (default 6)
      WEEKLY_WEEKDAY   - weekday for the weekly roundup, Mon=0 .. Sun=6 (default 1 = Tuesday)
    Returns (tzinfo, hour:int, weekly_weekday:int).
    """
    logger = logging.getLogger("main")

    tz_name = os.environ.get("NOTIFY_TIMEZONE", "America/New_York")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning(f"Unknown NOTIFY_TIMEZONE '{tz_name}' — falling back to America/New_York")
        tz = ZoneInfo("America/New_York")

    try:
        hour = int(os.environ.get("NOTIFY_HOUR", "6"))
        if not 0 <= hour <= 23:
            raise ValueError
    except ValueError:
        logger.warning("Invalid NOTIFY_HOUR — defaulting to 6")
        hour = 6

    try:
        weekly_weekday = int(os.environ.get("WEEKLY_WEEKDAY", "1"))
        if not 0 <= weekly_weekday <= 6:
            raise ValueError
    except ValueError:
        logger.warning("Invalid WEEKLY_WEEKDAY — defaulting to 1 (Tuesday)")
        weekly_weekday = 1

    return tz, hour, weekly_weekday


def _next_fire(now: datetime, hour: int) -> datetime:
    """Next datetime at HH:00:00 (tz-aware, same tz as `now`) strictly after now."""
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _build_id() -> str:
    """Best-effort build/commit identifier from common deployment env vars."""
    for var in ("GIT_COMMIT", "SCM_COMMIT_ID", "COMMIT_SHA", "BUILD_ID",
                "WEBSITE_DEPLOYMENT_ID"):
        v = os.environ.get(var)
        if v:
            return v[:12]
    return "unknown"


def _send_personalized_digests(config: dict, matched_results: list,
                               run_date: str, cadence: str = "daily",
                               digest_label: str = "Daily") -> dict:
    """Fan out personalized per-faculty and per-dept-admin emails from one
    run's matched_results.

    Algorithm:
      1. Index matches by faculty email and by normalized department.
      2. For each enrolled faculty (`cadence` cadence) WITH matches today,
         build & send their personal digest. Skip if zero matches.
      3. For each dept that has at least one match, for each `cadence`
         dept admin in that dept, build & send the dept digest with ONLY
         matches from faculty in that dept.
      4. Every send is logged to subscriptions.email_log (audit + budget).
      5. The SendGrid free-tier daily cap is enforced before each send via
         remaining_budget_today(); when exhausted, we stop and log a warning.

    `cadence`      — which subscription bucket to read ("daily" or "weekly").
    `digest_label` — label rendered in the email subject + header
                     ("Daily" or "Weekly"); almost always matches cadence, but
                     kept separate so a manual one-off can label however it wants.

    Returns a stats dict for logging by the scheduler.
    """
    stats = {"faculty_sent": 0, "faculty_skipped_no_match": 0,
             "dept_admin_sent": 0, "depts_with_matches": 0,
             "budget_exhausted": False}

    if not matched_results:
        return stats

    dashboard_url = os.environ.get("DASHBOARD_URL", "")

    # ── 1. Index matches by faculty.email and by normalized department ──────
    # Each value is a list of {grant, matches:[Match]} where the match list
    # is filtered to just the relevant faculty (faculty bucket) or department
    # (dept bucket). This avoids leaking other faculty's data into a person's
    # email and other depts' data into a dept admin's email.
    by_faculty: dict[str, list] = {}
    by_dept:    dict[str, list] = {}

    for r in matched_results:
        grant = r.get("grant", r) if isinstance(r, dict) else r
        ms = r.get("matches", []) if isinstance(r, dict) else []
        # Per-faculty filtering
        per_fac: dict[str, list] = {}
        per_dept: dict[str, list] = {}
        for m in ms:
            email = (getattr(m, "faculty_email", "") or "").strip().lower()
            dept  = getattr(m, "faculty_department", "") or ""
            dept_key = subscriptions.normalize_dept_name(dept)
            if email:
                per_fac.setdefault(email, []).append(m)
            if dept_key:
                per_dept.setdefault(dept_key, []).append(m)
        for email, ms_for_fac in per_fac.items():
            by_faculty.setdefault(email, []).append({"grant": grant, "matches": ms_for_fac})
        for dept_key, ms_for_dept in per_dept.items():
            by_dept.setdefault(dept_key, []).append({"grant": grant, "matches": ms_for_dept})

    # ── 2. Per-faculty personal digests ─────────────────────────────────────
    enrolled = subscriptions.faculty_subs_for_cadence(cadence)
    for email, sub in enrolled.items():
        bucket = by_faculty.get(email)
        if not bucket:
            stats["faculty_skipped_no_match"] += 1
            continue
        if subscriptions.remaining_budget_today() <= 0:
            stats["budget_exhausted"] = True
            logger = logging.getLogger("main")
            logger.warning("SendGrid daily cap reached — skipping remaining faculty digests.")
            break
        name = sub.get("name", "")
        subject, html = build_faculty_email(name, bucket, run_date, dashboard_url,
                                            digest_label=digest_label)
        if send_personal_email(config, email, subject, html):
            subscriptions.log_email(
                kind="faculty", to=email, subject=subject,
                matches_count=len(bucket), faculty_name=name,
                department=sub.get("department", ""),
            )
            stats["faculty_sent"] += 1

    # ── 3. Per-department admin digests ─────────────────────────────────────
    stats["depts_with_matches"] = len(by_dept)
    for dept_key, dept_bucket in by_dept.items():
        # Pick a human-readable dept label from the first match
        dept_label = ""
        for r in dept_bucket:
            for m in r["matches"]:
                dl = getattr(m, "faculty_department", "")
                if dl:
                    dept_label = dl
                    break
            if dept_label:
                break
        dept_label = dept_label or dept_key.title()

        admins = subscriptions.admins_for_department(dept_label, cadence=cadence)
        for admin in admins:
            if subscriptions.remaining_budget_today() <= 0:
                stats["budget_exhausted"] = True
                logger = logging.getLogger("main")
                logger.warning("SendGrid daily cap reached — skipping remaining dept-admin digests.")
                return stats
            subject, html = build_dept_admin_email(dept_label, dept_bucket, run_date,
                                                   dashboard_url, digest_label=digest_label)
            if send_personal_email(config, admin["email"], subject, html):
                subscriptions.log_email(
                    kind="dept_admin", to=admin["email"], subject=subject,
                    matches_count=len(dept_bucket),
                    faculty_name=admin.get("name", ""),
                    department=dept_label,
                )
                stats["dept_admin_sent"] += 1
    return stats


def run_scheduler(config: dict):
    """
    Continuous scheduler for cloud (Azure Web App) deployment.

    Fires once per day at NOTIFY_HOUR (default 06:00) in NOTIFY_TIMEZONE
    (default America/New_York). On each fire it runs the pipeline once, then:
      • sends the daily digest to DAILY_RECIPIENTS              (every day)
      • sends a 7-day roundup to WEEKLY_RECIPIENTS              (only on WEEKLY_WEEKDAY, default Tuesday)
      • sends the diagnostic report to the admin                (every run)

    No email is ever sent on startup or restart: the loop only sleeps until the
    next *future* fire time and never "catches up" a missed slot. If the app is
    down at the fire time, that day is skipped — unseen grants simply surface on
    the next successful run.
    """
    logger = logging.getLogger("main")
    tz, hour, weekly_weekday = _scheduler_settings()
    logger.info(
        f"Scheduler started — daily digest at {hour:02d}:00 {getattr(tz, 'key', tz)}; "
        f"weekly roundup on {_WEEKDAY_NAMES[weekly_weekday]}. No grant email on startup/restart."
    )

    # ── Restart/health notification ──────────────────────────────────────────
    # The ONLY email sent on startup. Lets admins confirm a deploy is live.
    # The grant pipeline does not run here.
    try:
        restart_recipients = get_restart_recipients()
        if restart_recipients:
            info_rows = [
                ("Status",             "Started / restarted"),
                ("Started at (UTC)",   datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
                ("Environment",        os.environ.get("APP_ENV", "production")),
                ("Build / commit",     _build_id()),
                ("Next scheduled run", _next_fire(datetime.now(tz), hour).isoformat()),
                ("Daily recipients",   str(len(get_daily_recipients()))),
                ("Weekly recipients",  str(len(get_weekly_recipients()))),
            ]
            send_restart_email(config, info_rows, recipients=restart_recipients)
            logger.info(f"  ✓ Restart notification sent to {len(restart_recipients)} recipient(s)")
        else:
            logger.info("No RESTART_RECIPIENTS configured — no restart notification.")
    except Exception as e:
        logger.error(f"Restart notification failed: {e}", exc_info=True)

    # FORCE_SCRAPE=true forces a fresh faculty re-scrape on the FIRST scheduled run
    # (e.g. after a deploy that changed keyword extraction). Applied once, then cleared.
    force_scrape = os.environ.get("FORCE_SCRAPE", "").lower() in ("1", "true", "yes")
    if force_scrape:
        logger.info("FORCE_SCRAPE is set — the first scheduled run will re-scrape faculty.")

    while True:
        now = datetime.now(tz)
        fire_at = _next_fire(now, hour)
        logger.info(
            f"Next scheduled run: {fire_at.isoformat()} "
            f"({(fire_at - now).total_seconds() / 3600:.1f}h away)"
        )

        # Sleep in capped chunks so DST changes / clock drift are re-evaluated
        # and a long sleep can't drastically overshoot the fire time.
        while True:
            now = datetime.now(tz)
            remaining = (fire_at - now).total_seconds()
            if remaining <= 0:
                break
            try:
                time.sleep(min(remaining, 900))  # re-check at least every 15 min
            except KeyboardInterrupt:
                logger.info("Shutting down (KeyboardInterrupt)")
                return

        fire_time = datetime.now(tz)
        logger.info("=" * 60)
        logger.info(f"Scheduled run firing at {fire_time.isoformat()}")

        try:
            result = run_pipeline(config, force_scrape=force_scrape)
        except Exception as e:
            logger.error(f"Pipeline error during scheduled run: {e}", exc_info=True)
            continue

        if result is None:
            logger.warning("Pipeline aborted (no faculty or fetch failed) — no emails sent this run.")
            continue
        force_scrape = False  # only force on the first successful run
        matched_results, matcher_diag, scraper_health = result

        # ── Daily digest (every day) ─────────────────────────────────────────
        daily_recipients = get_daily_recipients()
        if not daily_recipients:
            logger.info("  Daily digest: no DAILY_RECIPIENTS configured — not sent.")
        elif not matched_results:
            logger.info("  Daily digest: no matches this run — not sent.")
        else:
            try:
                send_email(config, matched_results, recipients=daily_recipients)
                logger.info(f"  ✓ Daily digest sent to {len(daily_recipients)} recipient(s)")
            except Exception as e:
                logger.error(f"Daily digest failed: {e}", exc_info=True)

        # ── Per-faculty + per-dept personalized digests (daily cadence) ─────
        # Opt-in via the dashboard Subscriptions tab. Skipped silently if no
        # one is enrolled. SendGrid free-tier cap enforced inside the helper.
        if matched_results:
            try:
                run_date = fire_time.strftime("%Y-%m-%d")
                stats = _send_personalized_digests(config, matched_results,
                                                   run_date, cadence="daily")
                logger.info(
                    f"  ✓ Personalized digests — faculty sent: {stats['faculty_sent']}, "
                    f"dept admins sent: {stats['dept_admin_sent']} "
                    f"(across {stats['depts_with_matches']} dept(s))"
                    + ("  ⚠ budget exhausted" if stats["budget_exhausted"] else "")
                )
            except Exception as e:
                logger.error(f"Personalized digest fan-out failed: {e}", exc_info=True)

        # ── Weekly 7-day roundup (only on the configured weekday) ────────────
        weekly_roundup_cache = None  # share the 7-day fetch with the personalized fan-out below
        if fire_time.weekday() == weekly_weekday:
            weekly_recipients = get_weekly_recipients()
            if not weekly_recipients:
                logger.info("  Weekly roundup: no WEEKLY_RECIPIENTS configured — not sent.")
            else:
                weekly_roundup_cache = load_recent_matched_results(7)
                if not weekly_roundup_cache:
                    logger.info("  Weekly roundup: no matches in the last 7 days — not sent.")
                else:
                    try:
                        send_email(config, weekly_roundup_cache, recipients=weekly_recipients, digest_label="Weekly")
                        logger.info(f"  ✓ Weekly roundup sent to {len(weekly_recipients)} recipient(s)")
                    except Exception as e:
                        logger.error(f"Weekly roundup failed: {e}", exc_info=True)

            # ── Personalized weekly digests (mirror of the daily fan-out) ────
            # Same opt-in mechanism, same budget cap, but reads the 7-day
            # roundup and pulls only weekly-cadence enrollees. Always attempts
            # the fetch even if WEEKLY_RECIPIENTS is empty, so faculty/dept-admin
            # weekly subs work independently of the admin weekly setting.
            try:
                if weekly_roundup_cache is None:
                    weekly_roundup_cache = load_recent_matched_results(7)
                if weekly_roundup_cache:
                    run_date_w = fire_time.strftime("%Y-%m-%d")
                    stats_w = _send_personalized_digests(
                        config, weekly_roundup_cache, run_date_w,
                        cadence="weekly", digest_label="Weekly",
                    )
                    logger.info(
                        f"  ✓ Personalized weekly digests — faculty sent: {stats_w['faculty_sent']}, "
                        f"dept admins sent: {stats_w['dept_admin_sent']} "
                        f"(across {stats_w['depts_with_matches']} dept(s))"
                        + ("  ⚠ budget exhausted" if stats_w["budget_exhausted"] else "")
                    )
                else:
                    logger.info("  Personalized weekly digests: no matches in the last 7 days — none sent.")
            except Exception as e:
                logger.error(f"Personalized weekly fan-out failed: {e}", exc_info=True)

        # ── Diagnostic (admin only, every run) ───────────────────────────────
        try:
            send_diagnostic_email(config, matcher_diag, scraper_health)
            logger.info("  ✓ Diagnostic email sent")
        except Exception as e:
            logger.error(f"Diagnostic email failed: {e}", exc_info=True)

        logger.info("Scheduled run complete.")

def main():
    parser = argparse.ArgumentParser(description="UMSOM Grant Matcher")
    parser.add_argument("--run-once", action="store_true",
                        help="Run one cycle and exit (for testing/cron)")
    parser.add_argument("--scrape", action="store_true",
                        help="Force re-scrape of faculty profiles")
    parser.add_argument("--test-email", action="store_true",
                        help="Send a test email to verify configuration")
    parser.add_argument("--refresh", action="store_true",
                        help="Run the pipeline once to populate data/dashboard WITHOUT sending email, then exit "
                             "(use with --scrape to force a full faculty re-scrape)")
    parser.add_argument("--send-digest", action="store_true",
                        help="Manually send a digest of matches from the last N days, then exit")
    parser.add_argument("--days", type=int, default=1,
                        help="Window in days for --send-digest (default 1 = last 24h)")
    parser.add_argument("--to", default="",
                        help="Comma-separated recipient override for --send-digest")
    parser.add_argument("--config", default="config/config.yaml",
                        help="Path to config file")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)
    logger = logging.getLogger("main")

    logger.info("UMSOM Grant Matcher starting up")
    logger.info(f"Faculty re-scrape interval: {config['faculty']['rescrape_interval_hours']}h")
    logger.info(f"Daily recipients:  {len(get_daily_recipients())} configured")
    logger.info(f"Weekly recipients: {len(get_weekly_recipients())} configured")

    if args.test_email:
        run_test_email(config)
        return

    if args.refresh:
        run_refresh(config, force_scrape=args.scrape)
        return

    if args.send_digest:
        to = [r.strip() for r in args.to.split(",") if r.strip()] or None
        run_manual_digest(config, recipients=to, days=args.days)
        return

    if args.run_once:
        run_cycle(config, force_scrape=args.scrape)
        return

    # Scheduled loop for server/cloud deployment (no email on startup/restart).
    run_scheduler(config)


if __name__ == "__main__":
    main()
