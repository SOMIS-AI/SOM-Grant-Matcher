"""
UMSOM Grant Matcher — Enhanced Web Dashboard v3
================================================
6-tab analytics dashboard with charts, match explanations,
pipeline status, department analytics, and deep faculty insights.
"""

import json
import os
import re
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, session, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

DATA_DIR  = Path(os.environ.get("DATA_DIR",  "data"))
LOG_FILE  = Path(os.environ.get("LOG_FILE",  "logs/grant_matcher.log"))
CFG_FILE  = Path(os.environ.get("CONFIG_FILE","config/config.yaml"))

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "changeme")
APP_ENV        = os.environ.get("APP_ENV", "production")

# ── Auth ──────────────────────────────────────────────────────────────────────

def check_auth(u, p):
    return (secrets.compare_digest(u, DASHBOARD_USER) and
            secrets.compare_digest(p, DASHBOARD_PASS))

def login_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return decorated

# ── Data helpers ──────────────────────────────────────────────────────────────

def load_json(path, default=None):
    try:
        if Path(path).exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}

def get_faculty():
    data = load_json(DATA_DIR / "faculty_profiles.json", {})
    return data.get("faculty", []) if isinstance(data, dict) else (data or [])

def get_matches():
    return load_json(DATA_DIR / "match_results.json", [])

def get_stats():
    return load_json(DATA_DIR / "run_stats.json", {})

def get_log_tail(n=500):
    try:
        if not LOG_FILE.exists():
            return []
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []

def time_ago(iso):
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso.replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
        if s < 60:    return f"{s}s ago"
        if s < 3600:  return f"{s//60}m ago"
        if s < 86400: return f"{s//3600}h ago"
        return f"{s//86400}d ago"
    except Exception:
        return iso

# ── API: Overview/Stats ───────────────────────────────────────────────────────

@app.route("/api/stats")
@login_required
def api_stats():
    stats   = get_stats()
    faculty = get_faculty()
    matches = get_matches()
    logs    = get_log_tail(500)
    errors  = [l for l in logs if "[ERROR]" in l or "[WARNING]" in l]

    scrape     = stats.get("last_scrape", {})
    grants_run = stats.get("last_grants_run", {})

    # Keyword coverage
    with_kw   = sum(1 for f in faculty if f.get("keywords"))
    total_fac = len(faculty)
    kw_pct    = round(with_kw / total_fac * 100, 1) if total_fac else 0

    # Embedding coverage
    with_emb = sum(1 for f in faculty if f.get("embedding"))
    emb_pct  = round(with_emb / total_fac * 100, 1) if total_fac else 0

    # Match type breakdown
    kw_matches  = sum(1 for m in matches if m.get("match_type") in ("keyword","both"))
    sem_matches = sum(1 for m in matches if m.get("match_type") in ("semantic","both"))
    both_matches= sum(1 for m in matches if m.get("match_type") == "both")

    # Matches by day (last 14 days)
    today = datetime.now(timezone.utc).date()
    day_counts = defaultdict(lambda: {"keyword":0,"semantic":0,"both":0})
    for m in matches:
        try:
            d = datetime.fromisoformat(m["timestamp"].replace("Z","+00:00")).date()
            mt = m.get("match_type","keyword")
            day_counts[d.isoformat()][mt] += 1
        except Exception:
            pass
    trend = []
    for i in range(13, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        trend.append({"date": d, **day_counts[d]})

    # Top agencies
    agency_ctr = Counter(m.get("grant_agency","Unknown") for m in matches)
    top_agencies = [{"agency": a, "count": c} for a,c in agency_ctr.most_common(8)]

    # Dept keyword coverage
    dept_stats = defaultdict(lambda: {"total":0,"with_kw":0})
    for f in faculty:
        d = f.get("department","Unknown") or "Unknown"
        dept_stats[d]["total"] += 1
        if f.get("keywords"):
            dept_stats[d]["with_kw"] += 1
    dept_coverage = sorted([
        {"dept": d, "total": v["total"], "with_kw": v["with_kw"],
         "pct": round(v["with_kw"]/v["total"]*100) if v["total"] else 0}
        for d,v in dept_stats.items()
    ], key=lambda x: -x["total"])[:15]

    # Award ceiling distribution
    ceilings = []
    for m in matches:
        try:
            c = int(str(m.get("grant_award_ceiling","0")).replace(",","").replace("$",""))
            if c > 0:
                ceilings.append(c)
        except Exception:
            pass
    buckets = {"<100K":0,"100K-500K":0,"500K-1M":0,"1M-5M":0,">5M":0}
    for c in ceilings:
        if c < 100000:       buckets["<100K"] += 1
        elif c < 500000:     buckets["100K-500K"] += 1
        elif c < 1000000:    buckets["500K-1M"] += 1
        elif c < 5000000:    buckets["1M-5M"] += 1
        else:                buckets[">5M"] += 1

    return jsonify({
        "scrape":    {**scrape,     "time_ago": time_ago(scrape.get("timestamp"))},
        "grants_run":{**grants_run, "time_ago": time_ago(grants_run.get("timestamp"))},
        "total_faculty":        total_fac,
        "faculty_with_keywords":with_kw,
        "keyword_coverage_pct": kw_pct,
        "faculty_with_embeddings": with_emb,
        "embedding_coverage_pct":  emb_pct,
        # Use run_stats.json as authoritative count — match_results.json is a capped rolling window
        "total_matches_logged": grants_run.get("total_faculty_matches", len(matches)),
        "keyword_matches":      grants_run.get("keyword_matches",  kw_matches),
        "semantic_matches":     grants_run.get("semantic_matches", sem_matches),
        "both_matches":         both_matches,
        "avg_confidence":       grants_run.get("avg_confidence", 0),
        "high_confidence_matches": grants_run.get("high_confidence_matches", 0),
        "match_trend":          trend,
        "top_agencies":         top_agencies,
        "dept_coverage":        dept_coverage,
        "award_buckets":        [{"label":k,"count":v} for k,v in buckets.items()],
        "recent_errors":        errors[-20:],
        "error_count":          len(errors),
    })

# ── API: Analytics ────────────────────────────────────────────────────────────

@app.route("/api/analytics")
@login_required
def api_analytics():
    matches = get_matches()
    faculty = get_faculty()

    # Top matched faculty
    fac_ctr = Counter(m.get("faculty_name") for m in matches)
    top_faculty = [{"name": n, "count": c} for n,c in fac_ctr.most_common(15)]

    # Top matched departments
    dept_ctr = Counter(m.get("faculty_department","Unknown") for m in matches)
    top_depts = [{"dept": d or "Unknown", "count": c}
                 for d,c in dept_ctr.most_common(10)]

    # Top keywords across all matches
    kw_ctr = Counter()
    for m in matches:
        for kw in m.get("matched_keywords", []):
            kw_ctr[kw.lower()] += 1
    top_keywords = [{"keyword": k, "count": c} for k,c in kw_ctr.most_common(20)]

    # Keyword source breakdown across faculty
    source_ctr = Counter()
    for f in faculty:
        sources = f.get("keyword_sources") or []
        for s in sources:
            base = s.split("(")[0].strip()
            source_ctr[base] += 1
    source_breakdown = [{"source": s, "count": c}
                        for s,c in source_ctr.most_common()]

    # Avg keywords per faculty by dept
    dept_kw = defaultdict(list)
    for f in faculty:
        d = f.get("department","Unknown") or "Unknown"
        dept_kw[d].append(len(f.get("keywords") or []))
    dept_avg_kw = sorted([
        {"dept": d, "avg_keywords": round(sum(v)/len(v),1), "faculty_count": len(v)}
        for d,v in dept_kw.items() if v
    ], key=lambda x: -x["avg_keywords"])[:12]

    # Confidence score distribution (0-100 in buckets of 10)
    conf_buckets_raw = Counter(min(m.get("confidence_score", m.get("match_score",0)*8), 99) // 10 * 10 for m in matches)
    score_chart = [{"score": f"{i}-{i+9}%", "count": conf_buckets_raw.get(i,0)} for i in range(0,100,10)]

    # Semantic similarity distribution (for semantic/both matches)
    sim_scores = [m.get("similarity_score",0) for m in matches
                  if m.get("match_type") in ("semantic","both") and m.get("similarity_score")]
    sim_buckets = {"0.55-0.60":0,"0.60-0.65":0,"0.65-0.70":0,"0.70-0.75":0,"0.75-0.80":0,"0.80+":0}
    for s in sim_scores:
        if   s < 0.60: sim_buckets["0.55-0.60"] += 1
        elif s < 0.65: sim_buckets["0.60-0.65"] += 1
        elif s < 0.70: sim_buckets["0.65-0.70"] += 1
        elif s < 0.75: sim_buckets["0.70-0.75"] += 1
        elif s < 0.80: sim_buckets["0.75-0.80"] += 1
        else:          sim_buckets["0.80+"]      += 1
    sim_chart = [{"range": k, "count": v} for k,v in sim_buckets.items()]

    # Grant open/close timeline (upcoming closes)
    upcoming = []
    seen_grants = set()
    today = datetime.now(timezone.utc).date()
    for m in matches:
        gid = m.get("grant_id","")
        if gid in seen_grants:
            continue
        seen_grants.add(gid)
        try:
            close = m.get("grant_close_date","")
            if close:
                cd = datetime.strptime(close[:10], "%Y-%m-%d").date()
                days_left = (cd - today).days
                if -7 <= days_left <= 90:
                    upcoming.append({
                        "title":      m.get("grant_title","")[:60],
                        "agency":     m.get("grant_agency",""),
                        "close_date": close[:10],
                        "days_left":  days_left,
                        "faculty_count": sum(1 for x in matches if x.get("grant_id")==gid),
                    })
        except Exception:
            pass
    upcoming.sort(key=lambda x: x["days_left"])

    return jsonify({
        "top_faculty":       top_faculty,
        "top_departments":   top_depts,
        "top_keywords":      top_keywords,
        "source_breakdown":  source_breakdown,
        "dept_avg_keywords": dept_avg_kw,
        "score_distribution":score_chart,
        "sim_distribution":  sim_chart,
        "upcoming_deadlines":upcoming[:10],
    })

# ── API: Faculty ──────────────────────────────────────────────────────────────

@app.route("/api/faculty")
@login_required
def api_faculty():
    faculty  = get_faculty()
    matches  = get_matches()
    search   = request.args.get("search","").lower().strip()
    dept     = request.args.get("dept","").strip()
    kw_filt  = request.args.get("keywords","").lower().strip()
    src_filt = request.args.get("source","").lower().strip()
    sort_by  = request.args.get("sort","name")
    page     = int(request.args.get("page",1))
    per_page = int(request.args.get("per_page",50))

    # Build match-count lookup
    match_ctr = Counter(m.get("faculty_name") for m in matches)

    filtered = faculty

    if search:
        filtered = [f for f in filtered
                    if search in (f.get("name","") or "").lower()
                    or search in (f.get("email","") or "").lower()]
    if dept:
        filtered = [f for f in filtered if dept in (f.get("department","") or "")]
    if kw_filt:
        filtered = [f for f in filtered
                    if any(kw_filt in k.lower() for k in (f.get("keywords") or []))]
    if src_filt:
        filtered = [f for f in filtered
                    if any(src_filt in (s or "").lower()
                           for s in (f.get("keyword_sources") or []))]

    # Sorting
    def sort_key(f):
        if sort_by == "keywords": return -len(f.get("keywords") or [])
        if sort_by == "matches":  return -match_ctr.get(f.get("name",""), 0)
        return (f.get("name") or "").lower()

    filtered.sort(key=sort_key)

    total = len(filtered)
    start = (page-1) * per_page
    page_items = filtered[start:start+per_page]

    # Attach match count
    enriched = []
    for f in page_items:
        item = {k:v for k,v in f.items() if k != "embedding"}
        item["match_count"] = match_ctr.get(f.get("name",""), 0)
        enriched.append(item)

    depts   = sorted(set(f.get("department","") for f in faculty if f.get("department")))
    sources = sorted(set(
        s.split("(")[0].strip()
        for f in faculty
        for s in (f.get("keyword_sources") or [])
    ))

    return jsonify({
        "total": total, "page": page, "per_page": per_page,
        "pages": (total+per_page-1)//per_page,
        "departments": depts,
        "sources": sources,
        "faculty": enriched,
    })

# ── API: Faculty detail ───────────────────────────────────────────────────────

@app.route("/api/faculty/detail")
@login_required
def api_faculty_detail():
    name    = request.args.get("name","").strip()
    faculty = get_faculty()
    matches = get_matches()

    person = next((f for f in faculty if f.get("name","").lower() == name.lower()), None)
    if not person:
        return jsonify({"error": "Not found"}), 404

    person_matches = [m for m in matches if m.get("faculty_name","").lower() == name.lower()]
    person_copy = {k:v for k,v in person.items() if k != "embedding"}
    person_copy["match_history"] = person_matches[:20]
    person_copy["match_count"]   = len(person_matches)

    return jsonify(person_copy)

# ── API: Matches ──────────────────────────────────────────────────────────────

@app.route("/api/matches")
@login_required
def api_matches():
    matches  = get_matches()
    search   = request.args.get("search","").lower().strip()
    mt_filt  = request.args.get("match_type","").lower().strip()
    ag_filt  = request.args.get("agency","").lower().strip()
    page     = int(request.args.get("page",1))
    per_page = int(request.args.get("per_page",20))

    if search:
        matches = [m for m in matches if
                   search in (m.get("grant_title","") or "").lower() or
                   search in (m.get("faculty_name","") or "").lower() or
                   any(search in k.lower() for k in (m.get("matched_keywords") or []))]
    if mt_filt:
        matches = [m for m in matches if m.get("match_type","") == mt_filt]
    if ag_filt:
        matches = [m for m in matches if ag_filt in (m.get("grant_agency","") or "").lower()]

    # Unique agencies for filter
    all_agencies = sorted(set(m.get("grant_agency","") for m in get_matches() if m.get("grant_agency")))

    total = len(matches)
    start = (page-1)*per_page
    page_items = matches[start:start+per_page]

    return jsonify({
        "total": total, "page": page, "per_page": per_page,
        "pages": (total+per_page-1)//per_page,
        "matches": page_items,
        "agencies": all_agencies,
    })

# ── API: Pipeline status ──────────────────────────────────────────────────────

@app.route("/api/pipeline")
@login_required
def api_pipeline():
    stats   = get_stats()
    faculty = get_faculty()
    logs    = get_log_tail(1000)

    scrape     = stats.get("last_scrape", {})
    grants_run = stats.get("last_grants_run", {})

    # Parse pass completion from logs
    passes = {
        "Pass 1": {"name":"UMSOM Dept. Pages",   "status":"unknown","count":None},
        "Pass 2": {"name":"UMSOM Profiles",       "status":"unknown","count":None},
        "Pass 3": {"name":"PubMed / NCBI",        "status":"unknown","count":None},
        "Pass 4": {"name":"NIH RePORTER",         "status":"unknown","count":None},
        "Pass 5": {"name":"ORCID",                "status":"unknown","count":None},
        "Pass 6": {"name":"Semantic Scholar",     "status":"unknown","count":None},
        "Pass 7": {"name":"Embeddings",           "status":"unknown","count":None},
    }
    for line in logs:
        for pk in passes:
            if pk + "/" in line or pk + ":" in line:
                if "complete" in line.lower() or "✓" in line:
                    passes[pk]["status"] = "complete"
                elif "error" in line.lower() or "failed" in line.lower():
                    passes[pk]["status"] = "error"
                elif "skipping" in line.lower():
                    passes[pk]["status"] = "skipped"
                else:
                    passes[pk]["status"] = "running"

    # Embedding coverage
    with_emb = sum(1 for f in faculty if f.get("embedding"))
    total    = len(faculty)

    # Next scheduled runs (approximate from last run + intervals)
    def next_run(ts, interval_h):
        if not ts:
            return "unknown"
        try:
            dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            nxt = dt + timedelta(hours=interval_h)
            delta = nxt - datetime.now(timezone.utc)
            s = int(delta.total_seconds())
            if s < 0:   return "overdue"
            if s < 60:  return f"{s}s"
            if s < 3600: return f"{s//60}m"
            return f"{s//3600}h {(s%3600)//60}m"
        except Exception:
            return "unknown"

    # Recent error summary
    errors = [l for l in logs if "[ERROR]" in l][-10:]
    warns  = [l for l in logs if "[WARNING]" in l][-10:]

    return jsonify({
        "last_scrape":       {**scrape,     "time_ago": time_ago(scrape.get("timestamp"))},
        "last_grants_run":   {**grants_run, "time_ago": time_ago(grants_run.get("timestamp"))},
        "next_scrape":       next_run(scrape.get("timestamp"), 168),
        "next_grants_check": next_run(grants_run.get("timestamp"), 24),
        "passes":            passes,
        "faculty_total":     total,
        "faculty_with_embeddings": with_emb,
        "embedding_pct":     round(with_emb/total*100,1) if total else 0,
        "recent_errors":     errors,
        "recent_warnings":   warns,
        "log_total_lines":   len(logs),
    })

# ── API: Logs ─────────────────────────────────────────────────────────────────

@app.route("/api/logs")
@login_required
def api_logs():
    level  = request.args.get("level","all")
    module = request.args.get("module","all")
    search = request.args.get("search","").lower()
    lines  = get_log_tail(1000)

    if level == "errors":
        lines = [l for l in lines if "[ERROR]" in l or "[WARNING]" in l]
    if module != "all":
        lines = [l for l in lines if f"] {module}:" in l]
    if search:
        lines = [l for l in lines if search in l.lower()]

    # Parse module list
    mods = sorted(set(
        m.group(1) for l in get_log_tail(200)
        if (m := re.search(r"\[(?:INFO|WARNING|ERROR|DEBUG)\] ([\w.]+):", l))
    ))

    return jsonify({"lines": lines[-200:], "modules": mods,
                    "total": len(lines)})


# ── API: CSV Exports ──────────────────────────────────────────────────────────

@app.route("/api/export/matches")
@login_required
def api_export_matches():
    import csv, io
    matches = get_matches()
    search  = request.args.get("search","").lower().strip()
    mt_filt = request.args.get("match_type","").lower().strip()
    if search:
        matches = [m for m in matches if
                   search in (m.get("grant_title","") or "").lower() or
                   search in (m.get("faculty_name","") or "").lower()]
    if mt_filt:
        matches = [m for m in matches if m.get("match_type","") == mt_filt]

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Timestamp","Grant Title","Agency","Grant Number","Close Date",
                "Award Ceiling","Faculty Name","Department","Email",
                "Match Type","Match Score","Confidence Score","Similarity Score","Matched Keywords","Grant Link"])
    for m in matches:
        w.writerow([
            m.get("timestamp",""),
            m.get("grant_title",""),
            m.get("grant_agency",""),
            m.get("grant_number",""),
            m.get("grant_close_date",""),
            m.get("grant_award_ceiling",""),
            m.get("faculty_name",""),
            m.get("faculty_department",""),
            m.get("faculty_email",""),
            m.get("match_type","keyword"),
            m.get("match_score",0),
            m.get("confidence_score", ""),
            m.get("similarity_score",""),
            "; ".join(m.get("matched_keywords") or []),
            m.get("grant_link",""),
        ])
    from flask import Response
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=grant_matches.csv"}
    )


@app.route("/api/export/faculty")
@login_required
def api_export_faculty():
    import csv, io
    faculty  = get_faculty()
    matches  = get_matches()
    from collections import Counter
    match_ctr = Counter(m.get("faculty_name") for m in matches)

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name","Department","Email","Profile URL","Keyword Count",
                "Match Count","Keyword Sources","Top Keywords"])
    for f in faculty:
        kws = f.get("keywords") or []
        srcs = list(set(s.split("(")[0].strip() for s in (f.get("keyword_sources") or [])))
        w.writerow([
            f.get("name",""),
            f.get("department",""),
            f.get("email",""),
            f.get("url","") or f.get("profile_url",""),
            len(kws),
            match_ctr.get(f.get("name",""), 0),
            "; ".join(srcs),
            "; ".join(kws[:20]),
        ])
    from flask import Response
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=faculty_profiles.csv"}
    )


# ── API: Grant Explorer (grant-centric deduplicated view) ──────────────────────

@app.route("/api/grants")
@login_required
def api_grants():
    """Grant-centric view: one entry per unique grant, aggregating all faculty matches."""
    matches  = get_matches()
    search   = request.args.get("search", "").lower().strip()
    ag_filt  = request.args.get("agency", "").lower().strip()
    mt_filt  = request.args.get("match_type", "").lower().strip()
    sort_by  = request.args.get("sort", "recent")
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 15))

    # Deduplicate by grant_id
    grants_map = {}
    for m in matches:
        gid = m.get("grant_id", "")
        if not gid:
            continue
        if gid not in grants_map:
            grants_map[gid] = {
                "grant_id":           gid,
                "grant_title":        m.get("grant_title", ""),
                "grant_agency":       m.get("grant_agency", ""),
                "grant_number":       m.get("grant_number", ""),
                "grant_link":         m.get("grant_link", ""),
                "grant_close_date":   m.get("grant_close_date", ""),
                "grant_open_date":    m.get("grant_open_date", ""),
                "grant_award_ceiling":m.get("grant_award_ceiling", ""),
                "grant_synopsis":     m.get("grant_synopsis", ""),
                "first_seen":         m.get("timestamp", ""),
                "faculty_matches":    [],
                "match_types":        set(),
                "all_keywords":       set(),
                "max_score":          0,
                "max_similarity":     0.0,
            }
        g = grants_map[gid]
        g["faculty_matches"].append({
            "faculty_name":       m.get("faculty_name", ""),
            "faculty_department": m.get("faculty_department", ""),
            "faculty_email":      m.get("faculty_email", ""),
            "faculty_url":        m.get("faculty_url", ""),
            "matched_keywords":   m.get("matched_keywords", []),
            "match_score":        m.get("match_score", 0),
            "match_type":         m.get("match_type", "keyword"),
            "similarity_score":   m.get("similarity_score", 0.0),
        })
        g["match_types"].add(m.get("match_type", "keyword"))
        g["all_keywords"].update(m.get("matched_keywords") or [])
        g["max_score"] = max(g["max_score"], m.get("match_score", 0))
        g["max_similarity"] = max(g["max_similarity"], m.get("similarity_score", 0.0))

    grants = list(grants_map.values())
    for g in grants:
        g["match_types"]  = sorted(g["match_types"])
        g["all_keywords"] = sorted(g["all_keywords"])
        g["faculty_count"] = len(g["faculty_matches"])
        # Deadline urgency
        try:
            cd = g["grant_close_date"]
            if cd:
                from datetime import date
                days = (datetime.strptime(cd[:10], "%Y-%m-%d").date() - datetime.now(timezone.utc).date()).days
                g["days_until_close"] = days
            else:
                g["days_until_close"] = None
        except Exception:
            g["days_until_close"] = None
        # Award ceiling as int
        try:
            g["award_int"] = int(str(g.get("grant_award_ceiling","0")).replace(",","").replace("$","") or "0")
        except Exception:
            g["award_int"] = 0
        # Composite confidence — use pre-computed IDF-weighted confidence_score directly
        best = max(g["faculty_matches"], key=lambda m: m.get("confidence_score", 0), default={})
        if best.get("confidence_score") is not None:
            g["confidence"] = best["confidence_score"]
        else:
            # Fallback for old records without confidence_score
            kw_conf = min((best.get("match_score",0)) / 10, 1.0)
            sem_conf = best.get("similarity_score", 0.0)
            mt = best.get("match_type","keyword")
            if mt == "both":
                g["confidence"] = min(round((kw_conf*0.5 + sem_conf*0.5)*100), 99)
            elif mt == "semantic":
                g["confidence"] = min(round(sem_conf*100), 99)
            else:
                g["confidence"] = min(round(kw_conf*100), 99)

    # Filters
    if search:
        grants = [g for g in grants if
                  search in g["grant_title"].lower() or
                  search in g["grant_agency"].lower() or
                  any(search in k.lower() for k in g["all_keywords"]) or
                  any(search in m["faculty_name"].lower() for m in g["faculty_matches"])]
    if ag_filt:
        grants = [g for g in grants if ag_filt in g["grant_agency"].lower()]
    if mt_filt:
        grants = [g for g in grants if mt_filt in g["match_types"]]

    # Sort
    if sort_by == "faculty":
        grants.sort(key=lambda g: -g["faculty_count"])
    elif sort_by == "award":
        grants.sort(key=lambda g: -g["award_int"])
    elif sort_by == "deadline":
        grants.sort(key=lambda g: (g["days_until_close"] is None, g["days_until_close"] or 9999))
    elif sort_by == "confidence":
        grants.sort(key=lambda g: -g["confidence"])
    else:  # recent
        grants.sort(key=lambda g: g["first_seen"], reverse=True)

    all_agencies = sorted(set(g["grant_agency"] for g in list(grants_map.values()) if g["grant_agency"]))
    total = len(grants)
    start = (page-1)*per_page
    page_items = grants[start:start+per_page]

    return jsonify({
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "pages":       (total+per_page-1)//per_page,
        "grants":      page_items,
        "agencies":    all_agencies,
        "unique_grants": len(grants_map),
    })


# ── API: Keyword analytics deep-dive ──────────────────────────────────────────

@app.route("/api/keyword-analysis")
@login_required
def api_keyword_analysis():
    """Deep keyword analytics: co-occurrence, per-dept top keywords, keyword velocity."""
    matches = get_matches()
    faculty = get_faculty()

    # Top keywords with match frequency and dept breakdown
    kw_grants   = defaultdict(set)   # keyword -> set of grant_ids
    kw_faculty  = defaultdict(set)   # keyword -> set of faculty names
    kw_depts    = defaultdict(Counter) # keyword -> dept -> count

    for m in matches:
        gid  = m.get("grant_id","")
        name = m.get("faculty_name","")
        dept = m.get("faculty_department","Unknown") or "Unknown"
        for kw in (m.get("matched_keywords") or []):
            k = kw.lower()
            kw_grants[k].add(gid)
            kw_faculty[k].add(name)
            kw_depts[k][dept] += 1

    top_kw_detail = []
    for kw in sorted(kw_grants, key=lambda k: -len(kw_grants[k]))[:30]:
        top_dept = kw_depts[kw].most_common(1)
        top_kw_detail.append({
            "keyword":      kw,
            "grant_count":  len(kw_grants[kw]),
            "faculty_count":len(kw_faculty[kw]),
            "top_dept":     top_dept[0][0] if top_dept else "—",
        })

    # Keyword count distribution across faculty
    kw_count_dist = Counter(len(f.get("keywords") or []) for f in faculty)
    # Bucket into ranges
    buckets = {"0":0,"1-10":0,"11-25":0,"26-50":0,"51-100":0,"100+":0}
    for cnt, n in kw_count_dist.items():
        if cnt == 0:         buckets["0"] += n
        elif cnt <= 10:      buckets["1-10"] += n
        elif cnt <= 25:      buckets["11-25"] += n
        elif cnt <= 50:      buckets["26-50"] += n
        elif cnt <= 100:     buckets["51-100"] += n
        else:                buckets["100+"] += n

    # Dept match leaderboard
    dept_stats = defaultdict(lambda: {"matches":0,"grants":set(),"faculty":set(),"scores":[],"sem_matches":0})
    for m in matches:
        dept = m.get("faculty_department","Unknown") or "Unknown"
        dept_stats[dept]["matches"] += 1
        dept_stats[dept]["grants"].add(m.get("grant_id",""))
        dept_stats[dept]["faculty"].add(m.get("faculty_name",""))
        dept_stats[dept]["scores"].append(m.get("match_score",0))
        if m.get("match_type") in ("semantic","both"):
            dept_stats[dept]["sem_matches"] += 1

    dept_leaderboard = []
    for dept, v in dept_stats.items():
        scores = v["scores"]
        dept_leaderboard.append({
            "dept":           dept,
            "total_matches":  v["matches"],
            "unique_grants":  len(v["grants"]),
            "unique_faculty": len(v["faculty"]),
            "avg_score":      round(sum(scores)/len(scores),1) if scores else 0,
            "sem_matches":    v["sem_matches"],
            "sem_pct":        round(v["sem_matches"]/v["matches"]*100) if v["matches"] else 0,
        })
    dept_leaderboard.sort(key=lambda d: -d["total_matches"])

    # Confidence distribution across all matches
    conf_buckets = {"0-20":0,"20-40":0,"40-60":0,"60-80":0,"80-100":0}
    for m in matches:
        kw_c  = (m.get("confidence_score",
                  min(round((min(m.get("match_score",0)/10,1.0))*100), 99))) / 100
        sem_c = m.get("similarity_score", 0.0) or 0.0
        mt = m.get("match_type","keyword")
        if mt == "both":    conf = (kw_c*0.5 + sem_c*0.5)*100
        elif mt == "semantic": conf = sem_c*100
        else:               conf = kw_c*100
        conf = round(conf)
        if conf < 20:   conf_buckets["0-20"] += 1
        elif conf < 40: conf_buckets["20-40"] += 1
        elif conf < 60: conf_buckets["40-60"] += 1
        elif conf < 80: conf_buckets["60-80"] += 1
        else:           conf_buckets["80-100"] += 1

    return jsonify({
        "top_keywords_detail": top_kw_detail,
        "keyword_count_dist":  [{"range":k,"count":v} for k,v in buckets.items()],
        "dept_leaderboard":    dept_leaderboard[:20],
        "confidence_dist":     [{"range":k,"count":v} for k,v in conf_buckets.items()],
    })

# ── Login/logout ──────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html><html><head>
<title>UMSOM Grant Matcher</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0c10;font-family:'DM Sans',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#0f1117;border:1px solid #1e2330;border-radius:14px;padding:44px;width:100%;max-width:390px}
.logo{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#3b82f6;letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px}
.dot{display:inline-block;width:6px;height:6px;background:#22c55e;border-radius:50%;margin-right:6px;animation:p 2s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
h1{color:#f1f5f9;font-size:22px;font-weight:600;margin-bottom:30px}
label{display:block;color:#64748b;font-size:12px;font-family:'IBM Plex Mono',monospace;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
input{width:100%;padding:11px 14px;background:#141720;border:1px solid #252d3d;border-radius:8px;color:#f1f5f9;font-size:14px;margin-bottom:18px;outline:none;transition:border-color .2s}
input:focus{border-color:#3b82f6}
button{width:100%;padding:12px;background:#3b82f6;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:background .2s}
button:hover{background:#2563eb}
.err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);color:#f87171;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:18px}
</style></head><body><div class="card">
<div class="logo"><span class="dot"></span>UMSOM Grant Matcher</div>
<h1>Sign in</h1>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post">
<label>Username</label><input type="text" name="username" autofocus autocomplete="username">
<label>Password</label><input type="password" name="password" autocomplete="current-password">
<button type="submit">Sign in →</button>
</form></div>
<!-- ═══ Faculty Detail Modal ═══ -->
<div class="modal-overlay" id="fac-modal" style="display:none" onclick="closeFacModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-hdr">
      <div class="modal-avatar" id="fac-modal-initials"></div>
      <div>
        <div class="modal-name" id="fac-modal-name"></div>
        <div class="modal-dept" id="fac-modal-dept"></div>
        <div class="modal-email" id="fac-modal-email"></div>
      </div>
      <button class="modal-close" onclick="closeFacModal()">✕</button>
    </div>
    <div class="modal-tabs">
      <div class="modal-tab active" onclick="switchModalTab('keywords',this)">Keywords</div>
      <div class="modal-tab" onclick="switchModalTab('matches',this)">Match History</div>
      <div class="modal-tab" onclick="switchModalTab('sources',this)">Sources</div>
    </div>
    <div class="modal-body">
      <div class="modal-tab-content active" id="mtab-keywords"></div>
      <div class="modal-tab-content" id="mtab-matches"></div>
      <div class="modal-tab-content" id="mtab-sources">
        <div style="height:220px;position:relative"><canvas id="modal-src-chart"></canvas></div>
      </div>
    </div>
  </div>
</div>
</body></html>"""

@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        if check_auth(request.form.get("username",""), request.form.get("password","")):
            session["logged_in"] = True
            return redirect(request.args.get("next") or "/")
        error = "Invalid credentials."
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD HTML
# ══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UMSOM Grant Matcher</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
/* ── Variables ── */
:root {
  --bg:#090b0f; --surf:#0f1218; --surf2:#141820; --surf3:#1a1f2a;
  --border:#1c2235; --border2:#232c40;
  --blue:#3b82f6; --blue2:#60a5fa; --bluedim:rgba(59,130,246,.12);
  --cyan:#06b6d4; --cyandim:rgba(6,182,212,.12);
  --green:#22c55e; --greendim:rgba(34,197,94,.12);
  --yellow:#f59e0b; --yellowdim:rgba(245,158,11,.12);
  --red:#ef4444; --reddim:rgba(239,68,68,.12);
  --purple:#a855f7; --purpledim:rgba(168,85,247,.12);
  --text:#e2e8f0; --text2:#94a3b8; --text3:#4b5675; --text4:#2d3a52;
  --mono:'IBM Plex Mono',monospace; --sans:'DM Sans',sans-serif;
  --radius:10px; --sidebar:228px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;display:flex}

/* ── Sidebar ── */
.sidebar{width:var(--sidebar);min-width:var(--sidebar);height:100vh;background:var(--surf);
  border-right:1px solid var(--border);display:flex;flex-direction:column;z-index:100;overflow:hidden}
.sb-logo{padding:22px 20px 18px;border-bottom:1px solid var(--border)}
.sb-wordmark{font-family:var(--mono);font-size:10.5px;color:var(--blue);letter-spacing:.15em;text-transform:uppercase}
.sb-sub{font-family:var(--mono);font-size:10px;color:var(--text3);margin-top:3px}
.pulse{display:inline-block;width:5px;height:5px;background:var(--green);border-radius:50%;
  margin-right:6px;animation:pulse 2.2s infinite}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(34,197,94,.4)}60%{opacity:.7;box-shadow:0 0 0 5px rgba(34,197,94,0)}}
.nav{padding:10px 0;flex:1;overflow-y:auto}
.nav-item{display:flex;align-items:center;gap:11px;padding:9px 20px;cursor:pointer;
  color:var(--text2);font-size:13px;font-weight:400;border-left:2px solid transparent;
  transition:all .15s;user-select:none;position:relative}
.nav-item:hover{color:var(--text);background:rgba(255,255,255,.03)}
.nav-item.active{color:var(--blue);border-left-color:var(--blue);background:var(--bluedim)}
.nav-icon{font-size:14px;width:18px;text-align:center;flex-shrink:0}
.nav-label{flex:1}
.nav-badge{background:var(--red);color:#fff;font-size:10px;font-family:var(--mono);
  padding:1px 6px;border-radius:10px;font-weight:600}
.sb-footer{padding:14px 20px;border-top:1px solid var(--border);
  font-family:var(--mono);font-size:10px;color:var(--text3);line-height:1.6}

/* ── Main layout ── */
.main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}
.topbar{height:52px;border-bottom:1px solid var(--border);display:flex;align-items:center;
  padding:0 28px;gap:12px;background:rgba(9,11,15,.96);backdrop-filter:blur(10px);
  z-index:50;flex-shrink:0}
.topbar-title{font-size:15px;font-weight:500}
.topbar-crumb{font-size:12px;color:var(--text3);font-family:var(--mono)}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.btn-sm{background:var(--surf2);border:1px solid var(--border2);color:var(--text2);
  padding:5px 12px;border-radius:6px;font-size:12px;font-family:var(--mono);
  cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:5px}
.btn-sm:hover{border-color:var(--blue);color:var(--blue)}
.btn-sm.active-filter{border-color:var(--blue);color:var(--blue);background:var(--bluedim)}
.updated{font-family:var(--mono);font-size:10px;color:var(--text3)}
.pages{flex:1;overflow-y:auto;overflow-x:hidden}
.page{display:none;padding:24px 28px;animation:fadeIn .2s ease}
.page.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* ── Stat cards ── */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px;margin-bottom:22px}
.stat-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px 20px;position:relative;overflow:hidden;transition:border-color .2s;cursor:default}
.stat-card:hover{border-color:var(--border2)}
.stat-accent{position:absolute;top:0;left:0;right:0;height:2px}
.stat-label{font-size:10.5px;font-family:var(--mono);color:var(--text3);
  text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}
.stat-val{font-size:30px;font-weight:600;font-family:var(--mono);color:var(--text);line-height:1}
.stat-meta{font-size:11px;color:var(--text3);margin-top:7px;font-family:var(--mono)}
.stat-delta{font-size:11px;font-family:var(--mono);margin-top:6px}
.stat-delta.up{color:var(--green)} .stat-delta.down{color:var(--red)} .stat-delta.neu{color:var(--text3)}

/* ── Section headers ── */
.sec-hdr{display:flex;align-items:center;gap:10px;margin-bottom:14px;margin-top:4px}
.sec-title{font-size:12px;font-weight:500;font-family:var(--mono);text-transform:uppercase;
  letter-spacing:.1em;color:var(--text2)}
.sec-count{font-family:var(--mono);font-size:11px;color:var(--text3);background:var(--surf2);
  border:1px solid var(--border);padding:1px 8px;border-radius:4px}
.sec-actions{margin-left:auto;display:flex;gap:6px}

/* ── Cards / panels ── */
.panel{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  margin-bottom:18px;overflow:hidden}
.panel-hdr{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;
  align-items:center;gap:10px}
.panel-title{font-size:12px;font-family:var(--mono);text-transform:uppercase;
  letter-spacing:.08em;color:var(--text2);font-weight:500}
.panel-body{padding:18px}
.panel-body.p0{padding:0}

/* ── Grid layouts ── */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
@media(max-width:1100px){.grid2{grid-template-columns:1fr}.grid3{grid-template-columns:1fr 1fr}}

/* ── Chart containers ── */
.chart-wrap{position:relative;height:200px;width:100%}
.chart-wrap.tall{height:260px}
.chart-wrap.sm{height:160px}

/* ── Tables ── */
.tbl-wrap{overflow:auto}
table{width:100%;border-collapse:collapse}
th{background:var(--surf2);padding:9px 14px;text-align:left;font-family:var(--mono);
  font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--text3);
  border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:var(--text2)}
td{padding:10px 14px;border-bottom:1px solid var(--border);font-size:12px;
  vertical-align:top;color:var(--text2)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.018)}
.td-name{color:var(--text);font-weight:500;font-size:13px}
.td-email{color:var(--cyan);font-family:var(--mono);font-size:11px}
.td-dept{color:var(--text3);font-size:11px}
.td-num{font-family:var(--mono);text-align:right;color:var(--text2)}

/* ── Toolbar / filters ── */
.toolbar{padding:12px 16px;border-bottom:1px solid var(--border);
  display:flex;gap:8px;align-items:center;flex-wrap:wrap;background:var(--surf2)}
.search-box{background:var(--bg);border:1px solid var(--border2);color:var(--text);
  padding:7px 12px;border-radius:6px;font-size:12px;font-family:var(--mono);
  outline:none;width:220px;transition:border-color .15s}
.search-box:focus{border-color:var(--blue)}
.search-box::placeholder{color:var(--text3)}
.filter-sel{background:var(--bg);border:1px solid var(--border2);color:var(--text2);
  padding:7px 10px;border-radius:6px;font-size:12px;font-family:var(--mono);outline:none;cursor:pointer}
.filter-pill{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;
  border-radius:20px;font-size:11px;font-family:var(--mono);cursor:pointer;
  border:1px solid var(--border2);color:var(--text2);background:var(--surf2);transition:all .15s}
.filter-pill:hover{border-color:var(--blue);color:var(--blue)}
.filter-pill.active{background:var(--bluedim);border-color:var(--blue);color:var(--blue2)}

/* ── Keywords / badges ── */
.kw-list{display:flex;flex-wrap:wrap;gap:3px}
.kw{background:var(--bluedim);border:1px solid rgba(59,130,246,.2);color:var(--blue2);
  font-size:10px;font-family:var(--mono);padding:2px 7px;border-radius:4px;white-space:nowrap}
.kw.match{background:var(--greendim);border-color:rgba(34,197,94,.25);color:var(--green)}
.kw.sem{background:var(--purpledim);border-color:rgba(168,85,247,.25);color:var(--purple)}
.src-badge{font-size:10px;font-family:var(--mono);padding:2px 7px;border-radius:4px;
  white-space:nowrap;display:inline-block}
.src-umsom{background:rgba(59,130,246,.15);color:#7ab3e0;border:1px solid rgba(59,130,246,.2)}
.src-pubmed{background:rgba(245,158,11,.12);color:#fbbf24;border:1px solid rgba(245,158,11,.2)}
.src-nih{background:rgba(239,68,68,.1);color:#fca5a5;border:1px solid rgba(239,68,68,.2)}
.src-orcid{background:rgba(34,197,94,.1);color:#86efac;border:1px solid rgba(34,197,94,.2)}
.src-s2{background:rgba(168,85,247,.12);color:#d8b4fe;border:1px solid rgba(168,85,247,.2)}

/* ── Match type badges ── */
.mt-badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;
  border-radius:10px;text-transform:uppercase;letter-spacing:.04em;font-family:var(--mono)}
.mt-keyword{background:#1e3a5f;color:#7ab3e0}
.mt-semantic{background:#1a4a2e;color:#5ecb8a}
.mt-both{background:#4a2e00;color:#f0b840}

/* ── Score badge ── */
.score{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
  border-radius:50%;font-family:var(--mono);font-size:11px;font-weight:600;
  background:var(--bluedim);border:1px solid rgba(59,130,246,.3);color:var(--blue)}
.score.hi{background:var(--greendim);border-color:rgba(34,197,94,.3);color:var(--green)}

/* ── Similarity bar ── */
.sim-bar-wrap{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:10px;color:var(--text3)}
.sim-bar{height:4px;border-radius:2px;background:var(--border2);flex:1;overflow:hidden}
.sim-bar-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--purple),var(--cyan))}

/* ── Match cards ── */
.match-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  margin-bottom:14px;overflow:hidden;transition:border-color .2s}
.match-card:hover{border-color:var(--border2)}
.match-card-hdr{padding:14px 18px;border-bottom:1px solid var(--border);background:var(--surf2)}
.match-title{font-size:14px;font-weight:500;color:var(--text);margin-bottom:8px;line-height:1.4}
.match-meta{display:flex;flex-wrap:wrap;gap:6px 16px}
.meta-chip{font-size:11px;font-family:var(--mono);color:var(--text3)}
.meta-chip span{color:var(--text2)}
.meta-chip.award span{color:var(--yellow)}
.match-body{padding:0}

/* Synopsis expand */
.synopsis-row{padding:10px 18px;border-bottom:1px solid var(--border);
  background:rgba(255,255,255,.01);cursor:pointer}
.synopsis-text{font-size:12px;color:var(--text2);line-height:1.6;margin-top:8px;display:none}
.synopsis-toggle{font-size:11px;font-family:var(--mono);color:var(--text3);
  display:flex;align-items:center;gap:5px}
.synopsis-toggle:hover{color:var(--blue)}

/* Faculty rows */
.fac-row{display:flex;align-items:flex-start;gap:14px;padding:14px 18px;
  border-bottom:1px solid var(--border)}
.fac-row:last-child{border-bottom:none}
.fac-avatar{width:34px;height:34px;border-radius:50%;background:var(--bluedim);
  border:1px solid rgba(59,130,246,.25);display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:600;color:var(--blue);flex-shrink:0;font-family:var(--mono)}
.fac-info{flex:1;min-width:0}
.fac-name-link{color:var(--text);font-weight:500;font-size:13px;text-decoration:none}
.fac-name-link:hover{color:var(--blue)}
.fac-dept{font-size:11px;color:var(--text3);margin-top:2px}
.fac-email{font-size:11px;color:var(--cyan);font-family:var(--mono);margin-top:1px}
.fac-right{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0}

/* Match explanation panel */
.explain-panel{background:var(--surf2);border:1px solid var(--border);
  border-radius:6px;padding:10px 12px;margin-top:8px;font-size:11px;
  font-family:var(--mono);color:var(--text3);line-height:1.7}
.explain-panel .lbl{color:var(--text3);font-size:10px;text-transform:uppercase;
  letter-spacing:.08em;margin-bottom:4px}
.explain-kw{color:var(--green);font-weight:500}
.explain-why{color:var(--text2);margin-top:6px;font-family:var(--sans);font-size:11px}

/* Expand toggle */
.expand-toggle{font-size:10px;font-family:var(--mono);color:var(--text3);cursor:pointer;
  display:flex;align-items:center;gap:4px;margin-top:6px}
.expand-toggle:hover{color:var(--blue)}

/* ── Pipeline pass grid ── */
.pass-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.pass-card{background:var(--surf2);border:1px solid var(--border);border-radius:8px;padding:14px}
.pass-num{font-family:var(--mono);font-size:10px;color:var(--text3);margin-bottom:6px}
.pass-name{font-size:13px;font-weight:500;color:var(--text);margin-bottom:6px}
.pass-status{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:10px;
  display:inline-block}
.ps-complete{background:var(--greendim);color:var(--green)}
.ps-running{background:var(--yellowdim);color:var(--yellow)}
.ps-error{background:var(--reddim);color:var(--red)}
.ps-skipped{background:var(--surf3);color:var(--text3)}
.ps-unknown{background:var(--surf3);color:var(--text3)}

/* ── Progress bar ── */
.prog-wrap{background:var(--border);border-radius:4px;height:6px;overflow:hidden;margin-top:6px}
.prog-fill{height:100%;border-radius:4px;transition:width .4s ease}

/* ── Deadline timeline ── */
.deadline-row{display:flex;align-items:center;gap:14px;padding:10px 0;
  border-bottom:1px solid var(--border)}
.deadline-row:last-child{border-bottom:none}
.dl-days{width:50px;text-align:center;font-family:var(--mono);font-size:13px;font-weight:600;flex-shrink:0}
.dl-days.overdue{color:var(--red)}
.dl-days.soon{color:var(--yellow)}
.dl-days.ok{color:var(--green)}
.dl-info{flex:1;min-width:0}
.dl-title{font-size:12px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dl-meta{font-size:11px;color:var(--text3);font-family:var(--mono);margin-top:2px}

/* ── Log terminal ── */
.log-wrap{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
.log-body{background:#060810;max-height:55vh;overflow-y:auto;padding:12px;font-family:var(--mono);font-size:11.5px;line-height:1.75}
.log-line{padding:1px 0;border-radius:2px}
.log-line.error{color:var(--red)}
.log-line.warning{color:var(--yellow)}
.log-line.info{color:#6b8cac}
.log-ts{color:var(--text4)}
.log-lvl-err{color:var(--red)} .log-lvl-warn{color:var(--yellow)} .log-lvl-info{color:#3a5a7a}
.log-module{color:#4a7a5a}
.log-msg{color:#a0c0d0}

/* ── Pagination ── */
.pagination{display:flex;align-items:center;gap:5px;padding:12px 16px;
  border-top:1px solid var(--border);background:var(--surf2)}
.pg-btn{background:var(--surf);border:1px solid var(--border2);color:var(--text2);
  padding:4px 10px;border-radius:5px;font-size:12px;font-family:var(--mono);
  cursor:pointer;transition:all .15s}
.pg-btn:hover{border-color:var(--blue);color:var(--blue)}
.pg-btn.active{background:var(--blue);border-color:var(--blue);color:#fff}
.pg-btn:disabled{opacity:.3;cursor:default}
.pg-info{font-size:11px;font-family:var(--mono);color:var(--text3);margin-right:4px}

/* ── Empty state ── */
.empty{text-align:center;padding:48px 20px;color:var(--text3)}
.empty-icon{font-size:32px;margin-bottom:12px}
.empty-msg{font-size:13px;font-family:var(--mono)}

/* ── Info rows ── */
.info-rows{}
.info-row{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)}
.info-row:last-child{border-bottom:none}
.info-k{width:210px;flex-shrink:0;font-family:var(--mono);font-size:10.5px;
  color:var(--text3);text-transform:uppercase;letter-spacing:.08em;display:flex;align-items:center}
.info-v{font-size:12px;color:var(--text);font-family:var(--mono);word-break:break-all}
.info-v.good{color:var(--green)} .info-v.warn{color:var(--yellow)} .info-v.bad{color:var(--red)}


/* ── Faculty detail modal ── */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:500;
  display:flex;align-items:center;justify-content:center;animation:fadeIn .15s ease}
.modal{background:var(--surf);border:1px solid var(--border2);border-radius:14px;
  width:min(820px,95vw);max-height:88vh;display:flex;flex-direction:column;overflow:hidden;
  box-shadow:0 24px 60px rgba(0,0,0,.5)}
.modal-hdr{padding:20px 24px 16px;border-bottom:1px solid var(--border);
  display:flex;align-items:flex-start;gap:14px;flex-shrink:0}
.modal-avatar{width:44px;height:44px;border-radius:50%;background:var(--bluedim);
  border:1px solid rgba(59,130,246,.3);display:flex;align-items:center;justify-content:center;
  font-size:15px;font-weight:600;color:var(--blue);flex-shrink:0;font-family:var(--mono)}
.modal-name{font-size:17px;font-weight:600;color:var(--text);line-height:1.2}
.modal-dept{font-size:12px;color:var(--text3);margin-top:3px}
.modal-email{font-size:12px;color:var(--cyan);font-family:var(--mono);margin-top:2px}
.modal-close{margin-left:auto;background:none;border:none;color:var(--text3);
  font-size:20px;cursor:pointer;padding:4px 8px;border-radius:6px;line-height:1}
.modal-close:hover{color:var(--text);background:var(--surf2)}
.modal-body{flex:1;overflow-y:auto;padding:20px 24px}
.modal-tabs{display:flex;gap:2px;padding:0 24px;background:var(--surf2);
  border-bottom:1px solid var(--border);flex-shrink:0}
.modal-tab{padding:10px 16px;font-size:12px;font-family:var(--mono);color:var(--text3);
  cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.modal-tab:hover{color:var(--text)}
.modal-tab.active{color:var(--blue);border-bottom-color:var(--blue)}
.modal-tab-content{display:none}.modal-tab-content.active{display:block}

/* ── Confidence meter ── */
.conf-wrap{display:flex;align-items:center;gap:6px}
.conf-bar-outer{flex:1;height:5px;background:var(--border2);border-radius:3px;overflow:hidden;min-width:60px}
.conf-bar-inner{height:100%;border-radius:3px;transition:width .4s ease}
.conf-label{font-family:var(--mono);font-size:10px;color:var(--text3);width:32px;text-align:right}

/* ── Urgency banner ── */
.urgency-banner{display:flex;align-items:center;gap:8px;padding:7px 18px;
  font-size:11px;font-family:var(--mono);font-weight:500;border-bottom:1px solid}
.urgency-critical{background:rgba(239,68,68,.08);color:var(--red);border-color:rgba(239,68,68,.2)}
.urgency-warning{background:rgba(245,158,11,.08);color:var(--yellow);border-color:rgba(245,158,11,.2)}

/* ── High-value grant badge ── */
.hv-badge{display:inline-block;background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.3);
  color:var(--yellow);font-size:10px;font-family:var(--mono);padding:2px 7px;
  border-radius:4px;margin-left:8px;vertical-align:middle}

/* ── Keyword source legend in modal ── */
.kw-src-section{margin-bottom:14px}
.kw-src-label{font-size:10px;font-family:var(--mono);text-transform:uppercase;
  letter-spacing:.08em;color:var(--text3);margin-bottom:6px;display:flex;align-items:center;gap:8px}
.kw-src-count{font-size:10px;color:var(--text4);font-family:var(--mono)}

/* ── Match history in modal ── */
.mh-row{display:flex;align-items:flex-start;gap:12px;padding:10px 0;
  border-bottom:1px solid var(--border)}
.mh-row:last-child{border-bottom:none}
.mh-date{font-family:var(--mono);font-size:10px;color:var(--text3);width:80px;flex-shrink:0;padding-top:2px}
.mh-title{font-size:12px;color:var(--text);flex:1;line-height:1.4}
.mh-meta{font-size:11px;color:var(--text3);font-family:var(--mono);margin-top:2px}

/* ── Dept coverage chart row ── */
.dept-cov-row{display:flex;align-items:center;gap:10px;padding:5px 0}
.dept-cov-name{font-size:11px;color:var(--text2);width:180px;flex-shrink:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dept-cov-bar{flex:1;height:5px;background:var(--border2);border-radius:3px;overflow:hidden}
.dept-cov-fill{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--blue),var(--cyan))}
.dept-cov-pct{font-family:var(--mono);font-size:10px;color:var(--text3);width:36px;text-align:right}
.dept-cov-n{font-family:var(--mono);font-size:10px;color:var(--text4);width:60px;text-align:right}
/* ── Scroll customization ── */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--text4)}

/* ── Tooltip ── */
.tip{position:relative;cursor:help}
.tip:after{content:attr(data-tip);position:absolute;bottom:calc(100% + 5px);left:50%;
  transform:translateX(-50%);background:#1e2a3a;color:var(--text);font-size:11px;
  font-family:var(--mono);padding:5px 10px;border-radius:5px;white-space:nowrap;
  opacity:0;pointer-events:none;transition:opacity .15s;z-index:200;border:1px solid var(--border2)}
.tip:hover:after{opacity:1}

/* ── Donut center label ── */
.donut-wrap{position:relative;display:flex;align-items:center;justify-content:center}
.donut-center{position:absolute;text-align:center;pointer-events:none}
.donut-center .big{font-size:26px;font-weight:600;font-family:var(--mono);color:var(--text)}
.donut-center .lbl{font-size:10px;font-family:var(--mono);color:var(--text3);text-transform:uppercase;letter-spacing:.08em}

/* ── Grant Explorer ── */
.grant-exp-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  margin-bottom:12px;overflow:hidden;transition:border-color .18s}
.grant-exp-card:hover{border-color:var(--border2)}
.ge-hdr{padding:13px 18px 10px;background:var(--surf2);border-bottom:1px solid var(--border);
  display:flex;align-items:flex-start;gap:10px}
.ge-title{font-size:13.5px;font-weight:500;color:var(--text);line-height:1.4;flex:1}
.ge-pills{display:flex;flex-wrap:wrap;gap:5px 12px;padding:8px 18px;border-bottom:1px solid var(--border);
  background:rgba(0,0,0,.15)}
.ge-pill{font-size:10.5px;font-family:var(--mono);color:var(--text3);display:flex;align-items:center;gap:4px}
.ge-pill span{color:var(--text2)}
.ge-pill.award span{color:var(--yellow)}
.ge-pill.urgent span{color:var(--red)}
.ge-pill.ok span{color:var(--green)}
.ge-body{padding:12px 18px}
.ge-fac-chips{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px}
.ge-fac-chip{background:var(--surf2);border:1px solid var(--border2);border-radius:6px;
  padding:4px 10px;font-size:11px;display:flex;align-items:center;gap:6px;cursor:pointer;
  transition:border-color .15s}
.ge-fac-chip:hover{border-color:var(--blue)}
.ge-fac-name{color:var(--text2);font-weight:500}
.ge-fac-dept{color:var(--text3);font-size:10px}
.ge-fac-mt{font-size:9px;font-family:var(--mono);padding:1px 5px;border-radius:8px;text-transform:uppercase}
.ge-fac-mt.keyword{background:#1e3a5f;color:#7ab3e0}
.ge-fac-mt.semantic{background:#1a4a2e;color:#5ecb8a}
.ge-fac-mt.both{background:#4a2e00;color:#f0b840}
.ge-conf-ring{width:44px;height:44px;flex-shrink:0}
.ge-synopsis{font-size:11.5px;color:var(--text2);line-height:1.65;padding:0;max-height:0;
  overflow:hidden;transition:max-height .3s ease;margin-top:0}
.ge-synopsis.open{max-height:300px;margin-top:8px}
.ge-kw-cloud{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}
.ge-expand-btn{font-size:10px;font-family:var(--mono);color:var(--text3);cursor:pointer;
  padding:2px 0;display:inline-flex;align-items:center;gap:4px}
.ge-expand-btn:hover{color:var(--blue)}

/* ── Health score ring ── */
.health-ring-wrap{display:flex;align-items:center;gap:20px;padding:4px 0}
.health-ring{position:relative;width:70px;height:70px;flex-shrink:0}
.health-ring svg{transform:rotate(-90deg)}
.health-ring-val{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  flex-direction:column;font-family:var(--mono)}
.health-ring-num{font-size:18px;font-weight:700}
.health-ring-lbl{font-size:8px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em}
.health-items{flex:1;display:flex;flex-direction:column;gap:4px}
.health-item{display:flex;align-items:center;gap:8px;font-size:11px;font-family:var(--mono)}
.health-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.health-dot.ok{background:var(--green)}
.health-dot.warn{background:var(--yellow)}
.health-dot.err{background:var(--red)}

/* ── Countdown timer ── */
.countdown-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px 18px}
.countdown-lbl{font-size:10px;font-family:var(--mono);color:var(--text3);text-transform:uppercase;
  letter-spacing:.1em;margin-bottom:8px}
.countdown-val{font-size:24px;font-family:var(--mono);font-weight:600;color:var(--text);
  letter-spacing:.04em;line-height:1}
.countdown-sub{font-size:10px;font-family:var(--mono);color:var(--text3);margin-top:4px}

/* ── Dept leaderboard medal ── */
.dept-lb-rank{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;font-size:11px;font-weight:700;font-family:var(--mono);flex-shrink:0}
.rank-1{background:rgba(245,158,11,.2);color:var(--yellow);border:1px solid rgba(245,158,11,.3)}
.rank-2{background:rgba(148,163,184,.15);color:#94a3b8;border:1px solid rgba(148,163,184,.2)}
.rank-3{background:rgba(180,83,9,.15);color:#c2855a;border:1px solid rgba(180,83,9,.2)}
.rank-other{background:var(--surf2);color:var(--text3);border:1px solid var(--border)}
.sem-pct-bar{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:10px}
.sem-pct-fill{height:4px;background:var(--purple);border-radius:2px;display:inline-block}

/* ── Keyword highlight in synopsis ── */
.kw-hl{background:rgba(34,197,94,.18);color:var(--green);border-radius:2px;padding:0 2px;font-weight:500}
.kw-hl-sem{background:rgba(168,85,247,.18);color:var(--purple);border-radius:2px;padding:0 2px}

/* ── Overview urgency strip ── */
.urgency-strip{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  margin-bottom:18px;overflow:hidden}
.urgency-strip-hdr{padding:10px 16px;border-bottom:1px solid var(--border);
  font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.1em;
  color:var(--text3);display:flex;align-items:center;gap:8px}
.urgency-items{display:flex;gap:0;overflow-x:auto}
.urgency-item{padding:10px 16px;border-right:1px solid var(--border);min-width:200px;
  flex-shrink:0;cursor:pointer;transition:background .15s}
.urgency-item:hover{background:var(--surf2)}
.urgency-item:last-child{border-right:none}
.ui-days{font-family:var(--mono);font-size:16px;font-weight:700;line-height:1}
.ui-title{font-size:11px;color:var(--text2);margin-top:3px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:180px}
.ui-meta{font-size:10px;color:var(--text3);font-family:var(--mono);margin-top:2px}
</style>
</head>
<body>

<!-- ═══ Sidebar ═══ -->
<aside class="sidebar">
  <div class="sb-logo">
    <div class="sb-wordmark"><span class="pulse"></span>Grant Matcher</div>
    <div class="sb-sub">UMSOM · Intelligence Platform</div>
  </div>
  <nav class="nav">
    <div class="nav-item active" onclick="showPage('overview')" id="nav-overview">
      <span class="nav-icon">⬡</span><span class="nav-label">Overview</span>
    </div>
    <div class="nav-item" onclick="showPage('matches')" id="nav-matches">
      <span class="nav-icon">⟡</span><span class="nav-label">Grant Matches</span>
      <span class="nav-badge" id="sb-match-count" style="display:none"></span>
    </div>
    <div class="nav-item" onclick="showPage('grants')" id="nav-grants">
      <span class="nav-icon">◧</span><span class="nav-label">Grant Explorer</span>
      <span class="nav-badge" id="sb-grants-count" style="display:none"></span>
    </div>
    <div class="nav-item" onclick="showPage('analytics')" id="nav-analytics">
      <span class="nav-icon">◈</span><span class="nav-label">Analytics</span>
    </div>
    <div class="nav-item" onclick="showPage('faculty')" id="nav-faculty">
      <span class="nav-icon">◉</span><span class="nav-label">Faculty</span>
    </div>
    <div class="nav-item" onclick="showPage('pipeline')" id="nav-pipeline">
      <span class="nav-icon">◎</span><span class="nav-label">Pipeline</span>
    </div>
    <div class="nav-item" onclick="showPage('logs')" id="nav-logs">
      <span class="nav-icon">▤</span><span class="nav-label">Logs</span>
      <span class="nav-badge" id="sb-error-count" style="display:none"></span>
    </div>
  </nav>
  <div class="sb-footer">
    <div>v3.0 · Hybrid Matcher</div>
    <div style="margin-top:3px"><a href="/logout" style="color:var(--text3);text-decoration:none">Sign out →</a></div>
  </div>
</aside>

<!-- ═══ Main ═══ -->
<div class="main">
  <div class="topbar">
    <div class="topbar-title" id="topbar-title">Overview</div>
    <div class="topbar-crumb" id="topbar-crumb"></div>
    <div class="topbar-right">
      <span class="updated" id="last-updated"></span>
      <button class="btn-sm" onclick="refreshCurrent()">↺ Refresh</button>
    </div>
  </div>

  <div class="pages">

  <!-- ══ OVERVIEW ══ -->
  <div class="page active" id="page-overview">
    <div class="stat-grid" id="overview-stats">
      <div class="stat-card blue"><div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Faculty Tracked</div><div class="stat-val" id="s-faculty">—</div>
        <div class="stat-meta" id="s-faculty-meta"></div></div>
      <div class="stat-card green"><div class="stat-accent" style="background:var(--green)"></div>
        <div class="stat-label">Keyword Coverage</div><div class="stat-val" id="s-kw-pct">—</div>
        <div class="stat-meta" id="s-kw-meta"></div></div>
      <div class="stat-card yellow"><div class="stat-accent" style="background:var(--yellow)"></div>
        <div class="stat-label">Total Matches</div><div class="stat-val" id="s-matches">—</div>
        <div class="stat-meta" id="s-matches-meta"></div></div>
      <div class="stat-card" style="border-color:var(--border)"><div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">Semantic Coverage</div><div class="stat-val" id="s-emb-pct">—</div>
        <div class="stat-meta" id="s-emb-meta"></div></div>
      <div class="stat-card" style="border-color:var(--border)"><div class="stat-accent" style="background:var(--purple)"></div>
        <div class="stat-label">Last Grants Check</div><div class="stat-val" id="s-last-check" style="font-size:18px;padding-top:6px">—</div>
        <div class="stat-meta" id="s-last-check-meta"></div></div>
      <div class="stat-card" style="border-color:var(--border)"><div class="stat-accent" style="background:var(--yellow)"></div>
        <div class="stat-label">Last Faculty Scrape</div><div class="stat-val" id="s-last-scrape" style="font-size:18px;padding-top:6px">—</div>
        <div class="stat-meta" id="s-last-scrape-meta"></div></div>
    </div>

    <!-- Health Score + Countdown Row -->
    <div class="grid3" style="margin-bottom:18px">
      <div class="panel" style="margin-bottom:0">
        <div class="panel-hdr"><span class="panel-title">System Health</span></div>
        <div class="panel-body">
          <div class="health-ring-wrap">
            <div class="health-ring">
              <svg width="70" height="70" viewBox="0 0 70 70">
                <circle cx="35" cy="35" r="28" fill="none" stroke="var(--border2)" stroke-width="6"/>
                <circle cx="35" cy="35" r="28" fill="none" id="health-arc"
                  stroke="var(--green)" stroke-width="6" stroke-linecap="round"
                  stroke-dasharray="175.9" stroke-dashoffset="175.9"/>
              </svg>
              <div class="health-ring-val">
                <div class="health-ring-num" id="health-score">—</div>
                <div class="health-ring-lbl">health</div>
              </div>
            </div>
            <div class="health-items" id="health-items"></div>
          </div>
        </div>
      </div>
      <div class="countdown-card" style="margin-bottom:0">
        <div class="countdown-lbl">⏱ Next Grants Check</div>
        <div class="countdown-val" id="cd-check">—</div>
        <div class="countdown-sub" id="cd-check-sub"></div>
      </div>
      <div class="countdown-card" style="margin-bottom:0">
        <div class="countdown-lbl">🔄 Next Faculty Scrape</div>
        <div class="countdown-val" id="cd-scrape">—</div>
        <div class="countdown-sub" id="cd-scrape-sub"></div>
      </div>
    </div>

    <!-- Urgent deadlines strip -->
    <div class="urgency-strip" id="urgency-strip" style="display:none">
      <div class="urgency-strip-hdr">
        <span>⚠</span> Grants Closing Soon — click to view on Grants.gov
        <span class="sec-count" id="urgency-count" style="margin-left:auto"></span>
      </div>
      <div class="urgency-items" id="urgency-items"></div>
    </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Match Activity — Last 14 Days</span></div>
        <div class="panel-body"><div class="chart-wrap"><canvas id="chart-trend"></canvas></div></div>
      </div>
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Match Type Breakdown</span></div>
        <div class="panel-body">
          <div class="donut-wrap" style="height:200px">
            <canvas id="chart-donut"></canvas>
            <div class="donut-center"><div class="big" id="donut-total">—</div><div class="lbl">Total</div></div>
          </div>
        </div>
      </div>
    </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Top Agencies by Match Count</span></div>
        <div class="panel-body"><div class="chart-wrap"><canvas id="chart-agencies"></canvas></div></div>
      </div>
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Award Ceiling Distribution</span></div>
        <div class="panel-body"><div class="chart-wrap"><canvas id="chart-awards"></canvas></div></div>
      </div>
    </div>


      <div class="panel">
        <div class="panel-hdr">
          <span class="panel-title">Keyword Coverage by Department</span>
          <span class="sec-count" id="dept-cov-count" style="margin-left:auto"></span>
        </div>
        <div class="panel-body" id="dept-cov-body" style="max-height:340px;overflow-y:auto"></div>
      </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Last Faculty Scrape</span></div>
        <div class="panel-body p0"><div style="padding:16px" id="scrape-info"></div></div>
      </div>
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Last Grants Run</span></div>
        <div class="panel-body p0"><div style="padding:16px" id="grants-info"></div></div>
      </div>
    </div>

    <div class="panel" id="errors-panel">
      <div class="panel-hdr">
        <span class="panel-title">Recent Errors &amp; Warnings</span>
        <span class="sec-count" id="err-count-badge"></span>
      </div>
      <div class="panel-body p0" id="errors-body"></div>
    </div>
  </div>

  <!-- ══ MATCHES ══ -->
  <div class="page" id="page-matches">
    <div class="panel">
      <div class="toolbar">
        <input class="search-box" id="m-search" placeholder="Search grants, faculty, keywords…" oninput="debounce(loadMatches,400)()">
        <select class="filter-sel" id="m-type" onchange="loadMatches()">
          <option value="">All match types</option>
          <option value="keyword">Keyword only</option>
          <option value="semantic">Semantic only</option>
          <option value="both">Both (highest confidence)</option>
        </select>
        <select class="filter-sel" id="m-agency" onchange="loadMatches()">
          <option value="">All agencies</option>
        </select>
        <span class="sec-count" style="margin-left:auto" id="m-count-lbl"></span>
        <a href="/api/export/matches" id="m-export-link" class="btn-sm" style="text-decoration:none" download>↓ CSV</a>
      </div>
      <div id="matches-list" class="panel-body p0"></div>
      <div class="pagination" id="matches-pg" style="display:none"></div>
    </div>
  </div>


  <!-- ══ GRANT EXPLORER ══ -->
  <div class="page" id="page-grants">
    <div class="panel">
      <div class="toolbar">
        <input class="search-box" id="ge-search" placeholder="Search grants, faculty, keywords…" oninput="debounce(loadGrantExplorer,400)()" style="width:260px">
        <select class="filter-sel" id="ge-agency" onchange="loadGrantExplorer()"><option value="">All agencies</option></select>
        <select class="filter-sel" id="ge-type" onchange="loadGrantExplorer()">
          <option value="">All match types</option>
          <option value="keyword">Keyword only</option>
          <option value="semantic">Semantic only</option>
          <option value="both">Both</option>
        </select>
        <select class="filter-sel" id="ge-sort" onchange="loadGrantExplorer()">
          <option value="recent">Most Recent</option>
          <option value="faculty">Most Faculty</option>
          <option value="confidence">Highest Confidence</option>
          <option value="award">Largest Award</option>
          <option value="deadline">Earliest Deadline</option>
        </select>
        <span class="sec-count" style="margin-left:auto" id="ge-count-lbl"></span>
        <a href="/api/export/matches" class="btn-sm" style="text-decoration:none" download>↓ CSV</a>
      </div>
      <div id="grants-list" style="padding:0 18px 18px"></div>
      <div class="pagination" id="grants-pg" style="display:none"></div>
    </div>
  </div>

  <!-- ══ ANALYTICS ══ -->
  <div class="page" id="page-analytics">
    <div class="grid2">
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Top 15 Matched Faculty</span></div>
        <div class="panel-body"><div class="chart-wrap tall"><canvas id="chart-top-fac"></canvas></div></div>
      </div>
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Top 10 Matched Departments</span></div>
        <div class="panel-body"><div class="chart-wrap tall"><canvas id="chart-top-dept"></canvas></div></div>
      </div>
    </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Top 20 Matched Keywords</span></div>
        <div class="panel-body"><div class="chart-wrap tall"><canvas id="chart-top-kw"></canvas></div></div>
      </div>
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Keyword Source Breakdown</span></div>
        <div class="panel-body"><div class="chart-wrap tall"><canvas id="chart-sources"></canvas></div></div>
      </div>
    </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Match Score Distribution</span></div>
        <div class="panel-body"><div class="chart-wrap"><canvas id="chart-scores"></canvas></div></div>
      </div>
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Semantic Similarity Distribution</span></div>
        <div class="panel-body"><div class="chart-wrap"><canvas id="chart-sim"></canvas></div></div>
      </div>
    </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Avg Keywords / Faculty by Department</span></div>
        <div class="panel-body"><div class="chart-wrap tall"><canvas id="chart-dept-kw"></canvas></div></div>
      </div>
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Upcoming Grant Deadlines</span>
          <span class="sec-count" id="dl-count" style="margin-left:auto"></span></div>
        <div class="panel-body p0" id="deadline-list"></div>
      </div>
    </div>
  </div>


    <!-- New: Dept Leaderboard + Confidence Distribution + Keyword Deep-Dive -->
    <div class="panel">
      <div class="panel-hdr">
        <span class="panel-title">Department Match Leaderboard</span>
        <span class="sec-count" id="dept-lb-count" style="margin-left:auto"></span>
      </div>
      <div class="panel-body p0">
        <div class="tbl-wrap">
          <table>
            <thead><tr>
              <th>Department</th>
              <th style="text-align:right">Total Matches</th>
              <th style="text-align:right">Unique Grants</th>
              <th style="text-align:right">Faculty Matched</th>
              <th style="text-align:right">Avg Score</th>
              <th style="text-align:right">Semantic %</th>
            </tr></thead>
            <tbody id="dept-lb-tbody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Match Confidence Distribution</span></div>
        <div class="panel-body"><div class="chart-wrap"><canvas id="chart-confidence"></canvas></div></div>
      </div>
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Faculty Keyword Count Distribution</span></div>
        <div class="panel-body"><div class="chart-wrap"><canvas id="chart-kw-dist"></canvas></div></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-hdr">
        <span class="panel-title">Top Keywords — Detail View</span>
        <span style="font-size:10px;color:var(--text3);font-family:var(--mono);margin-left:8px">grant count · faculty count · top department</span>
      </div>
      <div class="panel-body p0">
        <div class="tbl-wrap">
          <table>
            <thead><tr>
              <th>Keyword</th>
              <th style="text-align:right">Grants Matched</th>
              <th style="text-align:right">Faculty Matched</th>
              <th>Top Department</th>
            </tr></thead>
            <tbody id="kw-detail-tbody"></tbody>
          </table>
        </div>
      </div>
    </div>

  <!-- ══ FACULTY ══ -->
  <div class="page" id="page-faculty">
    <div class="panel">
      <div class="toolbar">
        <input class="search-box" id="f-search" placeholder="Name or email…" oninput="debounce(loadFaculty,400)()">
        <select class="filter-sel" id="f-dept" onchange="loadFaculty()"><option value="">All departments</option></select>
        <input class="search-box" id="f-kw" placeholder="Filter by keyword…" oninput="debounce(loadFaculty,400)()" style="width:170px">
        <select class="filter-sel" id="f-source" onchange="loadFaculty()"><option value="">All sources</option></select>
        <select class="filter-sel" id="f-sort" onchange="loadFaculty()">
          <option value="name">Sort: Name</option>
          <option value="keywords">Sort: Most Keywords</option>
          <option value="matches">Sort: Most Matched</option>
        </select>
        <span class="sec-count" style="margin-left:auto" id="f-count-lbl"></span>
        <a href="/api/export/faculty" class="btn-sm" style="text-decoration:none" download>↓ CSV</a>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr>
            <th onclick="setFSort('name')">Name</th>
            <th>Department</th>
            <th>Email</th>
            <th onclick="setFSort('keywords')" style="text-align:right">Keywords</th>
            <th onclick="setFSort('matches')" style="text-align:right">Matches</th>
            <th>Sources</th>
            <th>Top Keywords</th>
          </tr></thead>
          <tbody id="f-tbody"></tbody>
        </table>
      </div>
      <div class="pagination" id="faculty-pg" style="display:none"></div>
    </div>
  </div>

  <!-- ══ PIPELINE ══ -->
  <div class="page" id="page-pipeline">
    <div class="stat-grid" id="pipeline-stats">
      <div class="stat-card blue"><div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Next Grants Check</div><div class="stat-val" id="p-next-check" style="font-size:18px;padding-top:6px">—</div></div>
      <div class="stat-card"><div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">Next Faculty Scrape</div><div class="stat-val" id="p-next-scrape" style="font-size:18px;padding-top:6px">—</div></div>
      <div class="stat-card green"><div class="stat-accent" style="background:var(--green)"></div>
        <div class="stat-label">Embedding Coverage</div><div class="stat-val" id="p-emb-pct">—</div>
        <div class="stat-meta" id="p-emb-meta"></div></div>
      <div class="stat-card"><div class="stat-accent" style="background:var(--yellow)"></div>
        <div class="stat-label">Log Lines</div><div class="stat-val" id="p-log-lines">—</div></div>
    </div>

    <div class="panel">
      <div class="panel-hdr"><span class="panel-title">Enrichment Pipeline — 7 Passes</span></div>
      <div class="panel-body">
        <div class="pass-grid" id="pass-grid"></div>
      </div>
    </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Embedding Coverage</span></div>
        <div class="panel-body">
          <div style="font-size:12px;color:var(--text2);margin-bottom:8px" id="emb-detail"></div>
          <div class="prog-wrap" style="height:10px">
            <div class="prog-fill" id="emb-prog" style="background:linear-gradient(90deg,var(--blue),var(--cyan))"></div>
          </div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-hdr"><span class="panel-title">Scrape Schedule</span></div>
        <div class="panel-body p0"><div style="padding:14px" id="schedule-info"></div></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-hdr"><span class="panel-title">Recent Errors</span></div>
      <div class="log-wrap" style="margin:0"><div class="log-body" id="pipeline-errors" style="max-height:220px"></div></div>
    </div>
  </div>

  <!-- ══ LOGS ══ -->
  <div class="page" id="page-logs">
    <div class="panel">
      <div class="toolbar">
        <input class="search-box" id="log-search" placeholder="Search logs…" oninput="debounce(loadLogs,400)()">
        <select class="filter-sel" id="log-level" onchange="loadLogs()">
          <option value="all">All levels</option>
          <option value="errors">Errors &amp; warnings</option>
        </select>
        <select class="filter-sel" id="log-module" onchange="loadLogs()">
          <option value="all">All modules</option>
        </select>
        <span class="sec-count" style="margin-left:auto" id="log-count-lbl"></span>
      </div>
      <div class="log-body" id="log-body" style="max-height:calc(100vh - 220px)"></div>
    </div>
  </div>

  </div><!-- /pages -->
</div><!-- /main -->

<script>
// ══ State ══
let currentPage = 'overview';
let matchesPage = 1, facultyPage = 1;
const charts = {};

// ══ Chart.js defaults ══
Chart.defaults.color = '#4b5675';
Chart.defaults.borderColor = '#1c2235';
Chart.defaults.font.family = "'IBM Plex Mono', monospace";
Chart.defaults.font.size = 10;

// ══ Navigation ══
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.getElementById('nav-' + name).classList.add('active');
  document.getElementById('topbar-title').textContent = {
    overview:'Overview', matches:'Grant Matches', analytics:'Analytics',
    faculty:'Faculty', pipeline:'Pipeline Status', logs:'System Logs'
  }[name];
  currentPage = name;
  ({overview:loadOverview, matches:loadMatches, grants:loadGrantExplorer,
    analytics:loadAnalytics, faculty:loadFaculty, pipeline:loadPipeline, logs:loadLogs})[name]?.();
}

function refreshCurrent() { showPage(currentPage); }

// ══ Debounce ══
const _dbt = {};
function debounce(fn, ms) {
  return function() {
    clearTimeout(_dbt[fn]);
    _dbt[fn] = setTimeout(() => fn(), ms);
  };
}

// ══ Chart helper ══
function mkChart(id, cfg) {
  if (charts[id]) charts[id].destroy();
  const ctx = document.getElementById(id);
  if (!ctx) return;
  charts[id] = new Chart(ctx, cfg);
  return charts[id];
}

const COLORS = {
  blue:'#3b82f6', cyan:'#06b6d4', green:'#22c55e',
  yellow:'#f59e0b', purple:'#a855f7', red:'#ef4444',
  orange:'#f97316', pink:'#ec4899', teal:'#14b8a6', indigo:'#6366f1'
};
const PALETTE = Object.values(COLORS);

function barDefaults(labels, data, color='#3b82f6', horizontal=false) {
  return {
    type: 'bar',
    data: {
      labels,
      datasets:[{data, backgroundColor: color + '33', borderColor: color,
        borderWidth:1, borderRadius:3}]
    },
    options:{
      indexAxis: horizontal ? 'y' : 'x',
      responsive:true, maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{
        x:{grid:{color:'#1c2235'},ticks:{color:'#4b5675'}},
        y:{grid:{color:'#1c2235'},ticks:{color:'#4b5675',
          callback: horizontal ? (v,i,a) => {
            const lbl = a[i]?.label || '';
            return lbl.length > 22 ? lbl.slice(0,22)+'…' : lbl;
          } : undefined
        }}
      }
    }
  };
}

// ══ OVERVIEW ══
async function loadOverview() {
  const d = await fetch('/api/stats').then(r=>r.json());

  // Stat cards
  setText('s-faculty', (d.total_faculty||0).toLocaleString());
  setText('s-faculty-meta', `${(d.faculty_with_keywords||0).toLocaleString()} with keywords`);
  setText('s-kw-pct', (d.keyword_coverage_pct||0) + '%');
  setText('s-kw-meta', 'of faculty have research keywords');
  setText('s-matches', (d.total_matches_logged||0).toLocaleString());
  setText('s-matches-meta', `kw: ${d.keyword_matches||0}  sem: ${d.semantic_matches||0}  both: ${d.both_matches||0}`);
  setText('s-emb-pct', (d.embedding_coverage_pct||0) + '%');
  setText('s-emb-meta', `${(d.faculty_with_embeddings||0).toLocaleString()} faculty embedded`);
  setText('s-last-check', d.grants_run?.time_ago || '—');
  setText('s-last-check-meta', d.grants_run?.timestamp ? fmtTs(d.grants_run.timestamp) : 'No run yet');
  setText('s-last-scrape', d.scrape?.time_ago || '—');
  setText('s-last-scrape-meta', d.scrape?.timestamp ? fmtTs(d.scrape.timestamp) : 'No scrape yet');

  // Sidebar badges
  const errBadge = document.getElementById('sb-error-count');
  if (d.error_count > 0) { errBadge.textContent=d.error_count; errBadge.style.display=''; }
  else errBadge.style.display='none';

  const matchBadge = document.getElementById('sb-match-count');
  if (d.total_matches_logged > 0) { matchBadge.textContent=d.total_matches_logged; matchBadge.style.display=''; }
  else matchBadge.style.display='none';

  // Trend chart
  const trend = d.match_trend || [];
  mkChart('chart-trend', {
    type:'line',
    data:{
      labels: trend.map(t => t.date.slice(5)),
      datasets:[
        {label:'Keyword',  data:trend.map(t=>t.keyword||0),  borderColor:COLORS.blue,
         backgroundColor:'rgba(59,130,246,.08)', fill:true, tension:.4, pointRadius:2},
        {label:'Semantic', data:trend.map(t=>t.semantic||0), borderColor:COLORS.purple,
         backgroundColor:'rgba(168,85,247,.06)', fill:true, tension:.4, pointRadius:2},
        {label:'Both',     data:trend.map(t=>t.both||0),     borderColor:COLORS.yellow,
         backgroundColor:'rgba(245,158,11,.06)', fill:true, tension:.4, pointRadius:2},
      ]
    },
    options:{responsive:true, maintainAspectRatio:false,
      plugins:{legend:{display:true, position:'top', labels:{padding:14, boxWidth:10}}},
      scales:{x:{grid:{color:'#1c2235'},ticks:{color:'#4b5675'}},
              y:{grid:{color:'#1c2235'},ticks:{color:'#4b5675'},beginAtZero:true}}}
  });

  // Donut
  const kw=d.keyword_matches||0, sem=d.semantic_matches||0, both=d.both_matches||0;
  setText('donut-total', (d.total_matches_logged||0).toLocaleString());
  mkChart('chart-donut', {
    type:'doughnut',
    data:{labels:['Keyword','Semantic','Both'],
          datasets:[{data:[kw,sem,both],
            backgroundColor:['rgba(59,130,246,.7)','rgba(168,85,247,.7)','rgba(245,158,11,.7)'],
            borderColor:['#3b82f6','#a855f7','#f59e0b'], borderWidth:2, hoverOffset:4}]},
    options:{responsive:true, maintainAspectRatio:false, cutout:'72%',
      plugins:{legend:{display:true, position:'right',
        labels:{padding:14, boxWidth:10, color:'#94a3b8'}}}}
  });

  // Agencies
  const ag = d.top_agencies || [];
  mkChart('chart-agencies', barDefaults(
    ag.map(a=>a.agency.replace('Department of ','Dept. of ').slice(0,28)),
    ag.map(a=>a.count), COLORS.cyan, true
  ));

  // Awards
  const aw = d.award_buckets || [];
  mkChart('chart-awards', barDefaults(
    aw.map(a=>a.label), aw.map(a=>a.count), COLORS.yellow
  ));

  // Dept coverage bars
  const dc = d.dept_coverage || [];
  setText('dept-cov-count', dc.length + ' departments');
  const dcEl = document.getElementById('dept-cov-body');
  if (dcEl) {
    if (!dc.length) {
      dcEl.innerHTML = '<div class="empty" style="padding:24px"><div class="empty-msg">No department data yet</div></div>';
    } else {
      dcEl.innerHTML = dc.map(d => `
        <div class="dept-cov-row">
          <div class="dept-cov-name" title="${esc(d.dept)}">${esc(d.dept.replace('Department of ','Dept. '))}</div>
          <div class="dept-cov-bar"><div class="dept-cov-fill" style="width:${d.pct}%"></div></div>
          <div class="dept-cov-pct">${d.pct}%</div>
          <div class="dept-cov-n">${d.with_kw}/${d.total}</div>
        </div>`).join('');
    }
  }

  // Scrape info
  const sc = d.scrape || {};
  document.getElementById('scrape-info').innerHTML = infoRows([
    ['Timestamp',      fmtTs(sc.timestamp)],
    ['Time ago',       sc.time_ago || '—'],
    ['Total faculty',  (sc.total_faculty||0).toLocaleString()],
    ['With keywords',  (sc.faculty_with_keywords||0).toLocaleString()],
    ['Dept pages',     sc.department_pages_scraped||'—'],
    ['Errors',         sc.pages_errored||0, sc.pages_errored > 0 ? 'bad':'good'],
  ]);

  // Grants run info
  const gr = d.grants_run || {};
  document.getElementById('grants-info').innerHTML = infoRows([
    ['Timestamp',        fmtTs(gr.timestamp)],
    ['Time ago',         gr.time_ago||'—'],
    ['Grants retrieved', (gr.grants_retrieved||0).toLocaleString()],
    ['New (unseen)',     (gr.new_grants_found||0).toLocaleString()],
    ['With matches',     (gr.grants_with_matches||0).toLocaleString()],
    ['Keyword matches',  (gr.keyword_matches||0).toLocaleString()],
    ['Semantic matches', gr.semantic_matching_used ? (gr.semantic_matches||0).toLocaleString() : 'disabled',
     gr.semantic_matching_used ? 'good' : 'warn'],
    ['Total seen (DB)',  (gr.seen_grants_total||0).toLocaleString()],
  ]);

  // Health score ring
  renderHealthScore(d);

  // Countdown timers
  initCountdowns(d);

  // Errors
  const errs = d.recent_errors || [];
  const ep = document.getElementById('errors-panel');
  setText('err-count-badge', errs.length + ' events');
  if (errs.length === 0) {
    document.getElementById('errors-body').innerHTML =
      '<div class="empty" style="padding:24px"><div class="empty-icon">✓</div><div class="empty-msg" style="color:var(--green)">No recent errors or warnings</div></div>';
    ep.style.borderColor='rgba(34,197,94,.3)';
  } else {
    document.getElementById('errors-body').innerHTML =
      `<div class="log-body" style="max-height:200px;padding:12px">${errs.slice(-15).map(fmtLog).join('')}</div>`;
    ep.style.borderColor='rgba(239,68,68,.3)';
  }

  // Urgency strip — show closing grants from match history
  try {
    const matchData = await fetch('/api/matches?per_page=200').then(r=>r.json());
    renderUrgencyStrip(matchData.matches||[]);
  } catch(e) {}

  setUpdated();
}

// ══ MATCHES ══
async function loadMatches() {
  const search = document.getElementById('m-search').value;
  const mtype  = document.getElementById('m-type').value;
  const agency = document.getElementById('m-agency').value;
  const params = new URLSearchParams({search, match_type:mtype, agency, page:matchesPage, per_page:15});
  const d = await fetch('/api/matches?'+params).then(r=>r.json());

  // Populate agency dropdown once
  const agSel = document.getElementById('m-agency');
  if (agSel.options.length <= 1 && d.agencies) {
    d.agencies.forEach(a => {
      const o = document.createElement('option');
      o.value = a; o.textContent = a.slice(0,40);
      agSel.appendChild(o);
    });
    agSel.value = agency;
  }

  setText('m-count-lbl', (d.total||0).toLocaleString() + ' match records');

  const list = document.getElementById('matches-list');
  if (!d.matches?.length) {
    list.innerHTML = '<div class="empty"><div class="empty-icon">⟡</div><div class="empty-msg">No matches yet — they appear after the first grant cycle runs</div></div>';
    document.getElementById('matches-pg').style.display='none';
    return;
  }

  // Group by grant_id
  const grouped = {};
  d.matches.forEach(m => {
    if (!grouped[m.grant_id]) grouped[m.grant_id] = {grant:m, faculty:[]};
    grouped[m.grant_id].faculty.push(m);
  });

  list.innerHTML = Object.values(grouped).map((g,gi) => {
    const grant = g.grant;
    const hasSynopsis = grant.grant_synopsis?.length > 10;
    const fRows = g.faculty.map((m,fi) => {
      const initials = (m.faculty_name||'?').split(/[\s,]+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('');
      const confScore = m.confidence_score != null ? m.confidence_score : Math.min(Math.round((Math.min((m.match_score||0)/10,1))*100), 99);
      const scoreHi = confScore >= 50;
      const isKw  = m.match_type === 'keyword';
      const isSem = m.match_type === 'semantic';
      const isBoth = m.match_type === 'both';
      const simPct = m.similarity_score ? Math.round(m.similarity_score*100) : 0;

      // Build explain panel
      let explainHtml = '';
      if (isKw || isBoth) {
        const kws = (m.matched_keywords||[]);
        explainHtml += `<div class="explain-panel" id="exp-${gi}-${fi}" style="display:none">
          <div class="lbl">Why this matched</div>
          ${kws.length ? `<div style="margin-bottom:4px"><span style="color:var(--text3)">Matched keywords:</span> ${kws.map(k=>`<span class="explain-kw">${esc(k)}</span>`).join(' · ')}</div>` : ''}
          ${isBoth ? `<div style="margin-top:4px"><span style="color:var(--text3)">Also matched semantically</span> — <span style="color:var(--purple)">${simPct}% similarity</span> to faculty research profile</div>` : ''}
          <div class="explain-why">The grant's text contains ${kws.length} of this faculty member's research keywords${isBoth ? ', and their overall research profile has strong semantic alignment with this grant' : ''}.</div>
        </div>
        <div class="expand-toggle" onclick="toggleExp('exp-${gi}-${fi}',this)">▶ Show match explanation</div>`;
      } else if (isSem) {
        explainHtml = `<div class="explain-panel" id="exp-${gi}-${fi}" style="display:none">
          <div class="lbl">Why this matched (semantic)</div>
          <div style="margin-bottom:4px"><span style="color:var(--text3)">Similarity score:</span> <span style="color:var(--purple)">${simPct}%</span></div>
          <div class="explain-why">No exact keyword overlap was found, but the vector embedding of this faculty member's research profile is <strong style="color:var(--purple)">${simPct}% similar</strong> to the grant's text. This means their research area aligns conceptually even when the specific vocabulary differs — e.g. "neurodegeneration" matching "Alzheimer's disease research."</div>
        </div>
        <div class="expand-toggle" onclick="toggleExp('exp-${gi}-${fi}',this)">▶ Show match explanation</div>`;
      }

      return `<div class="fac-row">
        <div class="fac-avatar">${esc(initials)}</div>
        <div class="fac-info">
          <a class="fac-name-link" href="${m.faculty_url||'#'}" target="_blank">${esc(m.faculty_name)}</a>
          <div class="fac-dept">${esc(m.faculty_department||'—')}</div>
          ${m.faculty_email ? `<div class="fac-email">${esc(m.faculty_email)}</div>` : ''}
          <div class="kw-list" style="margin-top:7px">
            ${(m.matched_keywords||[]).map(k=>`<span class="kw match">${esc(k)}</span>`).join('')}
            ${isSem && !(m.matched_keywords||[]).length ? `<span style="font-size:11px;color:var(--purple);font-style:italic;">Matched by research area similarity (semantic)</span>` : ''}
          </div>
          ${explainHtml}
        </div>
        <div class="fac-right">
          <div class="score ${scoreHi?'hi':''}" title="Confidence score (IDF-weighted)">${confScore}%</div>
          <span class="mt-badge mt-${m.match_type||'keyword'}">${m.match_type||'keyword'}</span>
          ${(() => {
            // Confidence = blend of keyword score (0-10 → 50%) + semantic sim (0-1 → 50%)
            const kwConf = confScore / 100;
            const semConf = m.similarity_score || 0;
            const conf = m.match_type === 'both' ? Math.round((kwConf*0.5 + semConf*0.5)*100)
                       : m.match_type === 'semantic' ? Math.round(semConf*100)
                       : Math.round(kwConf*100);
            const barColor = conf >= 70 ? 'var(--green)' : conf >= 45 ? 'var(--yellow)' : 'var(--blue)';
            return `<div class="conf-wrap" style="margin-top:4px">
              <div class="conf-bar-outer"><div class="conf-bar-inner" style="width:${conf}%;background:${barColor}"></div></div>
              <div class="conf-label">${conf}%</div>
            </div>
            <div style="font-size:9px;color:var(--text3);font-family:var(--mono);text-align:right;margin-top:2px">confidence</div>`;
          })()}
        </div>
      </div>`;
    }).join('');

    return `<div class="match-card">
      <div class="match-card-hdr">
        <div class="match-title">${esc(grant.grant_title)}${(() => {
          try {
            const c = parseInt(String(grant.grant_award_ceiling||'0').replace(/\D/g,''));
            if (c >= 1000000) return `<span class="hv-badge">$${c>=1000000?Math.round(c/1000000)+'M':Math.round(c/1000)+'K'}+ award</span>`;
          } catch(e) {} return '';
        })()}</div>
        <div class="match-meta">
          <div class="meta-chip">Agency: <span>${esc(grant.grant_agency||'—')}</span></div>
          <div class="meta-chip">Number: <span>${esc(grant.grant_number||'—')}</span></div>
          <div class="meta-chip">Closes: <span>${grant.grant_close_date||'—'}</span></div>
          ${grant.grant_award_ceiling ? `<div class="meta-chip award">Award: <span>$${Number(String(grant.grant_award_ceiling).replace(/\D/g,'')||0).toLocaleString()}</span></div>` : ''}
          <div class="meta-chip">Matched: <span>${g.faculty.length} faculty</span></div>
          <div class="meta-chip">Detected: <span>${fmtTs(grant.timestamp)}</span></div>
          <a href="${grant.grant_link||'#'}" target="_blank" class="btn-sm" style="margin-left:auto;text-decoration:none">View on Grants.gov ↗</a>
        </div>
      </div>
      ${(() => {
        // Urgency banner
        const closeDate = grant.grant_close_date;
        if (closeDate) {
          try {
            const daysLeft = Math.round((new Date(closeDate) - new Date()) / 86400000);
            if (daysLeft >= 0 && daysLeft <= 7)
              return `<div class="urgency-banner urgency-critical">⚠ Closes in ${daysLeft} day${daysLeft!==1?'s':''}! — ${closeDate}</div>`;
            if (daysLeft > 7 && daysLeft <= 21)
              return `<div class="urgency-banner urgency-warning">⏱ Closes in ${daysLeft} days — ${closeDate}</div>`;
          } catch(e) {}
        }
        return '';
      })()}
      ${hasSynopsis ? `<div class="synopsis-row" onclick="toggleSynopsis(this)">
        <div class="synopsis-toggle">▶ Grant Synopsis</div>
        <div class="synopsis-text">${esc(grant.grant_synopsis)}</div>
      </div>` : ''}
      <div class="match-body">${fRows}</div>
    </div>`;
  }).join('');

  // Pagination
  const pg = document.getElementById('matches-pg');
  if (d.pages > 1) {
    pg.style.display='flex';
    pg.innerHTML = renderPg(d.page, d.pages, p => { matchesPage=p; loadMatches(); });
  } else pg.style.display='none';

  setUpdated();
}

function toggleSynopsis(el) {
  const txt = el.querySelector('.synopsis-text');
  const tog = el.querySelector('.synopsis-toggle');
  const open = txt.style.display !== 'none' && txt.style.display !== '';
  txt.style.display = open ? 'none' : 'block';
  tog.textContent = (open ? '▶' : '▼') + ' Grant Synopsis';
}

function toggleExp(id, el) {
  const panel = document.getElementById(id);
  const open = panel.style.display === 'block';
  panel.style.display = open ? 'none' : 'block';
  el.textContent = (open ? '▶' : '▼') + ' ' + (open ? 'Show' : 'Hide') + ' match explanation';
}

// ══ ANALYTICS ══
async function loadAnalytics() {
  const d = await fetch('/api/analytics').then(r=>r.json());

  const topFac  = d.top_faculty || [];
  const topDept = d.top_departments || [];
  const topKw   = d.top_keywords || [];
  const sources = d.source_breakdown || [];
  const scores  = d.score_distribution || [];
  const sims    = d.sim_distribution || [];
  const deptKw  = d.dept_avg_keywords || [];
  const dl      = d.upcoming_deadlines || [];

  mkChart('chart-top-fac', barDefaults(
    topFac.map(f=>f.name.split(',')[0].slice(0,22)), topFac.map(f=>f.count), COLORS.blue, true));

  mkChart('chart-top-dept', barDefaults(
    topDept.map(d=>d.dept.slice(0,28)), topDept.map(d=>d.count), COLORS.cyan, true));

  mkChart('chart-top-kw', barDefaults(
    topKw.map(k=>k.keyword.slice(0,20)), topKw.map(k=>k.count), COLORS.green, true));

  mkChart('chart-sources', {
    type:'doughnut',
    data:{labels:sources.map(s=>s.source), datasets:[{
      data:sources.map(s=>s.count),
      backgroundColor:PALETTE.slice(0,sources.length).map(c=>c+'99'),
      borderColor:PALETTE.slice(0,sources.length), borderWidth:2
    }]},
    options:{responsive:true, maintainAspectRatio:false, cutout:'60%',
      plugins:{legend:{display:true, position:'right',
        labels:{padding:12, boxWidth:10, color:'#94a3b8'}}}}
  });

  mkChart('chart-scores', barDefaults(
    scores.map(s=>s.score), scores.map(s=>s.count), COLORS.yellow));

  mkChart('chart-sim', barDefaults(
    sims.map(s=>s.range), sims.map(s=>s.count), COLORS.purple));

  mkChart('chart-dept-kw', barDefaults(
    deptKw.map(d=>d.dept.slice(0,26)), deptKw.map(d=>d.avg_keywords), COLORS.teal, true));

  // Deadline list
  setText('dl-count', dl.length + ' upcoming');
  const dlEl = document.getElementById('deadline-list');
  if (!dl.length) {
    dlEl.innerHTML = '<div class="empty" style="padding:24px"><div class="empty-msg">No deadline data yet</div></div>';
  } else {
    dlEl.innerHTML = '<div style="padding:16px">' + dl.map(d => {
      const cls = d.days_left < 0 ? 'overdue' : d.days_left < 14 ? 'soon' : 'ok';
      const label = d.days_left < 0 ? `${Math.abs(d.days_left)}d ago` : d.days_left === 0 ? 'Today!' : `${d.days_left}d`;
      return `<div class="deadline-row">
        <div class="dl-days ${cls}">${label}</div>
        <div class="dl-info">
          <div class="dl-title" title="${esc(d.title)}">${esc(d.title)}</div>
          <div class="dl-meta">${esc(d.agency)} · ${d.faculty_count} faculty match${d.faculty_count!==1?'es':''} · Closes ${d.close_date}</div>
        </div>
      </div>`;
    }).join('') + '</div>';
  }

  // Load deep keyword analysis (separate endpoint)
  loadKeywordAnalysis();

  setUpdated();
}

// ══ FACULTY ══
async function loadFaculty() {
  const search = document.getElementById('f-search').value;
  const dept   = document.getElementById('f-dept').value;
  const kw     = document.getElementById('f-kw').value;
  const source = document.getElementById('f-source').value;
  const sort   = document.getElementById('f-sort').value;
  const params = new URLSearchParams({search, dept, keywords:kw, source, sort, page:facultyPage, per_page:50});
  const d = await fetch('/api/faculty?'+params).then(r=>r.json());

  // Populate dropdowns once
  const deptSel = document.getElementById('f-dept');
  if (deptSel.options.length <= 1 && d.departments) {
    d.departments.forEach(dep => { const o=document.createElement('option'); o.value=dep; o.textContent=dep; deptSel.appendChild(o); });
    deptSel.value = dept;
  }
  const srcSel = document.getElementById('f-source');
  if (srcSel.options.length <= 1 && d.sources) {
    d.sources.forEach(s => { const o=document.createElement('option'); o.value=s; o.textContent=s; srcSel.appendChild(o); });
    srcSel.value = source;
  }

  setText('f-count-lbl', (d.total||0).toLocaleString()+' faculty');

  const tbody = document.getElementById('f-tbody');
  if (!d.faculty?.length) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty"><div class="empty-icon">◉</div><div class="empty-msg">No faculty found</div></div></td></tr>`;
  } else {
    tbody.innerHTML = d.faculty.map(f => {
      const kwCount = (f.keywords||[]).length;
      const topKws  = (f.keywords||[]).slice(0,5);
      const sources = parseSources(f.keyword_sources||[]);
      return `<tr>
        <td><div class="td-name"><a href="${f.url||f.profile_url||'#'}" target="_blank" style="color:inherit;text-decoration:none">${esc(f.name)}</a></div></td>
        <td class="td-dept">${esc(f.department||'—')}</td>
        <td class="td-email">${f.email?`<a href="mailto:${esc(f.email)}" style="color:inherit">${esc(f.email)}</a>`:'—'}</td>
        <td class="td-num"><span class="tip" data-tip="${kwCount} keywords from ${sources.length} sources">${kwCount}</span></td>
        <td class="td-num">${f.match_count > 0 ? `<span style="color:var(--green)">${f.match_count}</span>` : `<span style="color:var(--text3)">0</span>`}</td>
        <td><div class="kw-list">${sources.map(s=>`<span class="src-badge src-${s}">${s}</span>`).join('')}</div></td>
        <td><div class="kw-list">${topKws.map(k=>`<span class="kw">${esc(k)}</span>`).join('')}${kwCount>5?`<span class="kw" style="opacity:.5">+${kwCount-5}</span>`:''}</div></td>
      </tr>`;
    }).join('');
  }

  const pg = document.getElementById('faculty-pg');
  if (d.pages > 1) { pg.style.display='flex'; pg.innerHTML=renderPg(d.page, d.pages, p=>{facultyPage=p;loadFaculty();}); }
  else pg.style.display='none';

  setUpdated();
}

function setFSort(s) {
  document.getElementById('f-sort').value = s;
  facultyPage = 1;
  loadFaculty();
}

function parseSources(sources) {
  const seen = new Set();
  sources.forEach(s => {
    const b = s.split('(')[0].trim().toLowerCase()
                .replace('umsom_profile','umsom').replace('semantic_scholar','s2')
                .replace('nih_reporter','nih').replace('pubmed','pubmed')
                .replace('orcid','orcid');
    seen.add(b);
  });
  return [...seen];
}

// ══ PIPELINE ══
async function loadPipeline() {
  const d = await fetch('/api/pipeline').then(r=>r.json());

  setText('p-next-check',  d.next_grants_check || '—');
  setText('p-next-scrape', d.next_scrape || '—');
  setText('p-emb-pct',     (d.embedding_pct||0) + '%');
  setText('p-emb-meta',    `${(d.faculty_with_embeddings||0).toLocaleString()} / ${(d.faculty_total||0).toLocaleString()} faculty`);
  setText('p-log-lines',   (d.log_total_lines||0).toLocaleString());

  // Passes
  const grid = document.getElementById('pass-grid');
  grid.innerHTML = Object.entries(d.passes||{}).map(([pk, pv]) => {
    const st = pv.status || 'unknown';
    return `<div class="pass-card">
      <div class="pass-num">${pk}</div>
      <div class="pass-name">${pv.name}</div>
      <span class="pass-status ps-${st}">${st}</span>
    </div>`;
  }).join('');

  // Embedding progress
  const pct = d.embedding_pct || 0;
  document.getElementById('emb-prog').style.width = pct + '%';
  document.getElementById('emb-detail').textContent =
    `${(d.faculty_with_embeddings||0).toLocaleString()} of ${(d.faculty_total||0).toLocaleString()} faculty have semantic embeddings (${pct}%)`;

  // Schedule info
  const sc = d.last_scrape || {};
  const gr = d.last_grants_run || {};
  document.getElementById('schedule-info').innerHTML = infoRows([
    ['Last scrape',       sc.time_ago || '—'],
    ['Next scrape',       d.next_scrape || '—'],
    ['Last grants check', gr.time_ago || '—'],
    ['Next grants check', d.next_grants_check || '—'],
    ['Scrape interval',   '7 days (168 hours)'],
    ['Check interval',    '24 hours'],
  ]);

  // Errors
  const errs = [...(d.recent_errors||[]), ...(d.recent_warnings||[])].slice(-20);
  const errEl = document.getElementById('pipeline-errors');
  errEl.innerHTML = errs.length
    ? errs.map(fmtLog).join('')
    : '<div style="color:var(--green);font-family:var(--mono);font-size:11px;padding:4px">✓ No recent errors</div>';

  setUpdated();
}

// ══ LOGS ══
async function loadLogs() {
  const search = document.getElementById('log-search').value;
  const level  = document.getElementById('log-level').value;
  const module = document.getElementById('log-module').value;
  const params = new URLSearchParams({search, level, module});
  const d = await fetch('/api/logs?'+params).then(r=>r.json());

  const modSel = document.getElementById('log-module');
  if (modSel.options.length <= 1 && d.modules) {
    d.modules.forEach(m => { const o=document.createElement('option'); o.value=m; o.textContent=m; modSel.appendChild(o); });
    modSel.value = module;
  }

  setText('log-count-lbl', (d.total||0) + ' lines');
  const body = document.getElementById('log-body');
  if (!d.lines?.length) {
    body.innerHTML = '<div style="color:var(--text3);font-family:var(--mono);font-size:11px;padding:8px">No log entries found</div>';
    return;
  }
  body.innerHTML = d.lines.map(fmtLog).join('');
  body.scrollTop = body.scrollHeight;
}

// ══ Helpers ══
function fmtLog(line) {
  const isE = line.includes('[ERROR]'), isW = line.includes('[WARNING]');
  const cls = isE ? 'error' : isW ? 'warning' : 'info';
  const m = line.match(/^(\d{4}-\d{2}-\d{2} [\d:,]+)\s+\[(\w+)\]\s+([\w.]+):\s+(.*)$/);
  if (m) {
    const [,ts,lv,mod,msg] = m;
    const lc = lv==='ERROR'?'log-lvl-err':lv==='WARNING'?'log-lvl-warn':'log-lvl-info';
    return `<div class="log-line ${cls}"><span class="log-ts">${esc(ts)}</span> <span class="${lc}">[${lv}]</span> <span class="log-module">${esc(mod)}</span>: <span class="log-msg">${esc(msg)}</span></div>`;
  }
  return `<div class="log-line ${cls}">${esc(line)}</div>`;
}

function infoRows(rows) {
  return rows.map(([k,v,cls]) =>
    `<div class="info-row"><div class="info-k">${k}</div><div class="info-v${cls?' '+cls:''}">${v ?? '—'}</div></div>`
  ).join('');
}

function renderPg(page, pages, cb) {
  let h = `<span class="pg-info">Page ${page} of ${pages}</span>`;
  h += `<button class="pg-btn" onclick="(${cb.toString()})(${page-1})" ${page<=1?'disabled':''}>‹</button>`;
  const range = [];
  for (let i=Math.max(1,page-2);i<=Math.min(pages,page+2);i++) range.push(i);
  range.forEach(p => { h += `<button class="pg-btn ${p===page?'active':''}" onclick="(${cb.toString()})(${p})">${p}</button>`; });
  h += `<button class="pg-btn" onclick="(${cb.toString()})(${page+1})" ${page>=pages?'disabled':''}>›</button>`;
  return h;
}

function setText(id, v) { const el=document.getElementById(id); if(el) el.textContent=v; }
function fmtTs(iso) { if(!iso)return'—'; try{return new Date(iso+'Z').toLocaleString();}catch{return iso;} }
function esc(s) { if(!s)return''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function setUpdated() { document.getElementById('last-updated').textContent = 'Updated '+new Date().toLocaleTimeString(); }


// ══ GRANT EXPLORER ══
let grantsPage = 1;
async function loadGrantExplorer() {
  const search = document.getElementById('ge-search').value;
  const agency = document.getElementById('ge-agency').value;
  const mtype  = document.getElementById('ge-type').value;
  const sort   = document.getElementById('ge-sort').value;
  const params = new URLSearchParams({search, agency, match_type:mtype, sort, page:grantsPage, per_page:12});
  const d = await fetch('/api/grants?'+params).then(r=>r.json());

  // Populate agency dropdown once
  const agSel = document.getElementById('ge-agency');
  if (agSel.options.length <= 1 && d.agencies) {
    d.agencies.forEach(a => {
      const o = document.createElement('option');
      o.value = a; o.textContent = a.slice(0,40);
      agSel.appendChild(o);
    });
    agSel.value = agency;
  }

  setText('ge-count-lbl', `${(d.total||0)} matching (${d.unique_grants||0} unique grants)`);
  const badge = document.getElementById('sb-grants-count');
  if (badge && d.unique_grants > 0) { badge.textContent = d.unique_grants; badge.style.display=''; }

  const list = document.getElementById('grants-list');
  if (!d.grants?.length) {
    list.innerHTML = '<div class="empty"><div class="empty-icon">◧</div><div class="empty-msg">No grants yet — they appear after the first matching cycle</div></div>';
    document.getElementById('grants-pg').style.display='none';
    return;
  }

  list.innerHTML = d.grants.map((g, gi) => {
    const daysLeft = g.days_until_close;
    const urgCls   = daysLeft === null ? '' : daysLeft < 0 ? 'urgent' : daysLeft <= 7 ? 'urgent' : daysLeft <= 21 ? '' : 'ok';
    const daysLbl  = daysLeft === null ? '—' : daysLeft < 0 ? `${Math.abs(daysLeft)}d ago` : daysLeft === 0 ? 'Today!' : `${daysLeft}d`;
    const awardFmt = g.award_int > 0 ? ('$' + (g.award_int>=1000000 ? (g.award_int/1000000).toFixed(1)+'M' : Math.round(g.award_int/1000)+'K')) : '—';
    const confColor = g.confidence >= 70 ? 'var(--green)' : g.confidence >= 45 ? 'var(--yellow)' : 'var(--blue)';
    const types = (g.match_types||[]).join('+');

    // SVG confidence ring
    const circ = 2*Math.PI*16; // r=16
    const filled = circ * (g.confidence||0) / 100;
    const ringColor = g.confidence >= 70 ? '#22c55e' : g.confidence >= 45 ? '#f59e0b' : '#3b82f6';
    const confSvg = `<svg class="ge-conf-ring" viewBox="0 0 44 44">
      <circle cx="22" cy="22" r="16" fill="none" stroke="#1c2235" stroke-width="4"/>
      <circle cx="22" cy="22" r="16" fill="none" stroke="${ringColor}" stroke-width="4"
        stroke-linecap="round" stroke-dasharray="${circ}" stroke-dashoffset="${circ-filled}"
        transform="rotate(-90 22 22)"/>
      <text x="22" y="26" text-anchor="middle" font-family="IBM Plex Mono,monospace"
        font-size="9" font-weight="700" fill="${ringColor}">${g.confidence}%</text>
    </svg>`;

    // Faculty chips
    const facs = (g.faculty_matches||[]).slice(0,6);
    const extraFac = (g.faculty_matches||[]).length - 6;
    const facChips = facs.map(m => `
      <div class="ge-fac-chip" title="${esc(m.faculty_name)} — ${esc(m.faculty_department||'')}">
        <div>
          <div class="ge-fac-name">${esc(m.faculty_name.split(',')[0].slice(0,24))}</div>
          <div class="ge-fac-dept">${esc((m.faculty_department||'').slice(0,28))}</div>
        </div>
        <span class="ge-fac-mt ${m.match_type||'keyword'}">${m.match_type||'kw'}</span>
      </div>`).join('');

    // Top keywords (highlighted)
    const kwCloud = (g.all_keywords||[]).slice(0,12).map(k =>
      `<span class="kw match" style="cursor:default">${esc(k)}</span>`).join('');

    return `<div class="grant-exp-card">
      <div class="ge-hdr">
        <div class="ge-title">
          ${esc(g.grant_title)}
          ${g.award_int >= 1000000 ? `<span class="hv-badge">${awardFmt}+ award</span>` : ''}
        </div>
        ${confSvg}
      </div>
      <div class="ge-pills">
        <div class="ge-pill"><span>${esc(g.grant_agency||'—')}</span></div>
        <div class="ge-pill"><span>${esc(g.grant_number||'—')}</span></div>
        <div class="ge-pill award">Award: <span>${awardFmt}</span></div>
        <div class="ge-pill ${urgCls}">Closes: <span>${g.grant_close_date||'—'} (${daysLbl})</span></div>
        <div class="ge-pill">Matched: <span>${g.faculty_count} faculty · ${types}</span></div>
        <div class="ge-pill">Detected: <span>${fmtTs(g.first_seen)}</span></div>
        <a href="${g.grant_link||'#'}" target="_blank" class="btn-sm" style="margin-left:auto;text-decoration:none;font-size:10px">Grants.gov ↗</a>
      </div>
      <div class="ge-body">
        <div class="ge-fac-chips">${facChips}${extraFac>0?`<div class="ge-fac-chip" style="opacity:.6">+${extraFac} more</div>`:''}</div>
        <div class="ge-kw-cloud">${kwCloud}</div>
        ${g.grant_synopsis ? `
        <div style="margin-top:8px">
          <span class="ge-expand-btn" onclick="toggleGeSynopsis(this)">▶ Show synopsis</span>
          <div class="ge-synopsis">${highlightKeywords(esc(g.grant_synopsis), g.all_keywords||[])}</div>
        </div>` : ''}
      </div>
    </div>`;
  }).join('');

  const pg = document.getElementById('grants-pg');
  if (d.pages > 1) {
    pg.style.display='flex';
    pg.innerHTML = renderPg(d.page, d.pages, p => { grantsPage=p; loadGrantExplorer(); });
  } else pg.style.display='none';

  setUpdated();
}

function toggleGeSynopsis(btn) {
  const syn = btn.nextElementSibling;
  const open = syn.classList.contains('open');
  syn.classList.toggle('open', !open);
  btn.textContent = (open ? '▶ Show' : '▼ Hide') + ' synopsis';
}

function highlightKeywords(html, keywords) {
  // Highlight matched keywords in synopsis text
  let result = html;
  (keywords||[]).slice(0,15).forEach(kw => {
    const escaped = kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp('\\b(' + escaped + ')\\b', 'gi');
    result = result.replace(re, '<span class="kw-hl">$1</span>');
  });
  return result;
}

// ══ HEALTH SCORE (Overview) ══
function computeHealth(d) {
  let score = 0, items = [];
  // Faculty with keywords
  const kwPct = d.keyword_coverage_pct || 0;
  if (kwPct >= 80)      { score += 25; items.push({ok:'ok', txt:`Keyword coverage: ${kwPct}%`}); }
  else if (kwPct >= 50) { score += 15; items.push({ok:'warn', txt:`Keyword coverage: ${kwPct}% (low)`}); }
  else                  { score += 5;  items.push({ok:'err', txt:`Keyword coverage: ${kwPct}% (poor)`}); }

  // Embedding coverage
  const embPct = d.embedding_coverage_pct || 0;
  if (embPct >= 90)     { score += 25; items.push({ok:'ok', txt:`Embeddings: ${embPct}%`}); }
  else if (embPct >= 50){ score += 15; items.push({ok:'warn', txt:`Embeddings: ${embPct}% (partial)`}); }
  else if (embPct > 0)  { score += 8;  items.push({ok:'warn', txt:`Embeddings: ${embPct}% (low)`}); }
  else                  { score += 0;  items.push({ok:'err', txt:'Embeddings: not generated'}); }

  // Recency of grants check
  const gr = d.grants_run || {};
  const checkAge = gr.timestamp ? (Date.now()-new Date(gr.timestamp+'Z').getTime())/3600000 : 9999;
  if (checkAge < 26)     { score += 25; items.push({ok:'ok', txt:`Grants checked ${gr.time_ago||'recently'}`}); }
  else if (checkAge < 50){ score += 15; items.push({ok:'warn', txt:`Grants check: ${gr.time_ago||'?'} (overdue)`}); }
  else                   { score += 0;  items.push({ok:'err', txt:'Grants check overdue'}); }

  // Recency of scrape
  const sc = d.scrape || {};
  const scrapeAge = sc.timestamp ? (Date.now()-new Date(sc.timestamp+'Z').getTime())/3600000 : 9999;
  if (scrapeAge < 170)   { score += 25; items.push({ok:'ok', txt:`Scrape: ${sc.time_ago||'recent'}`}); }
  else if (scrapeAge < 220){ score += 15; items.push({ok:'warn', txt:`Scrape: ${sc.time_ago||'?'} (overdue)`}); }
  else                   { score += 0;  items.push({ok:'err', txt:'Faculty scrape overdue'}); }

  // Error count penalty
  if (d.error_count > 10) { score = Math.max(0, score-15); items.push({ok:'err', txt:`${d.error_count} recent errors`}); }
  else if (d.error_count > 0) { items.push({ok:'warn', txt:`${d.error_count} recent warnings`}); }

  return {score: Math.min(100, score), items};
}

function renderHealthScore(d) {
  const {score, items} = computeHealth(d);
  const circ = 2*Math.PI*28;
  const filled = circ * score / 100;
  const color = score >= 75 ? '#22c55e' : score >= 45 ? '#f59e0b' : '#ef4444';
  const arc = document.getElementById('health-arc');
  if (arc) {
    arc.style.stroke = color;
    arc.style.strokeDashoffset = circ - filled;
  }
  setText('health-score', score);
  const hi = document.getElementById('health-items');
  if (hi) hi.innerHTML = items.slice(0,4).map(i =>
    `<div class="health-item"><div class="health-dot ${i.ok}"></div><span style="color:var(--text2)">${i.txt}</span></div>`
  ).join('');
}

// ══ COUNTDOWN TIMERS ══
let _checkTarget=null, _scrapeTarget=null;
function initCountdowns(d) {
  // Parse next run times from pipeline data
  const gr = d.grants_run || {};
  const sc = d.scrape || {};
  if (gr.timestamp) {
    const dt = new Date(gr.timestamp.includes('Z') ? gr.timestamp : gr.timestamp+'Z');
    _checkTarget = new Date(dt.getTime() + 24*3600*1000);
    document.getElementById('cd-check-sub').textContent = 'Last: ' + fmtTs(gr.timestamp);
  }
  if (sc.timestamp) {
    const dt = new Date(sc.timestamp.includes('Z') ? sc.timestamp : sc.timestamp+'Z');
    _scrapeTarget = new Date(dt.getTime() + 168*3600*1000);
    document.getElementById('cd-scrape-sub').textContent = 'Last: ' + fmtTs(sc.timestamp);
  }
}

function fmtCountdown(target) {
  if (!target) return '—';
  const s = Math.round((target - Date.now()) / 1000);
  if (s < 0) return 'OVERDUE';
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = s%60;
  if (h > 48) return Math.floor(h/24)+'d ' + (h%24) + 'h';
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;
}

setInterval(() => {
  if (document.getElementById('cd-check')) {
    setText('cd-check', fmtCountdown(_checkTarget));
    setText('cd-scrape', fmtCountdown(_scrapeTarget));
  }
}, 1000);

// ══ URGENCY STRIP (Overview) ══
function renderUrgencyStrip(matches) {
  // Extract unique grants closing within 30 days from match data
  const seen = new Set();
  const urgent = [];
  const today = new Date();
  matches.forEach(m => {
    if (seen.has(m.grant_id)) return;
    seen.add(m.grant_id);
    if (!m.grant_close_date) return;
    try {
      const cd = new Date(m.grant_close_date);
      const days = Math.round((cd - today) / 86400000);
      if (days >= -1 && days <= 30) urgent.push({...m, days_left: days});
    } catch(e) {}
  });
  urgent.sort((a,b) => a.days_left - b.days_left);
  const strip = document.getElementById('urgency-strip');
  const items = document.getElementById('urgency-items');
  if (!strip || !urgent.length) { if(strip) strip.style.display='none'; return; }
  strip.style.display='';
  setText('urgency-count', urgent.length + ' grants');
  items.innerHTML = urgent.slice(0,8).map(g => {
    const cl = g.days_left <= 3 ? 'var(--red)' : g.days_left <= 14 ? 'var(--yellow)' : 'var(--green)';
    const dl = g.days_left === 0 ? 'Today!' : g.days_left < 0 ? 'Yesterday' : `${g.days_left}d left`;
    return `<div class="urgency-item" onclick="window.open('${g.grant_link||'#'}','_blank')">
      <div class="ui-days" style="color:${cl}">${dl}</div>
      <div class="ui-title" title="${esc(g.grant_title)}">${esc(g.grant_title)}</div>
      <div class="ui-meta">${esc(g.grant_agency||'—')} · ${g.grant_close_date||'—'}</div>
    </div>`;
  }).join('');
}

// ══ ENHANCED ANALYTICS ══
async function loadKeywordAnalysis() {
  const d = await fetch('/api/keyword-analysis').then(r=>r.json());

  // Confidence distribution
  const conf = d.confidence_dist || [];
  mkChart('chart-confidence', {
    type:'bar',
    data:{labels:conf.map(c=>c.range), datasets:[{
      data:conf.map(c=>c.count),
      backgroundColor:['rgba(239,68,68,.6)','rgba(245,158,11,.6)','rgba(59,130,246,.6)','rgba(34,197,94,.6)','rgba(34,197,94,.8)'],
      borderColor:['#ef4444','#f59e0b','#3b82f6','#22c55e','#16a34a'],
      borderWidth:1
    }]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{x:{grid:{color:'#1c2235'},ticks:{color:'#4b5675'}},
              y:{grid:{color:'#1c2235'},ticks:{color:'#4b5675'},beginAtZero:true}}}
  });

  // Keyword count distribution
  const kd = d.keyword_count_dist || [];
  mkChart('chart-kw-dist', barDefaults(kd.map(k=>k.range), kd.map(k=>k.count), COLORS.teal));

  // Dept leaderboard table
  const lb = d.dept_leaderboard || [];
  setText('dept-lb-count', lb.length + ' departments');
  const tbody = document.getElementById('dept-lb-tbody');
  if (tbody) {
    tbody.innerHTML = lb.map((dept, i) => {
      const rankCls = i===0?'rank-1':i===1?'rank-2':i===2?'rank-3':'rank-other';
      const semBar = `<div class="sem-pct-bar"><div class="sem-pct-fill" style="width:${Math.min(dept.sem_pct,100)*0.8}px"></div><span>${dept.sem_pct}%</span></div>`;
      return `<tr>
        <td><div style="display:flex;align-items:center;gap:8px">
          <div class="dept-lb-rank ${rankCls}">${i+1}</div>
          <span class="td-name">${esc(dept.dept)}</span>
        </div></td>
        <td class="td-num" style="color:var(--text)">${dept.total_matches}</td>
        <td class="td-num">${dept.unique_grants}</td>
        <td class="td-num">${dept.unique_faculty}</td>
        <td class="td-num">${dept.avg_score}</td>
        <td class="td-num">${semBar}</td>
      </tr>`;
    }).join('');
  }

  // Keyword detail table
  const kws = d.top_keywords_detail || [];
  const kwTbody = document.getElementById('kw-detail-tbody');
  if (kwTbody) {
    kwTbody.innerHTML = kws.map((kw, i) => `<tr>
      <td><span class="kw match">${esc(kw.keyword)}</span></td>
      <td class="td-num">${kw.grant_count}</td>
      <td class="td-num">${kw.faculty_count}</td>
      <td class="td-dept">${esc(kw.top_dept)}</td>
    </tr>`).join('');
  }
}

// ══ Init ══
loadOverview();
setInterval(()=>{ if(currentPage==='overview') loadOverview(); }, 60000);
</script>

<!-- ═══ Faculty Detail Modal ═══ -->
<div class="modal-overlay" id="fac-modal" style="display:none" onclick="closeFacModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-hdr">
      <div class="modal-avatar" id="fac-modal-initials"></div>
      <div>
        <div class="modal-name" id="fac-modal-name"></div>
        <div class="modal-dept" id="fac-modal-dept"></div>
        <div class="modal-email" id="fac-modal-email"></div>
      </div>
      <button class="modal-close" onclick="closeFacModal()">✕</button>
    </div>
    <div class="modal-tabs">
      <div class="modal-tab active" onclick="switchModalTab('keywords',this)">Keywords</div>
      <div class="modal-tab" onclick="switchModalTab('matches',this)">Match History</div>
      <div class="modal-tab" onclick="switchModalTab('sources',this)">Sources</div>
    </div>
    <div class="modal-body">
      <div class="modal-tab-content active" id="mtab-keywords"></div>
      <div class="modal-tab-content" id="mtab-matches"></div>
      <div class="modal-tab-content" id="mtab-sources">
        <div style="height:220px;position:relative"><canvas id="modal-src-chart"></canvas></div>
      </div>
    </div>
  </div>
</div>
</body>
</html>"""


@app.route("/")
@login_required
def index():
    html = DASHBOARD_HTML
    if APP_ENV == 'dev':
        banner = (
            '<div style="position:fixed;top:0;left:0;right:0;z-index:9999;background:#16a34a;'
            'color:#fff;text-align:center;font-size:13px;font-weight:600;padding:6px;'
            'letter-spacing:.05em;font-family:monospace;">'
            '&#9888; DEV ENVIRONMENT &#8212; changes here are not production</div>'
            '<div style="height:33px"></div>'
        )
        html = html.replace('<body>', f'<body>{banner}', 1)
    return render_template_string(html)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", 8080)))
    app.run(host="0.0.0.0", port=port, debug=False)
