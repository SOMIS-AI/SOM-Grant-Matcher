#!/usr/bin/env python3
"""
Launcher: Flask dashboard (foreground) + grant matcher (background thread).
Azure Web Apps sets WEBSITES_PORT; also supports PORT and DASHBOARD_PORT overrides.
"""
import logging
import os
import sys
import threading
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

# ALSO log to the rotating file the dashboard's /api/logs viewer reads.
# main.setup_logging() is deliberately not called here (it would double the
# stdout handler), but skipping it entirely meant production wrote NO file logs
# at all — logs/grant_matcher.log never existed, so the dashboard Logs tab was
# reading a file nothing wrote, and post-restart forensics had only Azure's
# limited-retention log stream. Attach just the file handler to the root logger.
try:
    from logging.handlers import RotatingFileHandler
    _log_file = Path(os.environ.get("LOG_FILE", "logs/grant_matcher.log"))
    _log_file.parent.mkdir(parents=True, exist_ok=True)
    _fh = RotatingFileHandler(_log_file, maxBytes=10 * 1024 * 1024, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
except Exception as _e:
    logging.getLogger("run").warning(f"Could not attach file log handler: {_e}")

log = logging.getLogger("run")
log.info("Starting UMSOM Grant Matcher Dashboard")
log.info(f"Working directory: {ROOT}")
log.info(f"Python: {sys.version}")
log.info(f"ENV PORT={os.environ.get('PORT')} WEBSITES_PORT={os.environ.get('WEBSITES_PORT')} DASHBOARD_PORT={os.environ.get('DASHBOARD_PORT')}")


def start_matcher():
    try:
        import main as m
        config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")
        config = m.load_config(config_path)
        # Do NOT call m.setup_logging() here — logging is already configured by run.py
        # Calling it again adds a second handler and causes every line to print twice
        logger = logging.getLogger("main")
        logger.info("Grant Matcher background thread started")
        # The scheduler fires daily at NOTIFY_HOUR in NOTIFY_TIMEZONE and never
        # sends email on startup/restart — it only waits for the next fire time.
        m.run_scheduler(config)
    except Exception as e:
        logging.getLogger("main").error(f"Matcher fatal error: {e}", exc_info=True)


if __name__ == "__main__":
    port = int(os.environ.get("WEBSITES_PORT") or os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or 8080)
    log.info(f"Will bind Flask to port {port}")

    matcher_thread = threading.Thread(target=start_matcher, daemon=True, name="matcher")
    matcher_thread.start()

    try:
        log.info("Importing Flask dashboard...")
        from dashboard import app, register_matcher_thread
        register_matcher_thread(matcher_thread)  # lets /health report a dead scheduler
        log.info(f"Flask dashboard imported OK, starting on 0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        log.error(f"Dashboard error: {e}", exc_info=True)
        sys.exit(1)
