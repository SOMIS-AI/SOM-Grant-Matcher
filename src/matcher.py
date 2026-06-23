"""
Keyword + Semantic Matching Engine
===================================
Hybrid matcher: runs fast regex keyword matching first, then a semantic
embedding pass to catch faculty whose research is relevant but whose
exact keywords don't appear in the grant text.

Match types:
  "keyword"  -- found by exact keyword regex
  "semantic" -- found by cosine similarity of embeddings
  "both"     -- found by both methods (highest confidence)

Confidence Score (0-100)
------------------------
Replaces the old raw match_score count with a calibrated 0-100 confidence
score that accounts for:

  1. IDF-weighted keyword score: keywords rare across the faculty pool carry
     far more weight than generic terms like "education" or "chronic".
     IDF(kw) = log(total_faculty / faculty_who_have_this_keyword)
     A keyword only 4 faculty have (IDF≈6.5) outweighs one 600 have (IDF≈1.5).

  2. Dynamic stop-word expansion: any keyword appearing in more than
     `max_kw_prevalence_pct` of all faculty (default 15%) is treated as a
     stop word at match time, regardless of config.yaml stop_words list.

  3. Keyword density bonus: fraction of the grant's meaningful content
     covered by matched keywords (up to +10 pts).

  4. Title match bonus: matched keywords in the grant title (+3 pts each,
     up to +15 pts total) — title terms are the strongest signal.

  5. Semantic similarity: cosine similarity (0-1) from the embedding model,
     incorporated directly for semantic/both matches.

  6. Match type multiplier:
       "both"     → max(keyword_conf, semantic_conf) × 1.15  (capped at 99)
       "keyword"  → keyword_conf
       "semantic" → semantic_conf × 0.85  (slight penalty: no keyword confirmation)

Results are merged, de-duplicated, sorted by confidence desc, and persisted.
"""

import json
import logging
import math
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

MATCHES_FILE = "data/match_results.json"
STATS_FILE   = "data/run_stats.json"

# ── Diagnostic logging paths (read from Railway env vars, fall back to defaults) ──
_LOG_DIR    = os.getenv("MATCHER_LOG_DIR",    "logs")
_DB_PATH    = os.getenv("MATCHER_DB_PATH",    "data/matcher.db")

# Run-level tracking — set at the start of find_matches(), read at save time
_run_start_time:   float = 0.0
_raw_match_count:  int   = 0      # total faculty matches BEFORE confidence filter
_faculty_count:    int   = 0      # active faculty processed this run
_last_diagnostic:  dict  = {}     # diagnostic data from most recent run (read by main.py)

DEFAULT_SEMANTIC_THRESHOLD  = 0.55   # Lowered from 0.65: MiniLM-L6-v2 cosine similarities for
                                     # related biomedical content typically range 0.3–0.65.
                                     # 0.65 was at the ceiling and produced 0 semantic matches.
                                     # 0.55 should activate the semantic channel while staying
                                     # above general-domain noise (0.3–0.45).
DEFAULT_MAX_KW_PREVALENCE   = 0.08   # keywords in >8% of faculty → dynamic stop word
                                     # Lowered from 0.15: at 15% only "disease" was suppressed.
                                     # At 8%, common terms like "cancer", "clinical", "therapy"
                                     # will also be suppressed, reducing broad-grant false matches.
DEFAULT_MAX_MATCHES_PER_GRANT = 50   # Cap matches per grant — keeps email digest manageable
                                     # and prevents broad grants from matching 500+ faculty.
DEFAULT_MIN_IDF_FOR_MATCH     = 1.5  # Minimum IDF for a grant keyword to count in matching.
                                     # Terms below this are too common across faculty to be
                                     # meaningful match signals.

# ── Administrative / procedural keyword blocklist ─────────────────────────────
# These words appear in NIH notices and grant titles but carry zero scientific
# signal. Matching on "extension", "change", "correction", "continuation" etc.
# produces massive false positives — hundreds of faculty have these as incidental
# words in their profiles. We strip these BEFORE keyword matching so they never
# contribute to a match.
_ADMIN_KEYWORD_BLOCKLIST = {
    # NIH notice / administrative terms
    "extension", "correction", "change", "continuation", "supplement",
    "revision", "existing", "competitive", "cooperative", "temporary",
    "urgent", "notice", "eligibility", "compliance", "reissuance",
    "reissue", "update", "amendment", "modification", "announcement",
    # Grant mechanism terms (not science)
    "award", "program", "agreement", "guide", "guidance", "guideline",
    "information", "instruction", "requirement", "application",
    "submission", "deadline", "receipt", "review", "accounts",
    # Generic terms that match too broadly across faculty profiles
    "independence", "pathway", "americans", "opportunity",
    "initiative", "resource", "approach", "support", "mechanism",
}

# ── NIH administrative notice title patterns ──────────────────────────────────
# Grants whose titles match these patterns are administrative updates, not actual
# funding opportunities faculty can apply to. Skip them entirely.
_ADMIN_TITLE_PREFIXES = [
    "notice of correction",
    "notice of change",
    "notice of update",
    "notice of reissuance",
    "notice of modification",
    "urgent competitive revision to existing",
    "notice of intent to publish",
    "notice regarding",
    "request for information",
]


class Match(NamedTuple):
    faculty_name:       str
    faculty_url:        str
    faculty_department: str
    faculty_email:      str
    matched_keywords:   list
    match_score:        int        # legacy raw count (kept for backwards compat)
    match_type:         str        # "keyword" | "semantic" | "both"
    similarity_score:   float      # cosine similarity (0.0 if keyword-only)
    confidence_score:   int        # NEW: 0-100 calibrated confidence


# -- Text normalisation -------------------------------------------------------

def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


# -- IDF table ----------------------------------------------------------------

def build_idf_table(faculty: list) -> dict:
    """
    Build a keyword → IDF score mapping from the full faculty pool.
    IDF(kw) = log(N / df) where df = number of faculty who have this keyword.
    Keywords appearing in > max_kw_prevalence_pct of faculty are flagged
    as dynamic stop words (IDF too low to be meaningful).
    """
    N = len(faculty)
    if N == 0:
        return {}

    df = Counter()
    for person in faculty:
        seen = set()
        for kw in (person.get("keywords") or []):
            kw_norm = normalize(kw).strip()
            if kw_norm and kw_norm not in seen:
                df[kw_norm] += 1
                seen.add(kw_norm)

    idf = {}
    for kw_norm, count in df.items():
        idf[kw_norm] = math.log(N / count)

    logger.debug(f"IDF table built: {len(idf)} unique keywords across {N} faculty")
    return idf


def get_dynamic_stop_words(idf_table: dict, faculty_count: int,
                           max_prevalence: float) -> set:
    """
    Return set of normalised keywords that appear in more than
    max_prevalence fraction of all faculty — these are too generic
    to be meaningful match signals regardless of config stop_words.
    """
    threshold_idf = math.log(1.0 / max_prevalence)   # e.g. log(1/0.15) ≈ 1.90
    dynamic_stops = {kw for kw, score in idf_table.items() if score <= threshold_idf}
    if dynamic_stops:
        logger.info(
            f"Dynamic stop words: {len(dynamic_stops)} keywords suppressed "
            f"(appear in >{max_prevalence*100:.0f}% of faculty). "
            f"Examples: {sorted(dynamic_stops)[:10]}"
        )
    return dynamic_stops


# -- Token-level stop word filter --------------------------------------------

def _phrase_is_all_stops(phrase: str, all_stops: set, min_kw_len: int) -> bool:
    """
    Return True if every meaningful token in a keyword phrase is a stop word.

    This catches multi-word phrases that contain only generic terms, e.g.:
      "community health centers"  → all tokens are stops → suppress
      "developmental disabilities"→ 'disabilities' is not a stop → keep
      "cancer immunotherapy"      → 'cancer','immunotherapy' not stops → keep

    A token is considered "meaningful" if it meets min_kw_len AND is not a stop word.
    If the phrase has zero meaningful tokens it is suppressed.
    """
    tokens = re.sub(r"[^a-z0-9 ]", " ", phrase.lower()).split()
    meaningful = [t for t in tokens if len(t) >= min_kw_len and t not in all_stops]
    return len(meaningful) == 0


# -- Confidence scoring -------------------------------------------------------

def _compute_confidence(
    matched_keywords: list,
    idf_table: dict,
    grant_text: str,
    grant_title: str,
    similarity_score: float,
    match_type: str,
) -> int:
    """
    Compute a 0-100 confidence score for a single faculty-grant match.
    """
    # ── Factor 1: IDF-weighted keyword score ──────────────────────────────────
    # Each matched keyword contributes min(IDF, 5.0) points.
    # A cap of 5.0 prevents one ultra-specific term from dominating.
    # Score normalised over 20.0 (raised from 15.0) so that a faculty member needs
    # more/better keywords to score high — reducing false positives from 1-2 weak matches.
    # Calibrated spread with new normalizer:
    #   1 somewhat-specific (IDF 3.0) → 15%  (was 20% — now below min_confidence=20)
    #   1 specific (IDF 4.5)          → 22%  (was 30%)
    #   2 specific (IDF 4.5 each)     → 45%  (was 60%)
    #   3 medium (IDF 3.5 each)       → 52%  (was 70%)
    #   3 specific (IDF 4.5 each)     → 67%  (was 90%)
    #   3 highly-specific (IDF 5+)    → 75%  (was 99%)
    # Effect: a faculty member now needs 2+ genuinely specific keywords to pass min_confidence=20
    idf_sum = sum(min(idf_table.get(normalize(kw).strip(), 1.0), 5.0)
                  for kw in matched_keywords)
    kw_confidence = min(idf_sum / 20.0, 1.0)

    # ── Factor 2: Title match bonus ───────────────────────────────────────────
    title_matches = sum(
        1 for kw in matched_keywords
        if re.search(r"\b" + re.escape(normalize(kw).strip()) + r"\b", grant_title)
    )
    title_bonus = min(title_matches * 0.05, 0.15)   # up to +15 pts

    # ── Factor 3: Keyword density bonus ──────────────────────────────────────
    # What fraction of the grant text's "meaningful" chars do matched keywords cover?
    matched_chars = sum(len(kw) for kw in matched_keywords)
    grant_len = max(len(grant_text), 1)
    density_bonus = min((matched_chars / grant_len) * 5.0, 0.10)   # up to +10 pts

    # ── Combine keyword-based confidence ─────────────────────────────────────
    kw_conf = min(kw_confidence + title_bonus + density_bonus, 1.0)

    # ── Factor 4: Apply match type multiplier ─────────────────────────────────
    sem_conf = float(similarity_score or 0.0)

    if match_type == "both":
        # Both methods agree → strongest signal
        combined = max(kw_conf, sem_conf) * 1.15
    elif match_type == "keyword":
        combined = kw_conf
    else:
        # Semantic only: no keyword corroboration, but no penalty either —
        # the semantic_threshold (sim >= 0.40) already gates quality, and
        # min_confidence_score in config provides the final floor.
        # The previous 0.85 multiplier interacted badly with min_confidence=45:
        # at that floor it required sim >= 0.53, which almost no genuine match
        # achieves (typical max similarity 0.45-0.51), collapsing the semantic
        # discovery channel (~100/run -> 1/run on 2026-05-28). 1.0 restores it.
        combined = sem_conf

    # Scale to 0-99 (never show 100% — that would imply certainty)
    return min(round(combined * 100), 99)


# -- Pass 1: Keyword matching -------------------------------------------------

# ── Biomedical relevance pre-filter ──────────────────────────────────────────
#
# Before running keyword matching against all 2,700 faculty, check whether the
# grant is even plausibly relevant to a medical school.  Non-biomedical grants
# (e.g. EPA wetland programs, USDA agricultural grants, DoT infrastructure)
# can match hundreds of faculty on generic terms like "program", "development",
# "network", "community" — wasting time and flooding the email digest.
#
# Strategy:
#   1. Agency allow-list  — known biomedical funders always pass through
#   2. Agency block-list  — known non-biomedical agencies always blocked
#   3. Biomedical keyword scan — grant text must contain at least one term
#      from a curated biomedical vocabulary to proceed
#   4. CFDA prefix check  — Grants.gov CFDA numbers starting with 93 (HHS),
#      93.xxx are health/biomedical; other prefixes get vocab check
#
# All thresholds are configurable in config.yaml under matching.relevance_filter

# Agencies whose grants are always relevant to UMSOM — skip vocab check
_AGENCY_ALLOW = {
    "nih", "national institutes of health", "national institute",
    "nci", "nhlbi", "nimh", "niaid", "niddk", "nigms", "ninds", "nidcr",
    "nichd", "nccih", "niehs", "nimhd", "niaaa", "nida", "ncats", "nhgri",
    "obssr", "orwh", "fogarty", "ncmhd",
    "ahrq", "agency for healthcare research",
    "cdc", "centers for disease control",
    "hrsa", "health resources and services",
    "fda", "food and drug administration",
    "samhsa", "substance abuse and mental health",
    "acl", "administration for community living",
    "cms", "centers for medicare",
    "va", "veterans affairs", "department of veterans",
    "dod", "department of defense",        # DoD has many medical research programs
    "cdmrp", "congressionally directed medical",
    "barda", "biomedical advanced research",
    "arpa-h", "arpa h", "advanced research projects agency for health",
    "darpa",                                # often funds biomedical innovation
    "nsf",                                  # many biomedical/bioscience grants
    "hhmi", "howard hughes",
    "wellcome", "gates foundation", "gates",
    "american cancer society", "american heart association",
    "american diabetes association", "american medical association",
    "alzheimer", "parkinson", "multiple sclerosis",
    "march of dimes", "simons foundation",
    "burroughs wellcome", "doris duke",
    # Med-ed / humanism foundations (added 2026-06-05): without these, the
    # biomedical-vocab pre-filter rejects real research grants from these
    # foundations because the *title* doesn't carry biomedical vocabulary
    # (e.g. "Giambalvo Research Grant", "Picker Gold Challenge Grants").
    "arnold p. gold", "josiah macy",
    "pcori", "patient-centered outcomes research",
    # DoD biomedical-relevant sub-commands and research labs (added 2026-06-17
    # after the ONR "Special Program Announcement" miss surfaced in the
    # skipped-grants audit). The DoD/DARPA umbrella terms above don't substring
    # into agency strings like "Office of Naval Research", so they need
    # explicit allow-list entries. Word-boundary matching means these only
    # fire when the agency actually says "naval research" / "army research" /
    # etc., not as random substrings.
    "office of naval research", "naval research",
    "naval medical research", "namru",
    "army research", "walter reed", "wrair",
    "air force research", "afrl", "afosr",
    "defense health agency", "defense health",
    "usaarl",
}

# Agencies whose grants are never relevant to UMSOM
_AGENCY_BLOCK = {
    "epa", "environmental protection",
    "usda", "department of agriculture", "agricultural",
    "natural resources conservation", "nrcs", "conservation service",
    "farm service agency", "forest service", "rural business",
    "dot", "department of transportation", "federal highway", "federal transit",
    "hud", "housing and urban development",
    "doe", "department of energy",          # exception: some bioenergy/biotech
    "usgs", "geological survey",
    "noaa", "national oceanic", "oceanic and atmospheric",
    "nasa",                                 # exception: rare life sciences
    "fema", "emergency management",
    "sba", "small business administration",
    "treasury", "department of treasury",
    "commerce",                             # most (not all) commerce grants
    "labor", "department of labor",
    "education", "department of education", # not medical education grants
    "interior", "department of interior",
    "wetland", "watershed", "estuary", "forestry", "fisheries",
    "rural development", "rural utilities",
    "arts", "national endowment for the arts",
    "humanities", "national endowment for the humanities",
}

# Specific non-biomedical topic phrases that occasionally slip past the agency
# and vocabulary checks (e.g. an AI/security or agriculture grant that mentions
# "pathogen" or "molecular"). These are phrase-specific to avoid false positives
# with legitimate biomedical titles (e.g. "cell proliferation" ≠ "proliferation risk").
_NONBIO_TITLE_TERMS = {
    "nonproliferation",
    "proliferation risk",          # e.g. "Mitigating Proliferation Risks Posed by AI…"
    "weapons of mass destruction",
    "crop protection",
    "pest management",
    "agronomy",
    "rangeland",
    "conservation innovation",     # USDA NRCS Conservation Innovation Grants (CIG)
    "on-farm",
    "soil health",
    "grazing",
    "livestock",
    "agroforestry",
    "tribal students",
}

# Minimum vocabulary — at least one of these must appear in the grant text
# for a non-allow-listed agency to pass the relevance filter
_BIOMEDICAL_VOCAB = re.compile(
    r"\b("
    # Diseases and conditions
    r"cancer|tumor|carcinoma|oncol|leukemia|lymphoma|melanoma|"
    r"alzheimer|dementia|parkinson|neurodegenerat|"
    r"diabetes|diabetic|insulin|glucose|metabolic|obesity|"
    r"cardiovascular|cardiac|heart|coronary|hypertension|stroke|vascular|"
    r"infection|infectious|pathogen|bacterial|viral|fungal|antimicrobial|antibiotic|"
    r"HIV|AIDS|tuberculosis|malaria|sepsis|pneumonia|influenza|COVID|"
    r"autoimmune|immune|immunolog|allerg|asthma|"
    r"mental health|psychiatric|depression|anxiety|schizophrenia|bipolar|"
    r"opioid|substance use|addiction|overdose|"
    r"kidney|renal|liver|hepat|pulmonary|lung|respiratory|"
    r"neurolog|brain|spinal|cognitive|epilepsy|seizure|"
    r"musculoskeletal|orthopedic|arthritis|bone|"
    r"reproductive|maternal|neonatal|pediatric|geriatric|"
    r"dermatolog|skin|wound|"
    # Biomedical science
    r"genomic|genome|gene|genetic|DNA|RNA|protein|molecular|cellular|"
    r"stem cell|cell therap|regenerat|"
    r"biomarker|diagnostic|imaging|MRI|CT scan|PET|ultrasound|"
    r"pharmacol|drug|therapeut|treatment|clinical trial|randomized|"
    r"surgery|surgical|transplant|"
    r"microbiome|microbiota|"
    r"epidemiolog|cohort|longitudinal|"
    r"health disparit|health equit|"
    # Medical specialties that are unambiguous
    r"oncolog|cardiology|neurology|gastroenterol|dermatolog|"
    r"patholog|radiology|psychiatry|pediatrics|geriatrics|"
    r"nursing|pharmacy|dentistry|dental|"
    # Institutions
    r"hospital|clinic|patient care|medical center|health system"
    r")\b",
    re.IGNORECASE,
)


# Pre-compile the block/allow/non-bio lists into word-boundary regexes at module
# load. Plain substring checks (the previous implementation) misfire on short
# acronyms — e.g. 'epa' matched "DEPArtment of Housing", causing every HUD/DHS
# grant to be filtered with reason "blocked agency/title term: 'epa'" (caught
# 2026-06-16 via the new skipped-grants audit). Word boundaries (\b) make the
# acronym terms only match when they stand alone, while multi-word phrases like
# "department of housing and urban development" still work fine.
_AGENCY_BLOCK_RX = [
    (term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for term in _AGENCY_BLOCK
]
_AGENCY_ALLOW_RX = [
    (term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for term in _AGENCY_ALLOW
]
_NONBIO_TITLE_RX = [
    (term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for term in _NONBIO_TITLE_TERMS
]


def _is_biomedically_relevant(grant: dict, min_vocab_hits: int = 1) -> tuple[bool, str]:
    """
    Return (is_relevant, reason) for a grant.
    Grants that fail this check are skipped entirely — no keyword matching,
    no semantic matching, no email entry.
    """
    agency = (grant.get("agency") or "").lower().strip()
    title  = (grant.get("title")  or "").lower()
    text   = (grant.get("searchable_text") or grant.get("synopsis") or "").lower()
    full   = title + " " + text

    # 1. Non-biomedical topic phrases — applied UNCONDITIONALLY because phrases
    #    like "weapons of mass destruction" / "agroforestry" / "tribal students"
    #    are out of scope regardless of agency. These short-circuit any allow-list
    #    bypass below.
    for term, pattern in _NONBIO_TITLE_RX:
        if pattern.search(title):
            return False, f"non-biomedical topic term: '{term}'"

    # 2. Agency allow-list — fast accept (trusted agencies bypass the block-list).
    #    Reordered 2026-06-17: previously the block-list ran first, so SAMHSA
    #    "Preventing Youth Overdose: Treatment, Recovery, Education, Awareness"
    #    got rejected by the 'education' block term even though SAMHSA is
    #    allow-listed. Now allow-listed agencies pass without consulting the
    #    block-list; only non-bio topic terms above can stop them.
    for allowed, pattern in _AGENCY_ALLOW_RX:
        if pattern.search(agency):
            return True, f"allowed agency: '{allowed}'"

    # 3. Agency block-list — applies to NON-allow-listed agencies only.
    #    Word-boundary regex (see _AGENCY_BLOCK_RX comment).
    for blocked, pattern in _AGENCY_BLOCK_RX:
        if pattern.search(agency) or pattern.search(title):
            return False, f"blocked agency/title term: '{blocked}'"

    # 4. CFDA prefix — HHS is 93.xxx, always biomedical
    cfda = grant.get("cfda_number") or grant.get("number") or ""
    if str(cfda).startswith("93."):
        return True, f"HHS CFDA prefix: {cfda}"

    # 5. Biomedical vocabulary scan on full grant text
    hits = _BIOMEDICAL_VOCAB.findall(full)
    if len(hits) >= min_vocab_hits:
        return True, f"biomedical vocab match ({len(hits)} terms: {list(set(h.lower() for h in hits))[:3]})"

    return False, "no biomedical agency or vocabulary found"


# ── UMB-specific eligibility filter ──────────────────────────────────────────
# Drops grants UMB faculty are NOT eligible to apply for. Patterns live in
# matching.ineligible_grant_patterns (config.yaml) so the institution/PI
# screen-out is curatable without code changes. Each pattern is a regex run
# case-insensitive against the grant title (with optional inline `(?-i:...)`
# scopes for acronyms like IDeA that must stay case-sensitive). The diagnostic
# logs which pattern fired so it's easy to audit and refine.

def _compile_ineligible_patterns(raw_list):
    """Compile a list of {pattern, reason} dicts into [(compiled_regex, reason)]."""
    compiled = []
    for entry in (raw_list or []):
        if isinstance(entry, dict):
            pat, reason = entry.get("pattern"), entry.get("reason", "")
        else:
            pat, reason = entry, ""
        if not pat: continue
        try:
            compiled.append((re.compile(pat, re.IGNORECASE), reason))
        except re.error as e:
            logger.warning(f"Bad ineligible_grant_patterns regex {pat!r}: {e}")
    return compiled


def _is_institutionally_eligible(title: str, compiled_patterns) -> tuple[bool, str]:
    """Return (eligible, reason). reason is empty when eligible."""
    if not compiled_patterns or not title:
        return True, ""
    for rx, reason in compiled_patterns:
        if rx.search(title):
            return False, reason
    return True, ""


# ── Research track-record signal (Theme 2) ───────────────────────────────────
# A faculty member's external publication/grant footprint, read from the
# keywords_by_source attribution built during enrichment. Used to weight match
# confidence (clinical-care/educator faculty with no footprint are unlikely to
# PI major research) and to hard-gate the no-footprint tier off major grants.

# Source labels that represent a genuine external research footprint (vs.
# self-described profile keywords). Must match faculty_scraper._SOURCE_BASE_LABEL.
_EVIDENCE_SOURCE_LABELS = (
    "PubMed", "Europe PMC", "ORCID", "Semantic Scholar", "ClinicalTrials.gov",
)


def _research_tier(faculty: dict) -> str:
    """
    Classify a faculty member's external research footprint:
      'nih'     — has NIH RePORTER grant(s); strongest track record
      'pub'     — has publication footprint (PubMed/Europe PMC/ORCID/S2/CT.gov)
      'none'    — only profile / self-reported keywords; no external footprint
      'unknown' — attribution not captured (pre-rescrape cache); treat neutrally
    """
    kbs = faculty.get("keywords_by_source")
    if not kbs:
        return "unknown"
    if kbs.get("NIH RePORTER"):
        return "nih"
    if any(kbs.get(lbl) for lbl in _EVIDENCE_SOURCE_LABELS):
        return "pub"
    return "none"


def _compile_major_mechanism_patterns(raw_list):
    """Compile the major-mechanism title/number markers once per run."""
    compiled = []
    for pat in (raw_list or []):
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error as e:
            logger.warning(f"Bad major_mechanism_patterns regex {pat!r}: {e}")
    return compiled


def _is_major_mechanism(grant: dict, compiled_patterns) -> bool:
    """True if the grant title/number denotes a major independent-research award."""
    if not compiled_patterns:
        return False
    hay = f"{grant.get('title', '')} {grant.get('number', '')}"
    return any(rx.search(hay) for rx in compiled_patterns)


def _keyword_matches_for_grant(grant, faculty, stop_words, min_kw_len,
                                idf_table, dynamic_stops,
                                context_terms=None, context_dropped=None):
    """
    Run regex keyword matching for one grant against all faculty.
    Returns dict keyed by faculty_name for easy merging with semantic pass.

    context_terms: set of normalised generic terms that cannot anchor a match on
    their own (Theme 3). If EVERY matched keyword for a faculty is in this set,
    the match is dropped — they matched only on generic population/method words
    with no disease/domain-specific term. When dropped, an audit entry is
    appended to context_dropped (if provided) for the diagnostic.
    """
    grant_text  = normalize(grant["searchable_text"])
    grant_title = normalize(grant["title"])
    all_stops   = stop_words | dynamic_stops
    context_terms = context_terms or set()
    results     = {}

    for person in faculty:
        keywords = person.get("keywords", [])
        if not keywords:
            continue

        matched = []
        for kw in keywords:
            kw_norm = normalize(kw).strip()
            if len(kw_norm) < min_kw_len or kw_norm in all_stops:
                continue
            # Skip administrative/procedural terms that aren't scientific
            if kw_norm in _ADMIN_KEYWORD_BLOCKLIST:
                continue
            # Suppress phrases where every token is a stop word
            # e.g. "community health centers", "service delivery", "program evaluation"
            if _phrase_is_all_stops(kw_norm, all_stops, min_kw_len):
                continue
            pattern = r"\b" + re.escape(kw_norm) + r"\b"
            if re.search(pattern, grant_text):
                matched.append(kw)

        # Theme 3: a match must rest on ≥1 domain-specific keyword. If every
        # matched term is a generic context-dependent word (pediatric, risk, …)
        # the overall concept wasn't matched — drop and audit.
        if matched and context_terms:
            specific = [kw for kw in matched
                        if normalize(kw).strip() not in context_terms]
            if not specific:
                if context_dropped is not None:
                    context_dropped.append({
                        "faculty": person["name"],
                        "terms":   sorted(set(matched)),
                    })
                continue

        if matched:
            # Legacy raw score (kept for backwards compat / sorting fallback)
            title_bonus_raw = sum(
                1 for kw in matched
                if re.search(r"\b" + re.escape(normalize(kw).strip()) + r"\b", grant_title)
            )
            raw_score = len(matched) + title_bonus_raw

            confidence = _compute_confidence(
                matched, idf_table, grant_text, grant_title,
                similarity_score=0.0, match_type="keyword"
            )

            results[person["name"]] = Match(
                faculty_name       = person["name"],
                faculty_url        = person.get("url", ""),
                faculty_department = person.get("department", ""),
                faculty_email      = person.get("email", ""),
                matched_keywords   = sorted(set(matched)),
                match_score        = raw_score,
                match_type         = "keyword",
                similarity_score   = 0.0,
                confidence_score   = confidence,
            )

    return results


# -- Merge & sort -------------------------------------------------------------

def _merge_match_dicts(keyword_matches, semantic_matches, idf_table,
                        grant_text, grant_title, sim_by_name=None, sem_threshold=None):
    """
    Merge keyword and semantic matches into a single ranked list.

    keyword_matches and semantic_matches are disjoint by name (the semantic pass
    only adds faculty the keyword pass missed). A keyword match whose faculty ALSO
    has cosine similarity >= sem_threshold is upgraded to match_type="both" and
    rescored with the agreement boost — this is what lets the strongest matches
    (flagged by BOTH channels) rank highest. Sorted by confidence descending.
    """
    sim_by_name = sim_by_name or {}
    all_names = set(keyword_matches) | set(semantic_matches)
    merged = []

    for name in all_names:
        kw  = keyword_matches.get(name)
        sem = semantic_matches.get(name)

        if kw is not None:
            sim = sim_by_name.get(name, 0.0)
            if sem_threshold is not None and sim >= sem_threshold:
                confidence = _compute_confidence(
                    kw.matched_keywords, idf_table, grant_text, grant_title,
                    similarity_score=sim, match_type="both"
                )
                merged.append(kw._replace(
                    match_type="both", similarity_score=sim, confidence_score=confidence
                ))
            else:
                merged.append(kw)
        elif sem is not None:
            merged.append(sem)

    # Sort by confidence desc, then match_type priority, then similarity
    merged.sort(key=lambda m: (
        -m.confidence_score,
        0 if m.match_type == "both" else (1 if m.match_type == "keyword" else 2),
        -m.similarity_score,
    ))
    return merged


# -- Main entry point ---------------------------------------------------------

def find_matches(grants, faculty, config=None):
    """
    Hybrid matching: keyword regex + semantic embeddings.
    Produces IDF-weighted confidence scores (0-100) for each match.
    config is optional -- falls back to defaults if not provided.
    """
    global _run_start_time, _raw_match_count, _faculty_count
    _run_start_time  = time.time()
    _raw_match_count = 0
    _faculty_count   = sum(1 for f in faculty if not f.get("inactive"))

    matching_cfg     = (config or {}).get("matching", {})
    min_kw_len       = matching_cfg.get("min_keyword_length", 4)
    stop_words       = set(w.lower() for w in matching_cfg.get("stop_words", []))
    sem_threshold    = matching_cfg.get("semantic_threshold", DEFAULT_SEMANTIC_THRESHOLD)
    sem_enabled      = matching_cfg.get("semantic_matching", True)
    max_prevalence   = matching_cfg.get("max_kw_prevalence_pct", DEFAULT_MAX_KW_PREVALENCE)
    min_confidence   = matching_cfg.get("min_confidence_score", 35)  # default 35 — raised from 20 to suppress weak matches
    max_per_grant    = matching_cfg.get("max_matches_per_grant", DEFAULT_MAX_MATCHES_PER_GRANT)
    min_idf_match    = matching_cfg.get("min_idf_for_match", DEFAULT_MIN_IDF_FOR_MATCH)
    max_grant_chars  = matching_cfg.get("max_grant_text_chars", 3000)

    # Theme 3: generic terms that can't anchor a match alone
    context_terms    = {normalize(t).strip()
                        for t in matching_cfg.get("context_dependent_terms", [])}
    # Theme 2: research track-record weighting + hard gate
    re_cfg           = matching_cfg.get("research_evidence", {}) or {}
    tier_mult        = {
        "nih":     float(re_cfg.get("nih_reporter_multiplier", 1.0)),
        "pub":     float(re_cfg.get("publication_multiplier", 1.0)),
        "none":    float(re_cfg.get("none_multiplier", 1.0)),
        "unknown": 1.0,   # never penalize on missing attribution
    }
    gate_major       = bool(re_cfg.get("gate_major_mechanisms", False))
    major_mech_rx    = _compile_major_mechanism_patterns(
        matching_cfg.get("major_mechanism_patterns", [])
    )

    # ── Diagnostic data collector — gathered throughout the run, used by diagnostic email ──
    _diag = {
        "params": {
            "semantic_threshold": sem_threshold,
            "min_confidence": min_confidence,
            "max_kw_prevalence_pct": max_prevalence,
            "max_matches_per_grant": max_per_grant,
            "min_idf_for_match": min_idf_match,
            "semantic_enabled": sem_enabled,
        },
        "stop_words_suppressed": [],
        "per_grant": [],                   # per-grant detail for the diagnostic email
        "semantic_score_distributions": [],  # top semantic scores per grant
        "confidence_histograms": [],        # confidence distribution per grant
        "idf_filtered_keywords": [],        # keywords removed by IDF floor per grant
        "grants_capped": [],                # grants that hit the per-grant cap
        # Theme 3 / Theme 2 audit (2026-06-23): matches dropped because every
        # matched keyword was a generic context-dependent term, and matches
        # dropped because a no-research-footprint faculty hit the hard gate on a
        # major-mechanism grant. Samples kept for tuning; counts in summary.
        "context_filtered": [],             # {grant_title, count, sample:[{faculty,terms}]}
        "track_record_gated": [],           # {grant_title, count, sample:[faculty]}
        # Detailed audit lists for grants that were skipped before matching.
        # Counts are in summary.grants_skipped_*; these lists let us go back
        # and verify whether the filters dropped anything that should have
        # passed (or vice versa). Each entry carries the title + agency + link
        # so we can click through to the grant on Grants.gov.
        "skipped_grants": {
            "irrelevant": [],   # filtered by bio-relevance pre-filter
            "ineligible": [],   # filtered by UMB-eligibility patterns (with reason)
            "admin":      [],   # NIH "Notice of Correction" etc. amendments
        },
    }

    # Build IDF table and dynamic stop words from the full faculty pool
    logger.info(f"Building IDF table from {len(faculty)} faculty...")
    idf_table      = build_idf_table(faculty)
    dynamic_stops  = get_dynamic_stop_words(idf_table, len(faculty), max_prevalence)
    _diag["stop_words_suppressed"] = sorted(dynamic_stops)

    # Name → faculty lookup for building semantic matches without re-scanning
    faculty_by_name = {f.get("name", ""): f for f in faculty}

    faculty_with_embeddings = sum(1 for f in faculty if f.get("embedding"))
    if sem_enabled and faculty_with_embeddings == 0:
        logger.warning(
            "Semantic matching enabled but no faculty have embeddings yet -- "
            "run a full scrape with FORCE_SCRAPE=true to generate them."
        )
        sem_enabled = False

    if sem_enabled:
        logger.info(
            f"Hybrid matching: keyword + semantic "
            f"(threshold={sem_threshold}, {faculty_with_embeddings} faculty with embeddings)"
        )
    else:
        logger.info("Keyword-only matching (semantic matching disabled or embeddings unavailable)")

    if min_confidence > 0:
        logger.info(f"Confidence filter: only reporting matches with confidence >= {min_confidence}%")

    results = []
    kw_only = sem_only = both = 0
    suppressed = 0
    context_filtered_total = 0      # Theme 3: matches dropped (all-generic keywords)
    track_record_gated_total = 0    # Theme 2: no-footprint faculty gated off major grants

    skipped_irrelevant = 0
    skipped_admin = 0
    skipped_ineligible = 0
    ineligible_breakdown = {}

    # Compile UMB-eligibility patterns once per run (config-driven).
    ineligible_patterns = _compile_ineligible_patterns(
        matching_cfg.get("ineligible_grant_patterns", [])
    )

    for grant in grants:
        # ── Biomedical relevance pre-filter ──────────────────────────────────
        relevant, reason = _is_biomedically_relevant(grant)
        if not relevant:
            logger.info(
                f"  SKIPPED (not biomedical): '{grant['title'][:60]}' — {reason}"
            )
            # Standardised line parsed by grant_matcher_diagnostics.py
            if "nav" in reason.lower() or "navigation" in grant.get("title","").lower():
                print(f"Dropped: nav_page_detected — {grant['title'][:60]}")
            skipped_irrelevant += 1
            _diag["skipped_grants"]["irrelevant"].append({
                "title":  grant.get("title", ""),
                "agency": grant.get("agency", ""),
                "number": grant.get("number", ""),
                "link":   grant.get("link", ""),
                "reason": reason,
            })
            continue

        # ── UMB-specific eligibility filter ──────────────────────────────────
        # Skip grants UMB faculty are not eligible to apply for (resource-limited
        # institutions only, IDeA-state only, sole-source renewals, SuRE program,
        # NCORP Community Sites, trainee/dissertation/F-series fellowships, etc.).
        eligible, reason = _is_institutionally_eligible(grant.get("title", ""), ineligible_patterns)
        if not eligible:
            logger.info(
                f"  SKIPPED (UMB ineligible: {reason}): '{grant['title'][:60]}'"
            )
            skipped_ineligible += 1
            ineligible_breakdown[reason] = ineligible_breakdown.get(reason, 0) + 1
            _diag["skipped_grants"]["ineligible"].append({
                "title":  grant.get("title", ""),
                "agency": grant.get("agency", ""),
                "number": grant.get("number", ""),
                "link":   grant.get("link", ""),
                "reason": reason,
            })
            continue

        # ── Administrative notice filter ─────────────────────────────────────
        # NIH "Notice of Correction/Change/Update" etc. are administrative
        # amendments, not actual funding opportunities faculty can apply to.
        title_lower = grant["title"].lower().strip()
        is_admin_notice = False
        for prefix in _ADMIN_TITLE_PREFIXES:
            if title_lower.startswith(prefix):
                is_admin_notice = True
                break
        if is_admin_notice:
            logger.info(
                f"  SKIPPED (admin notice): '{grant['title'][:60]}'"
            )
            skipped_admin += 1
            skipped_irrelevant += 1  # count in the same bucket for diagnostics
            _diag["skipped_grants"]["admin"].append({
                "title":  grant.get("title", ""),
                "agency": grant.get("agency", ""),
                "number": grant.get("number", ""),
                "link":   grant.get("link", ""),
            })
            continue

        # Cap overly long scraped page text (some NSF/foundation pages carry the
        # whole page) so a single grant can't match hundreds of faculty at
        # inflated confidence. Applied in-place so _keyword_matches_for_grant,
        # which re-reads searchable_text, sees the same bound. searchable_text is
        # internal only (not persisted/emailed), so truncating here is safe.
        if max_grant_chars and grant.get("searchable_text"):
            grant["searchable_text"] = grant["searchable_text"][:max_grant_chars]

        grant_text  = normalize(grant["searchable_text"])
        grant_title = normalize(grant["title"])

        context_dropped = []
        keyword_matches = _keyword_matches_for_grant(
            grant, faculty, stop_words, min_kw_len, idf_table, dynamic_stops,
            context_terms=context_terms, context_dropped=context_dropped,
        )
        if context_dropped:
            context_filtered_total += len(context_dropped)
            _diag["context_filtered"].append({
                "grant_title": grant["title"][:80],
                "count":       len(context_dropped),
                "sample":      context_dropped[:5],
            })

        # ── Semantic pass: one cosine-similarity vector for ALL faculty ──────
        # sim_by_name powers two things: (1) semantic-only matches for faculty the
        # keyword pass missed, and (2) upgrading keyword matches that also clear the
        # threshold to match_type="both" (the agreement boost). See _merge_match_dicts.
        sim_by_name = {}
        semantic_matches = {}
        sem_score_info = None
        if sem_enabled:
            try:
                from embedder import grant_similarity_map, is_available
                if is_available():
                    sim_by_name = grant_similarity_map(grant, faculty)
            except ImportError:
                sim_by_name = {}

            if sim_by_name:
                # (1) Semantic-only matches: above threshold and not keyword-matched
                for name, sim in sim_by_name.items():
                    if sim < sem_threshold or name in keyword_matches:
                        continue
                    fac = faculty_by_name.get(name)
                    if fac is None:
                        continue
                    confidence = _compute_confidence(
                        [], idf_table, grant_text, grant_title,
                        similarity_score=sim, match_type="semantic"
                    )
                    semantic_matches[name] = Match(
                        faculty_name       = name,
                        faculty_url        = fac.get("url", ""),
                        faculty_department = fac.get("department", ""),
                        faculty_email      = fac.get("email", ""),
                        matched_keywords   = [],
                        match_score        = 0,
                        match_type         = "semantic",
                        similarity_score   = sim,
                        confidence_score   = confidence,
                    )

                # ── Semantic diagnostic: score distribution ───────────────────
                try:
                    import numpy as np
                    vals  = list(sim_by_name.values())
                    items = sorted(sim_by_name.items(), key=lambda x: -x[1])
                    sem_score_info = {
                        "grant_title": grant["title"][:80],
                        "max": round(float(max(vals)), 4) if vals else 0,
                        "p95": round(float(np.percentile(vals, 95)), 4) if vals else 0,
                        "p90": round(float(np.percentile(vals, 90)), 4) if vals else 0,
                        "median": round(float(np.median(vals)), 4) if vals else 0,
                        "above_threshold": sum(1 for s in vals if s >= sem_threshold),
                        "above_050": sum(1 for s in vals if s >= 0.50),
                        "above_055": sum(1 for s in vals if s >= 0.55),
                        "above_060": sum(1 for s in vals if s >= 0.60),
                        "above_065": sum(1 for s in vals if s >= 0.65),
                        "top_5": [(name, round(float(s), 4)) for name, s in items[:5]],
                    }
                    _diag["semantic_score_distributions"].append(sem_score_info)
                    logger.info(
                        f"  Semantic scores: max={sem_score_info['max']:.3f} "
                        f"p95={sem_score_info['p95']:.3f} "
                        f"above {sem_threshold}={sem_score_info['above_threshold']}"
                    )
                except Exception as e:
                    logger.debug(f"  Semantic diagnostic skipped: {e}")

        all_matches = _merge_match_dicts(
            keyword_matches, semantic_matches, idf_table, grant_text, grant_title,
            sim_by_name=sim_by_name, sem_threshold=sem_threshold,
        )

        # ── Theme 2: research track-record weighting + hard gate ─────────────
        # Re-weight each match by the faculty member's external footprint, then
        # (for major-mechanism grants) hard-drop faculty with no footprint. Runs
        # before the histogram/filters so the weighting flows through naturally.
        grant_is_major = gate_major and _is_major_mechanism(grant, major_mech_rx)
        if all_matches:
            gated_here = []
            reweighted = []
            for m in all_matches:
                fac  = faculty_by_name.get(m.faculty_name, {})
                tier = _research_tier(fac)
                if grant_is_major and tier == "none":
                    gated_here.append(m.faculty_name)
                    continue
                mult = tier_mult.get(tier, 1.0)
                if mult != 1.0:
                    new_conf = min(round(m.confidence_score * mult), 99)
                    m = m._replace(confidence_score=new_conf)
                reweighted.append(m)
            # keep ranking consistent after re-scoring
            reweighted.sort(key=lambda m: (
                -m.confidence_score,
                0 if m.match_type == "both" else (1 if m.match_type == "keyword" else 2),
                -m.similarity_score,
            ))
            all_matches = reweighted
            if gated_here:
                track_record_gated_total += len(gated_here)
                _diag["track_record_gated"].append({
                    "grant_title": grant["title"][:80],
                    "count":       len(gated_here),
                    "sample":      gated_here[:5],
                })

        # ── Confidence histogram (before filtering) ──────────────────────────
        if all_matches:
            conf_scores = [m.confidence_score for m in all_matches]
            histogram = {}
            for floor in [20, 25, 30, 35, 40, 45, 50, 60, 70, 80]:
                histogram[f">={floor}%"] = sum(1 for c in conf_scores if c >= floor)
            _diag["confidence_histograms"].append({
                "grant_title": grant["title"][:80],
                "total_before_filter": len(all_matches),
                "histogram": histogram,
                "avg_confidence": round(sum(conf_scores) / len(conf_scores), 1),
            })

        # Apply minimum confidence filter
        if min_confidence > 0:
            before = len(all_matches)
            _raw_match_count += before          # tally before filtering
            all_matches = [m for m in all_matches if m.confidence_score >= min_confidence]
            suppressed += before - len(all_matches)
        else:
            _raw_match_count += len(all_matches)

        # ── Single-keyword quality filter ────────────────────────────────────
        # Matches based on a single keyword are low-signal — require higher
        # confidence (50%) to be included. Multi-keyword matches (2+) are kept
        # at the normal min_confidence threshold.
        # This eliminates noise like matching on "change" or "extension" alone
        # while keeping genuinely specific single-keyword matches.
        single_kw_min = 50
        before_single = len(all_matches)
        all_matches = [
            m for m in all_matches
            if m.match_type != "keyword"                  # semantic/both always pass
            or len(m.matched_keywords) >= 2                # multi-keyword always pass
            or m.confidence_score >= single_kw_min         # high-conf single-kw pass
        ]
        suppressed += before_single - len(all_matches)

        # ── Per-grant match cap ──────────────────────────────────────────────
        if max_per_grant and len(all_matches) > max_per_grant:
            original_count = len(all_matches)
            all_matches.sort(key=lambda m: -m.confidence_score)
            all_matches = all_matches[:max_per_grant]
            _diag["grants_capped"].append({
                "grant_title": grant["title"][:80],
                "original": original_count,
                "capped_to": max_per_grant,
                "min_conf_kept": all_matches[-1].confidence_score if all_matches else 0,
            })
            logger.info(
                f"  Capped '{grant['title'][:50]}...' from {original_count} to {max_per_grant} matches"
            )

        if all_matches:
            for m in all_matches:
                if m.match_type == "both":      both    += 1
                elif m.match_type == "keyword": kw_only += 1
                else:                           sem_only += 1

            results.append({"grant": grant, "matches": all_matches})
            avg_conf = round(sum(m.confidence_score for m in all_matches) / len(all_matches))
            logger.info(
                f"  '{grant['title'][:55]}...' -> "
                f"{len(all_matches)} matches "
                f"(kw:{len(keyword_matches)} sem:{len(semantic_matches)}) "
                f"avg confidence: {avg_conf}%"
            )

            # ── Per-grant diagnostic summary ─────────────────────────────────
            _diag["per_grant"].append({
                "grant_title": grant["title"][:120],
                "keyword_matches": len(keyword_matches),
                "semantic_matches": len(semantic_matches),
                "after_confidence_filter": len(all_matches),
                "avg_confidence": avg_conf,
            })

    total_matches = kw_only + sem_only + both
    logger.info(
        f"Matching complete: {len(results)}/{len(grants)} grants matched | "
        f"{total_matches} faculty matches "
        f"(keyword-only: {kw_only}, semantic-only: {sem_only}, both: {both})"
    )
    if skipped_irrelevant:
        logger.info(f"  {skipped_irrelevant} grant(s) skipped — not biomedically relevant to UMSOM")
    if skipped_admin:
        logger.info(f"  ({skipped_admin} of those were administrative notices)")
    if suppressed:
        logger.info(f"  {suppressed} faculty matches suppressed by min_confidence={min_confidence}% filter")
    if context_filtered_total:
        logger.info(f"  {context_filtered_total} matches dropped — generic-only keywords (context filter)")
    if track_record_gated_total:
        logger.info(f"  {track_record_gated_total} matches gated — no research footprint on major-mechanism grant")

    # ── Standardised diagnostic log lines (parsed by grant_matcher_diagnostics.py) ──
    print(f"Processing {_faculty_count} faculty")
    print(f"Scraped {len(grants)} grants")
    print(f"{_raw_match_count} raw matches found")
    print(f"{total_matches} matches after filter")

    _save_match_results(results, len(grants), sem_enabled, config=config)

    # ── Store diagnostic data for main.py to access ──────────────────────────
    global _last_diagnostic
    _diag["summary"] = {
        "faculty_count": _faculty_count,
        "grants_checked": len(grants),
        "grants_matched": len(results),
        "grants_skipped_irrelevant": skipped_irrelevant,
        "grants_skipped_admin": skipped_admin,
        "grants_skipped_ineligible": skipped_ineligible,
        "ineligible_breakdown": ineligible_breakdown,
        "raw_matches": _raw_match_count,
        "matches_after_filter": total_matches,
        "suppressed_by_confidence": suppressed,
        "context_filtered": context_filtered_total,
        "track_record_gated": track_record_gated_total,
        "keyword_only": kw_only,
        "semantic_only": sem_only,
        "both": both,
        "run_duration_s": round(time.time() - _run_start_time, 1),
    }
    _last_diagnostic = _diag

    return results


def get_last_diagnostic() -> dict:
    """Return diagnostic data from the most recent find_matches() run."""
    return _last_diagnostic


def load_recent_matched_results(days: int) -> list:
    """
    Rebuild the {"grant": ..., "matches": [...]} structure (as produced by
    find_matches and consumed by emailer.send_email) from the persisted
    match_results.json, limited to matches recorded in the last `days` days.

    Used to build the weekly Tuesday roundup so weekly recipients see every
    grant matched over the past 7 days — not just whatever was newly found on
    the morning the email is sent. Faculty are de-duplicated per grant, keeping
    the highest-confidence entry; matches within each grant are sorted by
    confidence descending.

    Returns a list ordered by each grant's best confidence (descending).
    Returns [] if the store is missing/empty or nothing falls in the window.
    """
    try:
        if not Path(MATCHES_FILE).exists():
            return []
        with open(MATCHES_FILE) as f:
            entries = json.load(f)
    except Exception as e:
        logger.warning(f"Could not read {MATCHES_FILE} for roundup: {e}")
        return []

    if not isinstance(entries, list):
        return []

    cutoff = datetime.utcnow() - timedelta(days=days)

    # Group entries by grant, keeping best confidence per faculty member.
    grants_by_id: dict = {}          # grant_id -> grant dict
    matches_by_grant: dict = {}      # grant_id -> {faculty_name -> match dict}

    for e in entries:
        ts = e.get("timestamp", "")
        try:
            if datetime.fromisoformat(ts) < cutoff:
                continue
        except (ValueError, TypeError):
            continue  # unparseable timestamp — skip rather than guess

        gid = e.get("grant_id", "") or e.get("grant_link", "") or e.get("grant_title", "")
        if not gid:
            continue

        if gid not in grants_by_id:
            grants_by_id[gid] = {
                "id":            e.get("grant_id", ""),
                "title":         e.get("grant_title", ""),
                "agency":        e.get("grant_agency", ""),
                "number":        e.get("grant_number", ""),
                "link":          e.get("grant_link", ""),
                "close_date":    e.get("grant_close_date", ""),
                "open_date":     e.get("grant_open_date", ""),
                "award_ceiling": e.get("grant_award_ceiling", ""),
                "synopsis":      e.get("grant_synopsis", ""),
            }
            matches_by_grant[gid] = {}

        fac_key = e.get("faculty_name", "") or e.get("faculty_email", "")
        if not fac_key:
            continue
        match = {
            "faculty_name":       e.get("faculty_name", ""),
            "faculty_url":        e.get("faculty_url", ""),
            "faculty_department": e.get("faculty_department", ""),
            "faculty_email":      e.get("faculty_email", ""),
            "matched_keywords":   e.get("matched_keywords", []),
            "match_score":        e.get("match_score", 0),
            "match_type":         e.get("match_type", "keyword"),
            "similarity_score":   e.get("similarity_score", 0.0),
            "confidence_score":   e.get("confidence_score", 0),
        }
        existing = matches_by_grant[gid].get(fac_key)
        if existing is None or match["confidence_score"] > existing["confidence_score"]:
            matches_by_grant[gid][fac_key] = match

    results = []
    for gid, grant in grants_by_id.items():
        matches = sorted(
            matches_by_grant[gid].values(),
            key=lambda m: -m["confidence_score"],
        )
        if matches:
            results.append({"grant": grant, "matches": matches})

    # Order grants by their best (first) match confidence, descending.
    results.sort(key=lambda r: -(r["matches"][0]["confidence_score"] if r["matches"] else 0))

    logger.info(
        f"Roundup: rebuilt {len(results)} grant(s) with matches from the last {days} days "
        f"({MATCHES_FILE})"
    )
    return results


# -- Persistence --------------------------------------------------------------

def _save_match_results(results, total_grants_checked, semantic_used=False, config=None):
    Path("data").mkdir(parents=True, exist_ok=True)
    now = datetime.utcnow().isoformat()

    existing = []
    try:
        if Path(MATCHES_FILE).exists():
            with open(MATCHES_FILE) as f:
                existing = json.load(f)
    except Exception:
        existing = []

    new_entries = []
    for r in results:
        grant = r["grant"]
        for m in r["matches"]:
            new_entries.append({
                "timestamp":           now,
                "grant_id":            grant.get("id", ""),
                "grant_title":         grant.get("title", ""),
                "grant_agency":        grant.get("agency", ""),
                "grant_number":        grant.get("number", ""),
                "grant_link":          grant.get("link", ""),
                "grant_close_date":    grant.get("close_date", ""),
                "grant_open_date":     grant.get("open_date", ""),
                "grant_award_ceiling": grant.get("award_ceiling", ""),
                "grant_synopsis":      grant.get("synopsis", "")[:500],
                "faculty_name":        m.faculty_name,
                "faculty_department":  m.faculty_department,
                "faculty_email":       m.faculty_email,
                "faculty_url":         m.faculty_url,
                "matched_keywords":    m.matched_keywords,
                "match_score":         m.match_score,
                "match_type":          m.match_type,
                "similarity_score":    round(m.similarity_score, 4),
                "confidence_score":    m.confidence_score,
            })

    # Cap at 5000 entries (newest first) — authoritative count is in run_stats.json
    combined = (new_entries + existing)[:5000]
    with open(MATCHES_FILE, "w") as f:
        json.dump(combined, f, indent=2)

    stats = _load_stats()
    kw_count  = sum(1 for r in results for m in r["matches"] if m.match_type in ("keyword", "both"))
    sem_count = sum(1 for r in results for m in r["matches"] if m.match_type in ("semantic", "both"))
    all_matches_flat = [m for r in results for m in r["matches"]]
    avg_conf = round(sum(m.confidence_score for m in all_matches_flat) / len(all_matches_flat)) if all_matches_flat else 0
    high_conf = sum(1 for m in all_matches_flat if m.confidence_score >= 60)

    stats["last_grants_run"] = {
        "timestamp":              now,
        "grants_checked":         total_grants_checked,
        "grants_with_matches":    len(results),
        "total_faculty_matches":  sum(len(r["matches"]) for r in results),
        "keyword_matches":        kw_count,
        "semantic_matches":       sem_count,
        "semantic_matching_used": semantic_used,
        "avg_confidence":         avg_conf,
        "high_confidence_matches": high_conf,
    }
    _save_stats(stats)

    # ── Diagnostic instrumentation (Step 4) ───────────────────────────────────
    _write_diagnostic_log(results, total_grants_checked, stats, config)
    _write_sqlite_matches(results)


def _write_diagnostic_log(results, total_grants_checked, stats, config):
    """
    Write a structured JSON run log that the daily diagnostic script reads.
    Saved to MATCHER_LOG_DIR/run_YYYY-MM-DD.json.

    This is the primary data source for grant_matcher_diagnostics.py.
    It captures config values, match counts at each pipeline stage,
    and per-faculty/per-grant match details for trend analysis.
    """
    global _run_start_time, _raw_match_count, _faculty_count

    run_date     = date.today().isoformat()
    duration_s   = int(time.time() - _run_start_time) if _run_start_time else 0
    matching_cfg = (config or {}).get("matching", {})
    grants_run   = stats.get("last_grants_run", {})

    # Flatten all matches into a list for the diagnostic script
    flat_matches = []
    for r in results:
        grant = r["grant"]
        for m in r["matches"]:
            flat_matches.append({
                "faculty_id":        m.faculty_email or m.faculty_name,
                "faculty_name":      m.faculty_name,
                "faculty_email":     m.faculty_email,
                "faculty_dept":      m.faculty_department,
                "grant_id":          grant["id"],
                "grant_title":       grant["title"],
                "source":            grant.get("source", grant.get("agency", "")),
                "semantic_score":    round(m.similarity_score, 4),
                "confidence_score":  m.confidence_score,
                "match_type":        m.match_type,
                "matched_keywords":  m.matched_keywords,
            })

    # Source breakdown (grants scraped per source)
    source_counts: dict = {}
    for r in results:
        src = r["grant"].get("source", r["grant"].get("agency", "unknown"))
        source_counts[src] = source_counts.get(src, 0) + 1

    summary = {
        "run_date":             run_date,
        "generated_at":         datetime.utcnow().isoformat(),
        "run_duration_s":       duration_s,
        "faculty_processed":    _faculty_count,
        "grants_scraped":       total_grants_checked,
        "raw_matches":          _raw_match_count,
        "matches_after_filter": grants_run.get("total_faculty_matches", len(flat_matches)),
        "grants_with_matches":  len(results),
        "avg_confidence":       grants_run.get("avg_confidence", 0),
        "high_confidence_matches": grants_run.get("high_confidence_matches", 0),
        "source_breakdown":     source_counts,
        "config": {
            "semantic_threshold":          matching_cfg.get("semantic_threshold", DEFAULT_SEMANTIC_THRESHOLD),
            "confidence_threshold":        matching_cfg.get("min_confidence_score", 35) / 100.0,
            "max_matches_per_grant":       matching_cfg.get("max_matches_per_grant", DEFAULT_MAX_MATCHES_PER_GRANT),
            "min_idf_for_match":           matching_cfg.get("min_idf_for_match", DEFAULT_MIN_IDF_FOR_MATCH),
            "idf_normalisation":           True,   # always on in v4.0
            "biomedical_relevance_filter": True,   # always on in v4.0
            "active_faculty_filter":       True,   # always on in v4.0
            "max_kw_prevalence_pct":       matching_cfg.get("max_kw_prevalence_pct", DEFAULT_MAX_KW_PREVALENCE),
            "min_confidence_score":        matching_cfg.get("min_confidence_score", 35),
            "semantic_matching_enabled":   matching_cfg.get("semantic_matching", True),
        },
        "matches": flat_matches,
    }

    try:
        Path(_LOG_DIR).mkdir(parents=True, exist_ok=True)
        log_path = Path(_LOG_DIR) / f"run_{run_date}.json"
        with open(log_path, "w") as f:
            json.dump(summary, f, indent=2)
        logger.info(f"Diagnostic run log written → {log_path}")
    except Exception as e:
        logger.warning(f"Could not write diagnostic run log: {e}")


def _write_sqlite_matches(results):
    """
    Persist match results to SQLite so the diagnostic script can query by date,
    compute score distributions, and track faculty match counts over time.

    Table is created on first run. Subsequent runs append new rows.
    The diagnostic script reads from this DB via MATCHER_DB_PATH env var.
    """
    if not results:
        return

    try:
        Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                faculty_id       TEXT,
                faculty_name     TEXT,
                grant_id         TEXT,
                grant_title      TEXT,
                source           TEXT,
                semantic_score   REAL,
                confidence_score REAL,
                matched_keywords TEXT,
                match_type       TEXT,
                created_at       DATETIME
            )
        """)
        now = datetime.utcnow().isoformat()
        rows = []
        for r in results:
            grant = r["grant"]
            for m in r["matches"]:
                rows.append((
                    m.faculty_email or m.faculty_name,
                    m.faculty_name,
                    grant["id"],
                    grant["title"],
                    grant.get("source", grant.get("agency", "")),
                    round(m.similarity_score, 4),
                    m.confidence_score,
                    ",".join(m.matched_keywords),
                    m.match_type,
                    now,
                ))
        conn.executemany("""
            INSERT INTO matches
              (faculty_id, faculty_name, grant_id, grant_title, source,
               semantic_score, confidence_score, matched_keywords, match_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
        conn.close()
        logger.info(f"SQLite: {len(rows)} match rows written → {_DB_PATH}")
    except Exception as e:
        logger.warning(f"Could not write SQLite matches: {e}")


def _load_stats():
    try:
        if Path(STATS_FILE).exists():
            with open(STATS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_stats(stats):
    Path("data").mkdir(parents=True, exist_ok=True)
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)


def record_scrape_stats(faculty_count, with_keywords, dept_pages, errors):
    stats = _load_stats()
    stats["last_scrape"] = {
        "timestamp":                datetime.utcnow().isoformat(),
        "total_faculty":            faculty_count,
        "faculty_with_keywords":    with_keywords,
        "department_pages_scraped": dept_pages,
        "pages_errored":            errors,
    }
    _save_stats(stats)


def record_grants_fetch_stats(grants_retrieved, new_grants, seen_total):
    stats = _load_stats()
    prev = stats.get("last_grants_run", {})
    stats["last_grants_run"] = {
        **prev,
        "timestamp":         datetime.utcnow().isoformat(),
        "grants_retrieved":  grants_retrieved,
        "new_grants_found":  new_grants,
        "seen_grants_total": seen_total,
    }
    _save_stats(stats)
