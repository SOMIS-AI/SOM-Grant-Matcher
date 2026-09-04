"""
Faculty Profile Scraper
Scrapes UMSOM department faculty listing pages to extract names and research keywords.

Strategy:
1.  Scrape all department "all-faculty" listing pages — keywords listed directly here
2.  Visit individual UMSOM profile pages — parse Keywords: field + research bio text
3.  PubMed MeSH terms from recent publications (affiliation-verified)
4.  NIH RePORTER active grants — terms & abstract mining (affiliation-verified)
5.  ORCID — self-reported keywords + work titles (affiliation-verified)
6.  Semantic Scholar — fields of study from recent papers (affiliation-verified)
7.  ClinicalTrials.gov — condition/intervention terms from active trials (institution-verified)
8.  Europe PMC — MeSH & author keywords, broader than PubMed (affiliation-verified)
9.  Generate semantic embeddings for all faculty

Active faculty only: the live scrape is the authoritative faculty list.
Faculty present in the cache but absent from the current scrape are marked inactive
and excluded from matching — this prevents departed faculty from receiving alerts.

All external sources merge keywords rather than replace; UMSOM profile keywords
always appear first (highest trust). Sources are tracked per faculty member.
"""

import html
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

try:
    from matcher import record_scrape_stats
except ImportError:
    record_scrape_stats = None

try:
    from embedder import embed_faculty_batch, is_available as embeddings_available
except ImportError:
    embed_faculty_batch = None
    embeddings_available = lambda: False

BASE_URL = "https://www.medschool.umaryland.edu"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; UMSOMGrantMatcher/1.0; "
        "+mailto:grants@yourinstitution.edu)"
    )
}

PUBMED_HEADERS = {
    "User-Agent": "UMSOMGrantMatcher/1.0 (mailto:grants@yourinstitution.edu)"
}

DEPARTMENT_PAGES = [
    "/profiles/anesthesiology---all-faculty/",
    "/profiles/biochemistry--molecular-biology---all-faculty/",
    "/profiles/dermatology---all-faculty/",
    "/profiles/diagnostic-radiology-and-nuclear-medicine---all-faculty/",
    "/profiles/emergency-medicine---all-faculty/",
    "/profiles/epidemiology--public-health---all-faculty/",
    "/profiles/family-and-community-medicine---all-faculty/",
    "/profiles/medical-and-research-technology---all-faculty/",
    "/profiles/medicine---all-faculty/",
    "/profiles/microbiology-and-immunology---all-faculty/",
    "/profiles/neurobiology---all-faculty/",
    "/profiles/neurology---all-faculty/",
    "/profiles/neurosurgery---all-faculty/",
    "/profiles/obgyn---all-faculty/",
    "/profiles/obgyn---primary-faculty/",
    "/profiles/ophthalmology-and-visual-sciences---all-faculty/",
    "/profiles/orthopaedics---all-faculty/",
    "/profiles/otorhinolaryngology---head--neck-surgery---all-faculty/",
    "/profiles/pathology---primary-faculty/",
    "/profiles/pediatrics---all-faculty/",
    "/profiles/pharmacology--physiology---all-faculty/",
    "/profiles/physiology---all-faculty/",
    "/profiles/physical-therapy-and-rehabilitation-science---all-faculty/",
    "/profiles/psychiatry---all-faculty/",
    "/profiles/radiation-oncology---all-faculty/",
    "/profiles/surgery---all-faculty/",
    "/profiles/urology---primary-faculty/",
]


def clean_text(text: str) -> str:
    # html.unescape: dept pages carry entities ("Epidemiology &amp; Public
    # Health") which otherwise render literally in the digest/Excel (seen in
    # every report through 2026-08-04). Decode at capture so names/departments
    # are stored clean.
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def discover_department_pages(session: requests.Session, index_url: str) -> list:
    """
    Harvest the current department faculty-listing URLs from the live UMSOM
    faculty-profiles index. Returns a list of "/profiles/<dept>---(all|primary)-faculty/"
    paths. Skips "secondary-faculty" (cross-appointments → duplicates handled by
    dedup). Returns [] if discovery fails or looks implausible, so the caller can
    fall back to the built-in DEPARTMENT_PAGES.

    This makes the scraper resilient to site redesigns that rename department
    slugs (e.g. the 2026 redesign left "biochemistry--molecular-biology---all-faculty"
    returning 0 faculty and "urology---primary-faculty" 404'ing).
    """
    try:
        resp = session.get(index_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Department discovery failed ({e}); using built-in list")
        return []

    paths = sorted(set(re.findall(
        r"/profiles/[a-z0-9-]+---(?:all|primary)-faculty/", resp.text
    )))
    # Prefer a dept's "all-faculty" page; only add its "primary-faculty" page when
    # there is no all-faculty variant (dedup by faculty handles any overlap).
    all_depts = {p.split("---")[0] for p in paths if p.endswith("---all-faculty/")}
    chosen = [p for p in paths if p.endswith("---all-faculty/")]
    chosen += [p for p in paths
               if p.endswith("---primary-faculty/") and p.split("---")[0] not in all_depts]

    if len(chosen) < 15:
        logger.warning(
            f"Department discovery yielded only {len(chosen)} pages; using built-in list"
        )
        return []
    logger.info(f"Discovered {len(chosen)} department listing pages from the live index")
    return chosen


# Navigation / UI boilerplate that leaks out of the profile pages and must never
# be treated as a research keyword (e.g. "Update Your Profile", "Faculty Profiles
# Sync", "Download", "Email"). These were showing up as dynamic stop words in the
# diagnostic, which means they were being ingested as keywords. Filter at the source.
_BOILERPLATE_KW = {
    "academic title", "appointment", "primary appointment", "primary",
    "title", "download", "email", "profile", "profiles", "faculty profiles",
    "faculty profiles sync", "profiles sync", "home faculty profiles",
    "view full profile", "view profile", "full profile",
    "update", "update your", "update your profile", "your profile",
    "umaryland", "university of maryland", "school of medicine",
    "quick links", "skip to", "search",
}


def _is_boilerplate_kw(kw: str) -> bool:
    """True if a candidate keyword is page navigation/UI boilerplate, not research."""
    k = re.sub(r"\s+", " ", (kw or "").strip().lower())
    if not k or k in _BOILERPLATE_KW:
        return True
    if "profile" in k and any(w in k for w in ("update", "sync", "home", "view", "faculty")):
        return True
    if k.startswith("update your") or k.startswith("your "):
        return True
    if "umaryland" in k:
        return True
    return False


# ── Pass 1: Department pages ──────────────────────────────────────────────────

# Degree/credential tokens that can follow a name in "Last, First, MD, Title".
_CREDENTIAL_RE = re.compile(
    r"^(MD|PhD|DO|DrPH|DPT|MPH|MSc|MS|DSc|ScD|EdD|DDS|DMD|MBBS|MBChB|BMBS|MGC|MHS|"
    r"Dpharm|PharmD|MBA|MPP|JD|RN|APRN|BA|BS|MEng|FACP|FACS|FACOG)\b",
    re.IGNORECASE,
)


def _normalize_profile_name(raw: str) -> str:
    """
    Convert the redesigned site's "Last, First Middle, Credentials, Title" string
    into a "First [Middle] Last" name. This matters because every enrichment source
    (PubMed/ORCID/NIH RePORTER/ClinicalTrials/Europe PMC) parses the name as
    "First … Last" — a last-name-only value silently breaks all of them.
    """
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    last, first = parts[0], parts[1]
    # The 2nd comma-field should be a given name, not a credential/title.
    if _CREDENTIAL_RE.match(first) or len(first) > 40:
        return last
    return f"{first} {last}".strip()


def _extract_title(raw: str) -> str:
    """
    Extract the academic title/rank from "Last, First [Middle], Credential[s], Title".
    Drops leading name fields and any pure-credential parts (MD, PhD, MPH, …).
    Returns the joined remaining comma-fields — usually a single title string like
    "Assistant Professor", "Adjunct Associate Professor", "Professor Emeritus",
    "Post Doc Fellow", "Research Associate", etc. Returns "" if no title is present.
    """
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if len(parts) < 3:
        return ""
    rest = parts[2:]  # drop last + first
    title_parts = [p for p in rest if not _CREDENTIAL_RE.match(p)]
    return ", ".join(title_parts).strip()


def scrape_department_page(session: requests.Session, url: str) -> list[dict]:
    """
    Parse a redesigned UMSOM department listing page. Each faculty member is a
    structured block exposing `data-profile-name` (Last, First, creds, title) and
    `data-profile-link` (profile URL); the department name is in the page <title>.
    Keywords/email are NOT on this page — they come from the individual profile
    in Pass 2.
    """
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return []

    html = resp.text

    # Department from <title> (e.g. "Dermatology - All Faculty" -> "Dermatology")
    department = ""
    tm = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if tm:
        department = clean_text(re.sub(r"<[^>]+>", " ", tm.group(1)))
        department = re.sub(r"\s*[-–|]\s*(all|primary|secondary)\s*faculty.*$", "",
                            department, flags=re.I).strip()
        department = re.sub(r"\s*\|\s*University of Maryland.*$", "", department, flags=re.I).strip()

    # Structured faculty blocks, in document order. Each <li> carries the
    # employment classification used by the page's "Volunteer"/"Part Time" tabs
    # (data-salary-desc) and faculty-vs-fellow type (data-emp-type), followed by
    # the profile name + link. Capturing them together in one per-block regex
    # keeps salary/type aligned to the right person (validated: exact 1:1 parity
    # with the prior name/link parse across all departments).
    blocks = re.findall(
        r'<li\s+data-salary-desc="([^"]*)"\s+data-emp-type="([^"]*)"[^>]*?>'
        r'.*?class="data-profile-name"[^>]*>(.*?)</strong>'
        r'.*?class="data-profile-link"\s+href="([^"]+)"',
        html, re.S,
    )

    # Safety net: if the employment-aware parse under-captures (e.g. the page
    # markup drifts and some <li> lose data-salary-desc), fall back to the proven
    # name+link parse with empty employment fields so we never silently DROP
    # faculty — they just won't carry an employment signal (graceful, like the
    # pre-rescrape cache). Exclusions degrade off; matching is unaffected.
    n_names = len(re.findall(r'class="data-profile-name"[^>]*>(.*?)</strong>', html, re.S))
    if len(blocks) < n_names:
        logger.warning(
            f"  {department or url}: employment parse captured {len(blocks)}/{n_names} "
            f"faculty — falling back to name/link parse (no employment data this run)"
        )
        names = re.findall(r'class="data-profile-name"[^>]*>(.*?)</strong>', html, re.S)
        links = re.findall(r'class="data-profile-link"\s+href="([^"]+)"', html)
        blocks = [("", "", nm, lk) for nm, lk in zip(names, links)]

    faculty = []
    seen = set()
    for salary_desc, emp_type, raw_name, href in blocks:
        raw_name = clean_text(re.sub(r"<[^>]+>", " ", raw_name))
        name = _normalize_profile_name(raw_name)
        profile_url = urljoin(BASE_URL, href.strip())
        if not name or profile_url in seen:
            continue
        seen.add(profile_url)
        faculty.append({
            "name": name,
            "raw_profile_name": raw_name,    # full "Last, First, creds, title" string
            "title": _extract_title(raw_name),  # academic rank ("Assistant Professor", "Adjunct Professor", "Professor Emeritus", …)
            "employment_status": (salary_desc or "").strip().upper(),  # VOLUNTEER | PART TIME | FULL TIME | GEOGRAPHIC FULL TIME
            "emp_type": (emp_type or "").strip().upper(),              # FACULTY | FELLOW
            "url": profile_url,
            "profile_url": profile_url,
            "department": department,
            "email": "",                 # captured from the profile page in Pass 2
            "keywords": [],              # extracted from the profile page in Pass 2
            "keyword_source": "",
            "scraped_at": datetime.utcnow().isoformat(),
        })

    logger.info(f"  {department or url}: {len(faculty)} faculty")
    return faculty


# ── Pass 2: Individual UMSOM profile pages ────────────────────────────────────
#
# Visits each faculty member's individual UMSOM profile page and extracts their
# Research Interests section with a dedicated, structured approach.
#
# Four-strategy extraction (tried in order, results merged):
#   S1 — Structured label: find ANY element whose text is exactly or closely
#        "Research Interests" (or synonyms) and extract the content that follows.
#        Handles h2/h3/h4/strong/b/div/span/p as label elements — covers both
#        standard heading markup AND Drupal field-label patterns.
#   S2 — Explicit keyword line: scan plain text for "Keywords: ..." lines.
#   S3 — Research-vocabulary paragraphs: paragraphs containing biomedical terms.
#   S4 — Main content fallback: first 800 chars of main content area.
#
# Phrase extraction:
#   - For comma/semicolon lists → split directly into phrases (highest quality)
#   - For prose text → extract 1-3 word noun phrases using a sliding window
#     rather than individual words, preserving "blood-brain barrier",
#     "tau pathology", "cardiac stem cells" etc.
#
# Pass 2 now runs on ALL faculty (not just those missing keywords) and merges
# Research Interests content with any keywords already found in Pass 1.
# The early-return that was blocking subsequent enrichment passes is removed.

# Heading-like labels that signal the Research Interests section
_RI_LABELS = re.compile(
    r"^\s*(?:research\s+(?:interests?|summary|focus|areas?|background|expertise)|"
    r"areas?\s+of\s+(?:research|expertise|interest)|"
    r"clinical\s+(?:interests?|expertise|focus)|"
    r"laboratory\s+(?:focus|interests?|overview)|"
    r"scientific\s+(?:interests?|focus)|"
    r"expertise|"
    r"my\s+research)\s*:?\s*$",
    re.IGNORECASE,
)

# Stop words for prose phrase extraction (broader than matcher stop words —
# these are grammatical/structural words, not domain terms). Now also includes
# articles, conjunctions, prepositions, common verbs, and first-person/clinic
# preamble words ("I diagnose and treat patients with…") that otherwise pollute
# the keyword pool when a faculty member writes prose in a "keywords" field.
_BIO_STOP = {
    # articles/conjunctions/prepositions/auxiliaries
    "a", "an", "and", "or", "but", "nor", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "as", "if", "so", "than", "then", "that", "this",
    "these", "those", "there", "is", "am", "are", "was", "were", "be", "been",
    "being", "has", "have", "had", "do", "does", "did", "can", "could", "may",
    "might", "should", "would", "will", "shall", "into", "through", "between",
    "during", "after", "about", "before", "above", "below", "under", "over",
    "such", "also", "more", "very", "when", "where", "which", "while",
    # pronouns / first-person preamble
    "my", "our", "your", "his", "her", "their", "its", "hers", "ours", "theirs",
    "we", "i", "you", "they", "it", "he", "she", "them", "us", "me", "who",
    # common verbs that show up in prose ("I diagnose and treat patients with…")
    "seeks", "seek", "seeking", "treat", "treats", "treated", "treating",
    "predict", "predicts", "predicted", "predicting", "use", "uses", "used",
    "using", "utilize", "utilizes", "utilizing", "utilization", "understand",
    "understands", "understood", "understanding", "employ", "employs",
    "employed", "employing", "apply", "applies", "applied", "applying",
    "diagnose", "diagnoses", "diagnosing", "diagnosed",
    "study", "studies", "studied", "studying", "examine", "examines",
    "examined", "examining", "develop", "develops", "developed", "developing",
    "improve", "improves", "improved", "improving", "advance", "advances",
    "advanced", "advancing", "assess", "assesses", "assessed", "assessing",
    "investigate", "investigates", "investigated", "investigating",
    "explore", "explores", "explored", "exploring",
    "trained", "training", "received", "completed", "joined", "working", "worked",
    "focused", "focuses", "focusing", "focus", "include", "includes", "included",
    "including", "especially", "specifically", "primarily", "mainly", "mostly",
    "additionally", "furthermore", "moreover", "however", "although", "though",
    # structural/institutional preamble
    "laboratory", "lab", "labs", "group", "team", "center", "facility",
    "department", "division", "section", "clinic", "clinics", "hospital",
    "institute", "university", "maryland", "school", "medicine",
    "faculty", "professor", "assistant", "associate", "adjunct", "fellow",
    "instructor", "member", "staff", "board", "certified", "interested",
    "interests", "interest", "area", "areas", "field", "fields",
    "research", "project", "projects", "program", "programs",
    "approach", "approaches", "method", "methods", "technique", "techniques",
    "tool", "tools", "framework", "process", "processes", "system", "systems",
    # clinical filler
    "patient", "patients", "people", "person", "current", "ongoing", "recent",
    "novel", "new", "various", "multiple", "several", "many", "few", "both",
    "either", "neither", "such", "certain", "different", "similar", "related",
    "better", "best", "worse", "good", "bad", "well",
}

# Tokens ending in -ing that are valid biomedical nouns (the gerund -> noun
# heuristic below would otherwise reject them). Expand cautiously when a
# legitimate -ing noun shows up in real profiles.
_NOUN_LIKE_ING = {
    "editing", "imaging", "profiling", "sequencing", "screening", "modeling",
    "modelling", "staining", "signaling", "signalling", "training", "mapping",
    "probing", "grafting", "blotting", "tagging", "silencing", "splicing",
    "folding", "binding", "aging", "scanning", "tracking", "monitoring",
    "engineering",
}

# (Retained for back-compat — only referenced by this module's old code paths.)
_SINGLE_NOISE = {"also", "thus", "role", "such", "lead", "leads", "novel", "known"}


def _looks_verb_or_adv(tok: str) -> bool:
    """Heuristic: token is likely a verb form / adverb (not a noun phrase head)."""
    t = tok.lower()
    if t in _NOUN_LIKE_ING:
        return False
    if t.endswith("ly") and len(t) > 4:
        return True
    if t.endswith("ing") and len(t) > 4:
        return True
    if t.endswith("ed") and len(t) > 4:
        return True
    if t.endswith("ize") or t.endswith("ise"):
        return True
    return False


def _score_phrase(tokens: list[str]) -> int:
    """Score a phrase 0+ for ranking. Any phrase containing a stop word is rejected (0)."""
    s = 0
    for t in tokens:
        if t in _BIO_STOP:
            return 0
        if _looks_verb_or_adv(t):
            s -= 4
        elif len(t) < 4:
            s -= 1
        else:
            s += 2
    s += min(len(tokens) - 1, 2)  # mild bonus for multi-word
    return s


def _extract_phrases_from_text(text: str, max_phrases: int = 40) -> list:
    """
    Extract meaningful research-domain keywords from prose or sentence-style text.

    The 2026-05-29 rewrite addresses two real-world failure modes seen on UMSOM
    faculty profiles:
      1. Sliding-window n-grams crossing comma/sentence boundaries produced
         nonsense phrases like "disorders employ" / "biology bioinformatics" /
         "leukemia cmml chronic".
      2. The "Research/Clinical Keywords" field is sometimes a sentence rather
         than a comma list ("I diagnose and treat patients with blood disorders
         including acute myeloid leukemia (AML), …"), so a naive comma split
         leaks sentence preamble as a giant first "keyword".

    Strategy:
      - Capture parenthesised acronyms ("X (AAA)") first, recording both the
        noun phrase before the paren AND the acronym itself.
      - Split text on sentence/list punctuation [.!?;,()/] to form SEGMENTS;
        n-grams only form WITHIN a segment so commas can't bleed across.
      - Within each segment, score 1/2/3-grams. Drop anything containing a
        stop word, an unambiguous verb (-ing/-ed/-ly unless whitelisted),
        or a token < 4 chars (except trigram middle, where 'of' is allowed).
      - Suppress single-word keywords that are already covered by a multi-word
        keyword we kept ("biology" if "synthetic biology" already scored).
      - Return the top-N by score, longer phrases preferred on ties.
    """
    text = text or ""
    scored: dict[str, int] = {}
    freq: dict[str, int] = {}   # repetition counter — drives a small score bonus

    def record(ph: str, sc: int):
        if sc <= 0:
            return
        scored[ph] = max(scored.get(ph, 0), sc)
        freq[ph]   = freq.get(ph, 0) + 1

    # Parenthesised acronyms — "X (AAA)" -> noun phrase before + the acronym itself.
    for m in re.finditer(r"([\w\- ]+?)\s*\(([A-Z]{2,6}s?)\)", text):
        before = m.group(1).strip().lower()
        acro   = m.group(2).strip().lower()
        if acro and acro not in _BIO_STOP:
            record(acro, 10)
        if before:
            words = before.split()
            for n in (4, 3, 2, 1):
                if len(words) >= n:
                    cand = " ".join(words[-n:])
                    if all(w not in _BIO_STOP and not _looks_verb_or_adv(w)
                           for w in cand.split()):
                        record(cand, 12)
                        break

    # Segment on punctuation so n-grams stay within a single keyword/clause.
    for seg in re.split(r"[.!?;,()/]+", text):
        cleaned = re.sub(r"[^A-Za-z0-9\- ]", " ", seg)
        tokens  = [t.lower() for t in cleaned.split() if t]
        # 1-grams
        for t in tokens:
            if (t in _BIO_STOP or t in _SINGLE_NOISE
                    or _looks_verb_or_adv(t) or len(t) < 5):
                continue
            record(t, _score_phrase([t]))
        # 2-grams (within segment)
        for i in range(len(tokens) - 1):
            sc = _score_phrase(tokens[i:i+2])
            if sc > 0:
                record(" ".join(tokens[i:i+2]), sc)
        # 3-grams (within segment). First try the strict score (all 3 tokens
        # noun-like — yields score 8). If that fails, allow a relaxed form with
        # 'of' in the middle (e.g. "loss of function") at score 6.
        for i in range(len(tokens) - 2):
            a, b, c = tokens[i:i+3]
            sc = _score_phrase([a, b, c])
            if sc <= 0:
                if (a not in _BIO_STOP and c not in _BIO_STOP
                        and not _looks_verb_or_adv(a) and not _looks_verb_or_adv(c)
                        and len(a) >= 4 and len(c) >= 4 and b == "of"):
                    sc = 6
                else:
                    continue
            record(" ".join((a, b, c)), sc)

    # Frequency bonus: phrases that recur are more canonical than one-off
    # sliding-window variants. "frozen elephant trunk" appears 2x in Aakash
    # Shah's titles vs "anastomosis frozen elephant" 1x — this bonus surfaces
    # the canonical phrase ahead of its sliding-window neighbours.
    for ph in list(scored):
        if freq[ph] > 1:
            scored[ph] += min(freq[ph] - 1, 3)   # +1/+2/+3 for 2/3/4+ occurrences

    # Drop single-word keywords already covered by a kept multi-word keyword.
    multi_tokens = {tok for p in scored if " " in p for tok in p.split()}
    for tok in list(scored):
        if " " not in tok and tok in multi_tokens:
            del scored[tok]

    # Rank by score; on ties prefer LONGER (more specific) phrases.
    ranked = sorted(scored.items(), key=lambda x: (-x[1], -len(x[0]), x[0]))
    return [p for p, _ in ranked[:max_phrases]]


def _extract_list_items(tag) -> list:
    """Extract items from <ul>/<ol> list or comma/semicolon-separated text."""
    # Structured list
    items = tag.find_all("li")
    if items:
        return [it.get_text(" ", strip=True) for it in items
                if 3 < len(it.get_text(strip=True)) < 120]
    # Inline comma/semicolon list
    text = tag.get_text(" ", strip=True)
    parts = [p.strip() for p in re.split(r"[,;•|]", text) if 3 < len(p.strip()) < 120]
    return parts if len(parts) >= 2 else []


def scrape_individual_profile(session: requests.Session, faculty: dict) -> dict:
    """
    Visit the faculty member's individual UMSOM profile and extract Research
    Interests content, merging it with any keywords already on the record.
    Runs on ALL faculty (not just those missing keywords).
    """
    url = faculty.get("profile_url") or faculty.get("url", "")
    if not url or "/profiles/" not in url or url.endswith("---all-faculty/"):
        return faculty

    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        return faculty

    html = resp.text

    # ── Redesigned-site extraction (2026) ────────────────────────────────────
    # New UMSOM profiles mark sections with HTML comments + empty <span id>
    # anchors, e.g.:
    #   <!-- Research and/or Clinical Keywords -->  ...heading + comma list...  <!-- Highlighted Publications -->
    #   <!-- Research Interests Details --> <span id="Research Interest Details"></span> ...prose... <!-- Clinical Speciality Details -->
    # We slice the content between a section's start comment and the next comment,
    # strip tags, and remove the visible heading label. We deliberately read ONLY
    # these two curated fields — no whole-bio fallback (which scraped everything).
    def _section_text(start_marker):
        i = html.find(start_marker)
        if i < 0:
            return ""
        rest = html[i + len(start_marker):]
        j = rest.find("<!--")            # next section comment bounds this one
        chunk = rest[:j] if j >= 0 else rest[:4000]
        text = BeautifulSoup(chunk, "html.parser").get_text(" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()

    def _strip_heading(text):
        text = re.sub(r"^\s*research\s*/?\s*(?:and/or\s*)?clinical\s*keywords\s*:?\s*", "", text, flags=re.I)
        text = re.sub(r"^\s*research\s+interests?\s*:?\s*", "", text, flags=re.I)
        text = re.sub(r"^\s*keywords?\s*:?\s*", "", text, flags=re.I)
        return text.strip()

    # New dept listing pages carry an empty mailto, so capture the email here
    # from the profile page if we don't already have one.
    if not faculty.get("email"):
        em = re.search(r"[\w.+-]+@[\w.-]*(?:umaryland|umm)\.edu", html)
        if em:
            faculty["email"] = em.group(0)

    kw_keywords = []   # faculty-curated "Research/Clinical Keywords" (primary)
    ri_keywords = []   # phrases mined from the "Research Interests" prose

    kw_text = _strip_heading(_section_text("<!-- Research and/or Clinical Keywords -->"))
    if kw_text:
        # When the faculty member wrote a clean comma list, the comma split gives
        # us their exact curated terms — the most trustworthy source. When they
        # wrote prose instead ("I diagnose and treat patients with blood disorders
        # including acute myeloid leukemia (AML), …"), the comma split leaks the
        # sentence preamble as a giant first "keyword". Detect prose by either a
        # very long segment (>60 chars) or sentence-style punctuation, and fall
        # back to the same phrase extractor we use on the Research Interests prose.
        parts = [k.strip() for k in re.split(r"[,;]", kw_text) if k.strip()]
        looks_prose = (
            any(len(p) > 60 for p in parts)
            or (len(parts) < 3 and len(kw_text) > 80)
            or bool(re.search(r"\b(i|we|my|our|the)\s+\w", kw_text, re.I))
        )
        if looks_prose:
            kw_keywords = _extract_phrases_from_text(kw_text, max_phrases=30)
        else:
            kw_keywords = [p.lower() for p in parts if 2 < len(p) < 80]

    ri_text = _strip_heading(_section_text("<!-- Research Interests Details -->"))
    if ri_text and len(ri_text) > 20:
        # Bounded phrase extraction — never dump the whole narrative as keywords
        ri_keywords = _extract_phrases_from_text(ri_text, max_phrases=20)

    merged = []
    seen   = set()
    for kw_list, source in [
        (kw_keywords, "umsom_keywords"),
        (ri_keywords, "umsom_research_interests"),
    ]:
        new_kws = []
        for kw in kw_list:
            kw_clean = kw.strip().lower()
            if kw_clean and len(kw_clean) >= 3 and kw_clean not in seen:
                seen.add(kw_clean)
                new_kws.append(kw_clean)
        if new_kws:
            _merge_keywords(faculty, new_kws, source)
            merged.extend(new_kws)

    if merged:
        logger.debug(
            f"  Pass 2: {faculty.get('name','?')} → +{len(merged)} keywords "
            f"(keywords:{len(kw_keywords)} research_interests:{len(ri_keywords)})"
        )

    return faculty

# ── Pass 3: PubMed MeSH terms ─────────────────────────────────────────────────

PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_AFFIL   = "University of Maryland School of Medicine"


def _pubmed_name_query(name: str) -> str:
    """Convert 'John A. Smith, MD' → 'Smith J[Author] AND "University of Maryland"[Affiliation]'"""
    # Strip credentials
    clean = re.sub(r",\s*(MD|PhD|DO|DrPH|DPT|MPH|MS|DSc|DDS|DMD|MBBS|MBChB|MGC|MHS|Dpharm).*$", "", name).strip()
    parts = clean.split()
    if len(parts) < 2:
        return ""
    last = parts[-1]
    first_initial = parts[0][0]
    return f'{last} {first_initial}[Author] AND "University of Maryland"[Affiliation]'


def enrich_from_pubmed(session: requests.Session, faculty: dict) -> dict:
    """Query PubMed for recent publications and extract MeSH terms as keywords."""
    query = _pubmed_name_query(faculty.get("name", ""))
    if not query:
        return faculty

    try:
        # Search for up to 10 recent papers
        r = session.get(PUBMED_ESEARCH, params={
            "db": "pubmed", "term": query,
            "retmax": 10, "sort": "date",
            "retmode": "json", "datetype": "pdat",
            "reldate": 1825,  # last 5 years
        }, headers=PUBMED_HEADERS, timeout=15)
        r.raise_for_status()
        ids = r.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return faculty

        # Fetch records in XML to get MeSH terms
        r2 = session.get(PUBMED_EFETCH, params={
            "db": "pubmed", "id": ",".join(ids),
            "rettype": "xml", "retmode": "xml",
        }, headers=PUBMED_HEADERS, timeout=20)
        r2.raise_for_status()

        soup = BeautifulSoup(r2.text, "xml")
        mesh_terms = set()
        kw_terms = set()

        # Capture article TITLES for the semantic embedding (fix #4). High-signal,
        # concise text that grounds the faculty vector in their actual research.
        pub_titles = [t.get_text().strip()
                      for t in soup.find_all("ArticleTitle") if t.get_text().strip()]
        if pub_titles:
            _merge_evidence_text(faculty, pub_titles, f"pubmed({len(ids)}papers)")

        for descriptor in soup.find_all("DescriptorName"):
            term = descriptor.get_text().strip()
            if term and len(term) > 3:
                mesh_terms.add(term.lower())

        for kw in soup.find_all("Keyword"):
            term = kw.get_text().strip()
            if term and len(term) > 3:
                kw_terms.add(term.lower())

        # MeSH terms first (more controlled), then author keywords
        combined = list(mesh_terms) + [k for k in kw_terms if k not in mesh_terms]
        # Filter out generic stopwords
        stop = {"humans", "male", "female", "adult", "aged", "animals", "mice",
                "rats", "child", "adolescent", "middle aged", "young adult",
                "united states", "retrospective studies", "prospective studies",
                "treatment outcome", "time factors", "follow-up studies"}
        keywords = [k for k in combined if k not in stop][:60]

        if keywords:
            _merge_keywords(faculty, keywords, f"pubmed({len(ids)}papers,{len(mesh_terms)}MeSH)")
            logger.debug(f"  PubMed: {faculty['name']} → +{len(keywords)} keywords from {len(ids)} papers")

    except Exception as e:
        logger.debug(f"  PubMed lookup failed for {faculty.get('name')}: {e}")

    return faculty


# ── Pass 4: NIH RePORTER active grants ────────────────────────────────────────

NIH_REPORTER_URL = "https://api.reporter.nih.gov/v2/projects/search"


def enrich_from_nih_reporter(session: requests.Session, faculty: dict) -> dict:
    """Query NIH RePORTER for active grants and extract keywords from abstracts."""
    name = faculty.get("name", "")
    clean = re.sub(r",\s*(MD|PhD|DO|DrPH|DPT|MPH|MS|DSc|DDS|DMD|MBBS|MBChB|MGC|MHS|Dpharm).*$", "", name).strip()
    parts = clean.split()
    if len(parts) < 2:
        return faculty

    last_name = parts[-1]
    first_name = parts[0]

    try:
        payload = {
            "criteria": {
                "pi_names": [{"last_name": last_name, "first_name": first_name}],
                "org_names": ["UNIVERSITY OF MARYLAND BALTIMORE"],
                "project_nums": [],
                "activity_codes": [],
                "is_active": True,
            },
            "offset": 0,
            "limit": 5,
            "fields": ["ProjectTitle", "AbstractText", "Terms", "ProjectNum",
                       "FiscalYear", "PiNames", "OrgName"]
        }
        r = session.post(NIH_REPORTER_URL, json=payload, timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])

        if not results:
            return faculty

        all_terms = []

        # Capture grant PROJECT TITLES for the semantic embedding (fix #4).
        proj_titles = [p.get("ProjectTitle", "").strip()
                       for p in results if p.get("ProjectTitle", "").strip()]
        if proj_titles:
            _merge_evidence_text(faculty, proj_titles, f"nih_reporter({len(results)}grants)")

        for project in results:
            # Use the Terms field first (pre-extracted keywords)
            terms_raw = project.get("Terms", "") or ""
            if terms_raw:
                # Terms are pipe-separated or semicolon-separated
                terms = [t.strip().lower() for t in re.split(r"[|;]", terms_raw)
                         if t.strip() and len(t.strip()) > 3]
                all_terms.extend(terms)

            # Mine the grant abstract for noun phrases using the shared
            # phrase-aware extractor — segments on punctuation and rejects
            # verbs/stops, so multi-word clinical terms ("extracorporeal
            # membrane oxygenation", "cardiogenic shock", "type a dissection")
            # survive instead of being split into single words.
            abstract = project.get("AbstractText", "") or ""
            if abstract and len(all_terms) < 20:
                all_terms.extend(_extract_phrases_from_text(abstract, max_phrases=20))

        # Deduplicate preserving order
        seen = set()
        keywords = []
        for t in all_terms:
            if t not in seen:
                seen.add(t)
                keywords.append(t)
        keywords = keywords[:60]

        if keywords:
            _merge_keywords(faculty, keywords, f"nih_reporter({len(results)}grants)")
            logger.debug(f"  NIH RePORTER: {faculty['name']} → +{len(keywords)} keywords from {len(results)} grants")

    except Exception as e:
        logger.debug(f"  NIH RePORTER lookup failed for {faculty.get('name')}: {e}")

    return faculty



# ── Pass 8: ClinicalTrials.gov ────────────────────────────────────────────────

CT_SEARCH_URL = "https://clinicaltrials.gov/api/v2/studies"

def enrich_from_clinicaltrials(session: requests.Session, faculty: dict) -> dict:
    """
    Search ClinicalTrials.gov for ACTIVE trials where this faculty member is
    listed as PI or investigator at University of Maryland.
    Only active/recruiting/enrolling trials are used — this ensures we only
    match current faculty with live research programs.
    Extracts condition names and intervention names as keywords.
    """
    clean_name = _strip_credentials(faculty.get("name", ""))
    parts = clean_name.split()
    if len(parts) < 2:
        return faculty

    last_name = parts[-1]
    first_initial = parts[0][0]

    try:
        # Query for active trials with this investigator at UMaryland
        params = {
            "query.term": f"{last_name} {first_initial} University Maryland",
            "filter.overallStatus": "RECRUITING|ACTIVE_NOT_RECRUITING|ENROLLING_BY_INVITATION|NOT_YET_RECRUITING",
            "fields": "NCTId,BriefTitle,Condition,InterventionName,InterventionType,LeadSponsorName,OverallOfficialName,OverallOfficialAffiliation,ResponsiblePartyInvestigatorFullName,ResponsiblePartyInvestigatorAffiliation",
            "pageSize": 10,
            "format": "json",
        }
        r = session.get(CT_SEARCH_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        studies = data.get("studies", [])

        if not studies:
            return faculty

        keywords = []
        matched_studies = 0

        for study in studies:
            proto = study.get("protocolSection", {})
            id_mod = proto.get("identificationModule", {})
            contacts = proto.get("contactsLocationsModule", {})
            sponsor = proto.get("sponsorCollaboratorsModule", {})
            conditions = proto.get("conditionsModule", {})
            interventions = proto.get("armsInterventionsModule", {})
            responsible = proto.get("sponsorCollaboratorsModule", {}).get("responsibleParty", {})

            # Identity verification: check investigator name + UMaryland affiliation
            investigator_names = []
            investigator_affils = []

            # Check overall officials
            for official in contacts.get("overallOfficials", []):
                investigator_names.append(official.get("name", "").lower())
                investigator_affils.append(official.get("affiliation", "").lower())

            # Check responsible party
            rp_name = responsible.get("investigatorFullName", "").lower()
            rp_affil = responsible.get("investigatorAffiliation", "").lower()
            if rp_name:
                investigator_names.append(rp_name)
            if rp_affil:
                investigator_affils.append(rp_affil)

            # Name match: last name must appear in at least one investigator name
            name_matched = any(last_name.lower() in n for n in investigator_names)
            # Affiliation match: must mention Maryland
            affil_matched = any("maryland" in a for a in investigator_affils)

            if not name_matched or not affil_matched:
                continue

            matched_studies += 1

            # Extract conditions (disease areas)
            for cond in conditions.get("conditions", []):
                if cond and len(cond) > 3:
                    keywords.append(cond.lower())

            # Extract intervention names (drugs, devices, procedures)
            for intervention in interventions.get("interventions", []):
                iname = intervention.get("name", "")
                itype = intervention.get("type", "")
                if iname and len(iname) > 3 and itype not in ("OTHER", ""):
                    keywords.append(iname.lower())

        if keywords and matched_studies > 0:
            # Deduplicate
            seen = set()
            unique_kw = []
            for k in keywords:
                if k not in seen:
                    seen.add(k)
                    unique_kw.append(k)
            _merge_keywords(faculty, unique_kw[:40], f"clinicaltrials({matched_studies}trials)")
            logger.debug(f"  ClinicalTrials: {clean_name} → +{len(unique_kw)} keywords from {matched_studies} active trials")

    except Exception as e:
        logger.debug(f"  ClinicalTrials lookup failed for {faculty.get('name')}: {e}")

    return faculty


# ── Pass 9: Europe PMC ────────────────────────────────────────────────────────

EPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_HEADERS = {
    "User-Agent": "UMSOMGrantMatcher/1.0 (mailto:grants@yourinstitution.edu)",
    "Accept": "application/json",
}

def enrich_from_europe_pmc(session: requests.Session, faculty: dict) -> dict:
    """
    Query Europe PMC for recent publications by this faculty member.
    Europe PMC indexes PubMed + preprints + European journals — catches
    publications that PubMed may miss for international collaborators.
    Extracts MeSH terms and author keywords.

    Identity strategy (in order of precision):
      1. If enrich_from_orcid already ran and stored faculty["orcid_id"], query
         Europe PMC by AUTHORID:<orcid> — uniquely identifies this person and
         eliminates the "multiple Aakash Shahs at Maryland" conflation issue.
      2. Otherwise fall back to name + 'University of Maryland' affiliation
         (loose; risks merging same-name researchers at any UM-* campus).
    """
    clean_name = _strip_credentials(faculty.get("name", ""))
    parts = clean_name.split()
    if len(parts) < 2:
        return faculty

    last_name = parts[-1]
    first_name = parts[0]
    first_initial = first_name[0]

    orcid_id = faculty.get("orcid_id")
    try:
        # Search with affiliation filter
        if orcid_id:
            # Precise: only papers authored by this exact ORCID
            query = (
                f'AUTHORID:"{orcid_id}" '
                f'FIRST_PDATE:[2020-01-01 TO 2099-12-31]'
            )
        else:
            query = (
                f'AUTH:"{last_name} {first_initial}" '
                f'AFFILIATION:"University of Maryland" '
                f'FIRST_PDATE:[2020-01-01 TO 2099-12-31]'
            )
        r = session.get(
            EPMC_SEARCH_URL,
            params={
                "query": query,
                "resultType": "core",
                "pageSize": 10,
                "format": "json",
                "sort": "P_PDATE_D desc",
            },
            headers=EPMC_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("resultList", {}).get("result", [])

        if not results:
            return faculty

        if orcid_id:
            # ORCID query is already uniquely disambiguated — every paper here
            # is genuinely authored by THIS faculty member, including collaborative
            # papers where the Maryland affiliation may sit on a co-author.
            verified_results = list(results)
        else:
            # Name-based fallback: verify at least one author block matches
            # last-name AND has 'maryland' in its affiliation.
            verified_results = []
            for paper in results:
                author_list = paper.get("authorList", {}).get("author", [])
                for author in author_list:
                    aff = (author.get("affiliation") or "").lower()
                    auth_name = (author.get("lastName") or "").lower()
                    if last_name.lower() in auth_name and "maryland" in aff:
                        verified_results.append(paper)
                        break
            if not verified_results:
                for paper in results:
                    aff_str = (paper.get("affiliation") or "").lower()
                    if "maryland" in aff_str:
                        verified_results.append(paper)

        if not verified_results:
            return faculty

        mesh_terms = set()
        author_keywords = set()

        for paper in verified_results:
            # MeSH terms
            mesh_list = paper.get("meshHeadingList", {}).get("meshHeading", [])
            for mesh in mesh_list:
                desc = mesh.get("descriptorName", "")
                if desc and len(desc) > 3:
                    mesh_terms.add(desc.lower())

            # Author-provided keywords
            kw_list = paper.get("keywordList", {}).get("keyword", [])
            for kw in kw_list:
                if kw and len(kw) > 3:
                    author_keywords.add(kw.lower())

        # Filter generic MeSH stopwords. These are auto-assigned by indexers
        # to almost every clinical paper and convey no research-area signal.
        # Expanded from the original short list after seeing demographic and
        # study-design terms dominate Aakash Shah's Europe PMC results.
        mesh_stop = {
            # Demographics
            "humans", "male", "female", "adult", "aged", "aged, 80 and over",
            "adolescent", "child", "child, preschool", "preschool", "infant",
            "infant, newborn", "newborn", "middle aged", "young adult",
            "animals", "mice", "rats", "rats, wistar", "rats, sprague-dawley",
            # Geography
            "united states", "europe",
            # Study designs / outcomes / methods (carry no research-area signal)
            "retrospective studies", "prospective studies", "cohort studies",
            "case-control studies", "cross-sectional studies", "longitudinal studies",
            "follow-up studies", "treatment outcome", "time factors",
            "risk factors", "incidence", "prevalence", "survival rate",
            "survival analysis", "quality of life", "patient outcome assessment",
            "outcome assessment, health care", "comparative effectiveness research",
            "computer simulation", "models, statistical",
            # Pregnancy/clinical demographics that show up everywhere
            "pregnancy", "patient discharge",
        }
        clean_mesh = [t for t in mesh_terms if t not in mesh_stop]
        all_keywords = clean_mesh + [k for k in author_keywords if k not in mesh_terms]

        if all_keywords:
            _merge_keywords(faculty, all_keywords[:60], f"europepmc({len(verified_results)}papers)")
            logger.debug(
                f"  EuropePMC: {clean_name} → +{len(all_keywords)} keywords "
                f"from {len(verified_results)} verified papers"
            )

    except Exception as e:
        logger.debug(f"  Europe PMC lookup failed for {faculty.get('name')}: {e}")

    return faculty


# ── Cache helpers ─────────────────────────────────────────────────────────────

def deduplicate_faculty(faculty_list: list[dict]) -> list[dict]:
    seen_emails = set()
    seen_names = set()
    unique = []
    for f in faculty_list:
        key_email = f.get("email", "").lower().strip()
        key_name = f.get("name", "").lower().strip()
        if key_email and key_email in seen_emails:
            continue
        if not key_email and key_name and key_name in seen_names:
            continue
        if key_email:
            seen_emails.add(key_email)
        if key_name:
            seen_names.add(key_name)
        unique.append(f)
    return unique


def load_faculty_cache(cache_file: str) -> Optional[dict]:
    path = Path(cache_file)
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            # Validate structure: must be a dict with a "faculty" key
            if isinstance(data, dict) and "faculty" in data:
                return data
            # Handle corrupted cache: bare list or missing keys
            if isinstance(data, list):
                logger.warning(
                    f"Faculty cache is a bare list ({len(data)} items) instead of "
                    f"expected dict — treating as stale, will re-scrape."
                )
            else:
                logger.warning(
                    f"Faculty cache has unexpected structure "
                    f"(type={type(data).__name__}) — treating as stale, will re-scrape."
                )
            return None
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not read faculty cache: {e}")
    return None


def save_faculty_cache(cache_file: str, data: dict):
    # Atomic write: this is the largest state file in the app (1267+ profiles
    # with embeddings, multi-MB on an SMB mount) and therefore the longest
    # write window. A torn write here looks like a stale cache → full 3-6h
    # re-scrape and loss of all departed-faculty history.
    from atomic_io import atomic_write_json
    atomic_write_json(cache_file, data, indent=2)
    logger.info(f"Faculty cache saved: {len(data['faculty'])} profiles → {cache_file}")


# ── Title-based exclusion (from matching) ────────────────────────────────────

def _apply_title_exclusions(faculty_list: list[dict], patterns: list[str],
                            excluded_statuses=None, excluded_emp_types=None) -> list[dict]:
    """
    Mark faculty who should be excluded from the matching pool, in a single pass,
    and return only the faculty NOT excluded. A faculty is excluded if ANY of:
      • `title` matches one of the regex `patterns` (rank-based: emeritus, adjunct,
        clinical-track, …) → reason = "<pattern>"
      • `employment_status` is in `excluded_statuses` (the dept page's
        data-salary-desc: VOLUNTEER / PART TIME) → reason = "employment: <status>"
      • `emp_type` is in `excluded_emp_types` (data-emp-type: FELLOW = trainee)
        → reason = "emp_type: <type>"

    Marks are written back in place so they persist in the cache and show in the
    dashboard. Faculty missing `title`/`employment_status` (pre-rescrape cache)
    are never excluded by that signal — graceful degradation until the next fresh
    scrape populates the fields. Doing all checks in ONE pass keeps the stale-mark
    clearing correct (a faculty kept by every rule has its marks removed).
    """
    excluded_statuses  = {s.strip().upper() for s in (excluded_statuses or [])}
    excluded_emp_types = {t.strip().upper() for t in (excluded_emp_types or [])}
    compiled = [(p, re.compile(p, re.IGNORECASE)) for p in (patterns or [])]
    if not compiled and not excluded_statuses and not excluded_emp_types:
        return list(faculty_list)

    kept, excluded_by = [], Counter()
    for f in faculty_list:
        title  = (f.get("title") or "").strip()
        status = (f.get("employment_status") or "").strip().upper()
        etype  = (f.get("emp_type") or "").strip().upper()

        reason = next((p for p, rx in compiled if title and rx.search(title)), None)
        if reason is None and status and status in excluded_statuses:
            reason = f"employment: {status}"
        if reason is None and etype and etype in excluded_emp_types:
            reason = f"emp_type: {etype}"

        if reason is not None:
            f["excluded_from_matching"] = True
            f["excluded_reason"]        = reason
            excluded_by[reason] += 1
        else:
            # Clear stale marks (title updated, status changed, or rule removed)
            f.pop("excluded_from_matching", None)
            f.pop("excluded_reason", None)
            kept.append(f)
    if excluded_by:
        logger.info(
            f"  Matching-pool exclusion: dropped {sum(excluded_by.values())} faculty "
            f"by rule: {dict(excluded_by.most_common())}"
        )
    return kept


# ── Main entry point ──────────────────────────────────────────────────────────

def get_faculty_profiles(config: dict, force: bool = False) -> list[dict]:
    """Load faculty from cache, or re-scrape when the cache is stale.

    force=True skips the cache-AGE check (fresh scrape now) but still LOADS the
    cache: the roster-drop guard and the inactive-marking diff both need the
    previous roster to compare against. (The old FORCE_SCRAPE behavior deleted
    the cache file up-front, which silently disabled both protections and made
    prior enrichment unrecoverable if the scrape failed mid-way.)
    """
    cache_file = config["faculty"]["cache_file"]
    rescrape_hours = config["faculty"]["rescrape_interval_hours"]

    cache = load_faculty_cache(cache_file)
    if cache:
        last_scraped = datetime.fromisoformat(cache.get("scraped_at", "2000-01-01"))
        age_hours = (datetime.utcnow() - last_scraped).total_seconds() / 3600
        if force:
            logger.info(f"Force-scrape requested — ignoring cache age ({age_hours:.1f}h), re-scraping...")
        elif age_hours < rescrape_hours:
            logger.info(f"Using cached faculty data ({len(cache['faculty'])} profiles, {age_hours:.1f}h old)")
            # Return only active faculty — exclude anyone marked inactive
            active = [f for f in cache["faculty"] if not f.get("inactive")]
            if len(active) < len(cache["faculty"]):
                logger.info(f"  Excluded {len(cache['faculty']) - len(active)} inactive (departed) faculty")
            # Exclusion: rank/title + employment status (volunteer/part-time) + fellow
            return _apply_title_exclusions(
                active,
                config["faculty"].get("excluded_title_patterns", []),
                config["faculty"].get("excluded_employment_statuses", []),
                config["faculty"].get("excluded_emp_types", []),
            )
        else:
            logger.info(f"Faculty cache is {age_hours:.1f}h old, re-scraping...")

    session = requests.Session()
    all_faculty = []

    # Auto-discover the current department listing URLs from the live index so
    # the redesign's renamed slugs (e.g. biochemistry, urology) are handled and
    # we don't silently miss whole departments. Falls back to the built-in list.
    index_url = config["faculty"].get("profiles_url") or urljoin(BASE_URL, "/faculty/faculty-profiles/")
    dept_paths = discover_department_pages(session, index_url) or DEPARTMENT_PAGES
    total = len(dept_paths)

    # ── Pass 1: department listing pages ──────────────────────────────────────
    logger.info(f"Pass 1/9: Scraping {total} UMSOM department pages...")
    for i, path in enumerate(dept_paths, 1):
        url = urljoin(BASE_URL, path)
        logger.info(f"  Dept {i}/{total}: {url}")
        fac = scrape_department_page(session, url)
        all_faculty.extend(fac)
        time.sleep(0.5)

    all_faculty = deduplicate_faculty(all_faculty)
    with_kw = sum(1 for f in all_faculty if f.get("keywords"))
    logger.info(f"Pass 1 complete: {len(all_faculty)} unique faculty, {with_kw} with keywords")

    # ── Roster-drop safety guard ──────────────────────────────────────────────
    # A transient partial scrape (department pages timing out, a renamed slug that
    # discovery missed, the site briefly serving an error shell) can return far
    # fewer faculty than reality. The active-faculty check below marks EVERYONE
    # absent from this scrape inactive — cutting them off from grant alerts for up
    # to rescrape_interval_hours. So an implausibly small scrape must not be
    # trusted to overwrite a good roster: keep the cached roster, mark no one
    # inactive, and don't save (next run re-scrapes and retries). Compares raw
    # scraped counts (pre title-exclusion) so the intentional emeritus/adjunct/
    # volunteer filtering never trips this guard.
    if cache:
        prev_active = [f for f in cache.get("faculty", []) if not f.get("inactive")]
        max_drop = config["faculty"].get("max_roster_drop_pct", 0.15)
        if prev_active and len(all_faculty) < (1 - max_drop) * len(prev_active):
            logger.error(
                f"ROSTER GUARD TRIPPED: fresh scrape found only {len(all_faculty)} "
                f"faculty vs {len(prev_active)} active in cache "
                f"(drop > {max_drop:.0%}). Treating this as a partial scrape failure "
                f"— keeping the cached roster, marking no one inactive, not saving. "
                f"Next run will retry. Investigate department-page scraping/discovery."
            )
            return _apply_title_exclusions(
                prev_active,
                config["faculty"].get("excluded_title_patterns", []),
                config["faculty"].get("excluded_employment_statuses", []),
                config["faculty"].get("excluded_emp_types", []),
            )

    # ── Active faculty check: compare against previous cache ─────────────────
    # Anyone in the previous cache but NOT in this scrape is marked inactive.
    # This prevents departed faculty from receiving grant alerts.
    current_names = {f["name"].lower().strip() for f in all_faculty}
    current_emails = {f["email"].lower().strip() for f in all_faculty if f.get("email")}
    if cache:
        prev_faculty = cache.get("faculty", [])
        reactivated = 0
        departed = 0
        for prev in prev_faculty:
            prev_name = prev.get("name", "").lower().strip()
            prev_email = prev.get("email", "").lower().strip()
            in_current = (prev_name in current_names) or (prev_email and prev_email in current_emails)
            if not in_current:
                # Mark as inactive — preserve enrichment data but exclude from matching
                prev["inactive"] = True
                prev["inactive_since"] = datetime.utcnow().isoformat()
                all_faculty.append(prev)
                departed += 1
            else:
                reactivated += 1
        if departed:
            logger.info(f"Active faculty check: {departed} faculty marked inactive (not in current scrape), "
                        f"{len(current_names)} active")
    logger.info(f"Total profiles tracked: {len(all_faculty)} ({len(current_names)} active)")

    # Title-based exclusion (emeritus, adjunct, visiting, postdoc, research-associate).
    # Marks excluded faculty in place with excluded_from_matching=True so the marks
    # persist via the cache save and so the dashboard can show their status;
    # subsequent enrichment passes skip them (saves thousands of API calls).
    _apply_title_exclusions(
        all_faculty,
        config["faculty"].get("excluded_title_patterns", []),
        config["faculty"].get("excluded_employment_statuses", []),
        config["faculty"].get("excluded_emp_types", []),
    )

    # ── Pass 2: individual UMSOM profile pages (Research Interests extraction) ──
    # Runs on ALL active, non-excluded faculty — not just those missing keywords.
    # For faculty who already have keywords from Pass 1, the Research Interests
    # section is MERGED in as additional high-quality keywords.
    # For faculty with no keywords at all, this is their first enrichment opportunity.
    active_with_url = [f for f in all_faculty
                       if not f.get("inactive")
                       and not f.get("excluded_from_matching")
                       and (f.get("profile_url") or f.get("url","").startswith("http"))]
    logger.info(f"Pass 2/9: Visiting {len(active_with_url)} individual UMSOM profiles "
                f"(Research Interests extraction)...")
    for i, fac in enumerate(active_with_url, 1):
        if i % 100 == 0:
            logger.info(f"  Profile scrape: {i}/{len(active_with_url)}")
        scrape_individual_profile(session, fac)
        time.sleep(0.3)

    with_kw = sum(1 for f in all_faculty if not f.get("inactive") and f.get("keywords"))
    still_missing = sum(1 for f in all_faculty if not f.get("inactive") and not f.get("keywords"))
    ri_sourced = sum(1 for f in all_faculty
                     if not f.get("inactive")
                     and "umsom_research_interests" in f.get("keyword_source", ""))
    logger.info(f"Pass 2 complete: {with_kw} with keywords, {still_missing} still missing, "
                f"{ri_sourced} enriched from Research Interests section")

    # ── Pass 3: PubMed enrichment (ALL active, non-excluded faculty) ──────────
    active_faculty = [f for f in all_faculty
                      if not f.get("inactive") and not f.get("excluded_from_matching")]
    logger.info(f"Pass 3/9: PubMed enrichment for all {len(active_faculty)} active faculty...")
    for i, fac in enumerate(active_faculty, 1):
        if i % 100 == 0:
            logger.info(f"  PubMed progress: {i}/{len(active_faculty)}")
        enrich_from_pubmed(session, fac)
        time.sleep(0.4)  # NCBI rate limit: max 3 req/sec without API key

    with_kw = sum(1 for f in active_faculty if f.get("keywords"))
    logger.info(f"Pass 3 complete: {with_kw}/{len(active_faculty)} active faculty now have keywords")

    # ── Pass 4: NIH RePORTER enrichment (ALL active faculty) ─────────────────
    logger.info(f"Pass 4/9: NIH RePORTER keyword enrichment for all {len(active_faculty)} active faculty...")
    for i, fac in enumerate(active_faculty, 1):
        if i % 50 == 0:
            logger.info(f"  NIH RePORTER progress: {i}/{len(active_faculty)}")
        enrich_from_nih_reporter(session, fac)
        time.sleep(0.5)

    with_kw = sum(1 for f in active_faculty if f.get("keywords"))
    logger.info(f"Pass 4 complete: {with_kw}/{len(active_faculty)} active faculty now have keywords")

    # ── Pass 5: ORCID enrichment (ALL active faculty) ─────────────────────────
    logger.info(f"Pass 5/9: ORCID enrichment for all {len(active_faculty)} active faculty...")
    for i, fac in enumerate(active_faculty, 1):
        if i % 50 == 0:
            logger.info(f"  ORCID progress: {i}/{len(active_faculty)}")
        enrich_from_orcid(session, fac)
        time.sleep(0.5)

    with_kw = sum(1 for f in active_faculty if f.get("keywords"))
    logger.info(f"Pass 5 complete: {with_kw}/{len(active_faculty)} active faculty now have keywords")

    # ── Pass 6: Semantic Scholar enrichment (ALL active faculty) ──────────────
    logger.info(f"Pass 6/9: Semantic Scholar enrichment for all {len(active_faculty)} active faculty...")
    for i, fac in enumerate(active_faculty, 1):
        if i % 50 == 0:
            logger.info(f"  Semantic Scholar progress: {i}/{len(active_faculty)}")
        enrich_from_semantic_scholar(session, fac)
        time.sleep(1.0)  # S2 free tier: 1 req/sec

    with_kw = sum(1 for f in active_faculty if f.get("keywords"))
    logger.info(f"Pass 6 complete: {with_kw}/{len(active_faculty)} active faculty now have keywords")

    # ── Pass 7: ClinicalTrials.gov (ALL active faculty) ───────────────────────
    logger.info(f"Pass 7/9: ClinicalTrials.gov enrichment for all {len(active_faculty)} active faculty...")
    for i, fac in enumerate(active_faculty, 1):
        if i % 50 == 0:
            logger.info(f"  ClinicalTrials progress: {i}/{len(active_faculty)}")
        enrich_from_clinicaltrials(session, fac)
        time.sleep(0.4)

    with_kw = sum(1 for f in active_faculty if f.get("keywords"))
    logger.info(f"Pass 7 complete: {with_kw}/{len(active_faculty)} active faculty now have keywords")

    # ── Pass 8: Europe PMC (ALL active faculty) ───────────────────────────────
    logger.info(f"Pass 8/9: Europe PMC enrichment for all {len(active_faculty)} active faculty...")
    for i, fac in enumerate(active_faculty, 1):
        if i % 100 == 0:
            logger.info(f"  Europe PMC progress: {i}/{len(active_faculty)}")
        enrich_from_europe_pmc(session, fac)
        time.sleep(0.5)

    with_kw = sum(1 for f in active_faculty if f.get("keywords"))
    logger.info(f"Pass 8 complete: {with_kw}/{len(active_faculty)} active faculty now have keywords")

    # ── Pass 8b: Faculty self-reported keywords (from Eval App campaign) ──────
    # Reads data/eval_app_keywords.json (accumulated from spreadsheets dropped
    # into the campaign folder) and merges keywords onto matching faculty by
    # email. Runs before Pass 9 so the new keywords are included in the text
    # used for embedding generation.
    try:
        from eval_app_keywords import get_keywords_by_email, resolve_emails_by_name, normalize_person_name

        # ── Email backfill (2026-09-04) ──────────────────────────────────────
        # Some UMSOM profile pages publish no email at all — confirmed by
        # fetching them: no mailto, no obfuscation, no alternate domain, so the
        # Pass 2 regex has nothing to find. A blank email means the person can
        # never receive a personalised digest (that fan-out indexes by email)
        # and their 👍/👎 verdicts cannot be attributed. The Eval App export DOES
        # carry an address, so fill it in from there — by unique normalised-name
        # match only. Ambiguous names are logged and skipped rather than guessed:
        # attaching a verdict to the wrong person is worse than a blank.
        try:
            name_to_email = resolve_emails_by_name()
        except Exception as e:
            name_to_email = {}
            logger.warning(f"Pass 8b: email backfill index unavailable: {e}")
        if name_to_email:
            filled = 0
            for fac in active_faculty:
                if (fac.get("email") or "").strip():
                    continue
                key = normalize_person_name(fac.get("name", ""))
                em = name_to_email.get(key)
                if em:
                    fac["email"] = em
                    fac["email_source"] = "eval_app_backfill"
                    filled += 1
            still_missing = sum(1 for f in active_faculty if not (f.get("email") or "").strip())
            logger.info(
                f"Pass 8b: backfilled {filled} email(s) from the Eval App store "
                f"({still_missing} active faculty still have none)"
            )

        self_reported = get_keywords_by_email()
        if self_reported:
            merged_count = 0
            new_kw_count = 0
            for fac in active_faculty:
                email = (fac.get("email") or "").strip().lower()
                if not email:
                    continue
                kws = self_reported.get(email)
                if not kws:
                    continue
                before = len(fac.get("keywords") or [])
                # prepend: these are the most recent first-party statement of
                # what this person researches, and must survive the keywords[:40]
                # truncation in the embedder — see _merge_keywords.
                _merge_keywords(fac, kws, "faculty_self_reported", prepend=True)
                after = len(fac.get("keywords") or [])
                merged_count += 1
                new_kw_count += (after - before)
            logger.info(
                f"Pass 8b complete: merged self-reported keywords for "
                f"{merged_count} faculty (+{new_kw_count} new keywords; "
                f"{len(self_reported)} entries in store)"
            )
        else:
            logger.info("Pass 8b: no eval_app_keywords store found (skipping)")
    except Exception as e:
        logger.warning(f"Pass 8b (faculty self-reported keywords) failed: {e}")

    # ── Pass 9: Generate semantic embeddings ──────────────────────────────────
    if embed_faculty_batch and embeddings_available():
        logger.info(f"Pass 9/9: Generating semantic embeddings for {len(active_faculty)} active faculty...")
        success = embed_faculty_batch(active_faculty)
        if success:
            with_emb = sum(1 for f in active_faculty if f.get("embedding"))
            logger.info(f"Pass 9 complete: {with_emb}/{len(active_faculty)} faculty have embeddings")
        else:
            logger.warning("Pass 9: Embedding generation failed — semantic matching will be unavailable this cycle")
    else:
        logger.info("Pass 9/9: Skipping embeddings (sentence-transformers not available)")

    # ── Final summary ─────────────────────────────────────────────────────────
    with_kw = sum(1 for f in active_faculty if f.get("keywords"))
    still_missing = sum(1 for f in active_faculty if not f.get("keywords"))
    inactive_count = sum(1 for f in all_faculty if f.get("inactive"))

    sources = {}
    for f in active_faculty:
        for src in (f.get("keyword_sources") or [f.get("keyword_source", "none") or "none"]):
            src_key = src.split("(")[0].strip()
            sources[src_key] = sources.get(src_key, 0) + 1

    logger.info(f"Enrichment complete: {len(active_faculty)} active faculty, {with_kw} with keywords "
                f"({still_missing} still none), {inactive_count} inactive/departed")
    for src, count in sorted(sources.items(), key=lambda x: -x[1]):
        logger.info(f"  {src}: {count} faculty")

    if record_scrape_stats:
        record_scrape_stats(
            faculty_count=len(active_faculty),
            with_keywords=with_kw,
            dept_pages=len(DEPARTMENT_PAGES),
            errors=0
        )

    cache_data = {
        "scraped_at": datetime.utcnow().isoformat(),
        "faculty": all_faculty  # store all including inactive (with inactive flag)
    }
    save_faculty_cache(cache_file, cache_data)
    return active_faculty  # only return active faculty for matching


# ── Pass 5: ORCID ─────────────────────────────────────────────────────────────

ORCID_SEARCH_URL = "https://pub.orcid.org/v3.0/search"
ORCID_RECORD_URL = "https://pub.orcid.org/v3.0/{orcid}/record"
ORCID_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "UMSOMGrantMatcher/1.0 (mailto:grants@yourinstitution.edu)"
}


def _strip_credentials(name: str) -> str:
    """Remove degree suffixes: 'Jane Smith, MD, PhD' → 'Jane Smith'"""
    return re.sub(
        r",?\s*(MD|PhD|DO|DrPH|DPT|MPH|MS|DSc|DDS|DMD|MBBS|MBChB|MGC|MHS|Dpharm|MBA|MPP|JD|RN|APRN|FACP|FACS|FACOG)[\s,].*$",
        "", name, flags=re.IGNORECASE
    ).strip()

# Normalised display labels for the per-keyword source attribution.
# Callers pass raw source identifiers like "pubmed(5papers,12MeSH)" or
# "umsom_keywords"; we strip the parenthetical detail and map to a clean name.
_SOURCE_BASE_LABEL = {
    "umsom_keywords":           "UMSOM (Keywords)",
    "umsom_research_interests": "UMSOM (Interests)",
    "pubmed":                   "PubMed",
    "nih_reporter":             "NIH RePORTER",
    "clinicaltrials":           "ClinicalTrials.gov",
    "europepmc":                "Europe PMC",
    "orcid":                    "ORCID",
    "s2":                       "Semantic Scholar",
    "faculty_self_reported":    "Faculty Self-Reported",
}


def _base_source_label(raw: str) -> str:
    """Map a raw `_merge_keywords` source string to its clean display label."""
    s = (raw or "").strip()
    if not s:
        return "Unknown"
    base = s.split("(", 1)[0].strip().lower()
    return _SOURCE_BASE_LABEL.get(base, s)


def _merge_keywords(faculty: dict, new_keywords: list[str], source: str,
                    prepend: bool = False) -> None:
    """
    Merge new_keywords into faculty["keywords"], deduplicating case-insensitively.
    Tracks both:
      - faculty["keyword_sources"]   : list of raw source identifiers (legacy)
      - faculty["keywords_by_source"]: dict[clean_label, list[keywords]] — the
        per-keyword attribution that the dashboard's Faculty modal and the
        global Keywords view consume to show "which sources contributed each
        keyword". Multiple sources can claim the same keyword (e.g. both UMSOM
        and PubMed surfacing "epilepsy") — each gets its own entry, so the
        dashboard can derive sources-per-keyword by inversion.
    UMSOM profile keywords always stay first (highest trust), external sources appended.

    `prepend=True` puts this source's NEW keywords at the FRONT of the flat list
    instead of the end. Position is not cosmetic: `embedder.faculty_to_text()`
    embeds only `keywords[:40]`, so anything past that cutoff never reaches the
    semantic vector at all. Faculty enriched from PubMed / Semantic Scholar /
    ClinicalTrials.gov / Europe PMC routinely carry far more than 40 keywords, and
    Pass 8b (faculty self-reported, from the Eval App campaign) runs LAST — so
    appending meant a faculty member's own explicit, dated statement of what they
    research was the first thing truncated out of their embedding, while
    machine-derived MeSH terms kept their place. Prepending inverts that to match
    the actual trust ordering (2026-09-04).

    Keywords already present keep their existing position — only genuinely new
    ones move to the front — so this never reshuffles an established list.
    """
    # Filter boilerplate up-front so per-source records stay clean.
    contributed = [k for k in (new_keywords or []) if not _is_boilerplate_kw(k)]

    # Flat keyword list — only genuinely new ones get appended.
    existing = faculty.get("keywords") or []
    existing_lower = {k.lower() for k in existing}
    added = [k for k in contributed if k.lower() not in existing_lower]
    faculty["keywords"] = (added + existing) if prepend else (existing + added)

    # Per-keyword source attribution. We dedupe WITHIN a source but keep
    # the keyword in every source that legitimately surfaced it.
    src_label = _base_source_label(source)
    kbs = faculty.get("keywords_by_source") or {}
    bucket = kbs.get(src_label) or []
    bucket_lower = {k.lower() for k in bucket}
    for k in contributed:
        if k.lower() not in bucket_lower:
            bucket.append(k)
            bucket_lower.add(k.lower())
    if bucket:
        kbs[src_label] = bucket
        faculty["keywords_by_source"] = kbs

    # Legacy source list (raw identifiers — preserved for back-compat with
    # the existing /api/faculty `source` filter and the cached profile schema).
    sources = faculty.get("keyword_sources") or []
    if source not in sources:
        sources.append(source)
    faculty["keyword_sources"] = sources
    faculty["keyword_source"] = ", ".join(sources)



def _merge_evidence_text(faculty: dict, titles: list[str], source: str,
                         max_titles: int = 25) -> None:
    """
    Accumulate publication / grant TITLES into faculty["evidence_titles"] for the
    semantic embedding (fix #4, 2026-06-26). Self-listed keyword phrases are too
    sparse to embed well — a researcher whose only keyword is "substance use in
    pregnancy" sits below the semantic threshold against a maternal-SUD grant.
    Folding the titles of their actual papers/grants into the embedding text lifts
    the similarity above threshold (measured: 0.40 → 0.47 for K. Mark on MMHSUD).
    TITLES only (not abstracts) — abstracts add length that dilutes the signal.
    Deduped case-insensitively, capped, attributed in evidence_titles_by_source for
    auditing. Does NOT touch faculty["keywords"] (those still drive keyword matching).
    """
    clean = [re.sub(r"\s+", " ", t).strip() for t in (titles or []) if t and t.strip()]
    if not clean:
        return
    existing = faculty.get("evidence_titles") or []
    existing_lower = {t.lower() for t in existing}
    for t in clean:
        if t.lower() not in existing_lower and len(existing) < max_titles:
            existing.append(t)
            existing_lower.add(t.lower())
    faculty["evidence_titles"] = existing

    by_src = faculty.get("evidence_titles_by_source") or {}
    label = _base_source_label(source)
    bucket = by_src.get(label) or []
    bucket_lower = {t.lower() for t in bucket}
    for t in clean:
        if t.lower() not in bucket_lower:
            bucket.append(t); bucket_lower.add(t.lower())
    by_src[label] = bucket
    faculty["evidence_titles_by_source"] = by_src


def enrich_from_orcid(session: requests.Session, faculty: dict) -> dict:
    """
    Search ORCID for the faculty member, verify UMaryland affiliation,
    and extract keywords/research topics from their profile.
    """
    clean_name = _strip_credentials(faculty.get("name", ""))
    parts = clean_name.split()
    if len(parts) < 2:
        return faculty

    # Search by name
    query = f'family-name:{parts[-1]} AND given-names:{parts[0]} AND affiliation-org-name:"Maryland"'
    try:
        r = session.get(
            ORCID_SEARCH_URL,
            params={"q": query, "rows": 3, "start": 0},
            headers=ORCID_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("result", [])
        if not results:
            return faculty

        # Take the first result and fetch their full record
        orcid_id = results[0].get("orcid-identifier", {}).get("path", "")
        if not orcid_id:
            return faculty

        rec_r = session.get(
            ORCID_RECORD_URL.format(orcid=orcid_id),
            headers=ORCID_HEADERS,
            timeout=15,
        )
        rec_r.raise_for_status()
        record = rec_r.json()

        # Verify affiliation contains University of Maryland
        affiliations = []
        for aff_type in ("employments", "educations"):
            section = (record.get("activities-summary", {})
                             .get(aff_type, {})
                             .get("affiliation-group", []))
            for grp in section:
                for summary in grp.get("summaries", []):
                    key = aff_type.rstrip("s") + "-summary"
                    org = (summary.get(key, {})
                                  .get("organization", {})
                                  .get("name", ""))
                    if org:
                        affiliations.append(org.lower())

        if not any("maryland" in a for a in affiliations):
            logger.debug(f"  ORCID: {clean_name} found but affiliation doesn't match Maryland")
            return faculty

        # Extract keywords from the profile
        keywords_section = (record.get("person", {})
                                  .get("keywords", {})
                                  .get("keyword", []))
        keywords = [k.get("content", "").strip().lower()
                    for k in keywords_section
                    if k.get("content", "").strip()]

        # Fallback: if the faculty member hasn't filled in their ORCID
        # self-keyword list, mine recent paper titles for noun phrases.
        # The previous implementation re.findall(r"[a-zA-Z]{4,}", titles) split
        # multi-word terms ("Frozen Elephant Trunk", "Extracorporeal Membrane
        # Oxygenation", "Vascular Closure Device") into single-word fragments.
        # _extract_phrases_from_text segments on punctuation and extracts noun
        # phrases — dramatically better signal for clinical authors.
        if not keywords:
            works = (record.get("activities-summary", {})
                           .get("works", {})
                           .get("group", []))
            titles = []
            for grp in works[:10]:
                for summary in grp.get("work-summary", [])[:1]:
                    t = (summary.get("title", {})
                                .get("title", {})
                                .get("value", ""))
                    if t:
                        titles.append(t)
            if titles:
                # Join with periods so the segmenter treats each title as its
                # own clause and n-grams don't bridge across titles.
                combined = ". ".join(titles) + "."
                keywords = _extract_phrases_from_text(combined, max_phrases=25)

        # Store the disambiguated ORCID identifier on the faculty record so
        # downstream enrichment passes (Europe PMC, etc.) can use it to
        # uniquely query for THIS faculty member's publications instead of
        # falling back to loose name+affiliation searches that conflate
        # different people with the same name at the same institution.
        if orcid_id:
            faculty["orcid_id"] = orcid_id

        if keywords:
            _merge_keywords(faculty, keywords, f"orcid({orcid_id})")
            logger.debug(f"  ORCID: {clean_name} → +{len(keywords)} keywords [{orcid_id}]")

    except Exception as e:
        logger.debug(f"  ORCID lookup failed for {faculty.get('name')}: {e}")

    return faculty


# ── Pass 6: Semantic Scholar ──────────────────────────────────────────────────

S2_SEARCH_URL  = "https://api.semanticscholar.org/graph/v1/author/search"
S2_AUTHOR_URL  = "https://api.semanticscholar.org/graph/v1/author/{author_id}"
S2_HEADERS = {
    "User-Agent": "UMSOMGrantMatcher/1.0 (mailto:grants@yourinstitution.edu)"
}
# If you have a free S2 API key set it here or via env var for higher rate limits
# S2_API_KEY = os.environ.get("S2_API_KEY", "")


def enrich_from_semantic_scholar(session: requests.Session, faculty: dict) -> dict:
    """
    Search Semantic Scholar for the faculty member and extract
    fields of study from their author profile and recent papers.
    """
    clean_name = _strip_credentials(faculty.get("name", ""))
    if not clean_name:
        return faculty

    try:
        r = session.get(
            S2_SEARCH_URL,
            params={
                "query": clean_name,
                "fields": "name,affiliations,paperCount,hIndex,papers.year,papers.fieldsOfStudy,papers.s2FieldsOfStudy",
                "limit": 3,
            },
            headers=S2_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        candidates = r.json().get("data", [])
        if not candidates:
            return faculty

        # Find best match: name similarity + Maryland affiliation
        best = None
        for candidate in candidates:
            cand_name = candidate.get("name", "").lower()
            affiliations = [a.get("name", "").lower() for a in candidate.get("affiliations", [])]
            name_match = (parts[-1].lower() in cand_name) if (parts := clean_name.split()) else False
            affil_match = any("maryland" in a for a in affiliations)
            if name_match and affil_match:
                best = candidate
                break
            if name_match and best is None:
                best = candidate  # keep as fallback even without affil match

        if not best:
            return faculty

        # Collect fields of study from their papers (last 5 years)
        from datetime import datetime as _dt
        current_year = _dt.utcnow().year
        fields_counter: dict[str, int] = {}

        for paper in best.get("papers", []):
            paper_year = paper.get("year") or 0
            if paper_year < current_year - 5:
                continue
            for fos in paper.get("s2FieldsOfStudy", []):
                cat = fos.get("category", "").strip().lower()
                if cat and cat not in ("", "unknown"):
                    fields_counter[cat] = fields_counter.get(cat, 0) + 1
            for fos in paper.get("fieldsOfStudy", []) or []:
                f = fos.strip().lower()
                if f:
                    fields_counter[f] = fields_counter.get(f, 0) + 1

        # Sort by frequency, take top terms
        keywords = [k for k, _ in sorted(fields_counter.items(), key=lambda x: -x[1])][:30]

        if keywords:
            author_id = best.get("authorId", "")
            _merge_keywords(faculty, keywords, f"s2({author_id})")
            logger.debug(f"  S2: {clean_name} → +{len(keywords)} fields from {best.get('paperCount',0)} papers")

    except Exception as e:
        logger.debug(f"  Semantic Scholar lookup failed for {faculty.get('name')}: {e}")

    return faculty
