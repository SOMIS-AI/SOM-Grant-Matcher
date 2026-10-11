"""
Scheduler heartbeat
===================
`data/scheduler_heartbeat.json` is how the dashboard's unauthenticated
`/health` tells a live scheduler from a dead one. Until 2026-10-11 it was
written only from the scheduler's sleep loop — between runs — so a long run
looked exactly like a dead scheduler: on a roster re-scrape day the pipeline
takes ~1h45, the beat went stale at 45 min, `/health` returned 500 for an
hour, and Azure Health Check replaced the instance seconds after the run
finished (2026-10-10 11:46:55 UTC). A slower scrape would have been killed
mid-run.

Now the pipeline beats as it works — once per step, and throttled inside the
scraper's per-page and per-faculty loops — and the file carries whether a run
is in progress and when it started, so `assess()` can be lenient about a
missed beat during a run without ever excusing a genuinely dead scheduler.

  {
    "beat": "<iso utc>",            when this was written
    "stage": "step 2/3 fetch",      what the scheduler was doing
    "run_in_progress": true,        a pipeline run is under way
    "run_started": "<iso utc>",     when it started (null when idle)
    "next_fire": "<iso>"            next scheduled run (idle only)
  }

Both `beat()` and the run markers are best-effort and never raise: a
heartbeat problem must not disturb the scheduler.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = "data/scheduler_heartbeat.json"
STALE_SECS = 45 * 60          # idle: 3 missed 15-minute beats
RUN_GRACE_SECS = 4 * 3600     # in a run: a scrape day is ~1h45; allow 4h
THROTTLE_SECS = 60            # loops may call beat() freely; writes are rate-limited

_state = {"run_started": None, "stage": "idle", "last_write": 0.0, "next_fire": None}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def beat(stage: str | None = None, *, next_fire=None, force: bool = False) -> bool:
    """Stamp the heartbeat file. Throttled to one write per THROTTLE_SECS
    unless `force`. Returns True if a write happened."""
    try:
        if stage is not None:
            _state["stage"] = stage
        if next_fire is not None:
            _state["next_fire"] = next_fire.isoformat() if hasattr(next_fire, "isoformat") else str(next_fire)
        now_mono = time.monotonic()
        if not force and now_mono - _state["last_write"] < THROTTLE_SECS:
            return False
        payload = {
            "beat":            _now().isoformat(),
            "stage":           _state["stage"],
            "run_in_progress": _state["run_started"] is not None,
            "run_started":     _state["run_started"],
            "next_fire":       _state["next_fire"] if _state["run_started"] is None else None,
        }
        try:
            from atomic_io import atomic_write_json
            atomic_write_json(HEARTBEAT_FILE, payload)
        except ImportError:
            import json, os
            os.makedirs(os.path.dirname(HEARTBEAT_FILE) or ".", exist_ok=True)
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        _state["last_write"] = now_mono
        return True
    except Exception as e:  # never disturb the caller
        logger.debug(f"heartbeat write skipped: {e}")
        return False


def start_run(stage: str = "pipeline") -> None:
    """Mark a pipeline run as in progress (idempotent) and beat immediately."""
    try:
        if _state["run_started"] is None:
            _state["run_started"] = _now().isoformat()
        beat(stage, force=True)
    except Exception:
        pass


def end_run() -> None:
    """Mark the run finished and beat immediately."""
    try:
        _state["run_started"] = None
        beat("idle", force=True)
    except Exception:
        pass


def assess(hb: dict, now: datetime | None = None,
           stale_secs: int = STALE_SECS, run_grace_secs: int = RUN_GRACE_SECS) -> list:
    """Health problems implied by a heartbeat document. Pure; used by /health.

    - no file / no beat / unparseable beat  -> [] (first boot is fine)
    - beat fresher than stale_secs          -> []
    - stale, but a run is in progress that started within run_grace_secs
                                            -> [] (a long run, not a dead loop)
    - otherwise                             -> ["scheduler_heartbeat_stale"]
    """
    now = now or _now()
    beat_raw = (hb or {}).get("beat")
    if not beat_raw:
        return []
    try:
        beat_dt = datetime.fromisoformat(str(beat_raw))
        if beat_dt.tzinfo is None:
            beat_dt = beat_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return []
    age = (now - beat_dt).total_seconds()
    if age <= stale_secs:
        return []
    if hb.get("run_in_progress") and hb.get("run_started"):
        try:
            started = datetime.fromisoformat(str(hb["run_started"]))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (now - started).total_seconds() <= run_grace_secs:
                return []
        except (ValueError, TypeError):
            pass
    return ["scheduler_heartbeat_stale"]
