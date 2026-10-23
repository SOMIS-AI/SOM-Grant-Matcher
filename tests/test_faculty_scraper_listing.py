"""Department-listing parser, profile-page email guard and cache carry-forward
(2026-10-23). The fixture is the live Microbiology & Immunology listing around
Nevil Singh, who appears twice on the site: a stale Post Doc Fellow block with
no profile link, then the real Associate Professor block with mailto, link and
keywords. The old single-regex parse stapled the postdoc's name and FELLOW type
onto the professor's link and lost the professor."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import faculty_scraper as fs  # noqa: E402

LISTING = """<html><head><title>Microbiology and Immunology - All Faculty</title></head><body><ul>
<li data-salary-desc="FULL TIME" data-emp-type="FACULTY" data-academic-title="Professor Emeritus" data-gender="M">
  <strong class="data-profile-name">
    Silverman,
    David
     J.,
    PhD,
    Professor Emeritus
  </strong>
  <div></div>
<!-- <div><a href="mailto:"></a></div> -->
<a class="data-profile-link" href="https://www.medschool.umaryland.edu/profiles/silverman-david/">View full profile</a>
</li><li data-salary-desc="FULL TIME" data-emp-type="FELLOW" data-academic-title="Post Doc Fellow" data-gender="UNK">
  <strong class="data-profile-name">
    Singh,
    Nevil
    ,
    PhD, MSc, BS,
    Post Doc Fellow
  </strong>
</li><li data-salary-desc="FULL TIME" data-emp-type="FACULTY" data-academic-title="Associate Professor" data-gender="M">
  <strong class="data-profile-name">
    Singh,
    Nevil
     J.,
    PhD,
    Associate Professor
  </strong>
  <div>4107066916</div>
<div><a href='mailto:nsingh@som.umaryland.edu'>nsingh@som.umaryland.edu</a></div>
<!-- <div><a href="mailto:nsingh@som.umaryland.edu">nsingh@som.umaryland.edu</a></div> -->
<a class="data-profile-link" href="https://www.medschool.umaryland.edu/profiles/singh-nevil/">View full profile</a>
  <div>Keywords: T cell activation &amp; differentiation, Immunological tolerance, Neuroimmunology</div>
</li><li data-salary-desc="GEOGRAPHIC FULL TIME" data-emp-type="FACULTY" data-academic-title="Assistant Professor" data-gender="M">
  <strong class="data-profile-name">
    Skerry,
    Ciaran
    ,
    PhD,
    Assistant Professor
  </strong>
<!-- <div><a href="mailto:wrong@som.umaryland.edu">wrong@som.umaryland.edu</a></div> -->
<a class="data-profile-link" href="/profiles/skerry-ciaran/">View full profile</a>
</li><li data-salary-desc="GEOGRAPHIC FULL TIME" data-emp-type="FACULTY" data-academic-title="Professor" data-gender="M">
  <strong class="data-profile-name">
    Henn,
    R. Frank
    ,
    MD,
    Professor
  </strong>
<div><a href='mailto:cfox@som.umaryland.edu'>cfox@som.umaryland.edu</a></div>
<a class="data-profile-link" href="/profiles/henn-r-frank/">View full profile</a>
</li></ul></body></html>"""


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, text):
        self.text = text

    def get(self, url, **kw):
        return _Resp(self.text)


def test_listing_parses_each_li_on_its_own():
    fac = fs.scrape_department_page(_Session(LISTING), "https://x/profiles/micro---all-faculty/")
    names = [f["name"] for f in fac]
    assert names == ["David J. Silverman", "Nevil J. Singh", "Ciaran Skerry", "R. Frank Henn"], names
    assert all(f["emp_type"] != "FELLOW" for f in fac), \
        "the linkless postdoc block is skipped, not stapled onto the next link"
    singh = fac[1]
    assert singh["profile_url"].endswith("/profiles/singh-nevil/")
    assert singh["title"] == "Associate Professor" and singh["emp_type"] == "FACULTY"
    assert singh["email"] == "nsingh@som.umaryland.edu" and singh["email_source"] == "umsom_listing"
    assert "t cell activation & differentiation" in singh["keywords"]
    assert "neuroimmunology" in singh["keywords"]
    assert singh["keywords_by_source"]["UMSOM (Keywords)"]
    assert fac[0]["email"] == "" and fac[0]["title"] == "Professor Emeritus"
    assert fac[2]["email"] == "", "an HTML-commented mailto is not an address"
    assert fac[2]["profile_url"] == "https://www.medschool.umaryland.edu/profiles/skerry-ciaran/"
    assert fac[2]["employment_status"] == "GEOGRAPHIC FULL TIME"
    assert all(f["department"] == "Microbiology and Immunology" for f in fac)
    henn = fac[3]
    assert henn["email"] == "" and henn["email_source"] == "", \
        "the site lists a coordinator's address for Henn — not seeded as his"
    assert henn["listing_contact_email"] == "cfox@som.umaryland.edu"
    assert singh["listing_contact_email"] == ""


def test_email_matches_name():
    yes = [("nsingh@som.umaryland.edu", "Nevil J. Singh"),
           ("kevin.jones@som.umaryland.edu", "Kevin A. Jones"),
           ("kjones@som.umaryland.edu", "Kevin A. Jones"),
           ("yfouche@som.umaryland.edu", "LaRita Y. Fouche-Weber"),
           ("cxu@som.umaryland.edu", "Claire M. Xu"),
           ("wli2@som.umaryland.edu", "Wei Li"),
           ("kalecarter@som.umaryland.edu", "Kale Carter"),
           ("dwolderu@som.umaryland.edu", "Daniel A. Wolde-Rufael"),
           ("l-zhang@som.umaryland.edu", "Li-Qun Zhang")]
    no = [("cfox@som.umaryland.edu", "R. Frank Henn"),
          ("evalentin@som.umaryland.edu", "Jonathan D. Packer"),
          ("ethomas@som.umaryland.edu", "Robert V. O'Toole"),
          ("mcardona@som.umaryland.edu", "Maria A. Martino-Gomez"),
          ("dloane@som.umaryland.edu", "Grace Lin"),
          ("kfitzpatrick@som.umaryland.edu", "Steven C. Ludwig"),
          ("peds-endocrinology@som.umaryland.edu", "Chaitanya Sambangi"),
          ("", "Anyone"), ("x@y.edu", "")]
    for em, nm in yes:
        assert fs._email_matches_name(em, nm), (em, nm)
    for em, nm in no:
        assert not fs._email_matches_name(em, nm), (em, nm)


def test_profile_page_email_guard_leaves_a_strangers_address_blank():
    page = ("<html><body><p>Contact: <a href='mailto:cfox@som.umaryland.edu'>cfox@som.umaryland.edu</a></p>"
            "<!-- <div>cfox@som.umaryland.edu</div> --></body></html>")
    henn = {"name": "R. Frank Henn", "profile_url": "https://x/profiles/henn-r-frank/", "email": "", "keywords": []}
    fs.scrape_individual_profile(_Session(page), henn)
    assert henn["email"] == "", "an assistant's address is not the professor's"

    own = ("<html><body>admin@som.umaryland.edu "
           "<a href='mailto:rhenn@som.umaryland.edu'>rhenn@som.umaryland.edu</a></body></html>")
    henn2 = {"name": "R. Frank Henn", "profile_url": "https://x/profiles/henn-r-frank/", "email": "", "keywords": []}
    fs.scrape_individual_profile(_Session(own), henn2)
    assert henn2["email"] == "rhenn@som.umaryland.edu" and henn2["email_source"] == "umsom_profile"

    keep = {"name": "R. Frank Henn", "profile_url": "https://x/profiles/henn-r-frank/",
            "email": "nsingh@som.umaryland.edu", "keywords": []}
    fs.scrape_individual_profile(_Session(page), keep)
    assert keep["email"] == "nsingh@som.umaryland.edu", "an address seeded from the listing is never overwritten"


def test_carry_forward_fills_only_what_the_fresh_scrape_left_empty():
    prev = [
        {"name": "Stefan H. Kappe", "profile_url": "https://x/profiles/kappe-stefan/",
         "email": "skappe@som.umaryland.edu",
         "keywords": ["malaria", "vaccines"], "keywords_by_source": {"PubMed": ["malaria", "vaccines"]},
         "keyword_sources": ["pubmed(3papers)"], "keyword_source": "pubmed(3papers)",
         "evidence_titles": ["A malaria vaccine"], "scraped_at": "2026-10-10T10:00:00", "inactive": True},
        {"name": "Fresh Person", "profile_url": "https://x/profiles/fresh/", "email": "fresh@som.umaryland.edu",
         "keywords": ["old stuff"], "scraped_at": "2026-10-10T10:00:00"},
        {"name": "Role Only", "profile_url": "https://x/profiles/role/",
         "email": "peds-endocrinology@som.umaryland.edu",
         "keywords": ["a"], "scraped_at": "2026-10-10T10:00:00"},
    ]
    active = [
        {"name": "Stefan H. Kappe", "profile_url": "https://x/profiles/kappe-stefan/", "email": "", "keywords": []},
        {"name": "Fresh Person", "profile_url": "https://x/profiles/fresh/", "email": "", "keywords": ["new stuff"]},
        {"name": "Role Only", "profile_url": "https://x/profiles/role/", "email": "", "keywords": []},
        {"name": "Brand New", "profile_url": "https://x/profiles/new/", "email": "", "keywords": []},
    ]
    counts = fs._carry_forward_from_cache(active, prev)
    assert counts == {"keywords": 2, "emails": 2}, counts
    kappe = active[0]
    assert kappe["keywords"] == ["malaria", "vaccines"]
    assert kappe["keywords_by_source"] == {"PubMed": ["malaria", "vaccines"]}
    assert kappe["email"] == "skappe@som.umaryland.edu" and kappe["email_source"] == "carried_from_cache"
    assert kappe["evidence_titles"] == ["A malaria vaccine"]
    assert kappe["carried_from_cache"] == "2026-10-10T10:00:00"
    assert active[1]["keywords"] == ["new stuff"], "fresh keywords are never replaced"
    assert active[1]["email"] == "fresh@som.umaryland.edu", "but a missing email is still filled"
    assert active[2]["keywords"] == ["a"] and active[2]["email"] == "", "a role mailbox is not carried"
    assert active[3]["keywords"] == [] and "carried_from_cache" not in active[3]
    # the previous record is untouched (no shared references)
    kappe["keywords"].append("x")
    kappe["keywords_by_source"]["PubMed"].append("x")
    assert prev[0]["keywords"] == ["malaria", "vaccines"]
    assert prev[0]["keywords_by_source"]["PubMed"] == ["malaria", "vaccines"]


def test_split_curated_keywords():
    assert fs._split_curated_keywords("Malaria, Vaccines; Immunology") == ["malaria", "vaccines", "immunology"]
    assert fs._split_curated_keywords("") == []
    prose = fs._split_curated_keywords(
        "I diagnose and treat patients with blood disorders including acute myeloid leukemia and lymphoma in adults")
    assert prose and all(len(p) < 60 for p in prose), prose


if __name__ == "__main__":
    test_listing_parses_each_li_on_its_own()
    test_email_matches_name()
    test_profile_page_email_guard_leaves_a_strangers_address_blank()
    test_carry_forward_fills_only_what_the_fresh_scrape_left_empty()
    test_split_curated_keywords()
    print("PASS")
