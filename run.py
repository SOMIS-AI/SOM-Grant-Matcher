#!/usr/bin/env python3
"""
Launcher: Flask dashboard (foreground) + grant matcher (background thread).
Azure Web Apps sets WEBSITES_PORT; also supports PORT and DASHBOARD_PORT overrides.
"""
import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("run")
log.info("Starting UMSOM Grant Matcher Dashboard")
log.info(f"Working directory: {ROOT}")
log.info(f"Python: {sys.version}")
log.info(f"ENV PORT={os.environ.get('PORT')} WEBSITES_PORT={os.environ.get('WEBSITES_PORT')} DASHBOARD_PORT={os.environ.get('DASHBOARD_PORT')}")


def start_matcher():
    try:
        import main as m
        config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")
        force_scrape = os.environ.get("FORCE_SCRAPE", "").lower() in ("1", "true", "yes")
        config = m.load_config(config_path)
        # Do NOT call m.setup_logging() here — logging is already configured by run.py
        # Calling it again adds a second handler and causes every line to print twice
        logger = logging.getLogger("main")
        logger.info("Grant Matcher background thread started")
        check_interval = config["grants"]["check_interval_hours"] * 3600
        while True:
            try:
                m.run_cycle(config, force_scrape=force_scrape)
                force_scrape = False
            except Exception as e:
                logger.error(f"Run cycle error: {e}", exc_info=True)
            logger.info(f"Sleeping {config['grants']['check_interval_hours']}h...")
            time.sleep(check_interval)
    except Exception as e:
        logging.getLogger("main").error(f"Matcher fatal error: {e}", exc_info=True)


if __name__ == "__main__":
    port = int(os.environ.get("WEBSITES_PORT") or os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or 8080)
    log.info(f"Will bind Flask to port {port}")

    threading.Thread(target=start_matcher, daemon=True, name="matcher").start()

    try:
        log.info("Importing Flask dashboard...")
        from dashboard import app
        log.info(f"Flask dashboard imported OK, starting on 0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        log.error(f"Dashboard error: {e}", exc_info=True)
        sys.exit(1)
