"""Weekly roundup rebuilt under today's rules (2026-09-30): the recent-grants
store, the re-filter for stored rows, and build_weekly_roundup's merge of a
re-match with re-filtered fallback rows. Offline: semantic off, no network.

Run: `python -m pytest tests/` or `python tests/test_weekly_roundup.py`.
"""
import copy
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
logging.basicConfig(level=logging.ERROR)
os.environ.setdefault("SENDGRID_API_KEY", "x")

import yaml  # noqa: E402
import matcher  # noqa: E402
import grant_store as gs  # noqa: E402


def _config(**matching_overrides):
    cfg = yaml.safe_load(open(ROOT / "config/config.yaml", encoding="utf8"))
    m = cfg["matching"]
    m.update({"semantic_matching": False, "min_confidence_score": 0, "min_idf_for_match": 0,
              "max_kw_prevalence_pct": 1.0, "max_grants_per_faculty_per_run": 0})
    m["research_evidence"]["gate_major_mechanisms"] = False
    m["research_evidence"]["gate_pi_track_record"] = False
    m["feedback"] = dict(m.get("feedback", {}), enabled=False)
    m.update(matching_overrides)
    return cfg


def _fac(name, email, kws, dept="Medicine"):
    return {"name": name, "department": dept, "email": email, "keywords": list(kws),
            "research_interests": ", ".join(kws), "profile_url": "", "inactive": False}


def _grant(gid, title, text, agency="National Institutes of Health"):
    return {"id": gid, "number": gid, "title": title, "agency": agency,
            "searchable_text": f"{title}. {text}".lower(), "synopsis": text,
            "description": text, "link": f"http://x/{gid}", "source": "grants_gov"}


def _stored_row(grant, name, email, kws, mtype="keyword", conf=70, when=None):
    when = when or datetime.utcnow()
    return {"timestamp": when.isoformat(), "grant_id": grant["id"], "grant_title": grant["title"],
            "grant_agency": grant["agency"], "grant_number": grant["number"],
            "grant_link": grant["link"], "grant_synopsis": grant["synopsis"][:500],
            "faculty_name": name, "faculty_department": "Medicine", "faculty_email": email,
            "faculty_url": "", "matched_keywords": list(kws), "match_score": 1,
            "match_type": mtype, "similarity_score": 0.0, "confidence_score": conf}


class _cwd:
    def __init__(self, path): self.path = path
    def __enter__(self): self.old = os.getcwd(); os.chdir(self.path); return self
    def __exit__(self, *a): os.chdir(self.old)


# ── grant_store ──────────────────────────────────────────────────────────────

def test_grant_store_records_dedupes_and_prunes():
    with tempfile.TemporaryDirectory() as tmp, _cwd(tmp):
        a = _grant("A", "Alpha call", "pancreatic cancer detection")
        b = _grant("B", "Beta call", "hiv care")
        assert gs.record_grants([a, b]) == 2
        a2 = dict(a, title="Alpha call (reposted)")
        assert gs.record_grants([a2]) == 2, "same id replaces, never duplicates"
        got = {g["id"]: g for g in gs.load_recent_grants(7)}
        assert set(got) == {"A", "B"} and got["A"]["title"] == "Alpha call (reposted)"
        assert got["A"]["searchable_text"] == a["searchable_text"], "full text is kept"
        # age pruning: a record stamped 20 days ago drops out on the next write
        old = datetime.utcnow() - timedelta(days=20)
        gs.record_grants([_grant("OLD", "Old call", "x")], now=old)
        gs.record_grants([])
        assert {g["id"] for g in gs.load_recent_grants(30)} == {"A", "B"}
        assert gs.load_recent_grants(0) == [] or all(
            g["recorded_at"] >= datetime.utcnow().isoformat()[:10] for g in gs.load_recent_grants(0))
        assert gs.record_grants(None) == 2, "a bad call never raises"


# ── refilter_stored_results ──────────────────────────────────────────────────

def test_refilter_applies_current_gates_to_stored_rows():
    cfg = _config()
    bja   = _grant("BJA1", "BJA FY 2026 National Center for Veterans Justice",
                   "traumatic brain injury substance use", agency="Bureau of Justice Assistance")
    astro = _grant("ATI", "Astronomical Sciences Technology and Instrumentation (ATI)",
                   "imaging detectors", agency="National Science Foundation")
    nei   = _grant("NEI", "NEI Translational Research Program for Therapeutics", "eye therapeutics")
    g13   = _grant("G13", "NLM Grants for Scholarly Works in Biomedicine and Health (G13)", "books")
    stored = [
        {"grant": bja, "matches": [
            {"faculty_name": "Key Only", "faculty_email": "k@x.edu", "match_type": "keyword",
             "matched_keywords": ["traumatic brain injury"], "confidence_score": 95},
            {"faculty_name": "Sem Agrees", "faculty_email": "s@x.edu", "match_type": "both",
             "matched_keywords": ["substance use"], "confidence_score": 60}]},
        {"grant": astro, "matches": [
            {"faculty_name": "Any One", "faculty_email": "a@x.edu", "match_type": "keyword",
             "matched_keywords": ["imaging"], "confidence_score": 55}]},
        {"grant": nei, "matches": [
            {"faculty_name": "Boiler Plate", "faculty_email": "b@x.edu", "match_type": "keyword",
             "matched_keywords": ["applications", "preliminary"], "confidence_score": 55},
            {"faculty_name": "Real Topic", "faculty_email": "r@x.edu", "match_type": "keyword",
             "matched_keywords": ["technology", "retinal degeneration"], "confidence_score": 58},
            {"faculty_name": "Gen Eric", "faculty_email": "g@x.edu", "match_type": "semantic",
             "matched_keywords": ["≈ biomedical research", "≈ clinical trials"], "confidence_score": 60},
            {"faculty_name": "Topi Cal", "faculty_email": "t@x.edu", "match_type": "semantic",
             "matched_keywords": ["≈ retinal gene therapy"], "confidence_score": 60}]},
        {"grant": g13, "matches": [
            {"faculty_name": "Sem On Book", "faculty_email": "o@x.edu", "match_type": "semantic",
             "matched_keywords": ["≈ metagenomics"], "confidence_score": 62},
            {"faculty_name": "Key On Book", "faculty_email": "y@x.edu", "match_type": "keyword",
             "matched_keywords": ["scholarly"], "confidence_score": 52}]},
    ]
    out, audit = matcher.refilter_stored_results(copy.deepcopy(stored), cfg)
    by = {r["grant"]["id"]: [m["faculty_name"] for m in r["matches"]] for r in out}
    assert by["BJA1"] == ["Sem Agrees"], "corroboration gate drops the keyword-only DOJ row"
    assert "ATI" not in by, "astronomy title is rejected outright"
    assert set(by["NEI"]) == {"Real Topic", "Topi Cal"}, "boilerplate-only and generic-evidence rows go"
    assert by["G13"] == ["Key On Book"], "topic-less mechanism guards the semantic row only"
    d = audit["rows_dropped"]
    assert (d["corroboration"], d["blocked_grant"], d["context"], d["generic_evidence"]) == (1, 1, 1, 2)
    assert any("astronomical" in g for g in audit["grants_dropped"])


# ── build_weekly_roundup ─────────────────────────────────────────────────────

def test_roundup_rematches_stored_grants_and_refilters_the_rest():
    cfg = _config()
    with tempfile.TemporaryDirectory() as tmp, _cwd(tmp):
        Path("data").mkdir()
        pdac = _grant("PDAC", "Pancreatic Cancer Detection Consortium",
                      "early detection of pancreatic ductal adenocarcinoma biomarkers")
        bja  = _grant("BJA1", "BJA FY 2026 National Center for Veterans Justice",
                      "traumatic brain injury", agency="Bureau of Justice Assistance")
        hiv  = _grant("HIV", "HIV implementation science", "hiv care continuum")
        # Stored rows from earlier in the week: PDAC has a real row and a
        # boilerplate row recorded before the term was demoted; BJA is
        # keyword-only pre-gate; HIV is clean.
        stored = [
            _stored_row(pdac, "Pan Creas", "pc@x.edu", ["pancreatic cancer", "early detection"], conf=80),
            _stored_row(pdac, "Tech Only", "to@x.edu", ["technology", "design"], conf=53),
            _stored_row(bja, "Key Only", "ko@x.edu", ["traumatic brain injury"], conf=95),
            _stored_row(hiv, "Aitch Vee", "hv@x.edu", ["hiv care"], conf=70),
        ]
        Path("data/match_results.json").write_text(json.dumps(stored), encoding="utf-8")
        before = Path("data/match_results.json").read_text(encoding="utf-8")
        # Only PDAC has full text in the store.
        gs.record_grants([pdac])

        # two keywords each: the matcher's single-keyword floor (50) would otherwise
        # reject them at this pool size, which is not what this test is about
        faculty = [_fac("Pan Creas", "pc@x.edu", ["pancreatic cancer", "early detection"]),
                   _fac("Tech Only", "to@x.edu", ["technology", "design"]),
                   _fac("New Comer", "nc@x.edu", ["adenocarcinoma", "biomarkers"]),
                   _fac("Aitch Vee", "hv@x.edu", ["hiv care"])]
        results, audit = matcher.build_weekly_roundup(cfg, faculty, days=7)

        by = {r["grant"]["id"]: [m["faculty_name"] for m in r["matches"]] for r in results}
        assert set(by) == {"PDAC", "HIV"}, f"BJA gone (re-filtered), got {by}"
        assert "Tech Only" not in by["PDAC"], "stored boilerplate row replaced by today's re-match"
        assert set(by["PDAC"]) == {"Pan Creas", "New Comer"}, "re-match sees today's roster"
        assert by["HIV"] == ["Aitch Vee"]
        assert all(isinstance(m, dict) for r in results for m in r["matches"]), "fan-out shape"

        assert audit["store_grants_in_window"] == 1 and audit["rematched_grants"] == 1
        assert audit["replaced_stored_rows"] == 2 and audit["rematched_rows"] == 2
        assert audit["fallback_grants_before"] == 2 and audit["fallback_grants_after"] == 1
        assert audit["fallback_rows_dropped"]["corroboration"] == 1
        assert audit["stored_rows"] == 4 and audit["final_rows"] == 3
        assert audit["rematch_error"] == ""

        # persist=False: the daily results file and stats are untouched
        assert Path("data/match_results.json").read_text(encoding="utf-8") == before
        assert not Path("data/run_stats.json").exists()
        assert matcher.get_last_rematch_diagnostic()["summary"]["grants_checked"] == 1
        assert matcher.get_last_diagnostic() == {} or "weekly" not in matcher.get_last_diagnostic()


def test_roundup_with_empty_store_is_a_refiltered_replay():
    cfg = _config()
    with tempfile.TemporaryDirectory() as tmp, _cwd(tmp):
        Path("data").mkdir()
        hiv = _grant("HIV", "HIV implementation science", "hiv care continuum")
        Path("data/match_results.json").write_text(
            json.dumps([_stored_row(hiv, "Aitch Vee", "hv@x.edu", ["hiv care"], conf=70)]),
            encoding="utf-8")
        results, audit = matcher.build_weekly_roundup(cfg, [_fac("X", "x@x.edu", ["x"])], days=7)
        assert [r["grant"]["id"] for r in results] == ["HIV"]
        assert audit["store_grants_in_window"] == 0 and audit["rematched_grants"] == 0
        assert audit["fallback_rows_after"] == 1 and audit["final_rows"] == 1


if __name__ == "__main__":
    test_grant_store_records_dedupes_and_prunes()
    test_refilter_applies_current_gates_to_stored_rows()
    test_roundup_rematches_stored_grants_and_refilters_the_rest()
    test_roundup_with_empty_store_is_a_refiltered_replay()
    print("PASS")
