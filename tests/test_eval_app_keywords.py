"""Regression cases for the Faculty Eval App keyword parser.

Run with `python -m pytest tests/` or directly: `python tests/test_eval_app_keywords.py`.
Every case is a real shape seen in a campaign export; see TUNING_LOG /
seed_data/README.md for where they came from.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import eval_app_keywords as ek  # noqa: E402

CASES = [
    # clean lists
    ("trauma, whole blood", ["trauma", "whole blood"]),
    ("Parkinson&rsquo;s Disease; deep brain stimulation", ["Parkinson’s Disease", "deep brain stimulation"]),
    ("PET/CT, ECMO, St. Jude protocol", ["PET/CT", "ECMO", "St. Jude protocol"]),
    # run-together CamelCase
    ("CTLung cancer screenPulmonary nodules", ["Lung cancer screen", "Pulmonary nodules"]),
    # numbered lists whose line breaks were flattened (2026-09-23)
    ("pharmacoepidemiology2. epidemiology3. GLP-1", ["pharmacoepidemiology", "epidemiology", "GLP-1"]),
    ("Cancer Immunology2. Cancer Stem Cells", ["Cancer Immunology", "Cancer Stem Cells"]),
    # periods as separators (2026-09-23)
    ("live attenuated vaccines. conjugate vaccines. influenza",
     ["live attenuated vaccines", "conjugate vaccines", "influenza"]),
    ("carbon monoxide. decompression sickness", ["carbon monoxide", "decompression sickness"]),
    ("COVID-19. long COVID, IL6. cytokines", ["COVID-19", "long COVID", "IL6", "cytokines"]),
    ("Cognitive Functioning and Aging (normal aging, MCI). dementia",
     ["Cognitive Functioning and Aging (normal aging, MCI)", "dementia"]),
    # periods that are punctuation, not separators
    ("isolation and purification of recombinant proteins from E. coli",
     ["isolation and purification of recombinant proteins from E. coli"]),
    ("U.S. health policy, Ph.D. training, Dr. Smith lab",
     ["U.S. health policy", "Ph.D. training", "Dr. Smith lab"]),
    ("wearable sensors in PD. Collaboration with Dept. of Computer Sciences, College Park",
     ["wearable sensors in PD", "Collaboration with Dept. of Computer Sciences", "College Park"]),
    # leading filler words (2026-09-23)
    ("adolescent brain, the adolescent brain, and side effects, Ultimately, of Computer Sciences",
     ["adolescent brain", "side effects", "Computer Sciences"]),
    # short prepositions/articles that begin real terms must survive
    ("In vivo imaging, A / PDE11, At-risk youth", ["In vivo imaging", "A / PDE11", "At-risk youth"]),
    # opt-outs
    ("research is not my area of expertise", []),
    ("n/a", []),
]


def test_parse_keywords_field():
    for raw, want in CASES:
        assert ek.parse_keywords_field(raw) == want, raw


def test_normalize_person_name():
    assert ek.normalize_person_name("Kristyn N. Donohue") == "kristyn donohue"
    assert ek.normalize_person_name("Kristyn Donohue, Ph.D.") == "kristyn donohue"


if __name__ == "__main__":
    fails = 0
    for raw, want in CASES:
        got = ek.parse_keywords_field(raw)
        ok = got == want
        fails += not ok
        print(("ok   " if ok else "FAIL ") + repr(raw)[:60], "->", got)
    test_normalize_person_name()
    print("PASS" if not fails else f"{fails} FAIL")
    sys.exit(1 if fails else 0)
