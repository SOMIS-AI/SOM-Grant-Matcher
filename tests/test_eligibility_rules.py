"""Eligibility / relevance rules added 2026-11-16 from faculty feedback: R50 is a
staff-scientist mechanism, "Limited Competition:" calls are for existing awardees
(except the CTSA family, where UMB is eligible), and the FDA food rapid-response
programme is not biomedical research."""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import matcher  # noqa: E402

CFG = yaml.safe_load(open(ROOT / "config/config.yaml", encoding="utf8"))
PATS = matcher._compile_ineligible_patterns(CFG["matching"]["ineligible_grant_patterns"])


def _reason(title):
    for item in PATS:
        rx = item[0] if isinstance(item, tuple) else item.get("rx") or item.get("pattern")
        reason = item[1] if isinstance(item, tuple) else item.get("reason")
        if hasattr(rx, "search") and rx.search(title):
            return reason
    return None


def test_r50_research_specialist_is_ineligible():
    r = _reason("NCI Research Specialist (Clinician Scientist) Award (R50 Clinical Trial Not Allowed)")
    assert r and "R50" in r, r
    assert _reason("Research Project Grant (Parent R01 Clinical Trial Not Allowed)") is None


def test_limited_competition_is_ineligible_except_the_ctsa_family():
    assert "limited competition" in (_reason("Limited Competition: Biospecimen Banks to support NCI National Clinical Trials Network Biospecimen Banks (NCTN Biobanks)") or "")
    assert "limited competition" in (_reason("Limited Competition for the Adolescent Brain Cognitive Development -Extended (ABCD-E) Study Renewal") or "")
    assert "limited competition" in (_reason("Limited Competition: NIGMS Mature Synchrotron Resources (MSR) for Structural Biology (P30)") or "")
    # UMB holds a CTSA hub: these stay eligible
    assert _reason("Limited Competition: Clinical and Translational Science Awards (CTSA) Program: Collaborative and Innovative Acceleration") is None
    assert _reason("Limited Competition: Institutional Mentored Research Career Development Program for the Clinical and Translational Science Award (CTSA) Program (KL2)") is None
    assert _reason("Limited Competition: Research Education Program for the Clinical & Translational Science Awards Program") is None
    # an ordinary title
    assert _reason("Pancreatic Cancer Detection Consortium (U01 Clinical Trial Not Allowed)") is None


def test_fda_food_rapid_response_is_not_biomedical():
    ok, why = matcher._is_biomedically_relevant({
        "title": "Development and Maintenance of Human and Animal Food Rapid Response Teams",
        "agency": "Food and Drug Administration",
        "synopsis": "epidemiology surveillance emergency response environmental health laboratories"})
    assert not ok and "non-biomedical topic term" in why, why
    ok2, _ = matcher._is_biomedically_relevant({
        "title": "Food Allergy Immunotherapy Clinical Trials (U01 Clinical Trial Required)",
        "agency": "National Institutes of Health", "synopsis": "food allergy"})
    assert ok2, "a food ALLERGY call is still biomedical"
