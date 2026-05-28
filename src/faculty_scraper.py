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

import json
import logging
import re
import time
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
    return re.sub(r"\s+", " ", text).strip()


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

    # Structured faculty blocks, in document order (counts match: 1 name : 1 link).
    names = re.findall(r'class="data-profile-name"[^>]*>(.*?)</strong>', html, re.S)
    links = re.findall(r'class="data-profile-link"\s+href="([^"]+)"', html)

    faculty = []
    seen = set()
    for raw_name, href in zip(names, links):
        raw_name = clean_text(re.sub(r"<[^>]+>", " ", raw_name))
        name = _normalize_profile_name(raw_name)
        profile_url = urljoin(BASE_URL, href.strip())
        if not name or profile_url in seen:
            continue
        seen.add(profile_url)
        faculty.append({
            "name": name,
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
# these are grammatical/structural words, not domain terms)
_BIO_STOP = {
    "with", "that", "this", "from", "have", "been", "they", "their", "which",
    "will", "also", "more", "than", "when", "some", "into", "other", "were",
    "after", "about", "these", "both", "such", "very", "through", "between",
    "during", "multiple", "several", "currently", "previously", "including",
    "focused", "focuses", "working", "worked", "studies", "studying",
    "trained", "training", "received", "completed", "joined", "using",
    "university", "maryland", "school", "medicine", "department", "division",
    "section", "faculty", "professor", "assistant", "associate", "adjunct",
    "fellow", "instructor", "member", "staff", "board", "certified",
    "interested", "interests", "interest", "area", "areas", "field", "fields",
    "include", "includes", "included", "focus", "focuses", "focused",
    "research", "laboratory", "lab", "group", "team", "project", "projects",
    "program", "programs", "study", "studies", "approach", "approaches",
}

# Words that only add noise if they appear alone in a 1-gram keyword
_SINGLE_NOISE = {
    "also", "thus", "role", "such", "lead", "leads", "novel", "known",
    "using", "used", "well", "with", "both", "than", "when", "where",
    "which", "have", "been", "they", "their", "from", "that", "this",
}


def _extract_phrases_from_text(text: str, max_phrases: int = 40) -> list:
    """
    Extract meaningful 1-3 word phrases from prose text.
    Uses a sliding window over cleaned tokens to build bigrams and trigrams,
    preserving multi-word domain terms like "blood-brain barrier",
    "tau pathology", "cardiac stem cells".
    """
    # Normalise: keep letters, digits, hyphens
    cleaned = re.sub(r"[^a-zA-Z0-9\- ]", " ", text)
    tokens  = [t.lower() for t in cleaned.split() if len(t) >= 3]

    seen    = set()
    phrases = []

    # 1-grams: meaningful single domain words
    for t in tokens:
        if (t not in _BIO_STOP and t not in _SINGLE_NOISE
                and len(t) >= 5 and t not in seen):
            seen.add(t)
            phrases.append(t)

    # 2-grams: both tokens meaningful
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        phrase = f"{a} {b}"
        if (a not in _BIO_STOP and b not in _BIO_STOP
                and len(a) >= 4 and len(b) >= 4
                and phrase not in seen):
            seen.add(phrase)
            phrases.append(phrase)

    # 3-grams: at least 2 of 3 tokens meaningful
    for i in range(len(tokens) - 2):
        a, b, c = tokens[i], tokens[i + 1], tokens[i + 2]
        non_stop = sum(1 for t in (a, b, c) if t not in _BIO_STOP and len(t) >= 4)
        phrase = f"{a} {b} {c}"
        if non_stop >= 2 and phrase not in seen:
            seen.add(phrase)
            phrases.append(phrase)

    return phrases[:max_phrases]


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
        kw_keywords = [k.strip().lower() for k in re.split(r"[,;]", kw_text)
                       if 2 < len(k.strip()) < 80]

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

        for project in results:
            # Use the Terms field first (pre-extracted keywords)
            terms_raw = project.get("Terms", "") or ""
            if terms_raw:
                # Terms are pipe-separated or semicolon-separated
                terms = [t.strip().lower() for t in re.split(r"[|;]", terms_raw)
                         if t.strip() and len(t.strip()) > 3]
                all_terms.extend(terms)

            # Also mine the abstract for noun phrases
            abstract = project.get("AbstractText", "") or ""
            if abstract and len(all_terms) < 20:
                words = re.findall(r"[a-zA-Z]{4,}", abstract)
                stop = {"with", "that", "this", "from", "have", "been", "they", "their",
                        "which", "will", "also", "more", "than", "when", "some", "into",
                        "other", "were", "after", "about", "these", "both", "such",
                        "project", "study", "studies", "using", "used", "based",
                        "results", "data", "patients", "clinical", "treatment",
                        "university", "maryland", "grant", "funding", "supported"}
                all_terms.extend(w.lower() for w in words if w.lower() not in stop)

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
    Affiliation-verified: only counts papers with University of Maryland affiliation.
    """
    clean_name = _strip_credentials(faculty.get("name", ""))
    parts = clean_name.split()
    if len(parts) < 2:
        return faculty

    last_name = parts[-1]
    first_name = parts[0]
    first_initial = first_name[0]

    try:
        # Search with affiliation filter
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

        # Verify at least one result has Maryland affiliation in author list
        verified_results = []
        for paper in results:
            # Check author affiliations
            author_list = paper.get("authorList", {}).get("author", [])
            for author in author_list:
                aff = (author.get("affiliation") or "").lower()
                auth_name = (author.get("lastName") or "").lower()
                if last_name.lower() in auth_name and "maryland" in aff:
                    verified_results.append(paper)
                    break

        if not verified_results:
            # Fall back: accept any result if author name in full text affiliation
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

        # Filter generic MeSH stopwords
        mesh_stop = {
            "humans", "male", "female", "adult", "aged", "animals", "mice",
            "rats", "child", "adolescent", "middle aged", "young adult",
            "united states", "retrospective studies", "prospective studies",
            "treatment outcome", "time factors", "follow-up studies",
            "aged, 80 and over", "infant", "newborn", "preschool"
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
    path = Path(cache_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Faculty cache saved: {len(data['faculty'])} profiles → {cache_file}")


# ── Main entry point ──────────────────────────────────────────────────────────

def get_faculty_profiles(config: dict) -> list[dict]:
    cache_file = config["faculty"]["cache_file"]
    rescrape_hours = config["faculty"]["rescrape_interval_hours"]

    cache = load_faculty_cache(cache_file)
    if cache:
        last_scraped = datetime.fromisoformat(cache.get("scraped_at", "2000-01-01"))
        age_hours = (datetime.utcnow() - last_scraped).total_seconds() / 3600
        if age_hours < rescrape_hours:
            logger.info(f"Using cached faculty data ({len(cache['faculty'])} profiles, {age_hours:.1f}h old)")
            # Return only active faculty — exclude anyone marked inactive
            active = [f for f in cache["faculty"] if not f.get("inactive")]
            if len(active) < len(cache["faculty"]):
                logger.info(f"  Excluded {len(cache['faculty']) - len(active)} inactive (departed) faculty")
            return active
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

    # ── Pass 2: individual UMSOM profile pages (Research Interests extraction) ──
    # Runs on ALL active faculty — not just those missing keywords.
    # For faculty who already have keywords from Pass 1, the Research Interests
    # section is MERGED in as additional high-quality keywords.
    # For faculty with no keywords at all, this is their first enrichment opportunity.
    active_with_url = [f for f in all_faculty
                       if not f.get("inactive")
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

    # ── Pass 3: PubMed enrichment (ALL active faculty) ────────────────────────
    active_faculty = [f for f in all_faculty if not f.get("inactive")]
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
}


def _base_source_label(raw: str) -> str:
    """Map a raw `_merge_keywords` source string to its clean display label."""
    s = (raw or "").strip()
    if not s:
        return "Unknown"
    base = s.split("(", 1)[0].strip().lower()
    return _SOURCE_BASE_LABEL.get(base, s)


def _merge_keywords(faculty: dict, new_keywords: list[str], source: str) -> None:
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
    """
    # Filter boilerplate up-front so per-source records stay clean.
    contributed = [k for k in (new_keywords or []) if not _is_boilerplate_kw(k)]

    # Flat keyword list — only genuinely new ones get appended.
    existing = faculty.get("keywords") or []
    existing_lower = {k.lower() for k in existing}
    added = [k for k in contributed if k.lower() not in existing_lower]
    faculty["keywords"] = existing + added

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

        # Also pull research resource titles / work titles as context
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
                words = re.findall(r"[a-zA-Z]{4,}", " ".join(titles))
                stop = {"with", "that", "this", "from", "have", "been", "using",
                        "study", "analysis", "based", "novel", "role", "effect",
                        "effects", "human", "mice", "mouse", "patients", "cells"}
                keywords = list(dict.fromkeys(
                    w.lower() for w in words if w.lower() not in stop
                ))[:40]

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
