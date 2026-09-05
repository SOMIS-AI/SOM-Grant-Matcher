"""
Subscriptions Layer
===================
Persistence + helpers for the individualized-notifications feature added
2026-06-05. Three JSON files live next to the existing matcher data files
on the /app/data mount:

  data/faculty_subscriptions.json   keyed by faculty email →
    { name, department, cadence, enrolled_by, enrolled_at, status }

  data/dept_admins.json             list of records →
    [ { department, email, name, cadence, added_by, added_at } ]

  data/som_admins.json              list of records →
    [ { email, name, title, cadence, added_by, added_at } ]
    School-wide SOM Research Administrators (2026-09-05). Unlike dept admins,
    who receive only their own department's matches, these receive EVERY match
    across the school and rate on behalf of any faculty member — they know the
    faculty well enough to give per-match verdicts. No `department` field
    because the scope is deliberately the whole school.

  data/email_log.json               rolling audit log (capped) →
    [ { timestamp, type, to, faculty_name?, department?, matches_count, subject } ]

`cadence` is one of: "daily" | "weekly" | "off".
"off" is preserved as a tombstone so re-enrolling someone keeps their history.

These files are managed by the dashboard /api/subscriptions endpoints and read
by main.run_scheduler when fanning out per-faculty + per-dept emails.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Files are resolved relative to DATA_DIR (env var, defaults to "data") so the
# scheduler, dashboard, and tests can all agree without hardcoding paths.
import os
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))

FACULTY_SUBS_FILE = DATA_DIR / "faculty_subscriptions.json"
DEPT_ADMINS_FILE  = DATA_DIR / "dept_admins.json"
SOM_ADMINS_FILE   = DATA_DIR / "som_admins.json"
EMAIL_LOG_FILE    = DATA_DIR / "email_log.json"

# Cap on log size so it doesn't grow unbounded on the Azure Files mount.
EMAIL_LOG_CAP = 5000

# Daily SendGrid free-tier ceiling; we refuse to send overflow beyond this
# and log a warning so the operator can decide to upgrade.
SENDGRID_DAILY_CAP = 100
SAFETY_HEADROOM    = 10   # halt at cap - headroom to leave room for diagnostic + restart emails

VALID_CADENCES = {"daily", "weekly", "off"}

# ── locking ───────────────────────────────────────────────────────────────────
# Writes are serialised behind a module-level lock — the dashboard runs in a
# threaded Flask app and the scheduler runs in a background thread, so concurrent
# writes are possible. JSON files are tiny; a single lock is fine.
_write_lock = threading.Lock()


# ── low-level io ──────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Subscriptions: could not read {path}: {e}")
    return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── normalisation helpers ────────────────────────────────────────────────────

def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_dept_name(dept: str) -> str:
    """Canonical department key for matching dept-admin records against the
    faculty.department string. Strips leading "Department of " and trims
    surrounding whitespace; lowercases for comparison.

    "Department of Medicine" → "medicine"
    "Department of Surgery"  → "surgery"
    "  Anesthesiology  "     → "anesthesiology"
    """
    s = (dept or "").strip()
    s = re.sub(r"^\s*department\s+of\s+", "", s, flags=re.I)
    return s.strip().lower()


# ══════════════════════════════════════════════════════════════════════════════
# FACULTY SUBSCRIPTIONS
# ══════════════════════════════════════════════════════════════════════════════

def load_faculty_subs() -> dict:
    """Returns dict mapping email (lowercased) → subscription record."""
    data = _load_json(FACULTY_SUBS_FILE, {"subscriptions": {}})
    return data.get("subscriptions", {}) if isinstance(data, dict) else {}


def save_faculty_subs(subs: dict) -> None:
    with _write_lock:
        _save_json(FACULTY_SUBS_FILE, {
            "subscriptions": subs,
            "updated_at":    _now_iso(),
        })


def upsert_faculty_sub(email: str, *, name: str = "", department: str = "",
                       cadence: str = "daily", enrolled_by: str = "admin") -> dict:
    """Add or update a faculty subscription. Returns the saved record."""
    email_key = _normalize_email(email)
    if not email_key or "@" not in email_key:
        raise ValueError(f"Invalid email: {email!r}")
    if cadence not in VALID_CADENCES:
        raise ValueError(f"cadence must be one of {VALID_CADENCES}, got {cadence!r}")

    with _write_lock:
        all_subs = _load_json(FACULTY_SUBS_FILE, {"subscriptions": {}}).get("subscriptions", {})
        existing = all_subs.get(email_key) or {}
        record = {
            "email":       email_key,
            "name":        name or existing.get("name", ""),
            "department":  department or existing.get("department", ""),
            "cadence":     cadence,
            "enrolled_by": existing.get("enrolled_by") or enrolled_by,
            "enrolled_at": existing.get("enrolled_at") or _now_iso(),
            "updated_at":  _now_iso(),
        }
        all_subs[email_key] = record
        _save_json(FACULTY_SUBS_FILE, {
            "subscriptions": all_subs,
            "updated_at":    _now_iso(),
        })
    return record


def remove_faculty_sub(email: str) -> bool:
    """Set status to 'off' (tombstone, preserving history) rather than hard-delete.
    Returns True if a record existed and was tombstoned."""
    email_key = _normalize_email(email)
    if not email_key:
        return False
    with _write_lock:
        all_subs = _load_json(FACULTY_SUBS_FILE, {"subscriptions": {}}).get("subscriptions", {})
        if email_key not in all_subs:
            return False
        all_subs[email_key]["cadence"] = "off"
        all_subs[email_key]["updated_at"] = _now_iso()
        _save_json(FACULTY_SUBS_FILE, {
            "subscriptions": all_subs,
            "updated_at":    _now_iso(),
        })
    return True


def faculty_subs_for_cadence(cadence: str) -> dict:
    """Return only subscriptions whose cadence matches the given value."""
    return {e: r for e, r in load_faculty_subs().items() if r.get("cadence") == cadence}


# ══════════════════════════════════════════════════════════════════════════════
# DEPARTMENT ADMINS
# ══════════════════════════════════════════════════════════════════════════════

def load_dept_admins() -> list:
    """Returns list of {department, email, name, cadence, ...} records."""
    data = _load_json(DEPT_ADMINS_FILE, {"admins": []})
    return data.get("admins", []) if isinstance(data, dict) else []


def save_dept_admins(admins: list) -> None:
    with _write_lock:
        _save_json(DEPT_ADMINS_FILE, {
            "admins":     admins,
            "updated_at": _now_iso(),
        })


def _admin_key(department: str, email: str) -> tuple[str, str]:
    """Composite key for admin uniqueness — same email may admin multiple depts."""
    return (normalize_dept_name(department), _normalize_email(email))


def add_dept_admin(*, department: str, email: str, name: str = "",
                   cadence: str = "daily", added_by: str = "admin") -> dict:
    """Add or update a dept-admin record. Returns the saved record."""
    if not department or not (email := _normalize_email(email)) or "@" not in email:
        raise ValueError("department and a valid email are required")
    if cadence not in VALID_CADENCES:
        raise ValueError(f"cadence must be one of {VALID_CADENCES}, got {cadence!r}")

    with _write_lock:
        admins = _load_json(DEPT_ADMINS_FILE, {"admins": []}).get("admins", [])
        key = _admin_key(department, email)
        for a in admins:
            if _admin_key(a.get("department", ""), a.get("email", "")) == key:
                a["name"]       = name or a.get("name", "")
                a["cadence"]    = cadence
                a["updated_at"] = _now_iso()
                _save_json(DEPT_ADMINS_FILE, {"admins": admins, "updated_at": _now_iso()})
                return a
        record = {
            "department": department.strip(),
            "email":      email,
            "name":       name,
            "cadence":    cadence,
            "added_by":   added_by,
            "added_at":   _now_iso(),
            "updated_at": _now_iso(),
        }
        admins.append(record)
        _save_json(DEPT_ADMINS_FILE, {"admins": admins, "updated_at": _now_iso()})
    return record


def remove_dept_admin(department: str, email: str) -> bool:
    """Hard-remove a dept-admin record (admin departures are normal turnover)."""
    target = _admin_key(department, email)
    with _write_lock:
        admins = _load_json(DEPT_ADMINS_FILE, {"admins": []}).get("admins", [])
        before = len(admins)
        admins = [a for a in admins
                  if _admin_key(a.get("department",""), a.get("email","")) != target]
        if len(admins) == before:
            return False
        _save_json(DEPT_ADMINS_FILE, {"admins": admins, "updated_at": _now_iso()})
    return True


def admins_for_department(dept: str, *, cadence: Optional[str] = None) -> list:
    """All admin records whose normalised department matches `dept`."""
    target = normalize_dept_name(dept)
    out = [a for a in load_dept_admins()
           if normalize_dept_name(a.get("department","")) == target]
    if cadence is not None:
        out = [a for a in out if a.get("cadence") == cadence]
    return out


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL AUDIT LOG
# ══════════════════════════════════════════════════════════════════════════════

# ── SOM Research Administrators (school-wide) ────────────────────────────────
# A fourth audience, added 2026-09-05. The distinction that matters is SCOPE and
# TRUST, not delivery:
#
#   faculty sub  → their own matches            rater 'self'
#   dept admin   → their department's matches   rater 'admin'
#   SOM admin    → EVERY match, school-wide     rater 'som_admin'   ← this
#   main digest  → every match, shared list     rater 'digest'
#
# SOM admins see the same content as the shared weekly digest, so why a separate
# group? Two reasons. They are managed in the portal rather than an env var, so
# adding one does not need an app-settings change and a restart. And their
# verdicts carry their own rater value: they know the faculty personally and are
# answering on their behalf, which is worth more than an anonymous distribution
# list ('digest') and less than the faculty member's own answer ('self'). Keeping
# them distinct means that judgement can be made later, from the data, rather
# than being baked in now.

def load_som_admins() -> list:
    recs = _load_json(SOM_ADMINS_FILE, [])
    return recs if isinstance(recs, list) else []


def save_som_admins(admins: list) -> None:
    with _write_lock:
        _save_json(SOM_ADMINS_FILE, admins)


def add_som_admin(*, email: str, name: str = "", title: str = "",
                  cadence: str = "weekly", added_by: str = "") -> dict:
    """Create or update one school-wide research administrator."""
    em = _normalize_email(email)
    if not em or "@" not in em:
        raise ValueError("a valid email address is required")
    cad = (cadence or "weekly").strip().lower()
    if cad not in VALID_CADENCES:
        raise ValueError(f"cadence must be one of {sorted(VALID_CADENCES)}")

    admins = load_som_admins()
    now = _now_iso()
    for rec in admins:
        if _normalize_email(rec.get("email")) == em:
            rec.update({
                "name": name.strip() or rec.get("name", ""),
                "title": title.strip() or rec.get("title", ""),
                "cadence": cad,
                "updated_at": now,
            })
            save_som_admins(admins)
            logger.info(f"SOM admin updated: {em} ({cad})")
            return rec

    rec = {
        "email": em,
        "name": name.strip(),
        "title": title.strip(),
        "cadence": cad,
        "added_by": added_by,
        "added_at": now,
        "updated_at": now,
    }
    admins.append(rec)
    save_som_admins(admins)
    logger.info(f"SOM admin added: {em} ({cad})")
    return rec


def remove_som_admin(email: str) -> bool:
    """Tombstone (cadence='off') rather than delete, matching the faculty-sub
    convention so re-enabling someone keeps their history."""
    em = _normalize_email(email)
    admins = load_som_admins()
    for rec in admins:
        if _normalize_email(rec.get("email")) == em:
            rec["cadence"] = "off"
            rec["updated_at"] = _now_iso()
            save_som_admins(admins)
            logger.info(f"SOM admin disabled: {em}")
            return True
    return False


def som_admins_for_cadence(cadence: str) -> list:
    """Active SOM admins in one cadence bucket."""
    want = (cadence or "").strip().lower()
    return [a for a in load_som_admins()
            if (a.get("cadence") or "").lower() == want]


def log_email(*, kind: str, to: str, subject: str, matches_count: int = 0,
              faculty_name: str = "", department: str = "",
              recipients_count: int = 1) -> None:
    """Append one entry to the rolling audit log.

    `kind`: 'faculty' / 'dept_admin' (personalized), their *_failed variants,
    or 'digest' / 'diagnostic' / 'restart' / 'alert' (admin sends).
    `recipients_count`: how many SendGrid recipients this send consumed — a BCC
    digest to N addresses burns N against the free-tier daily cap, so the
    budget ledger must count N, not 1."""
    entry = {
        "timestamp":     _now_iso(),
        "kind":          kind,
        "to":            _normalize_email(to),
        "subject":       subject[:200],
        "matches_count": int(matches_count),
        "faculty_name":  faculty_name,
        "department":    department,
        "recipients_count": max(1, int(recipients_count)),
    }
    with _write_lock:
        log = _load_json(EMAIL_LOG_FILE, {"log": []}).get("log", [])
        log.append(entry)
        log = log[-EMAIL_LOG_CAP:]
        _save_json(EMAIL_LOG_FILE, {"log": log, "updated_at": _now_iso()})


def count_today_emails() -> int:
    """How many SendGrid recipients has the audit log recorded today (UTC)?
    Sums recipients_count (default 1 for older entries) so multi-recipient BCC
    sends count at their true cost. Previously only personalized sends were
    logged at all, so the digest/diagnostic/restart emails silently consumed
    budget the ledger never saw — and the fan-out then over-committed and hit
    SendGrid's hard reject."""
    today = datetime.now(timezone.utc).date().isoformat()
    log = _load_json(EMAIL_LOG_FILE, {"log": []}).get("log", [])
    return sum(max(1, int(e.get("recipients_count", 1) or 1)) for e in log
               if isinstance(e.get("timestamp"), str)
               and e["timestamp"].startswith(today))


def remaining_budget_today() -> int:
    """Emails we can still send today before hitting the safety cap."""
    used = count_today_emails()
    budget = SENDGRID_DAILY_CAP - SAFETY_HEADROOM - used
    return max(budget, 0)
