"""Report-only center-type probe on the clinical/basic filter (2026-10-16).
The live clinical_required_patterns still drop basic-only faculty; the probe
patterns only audit. Offline: pure config + regex, no model."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import yaml  # noqa: E402
import matcher  # noqa: E402

CFG = yaml.safe_load(open(ROOT / "config/config.yaml", encoding="utf8"))["matching"]["clinical_basic_filter"]
CB = matcher._compile_clinical_basic(CFG)

BEACON = {"title": "HEAL Initiative: Integrative Management of chronic Pain and OUD for Whole Recovery "
                   "(IMPOWR) - Building Engagement, Assistance, Capacity, Outreach, and Networks (BEACON) "
                   "Center (U2C Clinical Trial Not Allowed)"}
RM1 = {"title": "HEAL Initiative: Integrative Management of Chronic Pain and OUD for Whole Recovery "
                "(IMPOWR): Research Centers (RM1 Clinical Trial Required)"}
IGVF = {"title": "IGVF Data and Coordinating Center (U24 Clinical trials Not Allowed)"}
SENNET = {"title": "Cellular Senescence Network (SenNet): SenNet Data Coordination and Integration Center "
                   "(SenNet DCIC) (UM1 Clinical Trials Not Allowed)"}
PEPPER = {"title": "Coordinating Center for the Claude D. Pepper Older Americans Independence Center"}
NEXUS = {"title": "Neural Ensembles & Used Substances (NExUS) Collaboratory: Building a Multimodal "
                  "Inventory of Cell Ensembles Encoding the Effects of Addictive Substances (U01 Clinical Trial Not Allowed)"}
MALARIA = {"title": "Accelerating Research and Development of Next Generation Vaccines against Malaria "
                    "(R61/R33 Clinical Trial Not Allowed)"}

KELLER = {"name": "Asaf Keller", "keywords": ["chronic pain", "opioid", "rat somatosensory cortex", "mice, inbred c57bl"]}
GIGLIO = {"name": "Michelle G. Giglio", "keywords": ["metadata", "data coordination", "outreach"]}
CLINICIAN = {"name": "Doc Tor", "keywords": ["chronic pain", "patients", "mice"]}


def test_compile_has_both_groups():
    assert CB and CB["clinical_required"] and CB["clinical_probe"]
    assert len(CB["clinical_probe"]) == 5


def test_live_patterns_unchanged():
    assert matcher._grant_is_clinical_required(RM1, CB)
    for g in (BEACON, IGVF, SENNET, PEPPER, NEXUS, MALARIA):
        assert not matcher._grant_is_clinical_required(g, CB), g["title"][:40]


def test_probe_hits_center_type_calls_only():
    for g in (BEACON, IGVF, SENNET, PEPPER):
        assert matcher._grant_clinical_probe_hit(g, CB), g["title"][:40]
    for g in (NEXUS, MALARIA, RM1):
        assert matcher._grant_clinical_probe_hit(g, CB) is None, g["title"][:40]
    assert matcher._grant_clinical_probe_hit(BEACON, None) is None


def test_faculty_classification():
    assert matcher._faculty_is_basic_only(KELLER, CB), "two basic signals, zero clinical"
    assert not matcher._faculty_is_basic_only(GIGLIO, CB), "no basic signals -> informatics people survive"
    assert not matcher._faculty_is_basic_only(CLINICIAN, CB), "zero-clinical guard"


if __name__ == "__main__":
    test_compile_has_both_groups(); test_live_patterns_unchanged()
    test_probe_hits_center_type_calls_only(); test_faculty_classification(); print("PASS")
