#!/usr/bin/env python3
"""
purge_reporter_matches.py
=========================
Removes all NIH RePORTER and Federal RePORTER entries from match_results.json
and resets run_stats.json match counts to reflect the cleaned data.

Run this once after disabling nih_reporter in config.yaml to clear today's
false matches from the dashboard.

Usage (on Railway via console, or locally against downloaded data files):
  python purge_reporter_matches.py
  python purge_reporter_matches.py --data-dir /path/to/data
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def is_reporter_entry(match: dict) -> bool:
    """Return True if this match came from NIH RePORTER or Federal RePORTER."""
    grant_id = str(match.get("grant_id", ""))
    source   = str(match.get("source", ""))
    link     = str(match.get("grant_link", ""))

    if grant_id.startswith("nih-reporter-"):
        return True
    if grant_id.startswith("fed-reporter-"):
        return True
    if source in ("NIH RePORTER", "Federal RePORTER"):
        return True
    if "reporter.nih.gov/project-details" in link:
        return True

    return False


def purge(data_dir: Path, dry_run: bool = False):
    matches_file = data_dir / "match_results.json"
    stats_file   = data_dir / "run_stats.json"

    # ── match_results.json ───────────────────────────────────────────────────
    if not matches_file.exists():
        print(f"ERROR: {matches_file} not found.")
        return

    with open(matches_file) as f:
        matches = json.load(f)

    total_before = len(matches)
    reporter_entries = [m for m in matches if is_reporter_entry(m)]
    clean_matches    = [m for m in matches if not is_reporter_entry(m)]
    total_after  = len(clean_matches)
    removed      = total_before - total_after

    print(f"match_results.json:")
    print(f"  Before : {total_before:,} entries")
    print(f"  Removed: {removed:,} RePORTER entries")
    print(f"  After  : {total_after:,} entries")

    if reporter_entries:
        sample_ids = list({m['grant_id'] for m in reporter_entries})[:5]
        print(f"  Sample removed grant IDs: {sample_ids}")

    if not dry_run:
        with open(matches_file, "w") as f:
            json.dump(clean_matches, f, indent=2)
        print(f"  ✓ Saved cleaned match_results.json")
    else:
        print(f"  [DRY RUN] No changes written.")

    print()

    # ── run_stats.json ───────────────────────────────────────────────────────
    if not stats_file.exists():
        print(f"WARNING: {stats_file} not found — skipping stats reset.")
        return

    with open(stats_file) as f:
        stats = json.load(f)

    last_run = stats.get("last_grants_run", {})
    old_total    = last_run.get("total_faculty_matches", 0)
    old_kw       = last_run.get("keyword_matches", 0)
    old_sem      = last_run.get("semantic_matches", 0)

    # Recount from the cleaned matches
    new_total = total_after
    new_kw    = sum(1 for m in clean_matches if m.get("match_type") in ("keyword", "both"))
    new_sem   = sum(1 for m in clean_matches if m.get("match_type") in ("semantic", "both"))

    # Unique grants in cleaned set
    new_grants_with_matches = len({m["grant_id"] for m in clean_matches})

    print(f"run_stats.json (last_grants_run):")
    print(f"  total_faculty_matches : {old_total:,} → {new_total:,}")
    print(f"  keyword_matches       : {old_kw:,} → {new_kw:,}")
    print(f"  semantic_matches      : {old_sem:,} → {new_sem:,}")
    print(f"  grants_with_matches   : {last_run.get('grants_with_matches',0):,} → {new_grants_with_matches:,}")

    if not dry_run:
        stats["last_grants_run"].update({
            "total_faculty_matches": new_total,
            "keyword_matches":       new_kw,
            "semantic_matches":      new_sem,
            "grants_with_matches":   new_grants_with_matches,
            "purged_reporter_entries": removed,
            "purged_at": datetime.now(timezone.utc).isoformat(),
        })
        with open(stats_file, "w") as f:
            json.dump(stats, f, indent=2)
        print(f"  ✓ Saved updated run_stats.json")
    else:
        print(f"  [DRY RUN] No changes written.")

    print()
    print("Done. Reload the dashboard to see updated stats.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Purge NIH/Federal RePORTER matches from dashboard data.")
    parser.add_argument("--data-dir", default="data", help="Path to data directory (default: ./data)")
    parser.add_argument("--dry-run",  action="store_true", help="Preview changes without writing anything")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"ERROR: data directory '{data_dir}' does not exist.")
        exit(1)

    print(f"Purging RePORTER entries from: {data_dir.resolve()}")
    print(f"Dry run: {args.dry_run}")
    print()

    purge(data_dir, dry_run=args.dry_run)
