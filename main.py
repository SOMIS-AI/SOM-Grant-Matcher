#!/usr/bin/env python3
"""
UMSOM Grant Matcher — Main Application
=======================================
Polls Grants.gov for new opportunities, matches them to UMSOM faculty
research keywords, and sends email alerts.

Usage:
  python main.py               # Run once immediately, then on schedule
  python main.py --run-once    # Run once and exit (useful for testing)
  python main.py --scrape      # Force re-scrape of faculty profiles
  python main.py --test-email  # Send a test email and exit
"""

import argparse
import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path

import yaml

# Add src/ to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from faculty_scraper import get_faculty_profiles
from grants_poller import fetch_new_grants, fetch_all_sources
from matcher import find_matches, get_last_diagnostic
from emailer import send_email, send_diagnostic_email


# ── Config ──────────────────────────────────────────────────────────────────

def load_config(config_path: str = "config/config.yaml") -> dict:
    """
    Load config from YAML, then override sensitive values from environment
    variables if present. This allows safe deployment to Railway (or any
    cloud) without storing secrets in the GitHub repo.

    Environment variables (set in Railway dashboard under Variables):
      GMAIL_SENDER        - Gmail address to send from
      GMAIL_APP_PASSWORD  - Gmail App Password
      ALERT_RECIPIENTS    - Comma-separated list of recipient emails
    """
    import os

    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Override email secrets from environment variables if set
    if os.environ.get("GMAIL_SENDER"):
        config["email"]["sender"] = os.environ["GMAIL_SENDER"]

    if os.environ.get("GMAIL_APP_PASSWORD"):
        config["email"]["app_password"] = os.environ["GMAIL_APP_PASSWORD"]

    if os.environ.get("ALERT_RECIPIENTS"):
        recipients = [r.strip() for r in os.environ["ALERT_RECIPIENTS"].split(",") if r.strip()]
        if recipients:
            config["email"]["recipients"] = recipients

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

def run_cycle(config: dict, force_scrape: bool = False):
    """Execute one full scan: scrape (if needed) → fetch grants → match → email."""
    logger = logging.getLogger("main")
    logger.info("=" * 60)
    logger.info("Starting grant matching cycle")

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
        return

    if not faculty:
        logger.warning("No faculty profiles loaded. Skipping cycle.")
        return
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
                # Save updated embeddings back to cache so we don't redo this every cycle
                try:
                    cache_file = config["faculty"]["cache_file"]
                    Path(cache_file).parent.mkdir(parents=True, exist_ok=True)
                    with open(cache_file, "w") as f:
                        json.dump(faculty, f)
                    logger.info(f"  ✓ Updated embeddings saved to cache")
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
        return

    if not new_grants:
        logger.info("  ✓ No new grants found this cycle. Nothing to do.")
        # Still send diagnostic email even when no grants found
        try:
            send_diagnostic_email(config, {
                "summary": {"faculty_count": len(faculty), "grants_checked": 0,
                            "grants_matched": 0, "raw_matches": 0,
                            "matches_after_filter": 0, "note": "No new grants this cycle"},
                "params": {}, "stop_words_suppressed": [], "per_grant": [],
                "semantic_score_distributions": [], "confidence_histograms": [],
                "grants_capped": [], "idf_filtered_keywords": [],
            }, scraper_health)
        except Exception as e:
            logger.error(f"Diagnostic email failed: {e}", exc_info=True)
        return
    logger.info(f"  ✓ {len(new_grants)} new grants retrieved from all sources")

    # Step 3: Match grants to faculty keywords
    logger.info("Step 3/3 — Matching grants to faculty keywords...")
    matched_results = find_matches(new_grants, faculty, config=config)
    logger.info(f"  ✓ {len(matched_results)} grants with faculty matches")

    if not matched_results:
        logger.info("No keyword matches found this cycle. No match email sent.")
        # Still send diagnostic email even with no matches
        try:
            matcher_diag = get_last_diagnostic()
            send_diagnostic_email(config, matcher_diag, scraper_health)
            logger.info("  ✓ Diagnostic email sent (no matches this cycle)")
        except Exception as e:
            logger.error(f"Diagnostic email failed: {e}", exc_info=True)
        return

    # Step 4: Send email digest
    logger.info(f"Sending email digest for {len(matched_results)} matched grant(s)...")
    try:
        send_email(config, matched_results)
        logger.info("  ✓ Email sent successfully")
    except Exception as e:
        logger.error(f"Email sending failed: {e}", exc_info=True)

    # Step 5: Send diagnostic email (separate, to admin only)
    logger.info("Sending diagnostic email...")
    try:
        matcher_diag = get_last_diagnostic()
        send_diagnostic_email(config, matcher_diag, scraper_health)
        logger.info("  ✓ Diagnostic email sent successfully")
    except Exception as e:
        logger.error(f"Diagnostic email failed: {e}", exc_info=True)

    logger.info("Cycle complete.")


def run_test_email(config: dict):
    """Send a test email with synthetic data to verify email setup."""
    logger = logging.getLogger("main")
    logger.info("Sending test email...")

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
                "faculty_email": config["email"]["recipients"][0] if config["email"]["recipients"] else "",
                "matched_keywords": ["molecular biology", "biochemistry", "genetics", "proteomics"],
                "match_score": 4
            })()
        ]
    }]

    send_email(config, test_results)
    logger.info("Test email sent! Check your inbox.")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="UMSOM Grant Matcher")
    parser.add_argument("--run-once", action="store_true",
                        help="Run one cycle and exit (for testing/cron)")
    parser.add_argument("--scrape", action="store_true",
                        help="Force re-scrape of faculty profiles")
    parser.add_argument("--test-email", action="store_true",
                        help="Send a test email to verify configuration")
    parser.add_argument("--config", default="config/config.yaml",
                        help="Path to config file")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)
    logger = logging.getLogger("main")

    logger.info("UMSOM Grant Matcher starting up")
    logger.info(f"Grants.gov check interval: {config['grants']['check_interval_hours']}h")
    logger.info(f"Faculty re-scrape interval: {config['faculty']['rescrape_interval_hours']}h")
    logger.info(f"Recipients: {', '.join(config['email']['recipients'])}")

    if args.test_email:
        run_test_email(config)
        return

    if args.run_once:
        run_cycle(config, force_scrape=args.scrape)
        return

    # Continuous loop for server/cloud deployment
    check_interval_seconds = config["grants"]["check_interval_hours"] * 3600
    logger.info(f"Running continuously, checking every {config['grants']['check_interval_hours']} hours")

    while True:
        try:
            run_cycle(config, force_scrape=args.scrape)
            args.scrape = False  # Only force-scrape on first run if requested
        except KeyboardInterrupt:
            logger.info("Shutting down (KeyboardInterrupt)")
            break
        except Exception as e:
            logger.error(f"Unexpected error in run cycle: {e}", exc_info=True)

        logger.info(f"Next check in {config['grants']['check_interval_hours']} hours. Sleeping...")
        try:
            time.sleep(check_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Shutting down (KeyboardInterrupt)")
            break


if __name__ == "__main__":
    main()
