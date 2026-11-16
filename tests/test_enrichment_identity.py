"""Publication enrichment must attribute papers to THIS faculty member
(2026-11-16). "Johnson R[Author] AND Maryland[Affiliation]" had merged several
Johnsons into one Anesthesiology profile (glioma, phylogeny, visual cortex...)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import faculty_scraper as fs  # noqa: E402


def test_strip_credentials_handles_a_trailing_degree():
    assert fs._strip_credentials("Jane Smith, MD") == "Jane Smith"
    assert fs._strip_credentials("Jane Smith, MD, PhD") == "Jane Smith"
    assert fs._strip_credentials("Jane Smith") == "Jane Smith"


def test_author_is_faculty_matrix():
    yes = [("Rachel B. Johnson", "Johnson", "Rachel"),
           ("Rachel B. Johnson", "Johnson", "Rachel B"),
           ("Rachel B. Johnson", "Johnson", "R"),          # initials-only record
           ("Rachel B. Johnson", "Johnson", "RB"),
           ("R. Frank Henn", "Henn", "R Frank"),
           ("R. Frank Henn", "Henn", "Frank"),
           ("T.Chase Francis", "Francis", "Chase"),
           ("Li-Qun Zhang", "Zhang", "Li-Qun"),
           ("Daniel J. O. Roche, PhD", "Roche", "Daniel"),
           ("Maria A. Martino-Gomez", "Martino-Gomez", "Maria"),
           ("Wei Li", "Li", "W")]                         # 1-2 letter given name -> initial rule
    no = [("Rachel B. Johnson", "Johnson", "Robert"),
          ("Rachel B. Johnson", "Johnson", "Michael R"),
          ("Rachel B. Johnson", "Johnson", "MR"),
          ("Rachel B. Johnson", "Smith", "Rachel"),
          ("T.Chase Francis", "Francis", "Thomas"),
          ("Li-Qun Zhang", "Zhang", "Wei"),
          ("Stuart S. Martin", "Martin", "Stella"),
          ("Solo", "Solo", "S"), ("", "Johnson", "Rachel"), ("Rachel B. Johnson", "", "Rachel")]
    for fac, last, fore in yes:
        assert fs._author_is_faculty(fac, last, fore), (fac, last, fore)
    for fac, last, fore in no:
        assert not fs._author_is_faculty(fac, last, fore), (fac, last, fore)


ESEARCH = json.dumps({"esearchresult": {"idlist": ["1", "2"]}})
EFETCH = """<?xml version="1.0"?><PubmedArticleSet>
<PubmedArticle><MedlineCitation><Article>
 <ArticleTitle>Propofol dosing in obese adults</ArticleTitle>
 <AuthorList><Author><LastName>Johnson</LastName><ForeName>Rachel B</ForeName><Initials>RB</Initials>
   <AffiliationInfo><Affiliation>Department of Anesthesiology, University of Maryland School of Medicine</Affiliation></AffiliationInfo></Author></AuthorList>
 <KeywordList><Keyword>perioperative care</Keyword></KeywordList>
</Article><MeshHeadingList><MeshHeading><DescriptorName>Anesthesia</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation></PubmedArticle>
<PubmedArticle><MedlineCitation><Article>
 <ArticleTitle>Bird phylogeny of the Chesapeake</ArticleTitle>
 <AuthorList><Author><LastName>Johnson</LastName><ForeName>Robert</ForeName><Initials>R</Initials>
   <AffiliationInfo><Affiliation>University of Maryland, College Park</Affiliation></AffiliationInfo></Author></AuthorList>
</Article><MeshHeadingList><MeshHeading><DescriptorName>Phylogeny</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation></PubmedArticle>
</PubmedArticleSet>"""


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


class _Session:
    def get(self, url, params=None, **kw):
        return _Resp(ESEARCH if "esearch" in url else EFETCH)


def test_pubmed_keeps_only_this_persons_articles():
    fac = {"name": "Rachel B. Johnson", "keywords": []}
    fs.enrich_from_pubmed(_Session(), fac)
    kws = set(fac["keywords"])
    assert "anesthesia" in kws and "perioperative care" in kws, kws
    assert "phylogeny" not in kws, kws
    assert fac.get("evidence_titles") == ["Propofol dosing in obese adults"], fac.get("evidence_titles")
    assert any(src.startswith("pubmed(1papers") for src in fac.get("keyword_sources", [])), fac.get("keyword_sources")


def test_pubmed_skips_when_no_article_is_by_this_person():
    fac = {"name": "Rebecca Johnson", "keywords": []}
    fs.enrich_from_pubmed(_Session(), fac)
    assert fac["keywords"] == [] and not fac.get("evidence_titles")


EPMC = json.dumps({"resultList": {"result": [
    {"title": "A", "affiliation": "University of Maryland",
     "authorList": {"author": [{"lastName": "Johnson", "firstName": "Robert", "initials": "R",
                                "affiliation": "University of Maryland College Park"}]},
     "meshHeadingList": {"meshHeading": [{"descriptorName": "Phylogeny"}]}},
    {"title": "B", "affiliation": "University of Maryland",
     "authorList": {"author": [{"lastName": "Johnson", "firstName": "Rachel B", "initials": "RB",
                                "affiliation": "University of Maryland School of Medicine, Anesthesiology"}]},
     "meshHeadingList": {"meshHeading": [{"descriptorName": "Anesthesia, General"}]}},
]}})


class _EpmcSession:
    def get(self, url, params=None, **kw):
        return _Resp(EPMC)


def test_europe_pmc_name_fallback_requires_the_given_name():
    fac = {"name": "Rachel B. Johnson", "keywords": []}
    fs.enrich_from_europe_pmc(_EpmcSession(), fac)
    kws = set(fac["keywords"])
    assert "anesthesia, general" in kws and "phylogeny" not in kws, kws
