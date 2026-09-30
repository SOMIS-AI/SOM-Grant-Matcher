"""
Recent-grants store
===================
Every grant the daily fetch returns, kept with its FULL text for 14 days, so
the Tuesday weekly roundup can be re-matched under the rules in force on
Tuesday instead of replaying whatever each morning's run decided.

Why (2026-09-30): the weekly roundup was rebuilt from match_results.json,
which holds only delivered rows with a 500-character synopsis. Nothing could
be re-evaluated, so a fix shipped on Wednesday never reached the digest most
faculty actually read. The 2026-09-29 weekly led with 40 DOJ rows captured on
09-22 hours before the corroboration gate went live, and re-sent the 09-26
astronomy and boilerplate rows a second time. See TUNING_LOG 2026-09-30.

  data/recent_grants.json
    [ { ...grant fields exactly as the poller/scrapers produced them...,
        "recorded_at": "<iso utc>" }, ... ]

Keyed by the same identity the roundup uses (id, else link, else title).
A grant fetched twice keeps its latest record. Retention is by age; a count
guard bounds the file and LOGS if it ever trims in-window entries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

RECENT_GRANTS_FILE = "data/recent_grants.json"
RETENTION_DAYS = 14
MAX_ENTRIES = 5000


def grant_key(g: dict) -> str:
    """Identity used to line stored matches up with stored grants."""
    return (str(g.get("id") or g.get("grant_id") or "")
            or str(g.get("link") or g.get("grant_link") or "")
            or str(g.get("title") or g.get("grant_title") or ""))


def _read(path: str) -> list:
    try:
        p = Path(path)
        if not p.exists():
            return []
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Could not read {path}: {e}")
        return []


def record_grants(grants: list, path: str = RECENT_GRANTS_FILE,
                  retention_days: int = RETENTION_DAYS, now: datetime | None = None) -> int:
    """Append this run's grants (full records) to the store, dedupe by key
    keeping the newest, prune by age. Returns the number of entries kept.
    Never raises — a store failure must not take down the daily run."""
    try:
        now = now or datetime.utcnow()
        stamp = now.isoformat()
        cutoff = (now - timedelta(days=retention_days)).isoformat()

        by_key: dict = {}
        for e in _read(path):
            if not isinstance(e, dict) or e.get("recorded_at", "") < cutoff:
                continue
            k = grant_key(e)
            if k:
                by_key[k] = e
        added = 0
        for g in grants or []:
            if not isinstance(g, dict):
                continue
            k = grant_key(g)
            if not k:
                continue
            rec = {kk: vv for kk, vv in g.items() if kk != "embedding"}
            rec["recorded_at"] = stamp
            by_key[k] = rec
            added += 1

        entries = sorted(by_key.values(), key=lambda e: e.get("recorded_at", ""), reverse=True)
        if len(entries) > MAX_ENTRIES:
            logger.warning(f"{path}: count guard trimming {len(entries) - MAX_ENTRIES} "
                           f"in-window grant(s) (>{MAX_ENTRIES}) — weekly re-match may be incomplete")
            entries = entries[:MAX_ENTRIES]

        # Scraper records can carry non-JSON values (dates); coerce once so the
        # atomic writer (plain json.dump) never fails half-way.
        entries = json.loads(json.dumps(entries, default=str))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            from atomic_io import atomic_write_json
            atomic_write_json(path, entries)
        except ImportError:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entries, f)
        logger.info(f"Recent-grants store: recorded {added} grant(s), {len(entries)} kept "
                    f"(retention {retention_days}d)")
        return len(entries)
    except Exception as e:
        logger.error(f"Recent-grants store write failed (weekly re-match will fall back): {e}",
                     exc_info=True)
        return 0


def load_recent_grants(days: int, path: str = RECENT_GRANTS_FILE,
                       now: datetime | None = None) -> list:
    """Grants recorded in the last `days` days, newest first, with the
    `recorded_at` stamp left on each record."""
    now = now or datetime.utcnow()
    cutoff = (now - timedelta(days=days)).isoformat()
    out = [e for e in _read(path) if isinstance(e, dict) and e.get("recorded_at", "") >= cutoff]
    out.sort(key=lambda e: e.get("recorded_at", ""), reverse=True)
    return out
