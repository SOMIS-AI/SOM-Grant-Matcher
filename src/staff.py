"""
Staff Profiles
==============
UMSOM staff (non-faculty employees) who want to be notified of grants matching
their work. Added 2026-09-03.

Staff differ from every other person in this system in one important way: they
have no scraped profile. Faculty come from the UMSOM directory with keywords
harvested from their profile pages and publication footprint; staff are entered
by hand, and their entire match profile is whatever is typed into the dashboard.

  data/staff_profiles.json    list of records →
    [ { name, email, department, title, keywords: [...], profile_text,
        cadence, status, added_by, added_at, updated_at } ]

`cadence` is "weekly" | "off" (staff are a weekly-digest audience by design —
they are not the daily operational audience). "off" is kept as a tombstone so
re-enabling someone preserves their profile and history.

How a staff record reaches the matcher
--------------------------------------
`as_match_profiles()` reshapes these records into the same dict shape
`faculty_scraper.get_faculty_profiles()` returns, so the matcher, the IDF table,
and the embedder all treat them as ordinary people with no special-casing:

  keywords        → the keyword channel, exactly as for faculty
  profile_text    → `evidence_titles`, which `embedder.faculty_to_text()` folds
                    into the embedded sentence. This is what gives staff a
                    usable semantic vector despite having no publications.
  is_staff: True  → carried onto the Match so digests, the workbook, and the
                    diagnostic can tell staff from faculty downstream.

Deliberately NOT exempted from the research-track-record gates (decided
2026-09-03). Staff have no publication or RePORTER footprint, so
`matcher._research_tier()` classifies them 'none': confidence ×0.8, and hard
gated off the 12 major mechanisms (R01, U01, P01, UM1…) and the PI-track-record
mechanisms (K12, T32…). That is the intended behaviour — a staff member is not
going to PI an R01, and the gates already encode exactly that judgement for
footprint-less faculty. It does mean staff will match a narrow slice of grants;
if that proves too narrow, the lever is `research_evidence` in config.yaml, and
the decision should be revisited against real 👍/👎 data rather than by guess.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
STAFF_FILE = DATA_DIR / "staff_profiles.json"

VALID_CADENCES = {"weekly", "off"}

# Same guard the faculty pipeline applies: a person with no keywords cannot
# match anything, so admitting them just inflates the pool and the IDF table.
MIN_KEYWORDS = 1

_write_lock = threading.Lock()


# ── storage ───────────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception as e:
        logger.error(f"Failed reading {path}: {e}")
    return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def load_staff() -> list:
    """All staff records, including cadence='off' tombstones."""
    recs = _load_json(STAFF_FILE, [])
    return recs if isinstance(recs, list) else []


def save_staff(records: list) -> None:
    with _write_lock:
        _save_json(STAFF_FILE, records)


# ── keyword / profile-text parsing ────────────────────────────────────────────

def parse_keywords(raw) -> list:
    """Accept a list, or a blob pasted into a textarea — one keyword per line,
    or comma/semicolon separated. Order is preserved and duplicates dropped
    case-insensitively, because the matcher reports matched keywords back to the
    reader and a list that echoes what was typed is easier to audit."""
    if isinstance(raw, list):
        parts = [str(p) for p in raw]
    else:
        parts = re.split(r"[\n,;]+", str(raw or ""))
    out, seen = [], set()
    for p in parts:
        kw = " ".join(p.split()).strip()
        if not kw:
            continue
        low = kw.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(kw)
    return out


# ── CRUD ──────────────────────────────────────────────────────────────────────

def upsert_staff(*, email: str, name: str = "", department: str = "",
                 title: str = "", keywords=None, profile_text: str = "",
                 cadence: str = "weekly", added_by: str = "") -> dict:
    """Create or update one staff member. Email is the identity key."""
    em = _normalize_email(email)
    if not em or "@" not in em:
        raise ValueError("a valid email address is required")

    cad = (cadence or "weekly").strip().lower()
    if cad not in VALID_CADENCES:
        raise ValueError(f"cadence must be one of {sorted(VALID_CADENCES)}")

    kws = parse_keywords(keywords)
    text = (profile_text or "").strip()
    # Only enforce the keyword floor for someone who is actually going to be
    # matched; an 'off' record is a tombstone and need not be complete.
    if cad != "off" and len(kws) < MIN_KEYWORDS and not text:
        raise ValueError("give at least one keyword or some profile text — "
                         "a staff member with neither cannot match anything")

    records = load_staff()
    now = _now_iso()
    for rec in records:
        if _normalize_email(rec.get("email")) == em:
            rec.update({
                "name": name.strip() or rec.get("name", ""),
                "department": department.strip() or rec.get("department", ""),
                "title": title.strip() or rec.get("title", ""),
                "keywords": kws or rec.get("keywords", []),
                "profile_text": text if text else rec.get("profile_text", ""),
                "cadence": cad,
                "status": "active" if cad != "off" else "off",
                "updated_at": now,
            })
            save_staff(records)
            logger.info(f"Staff profile updated: {em} ({len(rec['keywords'])} keywords)")
            return rec

    rec = {
        "name": name.strip(),
        "email": em,
        "department": department.strip(),
        "title": title.strip(),
        "keywords": kws,
        "profile_text": text,
        "cadence": cad,
        "status": "active" if cad != "off" else "off",
        "added_by": added_by,
        "added_at": now,
        "updated_at": now,
    }
    records.append(rec)
    save_staff(records)
    logger.info(f"Staff profile added: {em} ({len(kws)} keywords)")
    return rec


def remove_staff(email: str) -> bool:
    """Tombstone a staff member (cadence='off'), preserving their profile so
    re-enabling does not mean retyping it. Returns False if unknown."""
    em = _normalize_email(email)
    records = load_staff()
    for rec in records:
        if _normalize_email(rec.get("email")) == em:
            rec["cadence"] = "off"
            rec["status"] = "off"
            rec["updated_at"] = _now_iso()
            save_staff(records)
            logger.info(f"Staff profile disabled: {em}")
            return True
    return False


def delete_staff(email: str) -> bool:
    """Hard-delete a staff record. Use remove_staff() for the normal path."""
    em = _normalize_email(email)
    records = load_staff()
    kept = [r for r in records if _normalize_email(r.get("email")) != em]
    if len(kept) == len(records):
        return False
    save_staff(kept)
    logger.info(f"Staff profile deleted: {em}")
    return True


def active_staff() -> list:
    """Staff who should be matched and emailed."""
    return [r for r in load_staff()
            if (r.get("cadence") or "").lower() not in ("", "off")]


def staff_emails() -> set:
    """Lowercased emails of ALL staff records, tombstones included. Used to tag
    a Match as staff downstream without re-reading the file per match."""
    return {_normalize_email(r.get("email")) for r in load_staff()
            if _normalize_email(r.get("email"))}


# ── bridge into the matching pipeline ─────────────────────────────────────────

def as_match_profiles() -> list:
    """Active staff, reshaped into the dict the matcher expects from
    `get_faculty_profiles()`.

    `profile_text` is placed in `evidence_titles` rather than folded into
    `keywords`: keywords drive the exact-match channel, where a sentence would
    never match and would pollute the IDF table, while `evidence_titles` is what
    `embedder.faculty_to_text()` appends as "Recent publications and projects
    include: …". That gives the free text its intended job — grounding the
    semantic vector — without corrupting keyword matching.
    """
    out = []
    for r in active_staff():
        kws = r.get("keywords") or []
        text = (r.get("profile_text") or "").strip()
        if not kws and not text:
            continue
        out.append({
            "name": r.get("name") or r.get("email", ""),
            "email": r.get("email", ""),
            "department": r.get("department", "") or "UMSOM Staff",
            "title": r.get("title", "") or "Staff",
            "url": "",
            "profile_url": "",
            "keywords": list(kws),
            "keyword_source": "staff profile (manually entered)",
            # Free text → semantic grounding. Kept as a single-element list
            # because faculty_to_text() joins the list with spaces.
            "evidence_titles": [text] if text else [],
            # Research tier MUST come out 'none', not 'unknown'. _research_tier()
            # returns 'unknown' for a FALSY keywords_by_source — and 'unknown' is
            # explicitly "never penalize": multiplier 1.0, and the major-mechanism
            # gate at matcher.py fires only on tier == 'none'. An empty dict here
            # would therefore have exempted staff from both the 0.8 multiplier and
            # the R01/U01/P01 gate — the exact opposite of the 2026-09-03 decision
            # to keep the gates on. A non-empty source map that contains no
            # evidence-bearing label (see matcher._EVIDENCE_SOURCE_LABELS) is what
            # lands on 'none'.
            "keywords_by_source": {"Staff profile": list(kws)} if kws else {"Staff profile": [text[:80]]},
            "is_staff": True,
            "inactive": False,
        })
    return out
