"""
UMSOM Grant Matcher — Daily Diagnostic Reporter
================================================
Run this script after each nightly matching run to generate a structured
daily diagnostic report.  The report is saved as both a JSON file (for
programmatic analysis) and a human-readable Markdown summary.

Usage:
    python grant_matcher_diagnostics.py [--date YYYY-MM-DD] [--log-dir ./logs] [--db-path ./matcher.db]

The script can pull data from three sources (use whichever apply to your stack):
  1. Structured JSON run logs  (preferred)
  2. A SQLite / Postgres results database
  3. Plain-text log files (regex-parsed as fallback)

All sections degrade gracefully — if a data source is missing, that section
is skipped and noted in the report.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config — edit these defaults to match your Railway environment
# ---------------------------------------------------------------------------
DEFAULT_LOG_DIR   = os.getenv("MATCHER_LOG_DIR",  "./logs")
DEFAULT_DB_PATH   = os.getenv("MATCHER_DB_PATH",  "./matcher.db")
DEFAULT_OUTPUT_DIR = os.getenv("MATCHER_REPORT_DIR", "./reports")

# Thresholds used in v4.0 — update if you change them in the main system
SEMANTIC_THRESHOLD_EXPECTED  = 0.72   # minimum cosine similarity
CONFIDENCE_THRESHOLD_EXPECTED = 0.65  # minimum confidence score
IDF_NORM_EXPECTED            = True   # IDF normalisation should be on

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json_log(log_dir: str, target_date: date) -> dict | None:
    """Look for a structured run log named YYYY-MM-DD.json or run_YYYY-MM-DD.json."""
    for pattern in (f"{target_date}.json", f"run_{target_date}.json", f"match_{target_date}.json"):
        p = Path(log_dir) / pattern
        if p.exists():
            with open(p) as f:
                return json.load(f)
    return None


def load_db_results(db_path: str, target_date: date) -> list[dict]:
    """Pull match results from SQLite for the target date."""
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Adjust table/column names to your schema
        cur.execute("""
            SELECT faculty_id, faculty_name, grant_id, grant_title, source,
                   semantic_score, confidence_score, matched_keywords,
                   created_at
            FROM matches
            WHERE DATE(created_at) = ?
        """, (str(target_date),))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"  [WARN] DB query failed: {e}", file=sys.stderr)
        return []


def parse_text_logs(log_dir: str, target_date: date) -> dict:
    """
    Regex-based fallback parser for plain-text Railway logs.
    Looks for lines matching common patterns in the matcher output.
    """
    results = {
        "total_faculty_processed": None,
        "total_grants_scraped": None,
        "total_matches_raw": None,
        "total_matches_after_filter": None,
        "scraper_errors": [],
        "filter_drop_reasons": Counter(),
        "raw_log_lines": [],
    }

    log_files = sorted(Path(log_dir).glob(f"*{target_date}*.log")) + \
                sorted(Path(log_dir).glob("*.log"))  # fallback: latest log

    if not log_files:
        return results

    log_file = log_files[0]  # use most specific match
    with open(log_file, errors="replace") as f:
        lines = f.readlines()

    for line in lines:
        results["raw_log_lines"].append(line.rstrip())

        # Tune these patterns to your actual log format
        if m := re.search(r"Processing (\d+) faculty", line, re.I):
            results["total_faculty_processed"] = int(m.group(1))
        if m := re.search(r"Scraped (\d+) grants?", line, re.I):
            results["total_grants_scraped"] = int(m.group(1))
        if m := re.search(r"(\d+) raw matches?", line, re.I):
            results["total_matches_raw"] = int(m.group(1))
        if m := re.search(r"(\d+) matches? after filter", line, re.I):
            results["total_matches_after_filter"] = int(m.group(1))
        if re.search(r"(error|exception|failed)", line, re.I):
            results["scraper_errors"].append(line.rstrip())
        if m := re.search(r"dropped.*?:\s+(.+)", line, re.I):
            results["filter_drop_reasons"][m.group(1).strip()] += 1
        if m := re.search(r"below.*?threshold", line, re.I):
            results["filter_drop_reasons"]["below_threshold"] += 1

    return results


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def analyse_matches(matches: list[dict]) -> dict:
    """Compute per-source, per-faculty, and score-distribution stats."""
    if not matches:
        return {}

    semantic_scores   = [m.get("semantic_score", 0)   for m in matches if m.get("semantic_score")   is not None]
    confidence_scores = [m.get("confidence_score", 0) for m in matches if m.get("confidence_score") is not None]

    def _stats(vals):
        if not vals:
            return {}
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        return {
            "count": n,
            "min":   round(min(vals_sorted), 4),
            "max":   round(max(vals_sorted), 4),
            "mean":  round(sum(vals_sorted) / n, 4),
            "p25":   round(vals_sorted[n // 4], 4),
            "p50":   round(vals_sorted[n // 2], 4),
            "p75":   round(vals_sorted[3 * n // 4], 4),
        }

    by_source  = Counter(m.get("source", "unknown") for m in matches)
    by_faculty = Counter(m.get("faculty_id", m.get("faculty_name", "?")) for m in matches)

    # Matches per faculty histogram (how many faculty got N matches)
    match_counts = list(by_faculty.values())
    faculty_histogram = Counter()
    for c in match_counts:
        bucket = f"{(c // 5) * 5}-{(c // 5) * 5 + 4}"
        faculty_histogram[bucket] += 1

    # Keyword frequency across matches
    keyword_counter: Counter = Counter()
    for m in matches:
        kws = m.get("matched_keywords", "")
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.split(",") if k.strip()]
        if isinstance(kws, list):
            keyword_counter.update(kws)

    # Flag matches that look suspicious (navigation pages, generic terms)
    suspicious_titles = []
    nav_patterns = re.compile(
        r"\b(home|menu|navigation|search results|page \d|login|sign in|about us|contact)\b",
        re.I,
    )
    for m in matches:
        title = m.get("grant_title", "")
        if nav_patterns.search(title):
            suspicious_titles.append({"grant_id": m.get("grant_id"), "title": title})

    # Below-threshold counts (sanity check that filters are working)
    below_semantic    = sum(1 for s in semantic_scores   if s < SEMANTIC_THRESHOLD_EXPECTED)
    below_confidence  = sum(1 for s in confidence_scores if s < CONFIDENCE_THRESHOLD_EXPECTED)

    return {
        "total_matches":         len(matches),
        "unique_faculty_matched": len(by_faculty),
        "unique_grants_matched": len(set(m.get("grant_id") for m in matches)),
        "by_source":             dict(by_source),
        "semantic_score_stats":  _stats(semantic_scores),
        "confidence_score_stats": _stats(confidence_scores),
        "faculty_match_histogram": dict(faculty_histogram),
        "top_20_faculty_by_match_count": by_faculty.most_common(20),
        "top_30_keywords": keyword_counter.most_common(30),
        "suspicious_navigation_matches": suspicious_titles,
        "threshold_violations": {
            "below_semantic_threshold":    below_semantic,
            "below_confidence_threshold":  below_confidence,
            "note": "Non-zero counts indicate filters may not be applied correctly",
        },
    }


def check_config_drift(json_log: dict | None) -> list[str]:
    """
    Compare the run config captured in the log against expected v4.0 values.
    Returns a list of warnings.
    """
    warnings = []
    if not json_log:
        warnings.append("No structured run log found — cannot verify runtime config.")
        return warnings

    cfg = json_log.get("config", json_log.get("settings", {}))
    if not cfg:
        warnings.append("Run log found but contains no 'config' / 'settings' section.")
        return warnings

    sem = cfg.get("semantic_threshold")
    if sem is not None and sem < SEMANTIC_THRESHOLD_EXPECTED:
        warnings.append(
            f"semantic_threshold is {sem} — below expected {SEMANTIC_THRESHOLD_EXPECTED}. "
            "Older (v3) code may be deployed!"
        )

    conf = cfg.get("confidence_threshold", cfg.get("min_confidence"))
    if conf is not None and conf < CONFIDENCE_THRESHOLD_EXPECTED:
        warnings.append(
            f"confidence_threshold is {conf} — below expected {CONFIDENCE_THRESHOLD_EXPECTED}."
        )

    idf = cfg.get("idf_normalisation", cfg.get("idf_normalization", cfg.get("use_idf")))
    if idf is False:
        warnings.append("IDF normalisation is DISABLED — expected True for v4.0.")

    bio_filter = cfg.get("biomedical_relevance_filter", cfg.get("bio_filter"))
    if bio_filter is False:
        warnings.append("Biomedical relevance pre-filter is DISABLED.")

    active_filter = cfg.get("active_faculty_filter", cfg.get("active_only"))
    if active_filter is False:
        warnings.append("Active-faculty filter is DISABLED — inactive faculty may be matched.")

    if not warnings:
        warnings.append("✓ All checked config parameters match v4.0 expectations.")

    return warnings


def scraper_health(json_log: dict | None, text_log: dict) -> dict:
    """Summarise scraper run health."""
    health: dict[str, Any] = {}

    if json_log:
        scrapers = json_log.get("scrapers", {})
        for name, info in scrapers.items():
            health[name] = {
                "grants_found":   info.get("grants_found", "N/A"),
                "pages_scraped":  info.get("pages_scraped", "N/A"),
                "errors":         info.get("errors", []),
                "nav_pages_dropped": info.get("nav_pages_dropped", "N/A"),
                "duration_s":     info.get("duration_s", "N/A"),
            }

    # Merge errors from text log fallback
    if text_log.get("scraper_errors"):
        health.setdefault("text_log_errors", {})["errors"] = text_log["scraper_errors"][:20]

    return health


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_markdown(report: dict) -> str:
    d = report
    lines = [
        f"# UMSOM Grant Matcher — Daily Diagnostic Report",
        f"**Run date:** {d['run_date']}  |  **Generated:** {d['generated_at']}",
        "",
        "---",
        "",
        "## 1. Pipeline Overview",
        "",
    ]

    overview = d.get("pipeline_overview", {})
    for k, v in overview.items():
        lines.append(f"- **{k.replace('_', ' ').title()}:** {v}")

    lines += ["", "---", "", "## 2. Config Validation", ""]
    for w in d.get("config_warnings", []):
        prefix = "✅" if w.startswith("✓") else "⚠️"
        lines.append(f"{prefix} {w}")

    lines += ["", "---", "", "## 3. Match Quality Metrics", ""]
    mq = d.get("match_quality", {})
    if mq:
        lines += [
            f"- **Total matches:** {mq.get('total_matches', 'N/A')}",
            f"- **Unique faculty matched:** {mq.get('unique_faculty_matched', 'N/A')}",
            f"- **Unique grants matched:** {mq.get('unique_grants_matched', 'N/A')}",
            "",
            "### Matches by Source",
        ]
        for src, cnt in sorted(mq.get("by_source", {}).items(), key=lambda x: -x[1]):
            lines.append(f"  - `{src}`: {cnt}")

        lines += ["", "### Semantic Score Distribution"]
        ss = mq.get("semantic_score_stats", {})
        if ss:
            lines.append(f"  min={ss.get('min')}  p25={ss.get('p25')}  p50={ss.get('p50')}  "
                         f"p75={ss.get('p75')}  max={ss.get('max')}  mean={ss.get('mean')}")

        lines += ["", "### Confidence Score Distribution"]
        cs = mq.get("confidence_score_stats", {})
        if cs:
            lines.append(f"  min={cs.get('min')}  p25={cs.get('p25')}  p50={cs.get('p50')}  "
                         f"p75={cs.get('p75')}  max={cs.get('max')}  mean={cs.get('mean')}")

        tv = mq.get("threshold_violations", {})
        lines += [
            "",
            "### Threshold Violations (should be 0 if filters are working)",
            f"  - Below semantic threshold:    {tv.get('below_semantic_threshold', 'N/A')}",
            f"  - Below confidence threshold:  {tv.get('below_confidence_threshold', 'N/A')}",
        ]

        suspicious = mq.get("suspicious_navigation_matches", [])
        lines += ["", f"### Suspicious Navigation-Page Matches: {len(suspicious)}"]
        for item in suspicious[:10]:
            lines.append(f"  - `{item['grant_id']}`: {item['title']}")
        if len(suspicious) > 10:
            lines.append(f"  - … and {len(suspicious) - 10} more")

    lines += ["", "---", "", "## 4. Faculty Match Distribution", ""]
    hist = mq.get("faculty_match_histogram", {})
    if hist:
        lines.append("Matches-per-faculty bucket → faculty count:")
        for bucket, cnt in sorted(hist.items()):
            lines.append(f"  - {bucket} matches: {cnt} faculty")

    lines += ["", "### Top 20 Faculty by Match Count"]
    for fid, cnt in mq.get("top_20_faculty_by_match_count", []):
        lines.append(f"  - `{fid}`: {cnt} matches")

    lines += ["", "---", "", "## 5. Top Matched Keywords", ""]
    for kw, cnt in mq.get("top_30_keywords", []):
        lines.append(f"  - `{kw}`: {cnt}")

    lines += ["", "---", "", "## 6. Scraper Health", ""]
    sh = d.get("scraper_health", {})
    if sh:
        for name, info in sh.items():
            lines += [
                f"### {name}",
                f"  - Grants found:       {info.get('grants_found', 'N/A')}",
                f"  - Pages scraped:      {info.get('pages_scraped', 'N/A')}",
                f"  - Nav pages dropped:  {info.get('nav_pages_dropped', 'N/A')}",
                f"  - Duration (s):       {info.get('duration_s', 'N/A')}",
            ]
            errs = info.get("errors", [])
            if errs:
                lines.append(f"  - **Errors ({len(errs)}):**")
                for e in errs[:5]:
                    lines.append(f"    - {e}")
    else:
        lines.append("_No scraper data available._")

    lines += ["", "---", "", "## 7. Action Items", ""]
    for i, item in enumerate(d.get("action_items", []), 1):
        lines.append(f"{i}. {item}")

    return "\n".join(lines)


def generate_action_items(report: dict) -> list[str]:
    items = []
    mq    = report.get("match_quality", {})
    cw    = report.get("config_warnings", [])
    sh    = report.get("scraper_health", {})

    # Config drift
    for w in cw:
        if "⚠️" in w or (not w.startswith("✓") and "below" in w.lower()):
            items.append(f"[CONFIG] {w}")

    # Volume anomaly
    total = mq.get("total_matches", 0)
    if total > 500:
        items.append(
            f"[VOLUME] {total} matches is unusually high — review semantic/confidence thresholds "
            "and check whether v4.0 code is deployed."
        )
    elif total == 0:
        items.append("[VOLUME] Zero matches produced — check scraper health and pipeline errors.")

    # Threshold violations
    tv = mq.get("threshold_violations", {})
    if tv.get("below_semantic_threshold", 0) > 0:
        items.append(
            f"[FILTER] {tv['below_semantic_threshold']} matches are below the semantic threshold "
            "— filters may not be applied post-matching."
        )
    if tv.get("below_confidence_threshold", 0) > 0:
        items.append(
            f"[FILTER] {tv['below_confidence_threshold']} matches are below the confidence threshold."
        )

    # Navigation page contamination
    suspicious = mq.get("suspicious_navigation_matches", [])
    if suspicious:
        items.append(
            f"[SCRAPER] {len(suspicious)} matches appear to be navigation/UI pages — "
            "tighten ARPA-H scraper URL/title filters."
        )

    # Scraper errors
    for src, info in sh.items():
        errs = info.get("errors", [])
        if errs:
            items.append(f"[SCRAPER] {src} reported {len(errs)} error(s) — investigate.")

    # Faculty concentration
    top = mq.get("top_20_faculty_by_match_count", [])
    if top and top[0][1] > 50:
        items.append(
            f"[QUALITY] Top faculty `{top[0][0]}` has {top[0][1]} matches — "
            "may indicate overly broad keyword matching for that profile."
        )

    if not items:
        items.append("No action items — run looks clean.")

    return items


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="UMSOM Grant Matcher Daily Diagnostics")
    parser.add_argument("--date",    default=str(date.today() - timedelta(days=1)),
                        help="Date to analyse (YYYY-MM-DD), default: yesterday")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date)
    output_dir  = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  UMSOM Grant Matcher — Diagnostics for {target_date}")
    print(f"{'='*60}\n")

    # 1. Load data sources
    print("→ Loading structured JSON run log …")
    json_log = load_json_log(args.log_dir, target_date)
    print(f"  {'Found' if json_log else 'Not found'}")

    print("→ Loading match results from DB …")
    db_matches = load_db_results(args.db_path, target_date)
    print(f"  {len(db_matches)} rows loaded")

    print("→ Parsing plain-text logs (fallback) …")
    text_log = parse_text_logs(args.log_dir, target_date)
    print(f"  {len(text_log.get('raw_log_lines', []))} log lines read")

    # 2. Choose best match source for analysis
    matches = db_matches or json_log.get("matches", []) if json_log else []

    # 3. Build report
    print("\n→ Analysing matches …")
    match_quality = analyse_matches(matches)

    print("→ Checking config …")
    config_warnings = check_config_drift(json_log)

    print("→ Assessing scraper health …")
    sh = scraper_health(json_log, text_log)

    pipeline_overview = {
        "faculty_processed":    (json_log or {}).get("faculty_processed") or text_log.get("total_faculty_processed", "N/A"),
        "grants_scraped":       (json_log or {}).get("grants_scraped")    or text_log.get("total_grants_scraped", "N/A"),
        "raw_matches":          (json_log or {}).get("raw_matches")        or text_log.get("total_matches_raw", "N/A"),
        "matches_after_filter": (json_log or {}).get("matches_after_filter") or text_log.get("total_matches_after_filter", len(matches) or "N/A"),
        "run_duration_s":       (json_log or {}).get("run_duration_s", "N/A"),
        "data_source_used":     "database" if db_matches else ("json_log" if json_log else "text_log_fallback"),
    }

    report = {
        "run_date":          str(target_date),
        "generated_at":      ts(),
        "pipeline_overview": pipeline_overview,
        "config_warnings":   config_warnings,
        "match_quality":     match_quality,
        "scraper_health":    sh,
    }

    report["action_items"] = generate_action_items(report)

    # 4. Write JSON report
    json_path = output_dir / f"diagnostic_{target_date}.json"
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n✅ JSON report → {json_path}")

    # 5. Write Markdown report
    md_path = output_dir / f"diagnostic_{target_date}.md"
    with open(md_path, "w") as f:
        f.write(render_markdown(report))
    print(f"✅ Markdown report → {md_path}")

    # 6. Print action items to stdout for Railway log visibility
    print("\n--- ACTION ITEMS ---")
    for item in report["action_items"]:
        print(f"  • {item}")
    print("--------------------\n")


if __name__ == "__main__":
    main()
