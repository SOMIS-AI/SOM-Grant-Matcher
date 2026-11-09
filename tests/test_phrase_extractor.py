"""Research Interests prose extraction must not leak filler or the person's name
(2026-11-09). Fixture paraphrases the Psychiatry profile whose fragments —
"identify the", "the etiology", "please see the", "roche s", "description",
"objectives" — anchored a 99% match on an unrelated NIDDK consortium."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import faculty_scraper as fs  # noqa: E402

PROSE = ("Dr. Roche\u2019s research combines behavioral pharmacology and human laboratory "
         "models of addiction to identify the behavioral and biological mechanisms of "
         "alcohol-use disorder and the etiology of treatment-resistant schizophrenia. "
         "Please see the description and objectives below. Research interests: clinical "
         "trial designs; medication development for substance- use.")

# the bare extractor knows no names — "roche" itself is removed in Pass 2 (second test)
BAD = {"identify the", "the behavioral", "the etiology", "please see the", "see the",
       "roche s", "description", "objectives", "the", "identify the behavioral",
       "substance-", "s"}


def test_prose_extraction_drops_filler_and_possessive_fragments():
    kws = fs._extract_phrases_from_text(PROSE, max_phrases=40)
    leaked = BAD & set(kws)
    assert not leaked, leaked
    assert all(not k.startswith("the ") and not k.endswith(" the") for k in kws), kws
    assert all(len(tok) > 1 for k in kws for tok in k.split()), kws
    assert "behavioral pharmacology" in kws
    assert any("alcohol" in k for k in kws), kws


def test_pass2_strips_the_persons_own_name_from_interest_phrases():
    html = ("<html><body><!-- Research Interests Details --><span id='Research Interest Details'></span>"
            "<p>" + PROSE + " Dr. Roche also leads the Roche laboratory for alcohol research.</p>"
            "<!-- Clinical Speciality Details --></body></html>")

    class R:
        text = html
        def raise_for_status(self): pass

    class S:
        def get(self, url, **kw): return R()

    fac = {"name": "Daniel J. O. Roche", "profile_url": "https://x/profiles/roche-daniel/", "email": "d@x.edu", "keywords": []}
    fs.scrape_individual_profile(S(), fac)
    assert fac["keywords"], "interest phrases were extracted"
    assert not any("roche" in k for k in fac["keywords"]), fac["keywords"]
    assert not any(k in BAD for k in fac["keywords"]), fac["keywords"]
