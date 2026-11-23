"""Rules added 2026-11-23 from the 11-17..11-23 review: eligibility-TEXT
ineligibility (non-competitive continuation NOFOs), NIC in the corroboration
gate, ninth wave of context terms, the P30 parent as a topicless mechanism,
cross-source title de-duplication, and the PubMed robustness work (retry,
status, initials-only affiliation, per-source carry-forward)."""
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import matcher  # noqa: E402
import grants_poller  # noqa: E402
import faculty_scraper as fs  # noqa: E402

CFG = yaml.safe_load(open(ROOT / "config/config.yaml", encoding="utf8"))
M = CFG["matching"]
TITLE_PATS = matcher._compile_ineligible_patterns(M["ineligible_grant_patterns"])
TEXT_PATS = matcher._compile_ineligible_patterns(M["ineligible_eligibility_text_patterns"])

NIC_CONTINUATION = ("This is not a request for competitive applications. This NOFO provides notice "
                    "of the continuation of Cooperative Agreement award 24PR03GLT6. Applications "
                    "received from organizations other than the prior award recipient will not be "
                    "reviewed or considered.")
NIC_OPEN = ("NIC invites applications from nonprofit organizations (including faith-based, community, "
            "and tribal organizations), for-profit organizations, and institutions of higher education.")


def test_non_competitive_continuation_text_is_ineligible():
    ok, why = matcher._is_institutionally_eligible(
        "Evidence-Informed Practices in the Management of Women's Prisons", TITLE_PATS, NIC_CONTINUATION, TEXT_PATS)
    assert not ok and "non-competitive" in why, why
    ok, why = matcher._is_institutionally_eligible(
        "Facilitator Training for Victim Offender Dialogue", TITLE_PATS,
        "Applications received from organizations other than the prior award recipient will not be reviewed.", TEXT_PATS)
    assert not ok, why
    # the open NIC call and a record without eligibility text are untouched
    assert matcher._is_institutionally_eligible("Facilitating Behavior Change Training for Trainers",
                                                TITLE_PATS, NIC_OPEN, TEXT_PATS) == (True, "")
    assert matcher._is_institutionally_eligible("Facilitating Behavior Change Training for Trainers",
                                                TITLE_PATS, "", TEXT_PATS) == (True, "")
    # title rules still fire with the new signature
    ok, why = matcher._is_institutionally_eligible(
        "NCI Research Specialist Award (R50 Clinical Trial Not Allowed)", TITLE_PATS, NIC_OPEN, TEXT_PATS)
    assert not ok and "R50" in why


def test_nic_requires_semantic_corroboration():
    assert "national institute of corrections" in M["corroboration_required_agencies"]


def test_ninth_wave_terms_present():
    terms = {t.lower() for t in M["context_dependent_terms"]}
    for t in ("challenges", "knowledge", "differences", "questions",
              "administration", "security", "behavior", "modeling"):
        assert t in terms, t
    # real topic words must not have crept in
    for t in ("mental health", "trauma", "robotics", "incarceration", "firearm"):
        assert t not in terms, t


def test_p30_parent_is_topicless():
    pats = [re.compile(p, re.I) for p in M["semantic_generic_guard"]["topicless_grant_patterns"]]
    assert any(p.search("NIH Research Center Core Grants") for p in pats)
    for title in ("Cancer Center Support Grants (P30 Clinical Trial Optional)",
                  "Core Facility Technology Development Program",
                  "Pancreatic Cancer Detection Consortium (U01)"):
        assert not any(p.search(title) for p in pats), title


def test_cross_source_dedupe_keeps_the_first_record():
    grants = [
        {"id": "27-501", "title": "Foundational Research in Robotics (FRR)", "source": "grants_gov"},
        {"id": "nsf-frr", "title": "Foundational Research in Robotics  (FRR)", "source": "nsf_funding"},
        {"id": "x", "title": "Something Else"},
        {"id": "y", "title": ""},
        {"id": "z", "title": ""},
    ]
    kept, dropped = grants_poller.dedupe_across_sources(grants)
    assert [g["id"] for g in kept] == ["27-501", "x", "y", "z"]
    assert [g["id"] for g in dropped] == ["nsf-frr"]


# ── PubMed robustness ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, text, status=200, headers=None):
        self.text, self.status_code, self.headers = text, status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise fs.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class _FlakySession:
    """429 on the first efetch, then success."""
    def __init__(self, esearch, efetch, failures=1):
        self.esearch, self.efetch, self.failures, self.calls = esearch, efetch, failures, []

    def get(self, url, params=None, **kw):
        self.calls.append(url)
        if "esearch" in url:
            return _Resp(self.esearch)
        if self.failures:
            self.failures -= 1
            return _Resp("", 429, {"Retry-After": "0"})
        return _Resp(self.efetch)


ESEARCH = json.dumps({"esearchresult": {"idlist": ["1", "2", "3"]}})
EFETCH = """<?xml version="1.0"?><PubmedArticleSet>
<PubmedArticle><MedlineCitation><Article>
 <ArticleTitle>Isoflurane and APOE4 lipid dysregulation</ArticleTitle>
 <AuthorList><Author><LastName>Wu</LastName><ForeName>Junfang</ForeName><Initials>J</Initials>
   <AffiliationInfo><Affiliation>University of Maryland School of Medicine</Affiliation></AffiliationInfo></Author></AuthorList>
</Article><MeshHeadingList><MeshHeading><DescriptorName>Anesthesia</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation></PubmedArticle>
<PubmedArticle><MedlineCitation><Article>
 <ArticleTitle>Higgs boson pair production</ArticleTitle>
 <AuthorList><Author><LastName>Wu</LastName><Initials>J</Initials>
   <AffiliationInfo><Affiliation>CERN, Geneva</Affiliation></AffiliationInfo></Author></AuthorList>
</Article><MeshHeadingList><MeshHeading><DescriptorName>Particle Physics</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation></PubmedArticle>
<PubmedArticle><MedlineCitation><Article>
 <ArticleTitle>Traumatic brain injury in mice</ArticleTitle>
 <AuthorList><Author><LastName>Wu</LastName><Initials>J</Initials>
   <AffiliationInfo><Affiliation>Shock Trauma and Anesthesiology Research Center, University of Maryland</Affiliation></AffiliationInfo></Author></AuthorList>
</Article><MeshHeadingList><MeshHeading><DescriptorName>Brain Injuries</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation></PubmedArticle>
</PubmedArticleSet>"""


def test_pubmed_retries_a_429_and_initials_only_authors_need_a_maryland_affiliation():
    s = _FlakySession(ESEARCH, EFETCH)
    fac = {"name": "Junfang Wu", "keywords": []}
    fs.enrich_from_pubmed(s, fac)
    kws = set(fac["keywords"])
    assert fac["pubmed_status"] == "ok", fac
    assert "anesthesia" in kws and "brain injuries" in kws, kws
    assert "particle physics" not in kws, kws
    assert fac["evidence_titles"] == ["Isoflurane and APOE4 lipid dysregulation", "Traumatic brain injury in mice"]


def test_pubmed_status_records_failures_and_empty_answers():
    class _Dead:
        def get(self, url, params=None, **kw):
            return _Resp("", 503)
    fac = {"name": "Elana J. Fertig", "keywords": ["pancreatic cancer"]}
    fs.enrich_from_pubmed(_Dead(), fac)
    assert fac["pubmed_status"] == "error" and fac["keywords"] == ["pancreatic cancer"]

    class _Empty:
        def get(self, url, params=None, **kw):
            return _Resp(json.dumps({"esearchresult": {"idlist": []}}))
    fac2 = {"name": "Elana J. Fertig", "keywords": []}
    fs.enrich_from_pubmed(_Empty(), fac2)
    assert fac2["pubmed_status"] == "no_results"

    class _Strangers:
        def get(self, url, params=None, **kw):
            return _Resp(ESEARCH if "esearch" in url else EFETCH)
    fac3 = {"name": "Rebecca Wu", "keywords": []}
    fs.enrich_from_pubmed(_Strangers(), fac3)
    assert fac3["pubmed_status"] == "no_author_match" and fac3["keywords"] == []


def test_carry_forward_restores_a_pubmed_bucket_only_after_a_failed_call():
    prev = [{"name": "Elana J. Fertig", "profile_url": "https://x/fertig/", "email": "e@som.umaryland.edu",
             "keywords": ["pancreatic cancer", "spatial transcriptomics"],
             "keywords_by_source": {"UMSOM (Keywords)": ["pancreatic cancer"], "PubMed": ["spatial transcriptomics"]},
             "keyword_sources": ["umsom_keywords", "pubmed(10papers,56MeSH)"],
             "evidence_titles": ["CODAvision"], "evidence_titles_by_source": {"PubMed": ["CODAvision"]},
             "scraped_at": "2026-11-11T11:00:00"},
            {"name": "Rachel B. Johnson", "profile_url": "https://x/rbj/", "email": "r@som.umaryland.edu",
             "keywords": ["ants"], "keywords_by_source": {"PubMed": ["ants"]},
             "keyword_sources": ["pubmed(10papers,50MeSH)"], "evidence_titles": ["Ant biodiversity"],
             "evidence_titles_by_source": {"PubMed": ["Ant biodiversity"]}, "scraped_at": "2026-11-11T11:00:00"}]
    active = [
        # PubMed FAILED this run: bucket + titles come back, tagged
        {"name": "Elana J. Fertig", "profile_url": "https://x/fertig/", "email": "e@som.umaryland.edu",
         "keywords": ["pancreatic cancer"], "keywords_by_source": {"UMSOM (Keywords)": ["pancreatic cancer"]},
         "keyword_sources": ["umsom_keywords"], "pubmed_status": "error"},
        # PubMed ANSWERED with nothing for this person: the old bucket stays gone
        {"name": "Rachel B. Johnson", "profile_url": "https://x/rbj/", "email": "r@som.umaryland.edu",
         "keywords": ["regional anesthesia"], "keywords_by_source": {"UMSOM (Keywords)": ["regional anesthesia"]},
         "keyword_sources": ["umsom_keywords"], "pubmed_status": "no_author_match"},
    ]
    counts = fs._carry_forward_from_cache(active, prev)
    assert counts == {"keywords": 0, "emails": 0, "pubmed": 1}, counts
    fertig = active[0]
    assert fertig["keywords"] == ["pancreatic cancer", "spatial transcriptomics"]
    assert fertig["keywords_by_source"]["PubMed"] == ["spatial transcriptomics"]
    assert "pubmed(10papers,56MeSH)" in fertig["keyword_sources"]
    assert fertig["evidence_titles"] == ["CODAvision"]
    assert fertig["pubmed_carried_from_cache"] == "2026-11-11T11:00:00"
    assert "carried_from_cache" not in fertig, "the whole-profile carry did not fire"
    assert active[1]["keywords"] == ["regional anesthesia"] and "PubMed" not in active[1]["keywords_by_source"]
    assert prev[0]["keywords_by_source"]["PubMed"] == ["spatial transcriptomics"], "no shared references"
