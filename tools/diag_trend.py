#!/usr/bin/env python3
"""
diag_trend.py — turn the daily diagnostic archive into a tuning evidence table.

The matcher writes one grant_matcher_diagnostic_YYYY-MM-DD.json per run, each
carrying the tuning parameters in force that day plus the resulting counts.
Read as a series, that archive answers the question a tuning log needs:
*did the change actually move the numbers?*

Usage
-----
  python tools/diag_trend.py <diagnostics-dir>
  python tools/diag_trend.py <dir> --since 2026-07-01
  python tools/diag_trend.py <dir> --params          # parameter changes only
  python tools/diag_trend.py <dir> --csv > trend.csv

Reading the output
------------------
  checked   grants that passed fetch and reached the relevance filter
  skipped   dropped by the relevance filter as irrelevant
  matched   grants that produced at least one delivered match
  raw       candidate faculty-grant pairs before the confidence floor
  kept      pairs surviving every filter — what faculty actually receive
  keep%     kept/raw. A sharp move here is usually a threshold change,
            not a change in the underlying grants.
  kw/sem/both  delivered pairs by match type. `sem` collapsing toward zero
            means the semantic channel has been gated off, which has
            happened before (see TUNING_LOG 2026-05-28).

Days with no grants are skipped: the matcher omits `params` entirely on a
zero-grant run, which would otherwise read as every parameter being unset.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

FIELDS = [
    ("faculty_count", "faculty"),
    ("grants_checked", "checked"),
    ("grants_skipped_irrelevant", "skipped"),
    ("grants_matched", "matched"),
    ("raw_matches", "raw"),
    ("matches_after_filter", "kept"),
    ("keyword_only", "kw"),
    ("semantic_only", "sem"),
    ("both", "both"),
    ("suppressed_by_confidence", "supp"),
]

PARAM_ORDER = [
    "min_confidence", "semantic_threshold", "min_idf_for_match",
    "max_matches_per_grant", "max_kw_prevalence_pct", "semantic_enabled",
]


def load_runs(directory, since=None):
    runs = []
    for path in sorted(Path(directory).glob("grant_matcher_diagnostic_*.json")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
        if not m:
            continue
        date = m.group(1)
        if since and date < since:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ! skipping {path.name}: {e}", file=sys.stderr)
            continue
        diag = data.get("matcher_diagnostic", {})
        params = diag.get("params") or {}
        summary = diag.get("summary") or {}
        # A zero-grant run records no params; carrying it through would show
        # every knob flipping to None and back.
        if not params:
            continue
        runs.append({"date": date, "file": path.name,
                     "params": params, "summary": summary})
    runs.sort(key=lambda r: (r["date"], r["file"]))
    return runs


def param_changes(runs):
    """
    {date: {param: (old, new)}} for every DATE where a parameter moved.

    Compared date-to-date, not run-to-run. A day can hold several runs — a
    re-run lands as ...-b.json — and an ad-hoc re-run under different settings
    would otherwise show up as two spurious flips (a change and its reversal)
    on the same day. The last run of each date is treated as that day's
    settled configuration.
    """
    by_date = {}
    for run in runs:
        by_date[run["date"]] = run["params"]

    changes, prev = {}, None
    for date in sorted(by_date):
        cur = by_date[date]
        if prev is None:
            changes[date] = {k: (None, v) for k, v in cur.items()}
        else:
            delta = {k: (prev.get(k), v) for k, v in cur.items() if prev.get(k) != v}
            if delta:
                changes[date] = delta
        prev = cur
    return changes


def fmt_params(delta):
    parts = []
    for key in PARAM_ORDER:
        if key in delta:
            old, new = delta[key]
            parts.append(f"{key} {old}->{new}" if old is not None else f"{key}={new}")
    for key in sorted(set(delta) - set(PARAM_ORDER)):
        old, new = delta[key]
        parts.append(f"{key} {old}->{new}" if old is not None else f"{key}={new}")
    return ", ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", help="folder holding grant_matcher_diagnostic_*.json")
    ap.add_argument("--since", help="only runs on/after this YYYY-MM-DD")
    ap.add_argument("--params", action="store_true", help="show parameter changes only")
    ap.add_argument("--csv", action="store_true", help="emit CSV on stdout")
    args = ap.parse_args()

    runs = load_runs(args.directory, args.since)
    if not runs:
        print("No diagnostics with recorded parameters found.", file=sys.stderr)
        return 1

    changes = param_changes(runs)

    if args.params:
        print(f"Parameter changes across {len(runs)} runs "
              f"({runs[0]['date']} to {runs[-1]['date']}):\n")
        for date, delta in sorted(changes.items()):
            print(f"  {date}  {fmt_params(delta)}")
        return 0

    if args.csv:
        w = csv.writer(sys.stdout)
        w.writerow(["date"] + [label for _, label in FIELDS]
                   + ["keep_pct"] + PARAM_ORDER + ["param_change"])
        for run in runs:
            s, p = run["summary"], run["params"]
            raw, kept = s.get("raw_matches") or 0, s.get("matches_after_filter") or 0
            w.writerow([run["date"]] + [s.get(k, "") for k, _ in FIELDS]
                       + [f"{100 * kept / raw:.1f}" if raw else ""]
                       + [p.get(k, "") for k in PARAM_ORDER]
                       + [fmt_params(changes[run["date"]]) if run["date"] in changes else ""])
        return 0

    printed_change = {}
    head = f"{'date':11}" + "".join(f"{label:>8}" for _, label in FIELDS) + f"{'keep%':>7}"
    print(head)
    print("-" * len(head))
    for run in runs:
        s = run["summary"]
        raw, kept = s.get("raw_matches") or 0, s.get("matches_after_filter") or 0
        row = f"{run['date']:11}" + "".join(
            f"{str(s.get(k, '-')):>8}" for k, _ in FIELDS)
        row += f"{(100 * kept / raw):>6.1f}%" if raw else f"{'-':>7}"
        if run["file"] != f"grant_matcher_diagnostic_{run['date']}.json":
            row += f"   (re-run: {run['file'].replace('grant_matcher_diagnostic_','')})"
        print(row)
        if run["date"] in changes and not printed_change.get(run["date"]):
            printed_change[run["date"]] = True
            print(f"            >> {fmt_params(changes[run['date']])}")
    print(f"\n{len(runs)} runs with recorded parameters. "
          f"'>>' marks a tuning change — compare the rows on either side.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
