"""Generic-evidence guard on semantic-only matches (2026-09-25). Offline:
the evidence function is injected, so no embedding model is needed."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import yaml  # noqa: E402
import matcher  # noqa: E402

CFG = yaml.safe_load(open(ROOT / "config/config.yaml", encoding="utf8"))["matching"]["semantic_generic_guard"]
GUARD = matcher._compile_semantic_generic_guard(CFG)


def _m(name, mtype="semantic", conf=60, kws=()):
    return matcher.Match(
        faculty_name=name, is_staff=False, faculty_url="", faculty_department="Medicine",
        faculty_email=f"{name.split()[0].lower()}@x.edu", matched_keywords=list(kws),
        match_score=0, match_type=mtype, similarity_score=0.5, confidence_score=conf,
    )


def test_compile_and_judge():
    assert GUARD and GUARD["demote"] == 0.4
    assert matcher._evidence_is_generic(["biomedical research", "clinical trials as topic"], GUARD)
    assert matcher._evidence_is_generic(["Correction to: Robust Institutional Support ..."], GUARD)
    assert matcher._evidence_is_generic([], GUARD)
    assert not matcher._evidence_is_generic(["biomedical research", "HIV implementation science"], GUARD)
    assert not matcher._evidence_is_generic(["diagnostic stewardship"], GUARD)
    assert matcher._compile_semantic_generic_guard({"enabled": False}) is None
    assert matcher._grant_is_topicless({"title": "NLM Grants for Scholarly Works in Biomedicine and Health (G13)", "number": "PAR-28-027"}, GUARD)
    assert matcher._grant_is_topicless({"title": "Support for Scientific Meetings (R13)", "number": ""}, GUARD)
    assert not matcher._grant_is_topicless({"title": "Immune Drivers of Autoimmune Disease", "number": "RFA-AI-27-001"}, GUARD)


def test_fill_and_guard():
    fac = {n: {"name": n} for n in ("Gen Eric", "Topi Cal", "Key Word", "Both Ways")}
    evidence = {
        "Gen Eric": ["biomedical research", "medical education"],
        "Topi Cal": ["HIV implementation science", "hiv care"],
    }
    def ev_fn(grant, f):
        return evidence.get(f["name"], [])
    matches = [_m("Gen Eric", conf=60), _m("Topi Cal", conf=60),
               _m("Key Word", mtype="keyword", conf=60, kws=["hiv"]),
               _m("Both Ways", mtype="both", conf=60, kws=["hiv"])]
    out, guarded = matcher._fill_semantic_evidence(matches, {"title": "G13"}, fac, ev_fn, GUARD, min_sem_conf=50)
    names = [m.faculty_name for m in out]
    assert "Gen Eric" not in names, "all-generic evidence at 60 -> 24 < floor 50 -> dropped"
    assert names == ["Topi Cal", "Key Word", "Both Ways"]
    assert guarded == [("Gen Eric", 60, ["biomedical research", "medical education"])]
    topi = next(m for m in out if m.faculty_name == "Topi Cal")
    assert topi.matched_keywords == ["≈ HIV implementation science", "≈ hiv care"] and topi.confidence_score == 60
    # demoted but still above the floor is kept, at the demoted score
    out2, g2 = matcher._fill_semantic_evidence([_m("Gen Eric", conf=99)], {"title": "G"}, fac, ev_fn, GUARD, min_sem_conf=30)
    assert out2 and out2[0].confidence_score == 40 and g2
    # topic-less grant: even specific evidence is guarded; keyword/both still pass
    out4, g4 = matcher._fill_semantic_evidence(
        [_m("Topi Cal", conf=60), _m("Key Word", mtype="keyword", conf=60, kws=["hiv"])],
        {"title": "G13"}, fac, ev_fn, GUARD, min_sem_conf=50, grant_topicless=True)
    assert [m.faculty_name for m in out4] == ["Key Word"] and g4[0][0] == "Topi Cal"
    # guard disabled: nothing changes
    out3, g3 = matcher._fill_semantic_evidence([_m("Gen Eric", conf=60)], {"title": "G"}, fac, ev_fn, None, min_sem_conf=50)
    assert out3[0].confidence_score == 60 and g3 == []


if __name__ == "__main__":
    test_compile_and_judge(); test_fill_and_guard(); print("PASS")
