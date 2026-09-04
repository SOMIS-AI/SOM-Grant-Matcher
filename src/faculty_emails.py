"""
Faculty Email Directory
=======================
Name → email, imported from a UMSOM directory export. Added 2026-09-04.

Why this exists: not every UMSOM profile page publishes an email address.
Verified by fetching the pages directly — for the affected faculty there is no
address in any form (no mailto, no obfuscation, no alternate domain), so the
Pass 2 scrape regex has nothing to find and no pattern change would help. Across
the match archive that left 356 faculty and 1,637 of 6,365 delivered matches
(25.7%) with a blank email, which:

  * makes their 👍/👎 feedback unattributable, and
  * excludes them from personalised digests entirely, because that fan-out
    indexes by email and skips empty ones silently.

This store is deliberately SEPARATE from `eval_app_keywords.json`. That file is
a keywords store that happens to carry addresses; this one is a directory of
names and addresses with no keywords, and conflating them would mean writing
keyword-less rows into a keywords file. Pass 8b consults both.

  seed_data/faculty_emails.json
    {
      "updated_at": "<iso>",
      "sources": [{"filename": ..., "imported_at": ..., "stats": {...}}],
      "by_name": {
        "<normalised name>": {
          "email": "...", "name": "...", "department": "...",
          "source_file": "...", "updated_at": "..."
        }
      }
    }

Keyed by NORMALISED name (first + last, credentials and middle initials
stripped) because that is the join key against the scraped roster — the whole
point is that these people have no email to join on.

**Names that resolve to more than one address are excluded**, not tie-broken.
Two real cases in the 04Sept2026 export ("Sarah E. Woodson Smith",
"Brian W. Jackson") map to two addresses each. Attaching someone's digest — or
their verdict on a grant — to the wrong colleague is worse than leaving it
blank, so ambiguity is recorded and skipped.

Import a new export with:

    python -m src.faculty_emails <path-to-csv>

expecting columns First, Last, Email, Department.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path("seed_data/faculty_emails.json")

# Reuse the eval-app normaliser so both indexes key identically — a name must
# resolve the same way whichever store it came from.
try:
    from eval_app_keywords import normalize_person_name
except ImportError:                                   # `python -m src.faculty_emails`
    from src.eval_app_keywords import normalize_person_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_store(path: Path = DEFAULT_STORE_PATH) -> dict:
    try:
        if Path(path).exists():
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception as e:
        logger.error(f"Failed reading {path}: {e}")
    return {"updated_at": None, "sources": [], "by_name": {}}


def save_store(store: dict, path: Path = DEFAULT_STORE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = _now_iso()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def import_csv(csv_path: Path, store: dict) -> dict:
    """Merge one directory export into `store`. Expects First, Last, Email,
    Department. Returns a stats dict."""
    csv_path = Path(csv_path)
    stats = {"rows_total": 0, "usable": 0, "added": 0, "updated": 0,
             "unchanged": 0, "skipped_no_email": 0, "ambiguous": 0}

    raw = io.open(csv_path, encoding="utf-8-sig", errors="replace").read()
    reader = csv.DictReader(io.StringIO(raw))
    cols = {(c or "").strip().lower() for c in (reader.fieldnames or [])}
    for required in ("first", "last", "email"):
        if required not in cols:
            raise ValueError(f"{csv_path.name}: missing required column "
                             f"'{required}'. Found: {sorted(cols)}")

    # First pass: group by normalised name so ambiguity is detected BEFORE
    # anything is written. A name seen with two different addresses must not be
    # resolved by import order.
    grouped: dict[str, list[dict]] = {}
    for row in reader:
        stats["rows_total"] += 1
        email = (row.get("Email") or row.get("email") or "").strip()
        if not email or "@" not in email:
            stats["skipped_no_email"] += 1
            continue
        first = (row.get("First") or row.get("first") or "").strip()
        last  = (row.get("Last")  or row.get("last")  or "").strip()
        key = normalize_person_name(f"{first} {last}")
        if not key:
            stats["skipped_no_email"] += 1
            continue
        stats["usable"] += 1
        grouped.setdefault(key, []).append({
            "email": email,
            "name": f"{first} {last}".strip(),
            "department": (row.get("Department") or row.get("department") or "").strip(),
        })

    by_name = store.setdefault("by_name", {})
    ambiguous_names = []
    for key, entries in grouped.items():
        addresses = {e["email"].lower() for e in entries}
        if len(addresses) > 1:
            stats["ambiguous"] += 1
            ambiguous_names.append({"name": entries[0]["name"],
                                    "emails": sorted(addresses)})
            by_name.pop(key, None)          # never keep a guess
            continue
        rec = entries[0]
        existing = by_name.get(key)
        new_rec = {
            "email": rec["email"],
            "name": rec["name"],
            "department": rec["department"],
            "source_file": csv_path.name,
            "updated_at": _now_iso(),
        }
        if existing is None:
            stats["added"] += 1
        elif existing.get("email", "").lower() != rec["email"].lower():
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1
        by_name[key] = new_rec

    store.setdefault("sources", []).append({
        "filename": csv_path.name,
        "imported_at": _now_iso(),
        "stats": stats,
        "ambiguous_names": ambiguous_names,
    })
    return stats


def resolve_emails_by_name(path: Path = DEFAULT_STORE_PATH) -> dict[str, str]:
    """{normalised name: email}, ready for the Pass 8b backfill."""
    store = load_store(path)
    return {k: r["email"] for k, r in store.get("by_name", {}).items()
            if r.get("email")}


def _cli(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(argv) < 2:
        print("Usage: python -m src.faculty_emails <directory-export.csv> [...]")
        return 2
    store = load_store()
    for arg in argv[1:]:
        p = Path(arg)
        if not p.exists():
            print(f"  x Missing: {p}")
            return 1
        print(f"  Importing {p.name}...")
        try:
            s = import_csv(p, store)
        except Exception as e:
            # Same rule as the keywords importer: a partial merge is never
            # persisted, because a half-imported directory with no audit entry
            # is worse than no import.
            print(f"  x {p.name}: {type(e).__name__}: {e}")
            print("  ABORTED - the store on disk was NOT modified.")
            return 1
        print(f"    rows={s['rows_total']} usable={s['usable']} added={s['added']} "
              f"updated={s['updated']} unchanged={s['unchanged']} "
              f"ambiguous_skipped={s['ambiguous']}")
    save_store(store)
    print(f"\n[OK] Directory now holds {len(store.get('by_name', {}))} unique names")
    print(f"  -> {DEFAULT_STORE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
