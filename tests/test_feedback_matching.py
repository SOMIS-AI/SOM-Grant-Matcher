"""Feedback suppression, per-person keyword weights, and the self-reported
tier (2026-09-23). Offline: semantic matching off, no network, no SendGrid.

Run: `python -m pytest tests/` or `python tests/test_feedback_matching.py`.
"""
import copy
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
logging.basicConfig(level=logging.ERROR)
os.environ.setdefault("SENDGRID_API_KEY", "x")

import yaml  # noqa: E402
import matcher  # noqa: E402
import feedback_store as fs  # noqa: E402


def _config(tmp: Path, **matching_overrides):
    cfg = yaml.safe_load(open(ROOT / "config/config.yaml", encoding="utf8"))
    m = cfg["matching"]
    m.update({"semantic_matching": False, "min_confidence_score": 0, "min_idf_for_match": 0,
              "max_kw_prevalence_pct": 1.0, "max_grants_per_faculty_per_run": 0,
              "corroboration_required_agencies": []})
    m["research_evidence"]["gate_major_mechanisms"] = False
    m["research_evidence"]["gate_pi_track_record"] = False
    m["feedback"] = dict(m.get("feedback", {}), enabled=True, store_path=str(tmp / "fb.json"))
    m.update(matching_overrides)
    return cfg


def _fac(name, email, kws, self_reported=(), dept="Medicine"):
    f = {"name": name, "department": dept, "email": email, "keywords": list(kws),
         "research_interests": ", ".join(kws), "profile_url": "", "inactive": False}
    if self_reported:
        f["keywords_by_source"] = {"Faculty Self-Reported": list(self_reported)}
    return f


def _grant(title, text, number, agency="National Institutes of Health"):
    return {"title": title, "agency": agency, "searchable_text": f"{title}. {text}",
            "description": text, "link": "http://x", "source": "grants_gov",
            "id": number, "number": number}


def _run(cfg, grants, faculty, cwd):
    old = os.getcwd(); os.chdir(cwd)      # keep data/ writes out of the repo
    try:
        res = matcher.find_matches(copy.deepcopy(grants), faculty, cfg)
    finally:
        os.chdir(old)
    return {r["grant"]["number"]: {m.faculty_name: m for m in r["matches"]} for r in res}, matcher.get_last_diagnostic()


def _write_store(path: Path, verdicts):
    json.dump({"updated_at": None, "sources": [], "verdicts": verdicts}, open(path, "w"))


def test_feedback_index_shapes():
    store = {"verdicts": [
        {"email": "a@x.edu", "grant": "G1", "verdict": "not_relevant", "rater": "self",
         "match_type": "keyword", "keywords": ["opioid", "substance use"], "grant_title": "Court Program"},
        {"email": "a@x.edu", "grant": "G2", "verdict": "good", "rater": "self",
         "match_type": "keyword", "keywords": ["opioid"]},
        {"email": "b@x.edu", "grant": "G3", "verdict": "not_relevant", "rater": "digest",
         "match_type": "keyword", "keywords": ["brain"]},
        {"email": "c@x.edu", "grant": "OPTOUT", "verdict": "optout", "rater": "self"},
    ]}
    idx = fs.build_feedback_index(store)
    assert ("a@x.edu", "G1") in idx["suppress"]
    assert ("a@x.edu", "court program") in idx["suppress_titles"]
    assert ("b@x.edu", "G3") not in idx["suppress"], "third-party verdicts are not applied by default"
    assert idx["optout"] == {"c@x.edu"}
    w = idx["kw_weights"]["a@x.edu"]
    assert w["substance use"] == 0.5 and abs(w["opioid"] - 0.6) < 1e-6, w   # 0.5 * 1.2
    idx2 = fs.build_feedback_index(store, use_third_party=True)
    assert ("b@x.edu", "G3") in idx2["suppress"]


def test_suppression_and_weights_in_find_matches():
    tmp = Path(tempfile.mkdtemp(prefix="fb_"))
    text = "opioid use disorder treatment court program substance use"
    grants = [_grant("Court Program", text, "G1"),
              _grant("Court Program", text, "G1-REPOST"),           # same title, new number
              _grant("Opioid Treatment R01", text, "G9")]
    faculty = [_fac("Ann Rejecter", "ann@x.edu", ["opioid", "substance use"]),
               _fac("Bob Neutral", "bob@x.edu", ["opioid", "substance use"]),
               # unrelated third member so the shared keywords are not 100%-prevalent dynamic stop words
               _fac("Cam Cardio", "cam@x.edu", ["heart failure", "cardiology"])]
    _write_store(tmp / "fb.json", [
        {"email": "ann@x.edu", "grant": "G1", "verdict": "not_relevant", "rater": "self",
         "match_type": "keyword", "keywords": ["opioid", "substance use"], "grant_title": "Court Program"},
    ])
    cfg = _config(tmp)
    res, diag = _run(cfg, grants, faculty, tmp)
    assert "Ann Rejecter" not in res.get("G1", {}), "rejected pair must be suppressed"
    assert "Ann Rejecter" not in res.get("G1-REPOST", {}), "same title re-posted must be suppressed"
    assert "Bob Neutral" in res["G1"] and "Bob Neutral" in res["G1-REPOST"]
    assert diag["summary"]["feedback_suppressed"] == 2
    # on an unrelated NIH call Ann still matches, but lower than Bob on the same keywords
    assert "Ann Rejecter" in res["G9"] and "Bob Neutral" in res["G9"]
    assert res["G9"]["Ann Rejecter"].confidence_score < res["G9"]["Bob Neutral"].confidence_score
    assert diag["params"]["feedback_verdicts_applied"] == 1


def test_self_reported_boost_and_tier():
    tmp = Path(tempfile.mkdtemp(prefix="fb_"))
    text = "traumatic brain injury biomarkers"
    grants = [_grant("TBI Biomarkers", text, "G1")]
    # two keywords each: a lone keyword is demoted by the single-keyword filter regardless of weight
    faculty = [_fac("Sam Self", "sam@x.edu", ["traumatic brain injury", "biomarkers"], self_reported=["traumatic brain injury"]),
               _fac("Pat Plain", "pat@x.edu", ["traumatic brain injury", "biomarkers"]),
               _fac("Cam Cardio", "cam@x.edu", ["heart failure", "cardiology"])]
    cfg = _config(tmp, self_reported_keyword_multiplier=1.25)
    res, diag = _run(cfg, grants, faculty, tmp)
    sam, pat = res["G1"]["Sam Self"].confidence_score, res["G1"]["Pat Plain"].confidence_score
    assert sam > pat, (sam, pat)
    cfg0 = _config(tmp, self_reported_keyword_multiplier=1.0)
    res0, _ = _run(cfg0, grants, faculty, tmp)
    assert res0["G1"]["Sam Self"].confidence_score == res0["G1"]["Pat Plain"].confidence_score
    # tier: self-reported only -> 'self'; scraped only with attribution -> 'none'
    assert matcher._research_tier(faculty[0]) == "self"
    assert matcher._research_tier({"keywords_by_source": {"UMSOM Profile": ["x"]}}) == "none"
    assert matcher._research_tier({"keywords_by_source": {"PubMed": ["x"]}}) == "pub"
    # major-mechanism gate still drops 'self'
    cfg_g = _config(tmp)
    cfg_g["matching"]["research_evidence"]["gate_major_mechanisms"] = True
    res_g, diag_g = _run(cfg_g, [_grant("TBI Biomarkers R01", text, "G1")], faculty, tmp)
    assert "Sam Self" not in res_g.get("G1", {}), "self tier must still be gated off R01"
    assert diag_g["summary"]["track_record_gated"] >= 1


if __name__ == "__main__":
    for t in (test_feedback_index_shapes, test_suppression_and_weights_in_find_matches,
              test_self_reported_boost_and_tier):
        t(); print("ok  ", t.__name__)
    print("PASS")
